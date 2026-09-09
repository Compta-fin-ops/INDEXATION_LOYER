"""Ligne de commande.

    python -m indexation_loyer fetch-indices [--depuis 2000-Q1] [--fichier export.csv --serie ILC]
    python -m indexation_loyer init "SCI DES HALLES" [--siren 123456789] [--baux baux.csv]
    python -m indexation_loyer refresh "SCI DES HALLES" | --tous
    python -m indexation_loyer courriers "SCI DES HALLES" [--bail B01] [--marquer]
    python -m indexation_loyer pennylane "SCI DES HALLES" [--push]
    python -m indexation_loyer demo
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from . import insee, pennylane
from .baux import Bail
from .workbook import Societe, construire, lire_classeur, lire_indices_classeur, rafraichir

RACINE = Path(__file__).resolve().parent.parent
DOSSIER_SUIVI = RACINE / "suivi"
DOSSIER_OUT = RACINE / "out"

log = logging.getLogger("indexation_loyer")


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.strip()).strip("_").upper()


def chemin_classeur(societe: str, dossier: Path = DOSSIER_SUIVI) -> Path:
    """Nom de société -> classeur dans `dossier` ; un chemin .xlsx existant est accepté tel quel."""
    if societe.lower().endswith(".xlsx") and Path(societe).exists():
        return Path(societe)
    return dossier / f"{_slug(societe)}_indexation_loyers.xlsx"


def _lire_baux_csv(chemin: Path) -> list[Bail]:
    """Import initial optionnel : CSV ; avec les mêmes intitulés que la feuille Baux (ou leurs clés python)."""
    alias = {
        "ID bail": "id", "Local (désignation / adresse)": "local", "Locataire": "locataire", "Adresse du locataire (courrier)": "adresse_locataire", "Type de bail": "type_bail",
        "Date de prise d'effet": "date_effet", "Durée (ans)": "duree_ans", "Loyer initial annuel HT": "loyer_initial_annuel_ht",
        "Charges annuelles HT": "charges_annuelles_ht", "TVA (%)": "tva_pct", "Périodicité de facturation": "periodicite_facturation",
        "Indice": "indice", "Trimestre indice de base": "trimestre_base", "Périodicité de révision (ans)": "periodicite_revision_ans",
        "Méthode": "methode", "Plafond annuel (%) – optionnel": "plafond_annuel_pct", "Pennylane customer_id": "pennylane_customer_id",
        "Pennylane product_id": "pennylane_product_id", "Pennylane subscription_id": "pennylane_subscription_id", "Notes": "notes",
    }
    baux = []
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        for ligne in csv.DictReader(f, delimiter=";"):
            d = {alias.get(k, k): (v.strip() if isinstance(v, str) else v) for k, v in ligne.items() if k}
            d = {k: v for k, v in d.items() if v not in ("", None)}
            for k in ("date_effet",):
                d[k] = datetime.strptime(d[k], "%d/%m/%Y" if "/" in d[k] else "%Y-%m-%d").date()
            for k in ("loyer_initial_annuel_ht", "charges_annuelles_ht", "tva_pct", "plafond_annuel_pct"):
                if k in d:
                    d[k] = float(str(d[k]).replace(",", ".").replace(" ", ""))
            for k in ("duree_ans", "periodicite_revision_ans"):
                if k in d:
                    d[k] = int(d[k])
            baux.append(Bail(**d))
    return baux


# --- commandes ----------------------------------------------------------------
def cmd_fetch(args) -> int:
    try:
        fusion, delta = insee.mettre_a_jour_cache(
            codes=args.series, fichier_csv=args.fichier, serie_forcee=args.serie, start_period=args.depuis,
            api_insee=args.api_insee)
    except (RuntimeError, ValueError) as e:
        print(f"Échec : {e}")
        return 1
    derniers = insee.dernieres_valeurs(fusion)
    print(f"Cache : {insee.CHEMIN_CACHE}  ({len(fusion)} observations, {len(delta)} nouvelles/modifiées)")
    for code, o in sorted(derniers.items()):
        print(f"  {code:<5} dernier trimestre {o.periode}  valeur {o.valeur:g}  statut {o.statut_obs or '-'}  "
              f"MAJ INSEE {o.date_maj_insee or '-'}  source {o.source}")
    for o in delta[-12:]:
        print(f"  + {o.serie} {o.periode} = {o.valeur:g}")
    return 0


def cmd_init(args) -> int:
    chemin = chemin_classeur(args.societe, Path(args.dossier))
    if chemin.exists() and not args.force:
        print(f"Le classeur existe déjà : {chemin} (utiliser refresh, ou --force pour repartir de zéro)")
        return 1
    baux = _lire_baux_csv(Path(args.baux)) if args.baux else []
    for b in baux:
        for e in b.erreurs:
            log.warning("Bail %s : %s", b.id, e)
    observations = insee.lire_cache()
    if not observations:
        log.warning("Cache d'indices vide : lancer d'abord `fetch-indices`. Le classeur est créé sans indices.")
    societe = Societe(nom=args.societe, siren=args.siren or "", forme=args.forme, contact=args.contact or "")
    construire(societe, baux, observations, chemin)
    print(f"Classeur créé : {chemin}  ({len(baux)} bail/baux, {len(observations)} observations d'indices)")
    return 0


def cmd_refresh(args) -> int:
    observations = insee.lire_cache()
    cibles = sorted(Path(args.dossier).glob("*_indexation_loyers.xlsx")) if args.tous else [chemin_classeur(args.societe, Path(args.dossier))]
    code = 0
    for chemin in cibles:
        if not chemin.exists():
            print(f"Introuvable : {chemin}")
            code = 1
            continue
        rafraichir(chemin, observations)
        _, baux, _ = lire_classeur(chemin)
        print(f"Rafraîchi : {chemin}  ({len(baux)} bail/baux, {len(observations)} observations)")
    return code


def _indices_du_classeur(chemin: Path) -> list:
    """Indices de la feuille Indices du classeur ; avertit si le cache local est plus récent (refresh à faire)."""
    observations = lire_indices_classeur(chemin)
    cache = insee.lire_cache()
    if cache:
        recents_cache = insee.dernieres_valeurs(cache)
        recents_classeur = insee.dernieres_valeurs(observations)
        retard = [c for c, o in recents_cache.items() if c not in recents_classeur or recents_classeur[c].periode < o.periode]
        if retard:
            log.warning("Le cache d'indices est plus récent que le classeur pour %s : lancer `refresh` d'abord.", ", ".join(retard))
    return observations


def cmd_pennylane(args) -> int:
    chemin = chemin_classeur(args.societe, Path(args.dossier))
    societe, baux, saisies = lire_classeur(chemin)
    observations = _indices_du_classeur(chemin)
    mapping = pennylane.charger_mapping()
    reglages = pennylane.ReglagesPennylane(societe.pl_mode, societe.pl_payment_conditions, societe.pl_payment_method)
    abonnements = [pennylane.construire_abonnement(b, observations, mapping, reglages, decisions=saisies.decision) for b in baux]
    chemins = pennylane.ecrire_dry_run(abonnements, DOSSIER_OUT, societe.nom)
    for a in abonnements:
        prix = a.corps["customer_invoice_data"]["invoice_lines"][0]["raw_currency_unit_price"]
        deja = f"   (abonnement {a.subscription_id_existant} déjà enregistré)" if a.subscription_id_existant else ""
        print(f"  {a.bail_id:<10} {a.locataire:<30} {prix:>10} HT / échéance{deja}"
              + (f"   ⚠ {' ; '.join(a.avertissements)}" if a.avertissements else ""))
    print(f"{len(chemins)} corps JSON écrits dans {DOSSIER_OUT} (aperçu, rien n'a été envoyé)")
    if not args.push:
        return 0
    token = os.environ.get(mapping["variable_environnement_token"], "")
    if not token:
        print(f"Variable {mapping['variable_environnement_token']} absente : envoi impossible.")
        return 1
    crees: dict[str, str] = {}
    code = 0
    for bail_id, statut, cree, brut in pennylane.envoyer(abonnements, mapping, token, remplacer=args.remplacer):
        print(f"  {bail_id}: HTTP {statut} {('-> abonnement ' + cree) if cree else brut[:300]}")
        if statut == 201 and cree:
            crees[bail_id] = cree
        else:
            code = 1
    if crees:
        n = pennylane.enregistrer_subscription_ids(chemin, crees)
        print(f"{n} identifiant(s) d'abonnement inscrit(s) dans la feuille Baux (sauvegarde .bak créée).")
    return code


def cmd_courriers(args) -> int:
    from . import courriers
    chemin = chemin_classeur(args.societe, Path(args.dossier))
    societe, baux, saisies = lire_classeur(chemin)
    observations = _indices_du_classeur(chemin)
    dossier_sortie = Path(args.sortie) if args.sortie else DOSSIER_OUT / "courriers" / _slug(societe.nom)
    cibles = courriers.selectionner(baux, observations, saisies, horizon_jours=args.horizon_jours,
                                    bail_id=args.bail, tous=args.tous)
    if not cibles:
        print("Aucune échéance à notifier (utiliser --tous pour régénérer les courriers déjà envoyés, --horizon-jours pour élargir).")
        return 0
    chemins = courriers.generer(societe, cibles, dossier_sortie)
    for (b, l), ch in zip(cibles, chemins):
        print(f"  {b.id:<6} {b.locataire:<30} révision du {l.echeance.date_revision:%d/%m/%Y}  {l.statut:<22} -> {ch.name}")
    print(f"{len(chemins)} courrier(s) PDF dans {dossier_sortie}")
    if args.marquer:
        n = courriers.marquer_envoyes(chemin, cibles)
        print(f"Date du jour inscrite dans « Courrier envoyé le » pour {n} échéance(s) (sauvegarde .bak créée).")
    return 0


def cmd_demo(args) -> int:
    from .demo import generer_demo
    chemin = generer_demo(Path(args.dossier) if args.dossier else RACINE / "demo")
    print(f"Classeur de démonstration (valeurs fictives) : {chemin}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="indexation_loyer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    f = sp.add_parser("fetch-indices", help="Met à jour le cache d'indices depuis l'INSEE (ou un export CSV)")
    f.add_argument("--series", nargs="*", help="Sous-ensemble : ILC ILAT ICC IRL (défaut : toutes)")
    f.add_argument("--depuis", default="2000-Q1", help="startPeriod SDMX (défaut 2000-Q1)")
    f.add_argument("--fichier", type=Path, help="Export CSV/zip insee.fr à importer au lieu d'appeler l'API")
    f.add_argument("--serie", help="Code série à forcer pour --fichier si la ligne idBank manque")
    f.add_argument("--api-insee", action="store_true", help="Passer par api.insee.fr/series/BDM avec le jeton INSEE_API_TOKEN")
    f.set_defaults(func=cmd_fetch)

    i = sp.add_parser("init", help="Crée le classeur d'une société")
    i.add_argument("societe")
    i.add_argument("--siren")
    i.add_argument("--forme", default="SCI")
    i.add_argument("--contact")
    i.add_argument("--baux", help="CSV ; d'import initial des baux (optionnel)")
    i.add_argument("--dossier", default=str(DOSSIER_SUIVI))
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    r = sp.add_parser("refresh", help="Recalcule le(s) classeur(s) avec le cache d'indices courant")
    r.add_argument("societe", nargs="?")
    r.add_argument("--tous", action="store_true")
    r.add_argument("--dossier", default=str(DOSSIER_SUIVI))
    r.set_defaults(func=cmd_refresh)

    pl = sp.add_parser("pennylane", help="Prépare (dry-run) ou envoie (--push) les abonnements de facturation")
    pl.add_argument("societe")
    pl.add_argument("--push", action="store_true", help="Envoie réellement (PENNYLANE_API_TOKEN requis)")
    pl.add_argument("--remplacer", action="store_true", help="Créer même si un subscription_id est déjà enregistré")
    pl.add_argument("--dossier", default=str(DOSSIER_SUIVI))
    pl.set_defaults(func=cmd_pennylane)

    co = sp.add_parser("courriers", help="Génère les courriers PDF d'information des locataires (application ou gel)")
    co.add_argument("societe")
    co.add_argument("--bail", help="Limiter à un ID bail")
    co.add_argument("--horizon-jours", type=int, default=120, help="Inclure les révisions à venir dans ce délai (défaut 120)")
    co.add_argument("--tous", action="store_true", help="Inclure les échéances déjà notifiées ou appliquées")
    co.add_argument("--marquer", action="store_true", help="Inscrire la date du jour dans « Courrier envoyé le »")
    co.add_argument("--sortie", help="Dossier de sortie (défaut out/courriers/<société>)")
    co.add_argument("--dossier", default=str(DOSSIER_SUIVI))
    co.set_defaults(func=cmd_courriers)

    d = sp.add_parser("demo", help="Génère un classeur de démonstration à valeurs fictives")
    d.add_argument("--dossier")
    d.set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    if args.cmd == "refresh" and not args.tous and not args.societe:
        p.error("préciser une société ou --tous")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

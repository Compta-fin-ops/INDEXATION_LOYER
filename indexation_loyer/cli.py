"""Ligne de commande.

    python -m indexation_loyer fetch-indices [--depuis 2000-Q1] [--fichier export.csv --serie ILC]
    python -m indexation_loyer init "SCI DES HALLES" [--siren 123456789] [--baux baux.csv]
    python -m indexation_loyer refresh "SCI DES HALLES" | --tous
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
from .workbook import Societe, construire, lire_classeur, rafraichir

RACINE = Path(__file__).resolve().parent.parent
DOSSIER_SUIVI = RACINE / "suivi"
DOSSIER_OUT = RACINE / "out"

log = logging.getLogger("indexation_loyer")


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.strip()).strip("_").upper()


def chemin_classeur(societe: str, dossier: Path = DOSSIER_SUIVI) -> Path:
    return dossier / f"{_slug(societe)}_indexation_loyers.xlsx"


def _lire_baux_csv(chemin: Path) -> list[Bail]:
    """Import initial optionnel : CSV ; avec les mêmes intitulés que la feuille Baux (ou leurs clés python)."""
    alias = {
        "ID bail": "id", "Local (désignation / adresse)": "local", "Locataire": "locataire", "Type de bail": "type_bail",
        "Date de prise d'effet": "date_effet", "Durée (ans)": "duree_ans", "Loyer initial annuel HT": "loyer_initial_annuel_ht",
        "Charges annuelles HT": "charges_annuelles_ht", "TVA (%)": "tva_pct", "Périodicité de facturation": "periodicite_facturation",
        "Indice": "indice", "Trimestre indice de base": "trimestre_base", "Périodicité de révision (ans)": "periodicite_revision_ans",
        "Méthode": "methode", "Plafond annuel (%) – optionnel": "plafond_annuel_pct", "Pennylane customer_id": "pennylane_customer_id",
        "Pennylane product_id": "pennylane_product_id", "Notes": "notes",
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
            codes=args.series, fichier_csv=args.fichier, serie_forcee=args.serie, start_period=args.depuis)
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


def cmd_pennylane(args) -> int:
    chemin = chemin_classeur(args.societe, Path(args.dossier))
    societe, baux, _ = lire_classeur(chemin)
    observations = insee.lire_cache()
    mapping = pennylane.charger_mapping()
    abonnements = [pennylane.construire_abonnement(b, observations, mapping) for b in baux]
    chemins = pennylane.ecrire_dry_run(abonnements, DOSSIER_OUT, societe.nom)
    for a in abonnements:
        print(f"  {a.bail_id:<10} {a.locataire:<30} {a.corps.get('invoice_lines', [{}])[0].get('raw_currency_unit_price', '?'):>10} HT / échéance"
              + (f"   ⚠ {' ; '.join(a.avertissements)}" if a.avertissements else ""))
    print(f"{len(chemins)} corps JSON écrits dans {DOSSIER_OUT} (aperçu, rien n'a été envoyé)")
    if args.push:
        token = os.environ.get(mapping["variable_environnement_token"], "")
        if not token:
            print(f"Variable {mapping['variable_environnement_token']} absente : envoi impossible.")
            return 1
        for bail_id, statut, corps in pennylane.envoyer(abonnements, mapping, token):
            print(f"  {bail_id}: HTTP {statut} {corps[:200]}")
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
    pl.add_argument("--push", action="store_true")
    pl.add_argument("--dossier", default=str(DOSSIER_SUIVI))
    pl.set_defaults(func=cmd_pennylane)

    d = sp.add_parser("demo", help="Génère un classeur de démonstration à valeurs fictives")
    d.add_argument("--dossier")
    d.set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    if args.cmd == "refresh" and not args.tous and not args.societe:
        p.error("préciser une société ou --tous")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

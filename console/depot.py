"""Lecture et écriture de la base : baux, collecte Pennylane, reprise d'un classeur, chargements."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from . import referentiel
from .pennylane_api import Client

log = logging.getLogger(__name__)
COLONNES_BAIL = ("code", "local", "locataire", "customer_id", "subscription_id", "indice", "periodicite", "tva_pct",
                 "date_effet", "date_fin", "loyer_initial_annuel", "loyer_attendu_annuel", "charges_annuelles",
                 "date_derniere_revision", "date_prochaine_revision", "loyer_prochain_annuel", "date_revision_suivante",
                 "statut_revision", "revisions_non_appliquees", "calcule", "type_bail", "adresse_locataire",
                 "trimestre_base", "periodicite_revision_ans", "methode", "plafond_annuel_pct", "duree_ans", "notes")
DEFAUTS_BAIL = {"calcule": 0, "type_bail": "Commercial", "adresse_locataire": "", "trimestre_base": "",
                "periodicite_revision_ans": 1, "methode": "Chaînée", "notes": "", "revisions_non_appliquees": 0}


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def enregistrer_dossier(cx: sqlite3.Connection, code: str, nom: str, siren: str = "", variable_jeton: str = "",
                        source: str = "") -> None:
    cx.execute("""INSERT INTO dossiers (code, nom, siren, variable_jeton, source_referentiel) VALUES (?,?,?,?,?)
                  ON CONFLICT(code) DO UPDATE SET nom=excluded.nom, siren=excluded.siren,
                  variable_jeton=CASE WHEN excluded.variable_jeton<>'' THEN excluded.variable_jeton ELSE dossiers.variable_jeton END,
                  source_referentiel=excluded.source_referentiel""", (code, nom, siren, variable_jeton, source))


def enregistrer_baux(cx: sqlite3.Connection, dossier: str, baux: list[dict], remplacer: bool = False) -> int:
    """Insère ou met à jour les baux d'un dossier, par code. Rien n'est retiré, sauf `remplacer` explicite."""
    for b in baux:
        valeurs = [_iso(b.get(c, DEFAUTS_BAIL.get(c))) for c in COLONNES_BAIL]
        cx.execute(f"""INSERT INTO baux (dossier, {', '.join(COLONNES_BAIL)}) VALUES (?, {', '.join('?' * len(COLONNES_BAIL))})
                       ON CONFLICT(dossier, code) DO UPDATE SET
                       {', '.join(f'{c}=excluded.{c}' for c in COLONNES_BAIL if c != 'code')}""", [dossier, *valeurs])
    if remplacer:
        codes = [b["code"] for b in baux]
        cx.execute(f"DELETE FROM baux WHERE dossier=? AND code NOT IN ({', '.join('?' * len(codes)) or 'NULL'})",
                   [dossier, *codes])
    cx.commit()
    return len(baux)


def importer_classeur(cx: sqlite3.Connection, chemin: Path, code_dossier: str | None = None,
                      variable_jeton: str = "", aujourdhui: date | None = None) -> int:
    """Reprise d'un classeur d'indexation : les baux, leurs paramètres et les décisions de gel passent
    dans la console, qui calcule ensuite seule. Opération de migration, à faire une fois par société."""
    from indexation_loyer import calcul
    from indexation_loyer.workbook import lire_classeur, lire_indices_classeur
    aujourdhui = aujourdhui or date.today()
    societe, baux, saisies = lire_classeur(chemin)
    indices = calcul.table_indices(lire_indices_classeur(chemin))
    code = code_dossier or "".join(c for c in societe.nom.upper() if c.isalnum())[:12]
    enregistrer_dossier(cx, code, societe.nom, societe.siren, variable_jeton, source=Path(chemin).name)
    cx.execute("UPDATE dossiers SET forme=?, adresse=?, signataire=?, qualite_signataire=?, ville_signature=? WHERE code=?",
               (societe.forme, societe.adresse, societe.signataire, societe.qualite_signataire, societe.ville_signature, code))
    lignes_baux = []
    for b in baux:
        lignes = calcul.calculer(b, indices, horizon=b.date_fin or aujourdhui.replace(year=aujourdhui.year + 3),
                                 aujourdhui=aujourdhui, decisions=saisies.decision)
        echues = [l for l in lignes if l.echeance.date_revision <= aujourdhui]
        effectives = [l for l in echues if l.loyer_retenu is not None]
        a_venir = [l for l in lignes if l.echeance.date_revision > aujourdhui]
        lignes_baux.append({
            "code": b.id, "local": b.local, "locataire": b.locataire,
            "customer_id": int(b.pennylane_customer_id) if b.pennylane_customer_id.isdigit() else None,
            "subscription_id": int(b.pennylane_subscription_id) if b.pennylane_subscription_id.isdigit() else None,
            "indice": b.indice, "periodicite": b.periodicite_facturation, "tva_pct": b.tva_pct,
            "date_effet": b.date_effet, "date_fin": b.date_fin,
            "loyer_initial_annuel": b.loyer_initial_annuel_ht,
            "loyer_attendu_annuel": calcul.loyer_actuel(b, lignes, aujourdhui),
            "charges_annuelles": b.charges_annuelles_ht,
            "date_derniere_revision": effectives[-1].echeance.date_revision if effectives else None,
            "date_prochaine_revision": a_venir[0].echeance.date_revision if a_venir else None,
            "loyer_prochain_annuel": a_venir[0].loyer_retenu if a_venir else None,   # None tant que l'indice manque
            "date_revision_suivante": a_venir[1].echeance.date_revision if len(a_venir) > 1 else None,
            "statut_revision": echues[-1].statut if echues else "À venir",
            "revisions_non_appliquees": sum(
                1 for l in echues if l.statut == calcul.STATUT_CALCULABLE
                and str(saisies.applique.get((b.id, l.echeance.numero), "")).strip().lower() != "oui"),
            "calcule": 1, "type_bail": b.type_bail, "adresse_locataire": b.adresse_locataire,
            "trimestre_base": b.trimestre_base, "periodicite_revision_ans": b.periodicite_revision_ans,
            "methode": b.methode, "plafond_annuel_pct": b.plafond_annuel_pct, "duree_ans": b.duree_ans, "notes": b.notes,
        })
    n = enregistrer_baux(cx, code, lignes_baux)
    ids = {r["code"]: r["id"] for r in cx.execute("SELECT id, code FROM baux WHERE dossier=?", (code,))}
    for (bail_code, numero), decision in saisies.decision.items():
        if bail_code not in ids or not decision:
            continue
        notes = [str(x) for x in (saisies.consigne.get((bail_code, numero)), saisies.commentaire.get((bail_code, numero))) if x]
        cx.execute("""INSERT INTO decisions (bail_id, numero, decision, commentaire, courrier_le, auteur, horodatage)
                      VALUES (?,?,?,?,?,?,?) ON CONFLICT(bail_id, numero) DO UPDATE SET decision=excluded.decision,
                      commentaire=excluded.commentaire, courrier_le=excluded.courrier_le""",
                   (ids[bail_code], numero, str(decision).strip(), " · ".join(notes),
                    _iso(saisies.courrier.get((bail_code, numero))) or None, "reprise du classeur",
                    datetime.now().isoformat(timespec="seconds")))
    referentiel.tracer(cx, "Reprise d'un classeur", f"{n} bail/baux repris de {Path(chemin).name}", code)
    cx.commit()
    return n


def synchroniser(cx: sqlite3.Connection, client: Client, codes: list[str] | None = None, source: str = "") -> list[dict]:
    """Remplace les abonnements et factures de chaque dossier par l'état lu dans Pennylane."""
    codes = codes or [r["code"] for r in cx.execute("SELECT code FROM dossiers ORDER BY code")]
    bilan = []
    for code in codes:
        horodatage = datetime.now().isoformat(timespec="seconds")
        try:
            abos, facts = client.abonnements(code), client.factures(code)
        except Exception as exc:                      # un dossier en échec n'arrête pas la collecte
            log.warning("Collecte du dossier %s en échec : %s", code, exc)
            cx.execute("INSERT INTO synchros (horodatage, dossier, source, message) VALUES (?,?,?,?)",
                       (horodatage, code, source, f"ÉCHEC : {exc}"))
            cx.commit()
            bilan.append({"dossier": code, "ok": False, "message": str(exc)})
            continue
        cx.execute("DELETE FROM abonnements WHERE dossier=?", (code,))
        cx.execute("DELETE FROM factures WHERE dossier=?", (code,))
        cx.executemany("""INSERT INTO abonnements (id, dossier, customer_id, label, statut, mode, debut, fin,
                          prochaine_occurrence, occurrence_precedente, type_regle, intervalle, montant_ht, parametres)
                          VALUES (:id, :dossier, :customer_id, :label, :statut, :mode, :debut, :fin,
                          :prochaine_occurrence, :occurrence_precedente, :type_regle, :intervalle, :montant_ht, :parametres)""",
                       [{**a, "dossier": code,
                         "parametres": json.dumps(a["parametres"], ensure_ascii=False) if a.get("parametres") else ""} for a in abos])
        cx.executemany("""INSERT INTO factures (id, dossier, customer_id, subscription_id, numero, libelle, date,
                          echeance, montant_ht, montant_ttc, restant_du, statut, reference)
                          VALUES (:id, :dossier, :customer_id, :subscription_id, :numero, :libelle, :date,
                          :echeance, :montant_ht, :montant_ttc, :restant_du, :statut, :reference)""",
                       [{"reference": "", **f, "dossier": code} for f in facts])
        cx.execute("UPDATE dossiers SET derniere_synchro=? WHERE code=?", (horodatage, code))
        cx.execute("INSERT INTO synchros (horodatage, dossier, source, abonnements, factures) VALUES (?,?,?,?,?)",
                   (horodatage, code, source, len(abos), len(facts)))
        cx.commit()
        bilan.append({"dossier": code, "ok": True, "abonnements": len(abos), "factures": len(facts)})
    return bilan


def charger(cx: sqlite3.Connection, code: str | None = None, aujourdhui: date | None = None,
            chemin_indices: Path | None = None) -> list[dict]:
    """[{dossier, baux, abonnements, factures}] pour un dossier ou pour tous.

    Un bail géré par la console (calcule = 1) reçoit ici son loyer en vigueur et ses dates de révision,
    recalculés à la date du jour à partir de ses paramètres, des indices relevés et des décisions de gel."""
    from indexation_loyer import calcul
    aujourdhui = aujourdhui or date.today()
    indices = None
    decisions: dict[int, dict[int, str]] = {}
    for r in cx.execute("SELECT bail_id, numero, decision FROM decisions"):
        decisions.setdefault(r["bail_id"], {})[r["numero"]] = r["decision"]
    filtre, args = ("WHERE code=?", (code,)) if code else ("", ())
    sortie = []
    for d in cx.execute(f"SELECT * FROM dossiers {filtre} ORDER BY nom", args).fetchall():
        lire = lambda table, ordre: [dict(r) for r in cx.execute(
            f"SELECT * FROM {table} WHERE dossier=? ORDER BY {ordre}", (d["code"],))]
        baux = lire("baux", "code")
        for b in baux:
            if b.get("calcule") and b.get("date_effet"):
                if indices is None:
                    indices = calcul.table_indices(referentiel.lire_indices(chemin_indices))
                b.update(referentiel.deriver(b, indices, decisions.get(b["id"], {}), aujourdhui))
                b["revisions_non_appliquees"] = 0        # la console le constate sur l'abonnement, plus de case à cocher
        abonnements = lire("abonnements", "debut")
        for a in abonnements:                            # paramètres et lignes lus dans Pennylane, conservés en JSON
            try:
                a["parametres"] = json.loads(a["parametres"]) if a.get("parametres") else {}
            except ValueError:
                a["parametres"] = {}
        # ce que la console a créé elle-même se rattache au bail par son journal, sans rien deviner
        liens = {"abonnements": {}, "factures": {}}
        for e in cx.execute("SELECT bail_id, subscription_id, facture_id FROM envois WHERE dossier=? AND etat='créé'", (d["code"],)):
            if e["subscription_id"]:
                liens["abonnements"][e["subscription_id"]] = e["bail_id"]
            if e["facture_id"]:
                liens["factures"][e["facture_id"]] = e["bail_id"]
        sortie.append({"dossier": dict(d), "baux": baux, "abonnements": abonnements,
                       "factures": lire("factures", "date DESC"), "liens": liens})
    return sortie

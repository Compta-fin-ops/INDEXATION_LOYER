"""Lecture et écriture de la base : référentiel des baux, collecte Pennylane, chargements."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .pennylane_api import Client

log = logging.getLogger(__name__)
COLONNES_BAIL = ("code", "local", "locataire", "customer_id", "subscription_id", "indice", "periodicite", "tva_pct",
                 "date_effet", "date_fin", "loyer_initial_annuel", "loyer_attendu_annuel", "charges_annuelles",
                 "date_derniere_revision", "date_prochaine_revision", "loyer_prochain_annuel", "date_revision_suivante",
                 "statut_revision", "revisions_non_appliquees")


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def enregistrer_dossier(cx: sqlite3.Connection, code: str, nom: str, siren: str = "", variable_jeton: str = "",
                        source: str = "") -> None:
    cx.execute("""INSERT INTO dossiers (code, nom, siren, variable_jeton, source_referentiel) VALUES (?,?,?,?,?)
                  ON CONFLICT(code) DO UPDATE SET nom=excluded.nom, siren=excluded.siren,
                  variable_jeton=CASE WHEN excluded.variable_jeton<>'' THEN excluded.variable_jeton ELSE dossiers.variable_jeton END,
                  source_referentiel=excluded.source_referentiel""", (code, nom, siren, variable_jeton, source))


def enregistrer_baux(cx: sqlite3.Connection, dossier: str, baux: list[dict], remplacer: bool = True) -> int:
    """Insère ou met à jour les baux d'un dossier ; les baux absents de la liste sont retirés si `remplacer`."""
    for b in baux:
        valeurs = [_iso(b.get(c)) for c in COLONNES_BAIL]
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
    """Alimente le référentiel depuis un classeur d'indexation : loyer en vigueur, dates de révision, statut."""
    from indexation_loyer import calcul
    from indexation_loyer.workbook import lire_classeur, lire_indices_classeur
    aujourdhui = aujourdhui or date.today()
    societe, baux, saisies = lire_classeur(chemin)
    indices = calcul.table_indices(lire_indices_classeur(chemin))
    code = code_dossier or "".join(c for c in societe.nom.upper() if c.isalnum())[:12]
    enregistrer_dossier(cx, code, societe.nom, societe.siren, variable_jeton, source=Path(chemin).name)
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
        })
    return enregistrer_baux(cx, code, lignes_baux)


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
                          prochaine_occurrence, occurrence_precedente, type_regle, intervalle, montant_ht)
                          VALUES (:id, :dossier, :customer_id, :label, :statut, :mode, :debut, :fin,
                          :prochaine_occurrence, :occurrence_precedente, :type_regle, :intervalle, :montant_ht)""",
                       [{**a, "dossier": code} for a in abos])
        cx.executemany("""INSERT INTO factures (id, dossier, customer_id, subscription_id, numero, libelle, date,
                          echeance, montant_ht, montant_ttc, restant_du, statut)
                          VALUES (:id, :dossier, :customer_id, :subscription_id, :numero, :libelle, :date,
                          :echeance, :montant_ht, :montant_ttc, :restant_du, :statut)""",
                       [{**f, "dossier": code} for f in facts])
        cx.execute("UPDATE dossiers SET derniere_synchro=? WHERE code=?", (horodatage, code))
        cx.execute("INSERT INTO synchros (horodatage, dossier, source, abonnements, factures) VALUES (?,?,?,?,?)",
                   (horodatage, code, source, len(abos), len(facts)))
        cx.commit()
        bilan.append({"dossier": code, "ok": True, "abonnements": len(abos), "factures": len(facts)})
    return bilan


def charger(cx: sqlite3.Connection, code: str | None = None) -> list[dict]:
    """[{dossier, baux, abonnements, factures}] pour un dossier ou pour tous."""
    filtre, args = ("WHERE code=?", (code,)) if code else ("", ())
    sortie = []
    for d in cx.execute(f"SELECT * FROM dossiers {filtre} ORDER BY nom", args):
        lire = lambda table, ordre: [dict(r) for r in cx.execute(
            f"SELECT * FROM {table} WHERE dossier=? ORDER BY {ordre}", (d["code"],))]
        sortie.append({"dossier": dict(d), "baux": lire("baux", "code"),
                       "abonnements": lire("abonnements", "debut"), "factures": lire("factures", "date DESC")})
    return sortie

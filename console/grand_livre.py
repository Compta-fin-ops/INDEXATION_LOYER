"""Comptes clients lus dans le grand livre exporté de Pennylane.

Le fichier est celui que Pennylane produit (export « Grand livre » au format xlsx), déposé à la main dans la
console : aucun appel à Pennylane. Seules les lignes des comptes clients (411…) sont conservées ; le reste du
grand livre n'est pas stocké.

Format constaté sur un export réel (dossier de test, exercice 2022) : une feuille, dix colonnes, une ligne par écriture,
le solde courant repartant de zéro pour chaque compte. Il n'y a pas de ligne de report : le solde d'un compte
est celui de la période exportée. Pour un solde juste, exporter depuis l'ouverture de l'exercice, ou depuis une
date où le compte était soldé. Le sens exact du signe « ↔ » qui suit parfois la lettre n'est pas documenté
(lettrage partiel, probablement) : il est conservé tel quel.
"""
from __future__ import annotations

import io
import re
import sqlite3
import unicodedata
import zipfile
from datetime import date, datetime

from . import referentiel

COLONNES = ("N° de compte", "Libellé de compte", "Date", "Journal", "Libellé de pièce", "N° de facture", "Let.",
            "Débit", "Crédit", "Solde")
TAILLE_MAX = 25 * 1024 * 1024
FORMES = {"SARL", "SAS", "SASU", "SCI", "EURL", "SA", "SELARL", "SNC", "SCP", "SCM", "STE", "SOCIETE", "CABINET", "ETS", "LES",
          "DES", "DU", "DE", "LA", "LE", "ET"}


class GrandLivreRefuse(Exception):
    pass


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(v.strip()[:10], fmt).date()
            except ValueError:
                pass
    return None


def _montant(v) -> float:
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    return round(float(str(v).replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", ".")), 2)


def periode_du_nom(nom: str) -> tuple[date | None, date | None]:
    """« …Grand_livre_2022_01_01_2022_12_31.xlsx » : la période figure dans le nom que donne Pennylane."""
    m = re.search(r"(\d{4})_(\d{2})_(\d{2})_(\d{4})_(\d{2})_(\d{2})", nom or "")
    if not m:
        return None, None
    a = [int(x) for x in m.groups()]
    try:
        return date(a[0], a[1], a[2]), date(a[3], a[4], a[5])
    except ValueError:
        return None, None


def lire(nom: str, contenu: bytes) -> dict:
    """Fichier xlsx du grand livre vers {lignes (comptes 411 seulement), du, au, comptes, total_lignes}."""
    if not contenu:
        raise GrandLivreRefuse("Fichier vide.")
    if len(contenu) > TAILLE_MAX:
        raise GrandLivreRefuse(f"Fichier trop lourd : {len(contenu) / 1e6:.1f} Mo, pour 25 Mo au plus.")
    if not contenu.startswith(b"PK\x03\x04") or not zipfile.is_zipfile(io.BytesIO(contenu)):
        raise GrandLivreRefuse("Ce fichier n'est pas un classeur Excel (.xlsx). Déposer l'export « Grand livre » de Pennylane, au format xlsx.")
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(contenu), read_only=True, data_only=True)
    except Exception as exc:                             # classeur illisible ou protégé
        raise GrandLivreRefuse(f"Classeur illisible : {type(exc).__name__}.") from exc
    ws = wb.worksheets[0]
    lignes_xlsx = ws.iter_rows(values_only=True)
    entete = tuple(str(x or "").strip() for x in next(lignes_xlsx, ()))[:len(COLONNES)]
    if entete != COLONNES:
        raise GrandLivreRefuse("Colonnes inattendues : ce n'est pas l'export « Grand livre » de Pennylane tel que la console le connaît. "
                               f"Attendu : {', '.join(COLONNES)}. Lu : {', '.join(x for x in entete if x) or 'rien'}.")
    sortie, total, dates = [], 0, []
    for n, r in enumerate(lignes_xlsx, start=2):
        if not r or all(v in (None, "") for v in r):
            continue
        total += 1
        compte = str(r[0] or "").strip()
        d = _date(r[2])
        if d:
            dates.append(d)
        if not compte.startswith("411"):
            continue
        if d is None:
            raise GrandLivreRefuse(f"Ligne {n} : date illisible « {r[2]} » sur le compte {compte}.")
        try:
            debit, credit, solde = _montant(r[7]), _montant(r[8]), _montant(r[9])
        except ValueError as exc:
            raise GrandLivreRefuse(f"Ligne {n} : montant illisible sur le compte {compte}.") from exc
        sortie.append({"compte": compte, "libelle": str(r[1] or "").strip(), "date": d.isoformat(), "journal": str(r[3] or "").strip(),
                       "piece": str(r[4] or "").strip(), "facture": str(r[5] or "").strip(), "lettrage": str(r[6] or "").strip(),
                       "debit": debit, "credit": credit, "solde": solde})
    du, au = periode_du_nom(nom)
    du, au = du or (min(dates) if dates else None), au or (max(dates) if dates else None)
    if not sortie:
        raise GrandLivreRefuse("Aucun compte client (411) dans ce grand livre.")
    return {"lignes": sortie, "du": du, "au": au, "total_lignes": total, "comptes": len({l["compte"] for l in sortie})}


def enregistrer(cx: sqlite3.Connection, dossier: str, nom_fichier: str, lu: dict) -> None:
    """Remplace les comptes clients du dossier par ceux du fichier : le dernier grand livre déposé fait foi."""
    cx.execute("DELETE FROM grand_livre WHERE dossier=?", (dossier,))
    cx.executemany("""INSERT INTO grand_livre (dossier, compte, libelle, date, journal, piece, facture, lettrage, debit, credit, solde)
                      VALUES (:dossier, :compte, :libelle, :date, :journal, :piece, :facture, :lettrage, :debit, :credit, :solde)""",
                   [{**l, "dossier": dossier} for l in lu["lignes"]])
    cx.execute("""INSERT INTO grand_livre_imports (dossier, horodatage, auteur, nom_fichier, du, au, lignes, comptes)
                  VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(dossier) DO UPDATE SET horodatage=excluded.horodatage, auteur=excluded.auteur,
                  nom_fichier=excluded.nom_fichier, du=excluded.du, au=excluded.au, lignes=excluded.lignes, comptes=excluded.comptes""",
               (dossier, datetime.now().isoformat(timespec="seconds"), referentiel.utilisateur(), nom_fichier,
                lu["du"].isoformat() if lu["du"] else None, lu["au"].isoformat() if lu["au"] else None, len(lu["lignes"]), lu["comptes"]))
    referentiel.tracer(cx, "Grand livre déposé", f"{nom_fichier} : {lu['comptes']} compte(s) client, {len(lu['lignes'])} ligne(s)", dossier)
    cx.commit()


def import_du_dossier(cx: sqlite3.Connection, dossier: str) -> dict | None:
    r = cx.execute("SELECT * FROM grand_livre_imports WHERE dossier=?", (dossier,)).fetchone()
    return dict(r) if r else None


def comptes(cx: sqlite3.Connection, dossier: str) -> list[dict]:
    """Un résumé par compte client : solde, montants non lettrés au débit et au crédit."""
    sortie = []
    for r in cx.execute("""SELECT compte, MAX(libelle) AS libelle, COUNT(*) AS lignes, ROUND(SUM(debit) - SUM(credit), 2) AS solde,
                                  ROUND(SUM(CASE WHEN lettrage='' THEN debit ELSE 0 END), 2) AS debit_non_lettre,
                                  ROUND(SUM(CASE WHEN lettrage='' THEN credit ELSE 0 END), 2) AS credit_non_lettre,
                                  MAX(date) AS derniere
                           FROM grand_livre WHERE dossier=? GROUP BY compte ORDER BY compte""", (dossier,)):
        sortie.append(dict(r))
    return sortie


def lignes_du_compte(cx: sqlite3.Connection, dossier: str, compte: str) -> list[dict]:
    return [dict(r) for r in cx.execute("SELECT * FROM grand_livre WHERE dossier=? AND compte=? ORDER BY date, rowid", (dossier, compte))]


def _mots(texte: str) -> set[str]:
    sans = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode("ascii").upper()
    return {m for m in re.split(r"[^A-Z0-9]+", sans) if len(m) >= 3 and m not in FORMES}


def suggerer(compte: dict, baux: list[dict]) -> int | None:
    """Bail dont le locataire partage le plus de mots avec le libellé du compte, s'il n'y en a qu'un. Simple suggestion."""
    mots = _mots(compte["libelle"])
    scores = sorted(((len(mots & _mots(b.get("locataire") or "")), b["id"]) for b in baux), reverse=True)
    if not scores or scores[0][0] == 0 or (len(scores) > 1 and scores[1][0] == scores[0][0]):
        return None
    return scores[0][1]

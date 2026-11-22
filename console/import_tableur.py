"""Import en lot de sociétés et de baux depuis un tableur (Excel .xlsx ou CSV).

Une ligne par bail ; les colonnes de la société sont répétées sur chaque ligne. L'import se fait en
deux temps : analyse et aperçu, puis confirmation. Il ne fait que créer : une société ou un bail
déjà présent n'est jamais modifié. Il est tout ou rien : une seule ligne en erreur et rien n'entre.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import unicodedata
from datetime import date, datetime

from indexation_loyer import calcul
from indexation_loyer.baux import INDICES, METHODES, PERIODICITES_FACTURATION, TYPES_BAIL

from . import referentiel
from .rapprochement import CODES_TVA

TAILLE_MAX = 5 * 1024 * 1024
LIGNES_MAX = 2000
# (titre de colonne, champ, obligatoire, aide)
COLONNES = (
    ("Code dossier", "dossier", True, "Code court de la société, 2 à 12 lettres majuscules ou chiffres, sans espace. Identique sur tous ses baux."),
    ("Raison sociale", "nom", False, "Obligatoire si la société n'existe pas encore dans la console."),
    ("Forme", "forme", False, "SCI, SARL…"),
    ("SIREN", "siren", False, "9 chiffres."),
    ("Adresse société", "adresse", False, "En-tête des courriers."),
    ("Signataire", "signataire", False, "Nom du signataire des courriers."),
    ("Qualité signataire", "qualite_signataire", False, "Gérant par défaut."),
    ("Ville de signature", "ville_signature", False, ""),
    ("Code bail", "code", False, "Vide : numéroté automatiquement B01, B02…"),
    ("Locataire", "locataire", True, ""),
    ("Local", "local", True, "Adresse et désignation du local."),
    ("Adresse de correspondance", "adresse_locataire", False, "Si elle diffère du local."),
    ("Type de bail", "type_bail", False, " / ".join(TYPES_BAIL) + ". Commercial par défaut."),
    ("Prise d'effet", "date_effet", True, "Date, JJ/MM/AAAA."),
    ("Durée (ans)", "duree_ans", False, "Sert à calculer le terme contractuel."),
    ("Terme contractuel", "date_fin", False, "Vide : calculé sur la durée."),
    ("Sortie du locataire", "date_sortie", False, "Arrête les révisions."),
    ("Loyer initial annuel HT", "loyer_initial_annuel", True, "À la prise d'effet, hors charges."),
    ("Charges annuelles HT", "charges_annuelles", False, "Provision sur charges."),
    ("TVA (%)", "tva_pct", False, "Le nombre seul : " + ", ".join(f"{t:g}".replace(".", ",") for t in sorted(CODES_TVA, reverse=True)) + ". 20 par défaut."),
    ("Facturation", "periodicite", False, " / ".join(PERIODICITES_FACTURATION) + ". Mensuelle par défaut."),
    ("Indice", "indice", False, " / ".join(INDICES) + ". ILC par défaut."),
    ("Trimestre de départ", "trimestre_base", False, "Celui que cite le bail, sous la forme 2023-T2. Vide : deux trimestres avant la prise d'effet, à confirmer."),
    ("Révision tous les (ans)", "periodicite_revision_ans", False, "1, 2 ou 3. 1 par défaut."),
    ("Méthode", "methode", False, " / ".join(METHODES) + ". Chaînée par défaut."),
    ("Plafond annuel (%)", "plafond_annuel_pct", False, "Vide : aucun plafond."),
    ("Reprise : date de la dernière révision", "reprise_date", False, "Bail repris en cours : date de la dernière révision réellement appliquée. Les trois colonnes « Reprise » vont ensemble."),
    ("Reprise : loyer annuel HT à cette date", "reprise_loyer_annuel", False, "Loyer réellement en vigueur à cette date, hors charges. La console ne recalcule rien avant."),
    ("Reprise : trimestre de l'indice appliqué", "reprise_trimestre", False, "Sous la forme 2024-T2 : celui appliqué à cette révision. La suivante prendra le même trimestre, une période plus tard."),
    ("Client Pennylane", "customer_id", False, "Identifiant numérique du client dans Pennylane."),
    ("Abonnement Pennylane", "subscription_id", False, "Seulement si un même client porte plusieurs baux."),
    ("Notes", "notes", False, ""),
)
CHAMPS_SOCIETE = ("nom", "forme", "siren", "adresse", "signataire", "qualite_signataire", "ville_signature")
EXEMPLES = (
    ("HALLES", "SCI des Halles (exemple)", "SCI", "123456789", "1 place du Marché, 37000 Tours", "A. Bailleur", "Gérant", "Tours",
     "B01", "Boulangerie Martin (exemple)", "12 rue des Halles, Tours – local RDC", "", "Commercial", date(2021, 7, 1), 9, "", "",
     36000, 2400, 20, "Trimestrielle", "ILC", "2021-T1", 1, "Chaînée", "", date(2025, 7, 1), 40500, "2025-T1", "", "",
     "Loyer renégocié en 2025 : repris au loyer constaté"),
    ("HALLES", "SCI des Halles (exemple)", "SCI", "123456789", "1 place du Marché, 37000 Tours", "A. Bailleur", "Gérant", "Tours",
     "", "Cabinet Durand (exemple)", "12 rue des Halles, Tours – bureaux 1er étage", "", "Professionnel", date(2024, 1, 1), 6, "", "",
     18600, 0, 20, "Mensuelle", "ILAT", "2023-T3", 1, "Chaînée", 3.5, "", "", "", "", "", "Plafond prévu à l'article 5"),
)


class ImportRefuse(ValueError):
    pass


def _cle(titre) -> str:
    sans = unicodedata.normalize("NFKD", str(titre or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", sans.lower())


_PAR_CLE = {_cle(titre): champ for titre, champ, _, _ in COLONNES}


def _texte(v) -> str:
    """Valeur de cellule vers le texte qu'un formulaire aurait reçu."""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return (v.date() if isinstance(v, datetime) else v).isoformat()
    if isinstance(v, bool):
        return ""
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else repr(v)
    return str(v).strip()


def modele_xlsx() -> bytes:
    """Classeur à remplir : feuille « Baux » vide avec les titres, feuille d'exemple, notice des colonnes."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    gras, fond, requis = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F3864"), PatternFill("solid", fgColor="9A5B00")
    for feuille, lignes in ((wb.active, ()), (wb.create_sheet("Exemple"), EXEMPLES)):
        feuille.title = feuille.title if lignes else "Baux"
        for i, (titre, _, obligatoire, _) in enumerate(COLONNES, start=1):
            c = feuille.cell(row=1, column=i, value=titre)
            c.font, c.fill = gras, (requis if obligatoire else fond)
            c.alignment = Alignment(wrap_text=True, vertical="center")
            feuille.column_dimensions[c.column_letter].width = max(14, min(34, len(titre) + 6))
        for n, ligne in enumerate(lignes, start=2):
            for i, v in enumerate(ligne, start=1):
                cellule = feuille.cell(row=n, column=i, value=v if v != "" else None)
                if isinstance(v, date):
                    cellule.number_format = "DD/MM/YYYY"
        feuille.freeze_panes = "A2"
        feuille.row_dimensions[1].height = 32
    notice = wb.create_sheet("Notice")
    notice.append(["Colonne", "Obligatoire", "À saisir"])
    for c in notice[1]:
        c.font, c.fill = gras, fond
    for titre, _, obligatoire, aide in COLONNES:
        notice.append([titre, "oui" if obligatoire else "", aide])
    for texte in ("", "Une ligne par bail, dans la feuille « Baux ». Les colonnes de la société se répètent sur chacun de ses baux.",
                  "Les titres orange sont obligatoires. Ne pas renommer les titres ; l'ordre des colonnes est libre.",
                  "L'import ne fait que créer : une société ou un bail déjà présent dans la console n'est pas modifié.",
                  "Rien n'entre tant que l'aperçu n'est pas confirmé, et rien n'entre si une seule ligne est en erreur."):
        notice.append([texte])
    notice.column_dimensions["A"].width, notice.column_dimensions["B"].width, notice.column_dimensions["C"].width = 30, 12, 110
    tampon = io.BytesIO()
    wb.save(tampon)
    return tampon.getvalue()


def lire(nom: str, contenu: bytes) -> list[dict]:
    """Fichier vers [{n: numéro de ligne dans le fichier, champ: texte}]. Lève ImportRefuse si illisible."""
    if not contenu:
        raise ImportRefuse("Fichier vide.")
    if len(contenu) > TAILLE_MAX:
        raise ImportRefuse(f"Fichier trop lourd : {TAILLE_MAX // (1024 * 1024)} Mo au plus.")
    extension = (nom or "").lower().rsplit(".", 1)[-1]
    if extension == "xlsx":
        if not contenu.startswith(b"PK\x03\x04"):
            raise ImportRefuse("Ce fichier n'est pas un classeur Excel .xlsx.")
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(contenu), read_only=True, data_only=True)
        except Exception as exc:
            raise ImportRefuse(f"Classeur illisible : {exc}") from exc
        feuille = wb["Baux"] if "Baux" in wb.sheetnames else wb[wb.sheetnames[0]]
        brut = [[_texte(v) for v in ligne] for ligne in feuille.iter_rows(values_only=True)]
    elif extension == "csv":
        try:
            texte = contenu.decode("utf-8-sig")
        except UnicodeDecodeError:
            texte = contenu.decode("cp1252", errors="replace")           # export CSV d'Excel sous Windows
        premiere = texte.split("\n", 1)[0]
        separateur = max(";,\t", key=premiere.count)
        brut = [[c.strip() for c in ligne] for ligne in csv.reader(io.StringIO(texte), delimiter=separateur)]
    else:
        raise ImportRefuse("Format refusé : déposer un classeur Excel .xlsx ou un fichier .csv.")
    numerotees = [(n, ligne) for n, ligne in enumerate(brut, start=1) if any(ligne)]
    if not numerotees:
        raise ImportRefuse("Le fichier ne contient aucune ligne.")
    _, entete = numerotees[0]
    champs = [_PAR_CLE.get(_cle(t)) for t in entete]
    inconnues = [t for t, c in zip(entete, champs) if t and c is None]
    if inconnues:
        raise ImportRefuse("Colonne(s) inconnue(s) : " + ", ".join(inconnues) + ". Reprendre les titres du modèle.")
    doublons = {c for c in champs if c and champs.count(c) > 1}
    if doublons:
        raise ImportRefuse("Colonne en double : " + ", ".join(t for t, c, _, _ in COLONNES if c in doublons) + ".")
    manquantes = [titre for titre, champ, obligatoire, _ in COLONNES if obligatoire and champ not in champs]
    if manquantes:
        raise ImportRefuse("Colonne(s) obligatoire(s) absente(s) : " + ", ".join(manquantes) + ".")
    lignes = [{"n": n, **{c: v for c, v in zip(champs, ligne) if c}} for n, ligne in numerotees[1:]]
    if not lignes:
        raise ImportRefuse("Le fichier ne contient que la ligne de titres : la feuille « Baux » est vide.")
    if len(lignes) > LIGNES_MAX:
        raise ImportRefuse(f"Trop de lignes : {len(lignes)}, pour {LIGNES_MAX} au plus par import.")
    return lignes


def analyser(cx: sqlite3.Connection, lignes: list[dict], indices: dict, aujourdhui: date) -> dict:
    """Contrôle chaque ligne sans rien écrire. Chaque ligne reçoit un état : créer, ignorer (déjà présent) ou erreur."""
    existants = {r["code"]: r["nom"] for r in cx.execute("SELECT code, nom FROM dossiers")}
    pris: dict[str, set[str]] = {}
    for r in cx.execute("SELECT dossier, code FROM baux"):
        pris.setdefault(r["dossier"], set()).add(r["code"])
    vus: dict[str, set[str]] = {}
    nouveaux: dict[str, dict] = {}
    sortie = []
    for l in lignes:
        erreurs, avertissements = [], []
        dossier = (l.get("dossier") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{2,12}", dossier):
            erreurs.append("Code dossier : 2 à 12 lettres majuscules, chiffres ou soulignés.")
        societe = {c: (l.get(c) or "").strip() for c in CHAMPS_SOCIETE}
        societe["siren"] = societe["siren"].replace(" ", "")
        if dossier and dossier not in existants:
            if dossier not in nouveaux:
                if not societe["nom"]:
                    erreurs.append("Raison sociale : obligatoire, la société n'existe pas encore.")
                if societe["siren"] and not re.fullmatch(r"\d{9}", societe["siren"]):
                    erreurs.append("SIREN : 9 chiffres.")
                if not erreurs:
                    nouveaux[dossier] = {**societe, "qualite_signataire": societe["qualite_signataire"] or "Gérant"}
            elif societe["nom"] and societe["nom"] != nouveaux[dossier]["nom"]:
                avertissements.append(f"Raison sociale différente de la première ligne du dossier : « {nouveaux[dossier]['nom']} » est retenue.")
        champs = dict(l)
        if not (champs.get("code") or "").strip():                       # numérotation automatique, sans collision
            occupes = pris.get(dossier, set()) | vus.get(dossier, set())
            champs["code"] = next(f"B{i:02d}" for i in range(1, 10000) if f"B{i:02d}" not in occupes)
        valeurs, e = referentiel.valider(champs)
        erreurs += e
        etat = "creer"
        if valeurs["code"] in vus.get(dossier, set()):
            erreurs.append(f"Code bail {valeurs['code']} en double dans le fichier pour ce dossier.")
        elif valeurs["code"] in pris.get(dossier, set()):
            etat = "ignorer"
        vus.setdefault(dossier, set()).add(valeurs["code"])
        loyer = None
        if not erreurs and etat == "creer":
            alerte = referentiel.controler_indice_de_depart(valeurs, indices)
            if alerte:
                avertissements.append(alerte)
            loyer = referentiel.deriver(valeurs, indices, {}, aujourdhui)["loyer_attendu_annuel"]
        sortie.append({"n": l["n"], "dossier": dossier, "nom": existants.get(dossier) or nouveaux.get(dossier, {}).get("nom", societe["nom"]),
                       "dossier_nouveau": dossier not in existants, "valeurs": valeurs, "erreurs": erreurs,
                       "avertissements": avertissements, "etat": "erreur" if erreurs else etat, "loyer_en_vigueur": loyer})
    a_creer = [x for x in sortie if x["etat"] == "creer"]
    return {"lignes": sortie, "nouveaux": {d: s for d, s in nouveaux.items() if any(x["dossier"] == d for x in a_creer)},
            "nb_creer": len(a_creer), "nb_ignorer": sum(1 for x in sortie if x["etat"] == "ignorer"),
            "nb_erreur": sum(1 for x in sortie if x["etat"] == "erreur"),
            "possible": bool(a_creer) and not any(x["etat"] == "erreur" for x in sortie)}


def executer(cx: sqlite3.Connection, analyse: dict, nom_fichier: str = "") -> tuple[int, int]:
    """Crée les sociétés et les baux de l'analyse en une seule transaction. Tout entre, ou rien."""
    if not analyse["possible"]:
        raise ImportRefuse("Import impossible : au moins une ligne est en erreur, ou il n'y a rien à créer.")
    try:
        for code, s in analyse["nouveaux"].items():
            cx.execute("""INSERT INTO dossiers (code, nom, siren, variable_jeton, source_referentiel, forme, adresse, signataire,
                          qualite_signataire, ville_signature) VALUES (?,?,?,'','console',?,?,?,?,?)""",
                       (code, s["nom"], s["siren"], s["forme"], s["adresse"], s["signataire"], s["qualite_signataire"], s["ville_signature"]))
            referentiel.tracer(cx, "Dossier créé", f"{s['nom']} · import de {nom_fichier}", code)
        n = 0
        for x in analyse["lignes"]:
            if x["etat"] == "creer":
                referentiel.enregistrer_bail(cx, x["dossier"], x["valeurs"], valider=False)
                n += 1
        referentiel.tracer(cx, "Import en lot", f"{nom_fichier} : {len(analyse['nouveaux'])} société(s), {n} bail/baux créés, "
                                                f"{analyse['nb_ignorer']} ligne(s) déjà présente(s) ignorée(s)")
        cx.commit()
    except Exception:
        cx.rollback()
        raise
    return len(analyse["nouveaux"]), n

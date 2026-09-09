"""Classeur Excel autonome : un onglet par bail, un récapitulatif, des indices à coller.

  Lisez-moi        mode d'emploi, derniers indices connus, points de vigilance
  Société          paramètres (raison sociale, signataire, réglages Pennylane)
  Récapitulatif    une ligne par onglet de bail (nom d'onglet saisi en colonne A, le reste en formules)
  Indices          quatre zones de collage (ILC, ILAT, ICC, IRL) au format de l'export insee.fr
  Grille indices   vue année × trimestre des valeurs collées (formules)
  Modèle           fiche de bail vierge à dupliquer (clic droit sur l'onglet > Déplacer ou copier > Créer une copie)
  <un onglet par bail>  fiche : paramètres, situation actuelle, tableau des révisions
  Pennylane        préparation des abonnements (une ligne par ligne du récapitulatif)

Tout est formules : le classeur vit sans le script. Le script crée le classeur, écrit les
indices INSEE dans les zones de collage, génère les courriers et prépare Pennylane.
Formules en syntaxe « fichier » (anglais, virgule) ; Excel et LibreOffice les localisent.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from . import __version__
from .baux import DECISIONS, INDICES, METHODES, PERIODICITES_FACTURATION, TYPES_BAIL, Bail
from .insee import Observation, charger_config

log = logging.getLogger(__name__)

MAX_ECHEANCES = 12          # révisions pré-câblées par fiche
MAX_RECAP = 40              # lignes du récapitulatif
LIGNES_ZONE = 200           # lignes de chaque zone de collage Indices (50 ans de trimestres)
ANNEE_MIN, ANNEE_MAX = 2000, 2035
SERIES = ("ILC", "ILAT", "ICC", "IRL")
FEUILLES_FIXES = {"Lisez-moi", "Société", "Récapitulatif", "Indices", "Grille indices", "Modèle", "Pennylane"}

# --- styles -----------------------------------------------------------------
BLEU = "1F3864"
FILL_ENTETE = PatternFill("solid", fgColor=BLEU)
FILL_SAISIE = PatternFill("solid", fgColor="FFF2CC")
FILL_DEMO = PatternFill("solid", fgColor="F8CBAD")
FILL_SECTION = PatternFill("solid", fgColor="D9E1F2")
FONT_ENTETE = Font(bold=True, color="FFFFFF")
FONT_TITRE = Font(bold=True, size=16, color=BLEU)
FONT_GRAS = Font(bold=True)
FONT_GRIS = Font(italic=True, color="7F7F7F")
BORDURE = Border(*(Side(style="thin", color="BFBFBF"),) * 4)
FMT_EUR = '#,##0.00\\ "€"'
FMT_PCT = "0.00%"
FMT_DATE = "DD/MM/YYYY"
FMT_INDICE = "0.00"


@dataclass
class Societe:
    nom: str
    siren: str = ""
    forme: str = "SCI"
    regime_tva: str = "Loyers soumis à TVA sur option (20 %)"
    pennylane_company: str = ""
    contact: str = ""
    adresse: str = ""
    signataire: str = ""
    qualite_signataire: str = "Gérant"
    ville_signature: str = ""
    pl_mode: str = "awaiting_validation"
    pl_payment_conditions: str = "upon_receipt"
    pl_payment_method: str = "offline"
    demo: bool = False
    max_echeances: int = MAX_ECHEANCES


@dataclass
class Saisies:
    """Colonnes saisies dans le tableau des révisions, clé (ID bail, n° révision)."""
    decision: dict[tuple[str, int], str] = field(default_factory=dict)
    consigne: dict[tuple[str, int], str] = field(default_factory=dict)
    applique: dict[tuple[str, int], str] = field(default_factory=dict)
    date_application: dict[tuple[str, int], object] = field(default_factory=dict)
    courrier: dict[tuple[str, int], object] = field(default_factory=dict)
    commentaire: dict[tuple[str, int], str] = field(default_factory=dict)

    CHAMPS = (("decision", "Décision"), ("consigne", "Consigne (à faire)"), ("applique", "Appliqué ? (Oui/Non)"),
              ("date_application", "Date d'application"), ("courrier", "Courrier envoyé le"), ("commentaire", "Commentaire"))


# =============================================================================
# Géométrie de la fiche de bail
# =============================================================================
# Paramètres : libellé en B, valeur en C (lignes 3..24). Situation : libellé G:J fusionné, valeur K (lignes 3..12).
PARAMS = [  # (clé, libellé, ligne, saisie ?)
    ("id", "ID bail (= nom de l'onglet)", 3, True), ("local", "Local (désignation / adresse)", 4, True),
    ("locataire", "Locataire", 5, True), ("adresse_locataire", "Adresse du locataire (courrier)", 6, True),
    ("type_bail", "Type de bail", 7, True), ("date_effet", "Date de prise d'effet", 8, True), ("duree_ans", "Durée (ans)", 9, True),
    ("date_fin", "Date de fin", 10, False), ("loyer_initial_annuel_ht", "Loyer initial annuel HT", 11, True),
    ("charges_annuelles_ht", "Charges annuelles HT", 12, True), ("tva_pct", "TVA (%)", 13, True),
    ("periodicite_facturation", "Périodicité de facturation", 14, True), ("indice", "Indice", 15, True),
    ("trimestre_base", "Trimestre indice de base", 16, True), ("periodicite_revision_ans", "Périodicité de révision (ans)", 17, True),
    ("methode", "Méthode", 18, True), ("plafond_annuel_pct", "Plafond annuel (%) – optionnel", 19, True),
    ("pennylane_customer_id", "Pennylane customer_id", 20, True), ("pennylane_product_id", "Pennylane product_id", 21, True),
    ("pennylane_subscription_id", "Pennylane subscription_id", 22, True), ("notes", "Notes / clause d'indexation", 23, True),
    ("controles", "Contrôles", 24, False),
]
PC = {cle: f"C{ligne}" for cle, _, ligne, _ in PARAMS}          # cellule de chaque paramètre
PARAM_LIGNE = {cle: ligne for cle, _, ligne, _ in PARAMS}

SITUATION = [
    ("loyer_actuel", "Loyer actuel (annuel HT)", 3), ("loyer_mensuel", "Loyer actuel mensuel HT", 4),
    ("n_eff", "N° dernière révision effective", 5), ("prochaine", "Prochaine révision", 6),
    ("trim_attendu", "Trimestre attendu", 7), ("indice_publie", "Indice publié ?", 8),
    ("a_facturer", "Révisions calculables non facturées", 9), ("gelees", "Révisions gelées", 10),
    ("action", "Action", 11), ("consigne_prochaine", "Consigne de la prochaine révision", 12),
]
SC = {cle: f"K{ligne}" for cle, _, ligne in SITUATION}

LIGNE_TABLE = 27            # ligne d'en-tête du tableau des révisions ; données à partir de 28
COL_TABLE = 5               # colonne E
COLS_REV = [
    ("N°", 5), ("Date de révision", 12), ("Trim. référence", 10), ("Valeur indice réf.", 10), ("Trim. précédent", 10),
    ("Valeur indice préc.", 10), ("Coefficient", 11), ("Décision", 24), ("Base de calcul (annuel HT)", 14),
    ("Loyer révisé brut", 13), ("Loyer retenu (annuel HT)", 14), ("Loyer précédent (annuel HT)", 14), ("Variation", 8),
    ("Loyer mensuel HT", 12), ("Loyer / échéance HT", 12), ("Charges / échéance HT", 12), ("TVA / échéance", 11),
    ("Total TTC / échéance", 12), ("Statut", 20), ("Consigne (à faire)", 40), ("Appliqué ? (Oui/Non)", 10),
    ("Date d'application", 12), ("Courrier envoyé le", 12), ("Commentaire", 28), ("Effectif", 6), ("Actif", 6),
]
R = {nom: get_column_letter(COL_TABLE + i) for i, (nom, _) in enumerate(COLS_REV)}


def ligne_revision(k: int) -> int:
    return LIGNE_TABLE + k


def nom_onglet(id_bail: str) -> str:
    """Nom d'onglet Excel valide (31 caractères max, sans []:*?/\\)."""
    nom = re.sub(r"[\[\]:*?/\\]", "-", str(id_bail)).strip().strip("'")
    return (nom or "Bail")[:31]


# --- Récapitulatif -----------------------------------------------------------
COLS_RECAP = [  # (libellé, largeur, clé de la fiche ou None)
    ("Onglet", 14, None), ("ID bail", 10, "id"), ("Local", 28, "local"), ("Locataire", 22, "locataire"), ("Indice", 6, "indice"),
    ("Prise d'effet", 11, "date_effet"), ("Date de fin", 11, "date_fin"), ("Loyer initial annuel HT", 13, "loyer_initial_annuel_ht"),
    ("Loyer actuel (annuel HT)", 13, "loyer_actuel"), ("Loyer actuel mensuel HT", 12, "loyer_mensuel"),
    ("Prochaine révision", 11, "prochaine"), ("Trimestre attendu", 10, "trim_attendu"), ("Indice publié ?", 8, "indice_publie"),
    ("À facturer", 8, "a_facturer"), ("Gelées", 7, "gelees"), ("Action", 34, "action"),
    ("Consigne prochaine révision", 40, "consigne_prochaine"), ("Contrôles", 30, None),
]
A = {nom: get_column_letter(i + 1) for i, (nom, _, _) in enumerate(COLS_RECAP)}

COLS_PL = [
    ("Onglet", 12), ("Locataire", 22), ("customer_id", 12), ("product_id", 10), ("label (abonnement)", 30),
    ("Périodicité", 12), ("recurring_rule.type", 10), ("interval", 7), ("unit", 9),
    ("Prix unitaire HT / échéance", 13), ("Charges HT / échéance", 12), ("TVA (%)", 7), ("vat_rate", 9),
    ("mode", 16), ("payment_conditions", 15), ("payment_method", 13), ("start (1er du mois suivant)", 12),
    ("subscription_id existant", 12), ("Corps JSON – POST /api/external/v2/billing_subscriptions", 100),
]
P = {nom: get_column_letter(i + 1) for i, (nom, _) in enumerate(COLS_PL)}

LIB_SOC = {
    "nom": "Raison sociale", "siren": "SIREN", "forme": "Forme", "regime_tva": "Régime TVA des loyers",
    "pennylane_company": "Identifiant société Pennylane", "contact": "Contact cabinet",
    "adresse": "Adresse du bailleur (courriers)", "signataire": "Signataire des courriers",
    "qualite_signataire": "Qualité du signataire", "ville_signature": "Ville de signature",
    "pl_mode": "Pennylane – mode des factures", "pl_payment_conditions": "Pennylane – conditions de paiement",
    "pl_payment_method": "Pennylane – moyen de paiement",
    "date": "Date de génération", "demo": "Mode démonstration", "max_echeances": "Capacité – révisions par bail", "version": "Version du modèle",
}
LISTES_SOC = {
    "pl_mode": ("awaiting_validation", "finalized"),
    "pl_payment_conditions": ("upon_receipt", "7_days", "15_days", "30_days", "30_days_end_of_month", "45_days", "45_days_end_of_month", "60_days"),
    "pl_payment_method": ("offline", "gocardless_direct_debit", "pro_account_sepa_core"),
}
SOC_ROW = {cle: i for i, cle in enumerate(LIB_SOC, start=1)}


# --- Indices : zones de collage ---------------------------------------------------
def colonne_zone(serie: str) -> int:
    """Première colonne (Période) de la zone de collage de la série."""
    return 1 + 4 * SERIES.index(serie)


def _lookup_indice(serie_ref: str, periode_ref: str) -> str:
    """Valeur d'indice ("" si absente) dans la zone de collage de la série : la colonne Période
    est repérée par le code série en ligne 1 de la feuille Indices."""
    col = f"MATCH({serie_ref},Indices!$1:$1,0)"
    s = f"SUMIFS(INDEX(Indices!$A:$P,0,{col}+1),INDEX(Indices!$A:$P,0,{col}),{periode_ref})"
    return f'IF(OR({periode_ref}="",{serie_ref}=""),"",IFERROR(IF({s}=0,"",{s}),""))'


# =============================================================================
# Construction
# =============================================================================
def construire(societe: Societe, baux: list[Bail], observations: list[Observation], chemin: Path,
               saisies: Saisies | None = None, aujourdhui: date | None = None) -> Path:
    aujourdhui = aujourdhui or date.today()
    saisies = saisies or Saisies()
    if len(baux) > MAX_RECAP:
        raise ValueError(f"{len(baux)} baux pour {MAX_RECAP} lignes de récapitulatif")
    wb = Workbook()
    wb.remove(wb.active)
    _feuille_lisezmoi(wb, societe, aujourdhui)
    _feuille_societe(wb, societe, aujourdhui)
    _feuille_recap(wb)
    _feuille_indices(wb)
    _feuille_grille(wb)
    _feuille_fiche(wb.create_sheet("Modèle"), societe.max_echeances, modele=True)
    onglets = []
    for b in baux:
        onglet = nom_onglet(b.id)
        ws = wb.create_sheet(onglet)
        _feuille_fiche(ws, societe.max_echeances)
        ecrire_parametres(ws, b)
        ecrire_saisies(ws, b.id, saisies, societe.max_echeances)
        onglets.append(onglet)
    _feuille_pennylane(wb)
    ws_r = wb["Récapitulatif"]
    for i, onglet in enumerate(onglets, start=2):
        ws_r[f"A{i}"] = onglet
    ecrire_indices(wb, observations)
    wb.calculation.fullCalcOnLoad = True
    chemin.parent.mkdir(parents=True, exist_ok=True)
    wb.save(chemin)
    log.info("Classeur écrit : %s", chemin)
    return chemin


def _entetes(ws: Worksheet, colonnes, ligne: int = 1, col0: int = 1, figer: bool = True) -> None:
    for i, col in enumerate(colonnes):
        nom, largeur = col[0], col[1]
        c = ws.cell(row=ligne, column=col0 + i, value=nom)
        c.fill, c.font, c.border = FILL_ENTETE, FONT_ENTETE, BORDURE
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.column_dimensions[get_column_letter(col0 + i)].width = largeur
    ws.row_dimensions[ligne].height = 42
    if figer:
        ws.freeze_panes = ws.cell(row=ligne + 1, column=1)


# --- Lisez-moi ---------------------------------------------------------------
def _feuille_lisezmoi(wb: Workbook, societe: Societe, aujourdhui: date) -> None:
    ws = wb.create_sheet("Lisez-moi")
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 44
    for col in "CDEF":
        ws.column_dimensions[col].width = 20
    ws["B2"] = f"Suivi des réindexations de loyers – {societe.nom}"
    ws["B2"].font = FONT_TITRE
    ws["B3"] = f"Modèle v{__version__} généré le {aujourdhui:%d/%m/%Y}"
    ws["B3"].font = FONT_GRIS
    l = 5
    if societe.demo:
        ws.cell(row=l, column=2, value="⚠ CLASSEUR DE DÉMONSTRATION : indices ILAT/ICC/IRL et baux FICTIFS (ILC = valeurs INSEE réelles). Ne pas utiliser pour un calcul réel.").font = Font(bold=True, color="C00000")
        for c in range(2, 7):
            ws.cell(row=l, column=c).fill = FILL_DEMO
        l += 2
    ws.cell(row=l, column=2, value="Derniers indices collés (feuille Indices)").font = FONT_GRAS
    l += 1
    for i, nom in enumerate(["Série", "Dernier trimestre", "Valeur", "Variation annuelle"], start=2):
        c = ws.cell(row=l, column=i, value=nom)
        c.fill, c.font = FILL_SECTION, FONT_GRAS
    config = charger_config()["series"]
    for code in SERIES:
        l += 1
        c0 = get_column_letter(colonne_zone(code))
        per = f"Indices!${c0}$4:${c0}${3 + LIGNES_ZONE}"
        # rang = année*4 + trimestre, calculé sur les périodes collées (ignore les lignes non conformes)
        rang = f'_xlfn.MAXIFS(Grille_rang!$B:$B,Grille_rang!$A:$A,"{code}")'
        ws.cell(row=l, column=2, value=f"{code} – {config[code]['libelle']}")
        ws.cell(row=l, column=3, value=f'=IF({rang}=0,"aucune valeur",INT(({rang}-1)/4)&"-T"&({rang}-INT(({rang}-1)/4)*4))')
        serie_lit = f'"{code}"'
        val_dernier = _lookup_indice(serie_lit, f"C{l}")
        ws.cell(row=l, column=4, value=f'=IF({rang}=0,"",{val_dernier})').number_format = FMT_INDICE
        prec = f'TEXT(VALUE(LEFT(C{l},4))-1,"0")&RIGHT(C{l},3)'
        val_prec = _lookup_indice(serie_lit, prec)
        ws.cell(row=l, column=5, value=f'=IFERROR(D{l}/{val_prec}-1,"")').number_format = FMT_PCT
    l += 2
    ws.cell(row=l, column=2, value="Mode d'emploi").font = FONT_GRAS
    for texte in [
        "1. Feuille « Indices » : sur insee.fr (série 001532540 pour l'ILC, 001617112 ILAT, 000008630 ICC, 001515333 IRL), « Télécharger » au format xlsx, puis copier les lignes Période / valeur / date JO dans la zone de la série. Un simple collage remplace le précédent. Le reste du classeur se recalcule.",
        "2. Nouveau bail : clic droit sur l'onglet « Modèle » > Déplacer ou copier > Créer une copie ; renommer l'onglet avec l'ID du bail (ex. B01 ou DUPONT) ; remplir les cellules jaunes de la fiche ; la colonne Contrôles doit afficher OK.",
        "3. Feuille « Récapitulatif » : saisir le nom du nouvel onglet en colonne A. La ligne se remplit (loyer actuel, prochaine révision, action…). Le total des loyers est en bas.",
        "4. Fiche de bail : le tableau des révisions se remplit dès la saisie. Statut ✔ Calculable = révision échue et indice connu : facturer, puis renseigner « Appliqué ? » et la date.",
        "   Colonne « Décision » : vide ou Appliquer = révision appliquée ; « Geler – sans rattrapage » = le client renonce à la variation de l'année ; « Geler – rattrapage possible » = loyer inchangé mais la révision suivante repart de l'indice de la dernière révision appliquée.",
        "   Colonne « Consigne (à faire) » : instruction libre du dossier, reprise dans la situation de la fiche et dans le récapitulatif.",
        "5. Feuille « Pennylane » : corps des abonnements de facturation, pour l'automatisation ultérieure. Réglages communs dans la feuille Société.",
        "Trimestres : notation AAAA-Tn (2024-T2 = 2e trimestre 2024). Convention par défaut du trimestre de base : dernier indice publié à la prise d'effet (T-2) ; à caler sur la clause du bail.",
        "Ne pas modifier la structure des fiches (lignes 3 à 24 et tableau) : le récapitulatif et le script y lisent des cellules fixes.",
    ]:
        l += 1
        ws.cell(row=l, column=2, value=texte)
    l += 2
    ws.cell(row=l, column=2, value="Points de vigilance juridiques (à confirmer par le juriste du dossier)").font = FONT_GRAS
    for texte in [
        "• Depuis la loi Pinel (n° 2014-626 du 18 juin 2014), l'ILC et l'ILAT sont les indices de référence des baux commerciaux (art. L145-34 et L145-38 C. com.) ; l'ICC subsiste dans les clauses des baux antérieurs.",
        "• Plafonnement légal de la variation de l'ILC à 3,5 % pour les PME (loi n° 2022-1158 du 16 août 2022, art. 14, puis prolongation) : période et champ à vérifier bail par bail ; utiliser « Plafond annuel (%) ».",
        "• Une clause d'indexation ne jouant qu'à la hausse est réputée non écrite (art. L112-1 C. mon. fin. et jurisprudence Cass. 3e civ.) : le classeur applique la baisse si l'indice recule.",
        "• Révision légale triennale (L145-38), déplafonnement et lissage : hors périmètre ; le classeur traite la clause d'échelle mobile contractuelle.",
        "• Le renoncement du bailleur à une indexation (gel) est un acte de gestion à documenter (consigne, courrier) ; sa portée juridique n'est pas tranchée par l'outil.",
    ]:
        l += 1
        ws.cell(row=l, column=2, value=texte)
    l += 2
    ws.cell(row=l, column=2, value="Légende : fond jaune = saisie ; autres cellules = formules.").font = FONT_GRIS


# --- Société -----------------------------------------------------------------
def _feuille_societe(wb: Workbook, s: Societe, aujourdhui: date) -> None:
    ws = wb.create_sheet("Société")
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 50
    valeurs = {**{k: getattr(s, k) for k in LIB_SOC if hasattr(s, k)}, "date": aujourdhui, "demo": "OUI" if s.demo else "non", "version": __version__}
    saisissables = {"nom", "siren", "forme", "regime_tva", "pennylane_company", "contact", "adresse", "signataire",
                    "qualite_signataire", "ville_signature", "pl_mode", "pl_payment_conditions", "pl_payment_method"}
    for cle, i in SOC_ROW.items():
        ws.cell(row=i, column=1, value=LIB_SOC[cle]).font = FONT_GRAS
        c = ws.cell(row=i, column=2, value=valeurs.get(cle))
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if cle in saisissables:
            c.fill = FILL_SAISIE
        else:
            c.font = FONT_GRIS
        if isinstance(valeurs.get(cle), date):
            c.number_format = FMT_DATE
        if cle in LISTES_SOC:
            dv = DataValidation(type="list", formula1='"' + ",".join(LISTES_SOC[cle]) + '"', allow_blank=False)
            dv.add(f"B{i}")
            ws.add_data_validation(dv)


# --- Récapitulatif -------------------------------------------------------------
def _feuille_recap(wb: Workbook) -> None:
    ws = wb.create_sheet("Récapitulatif")
    _entetes(ws, COLS_RECAP)
    for i in range(2, MAX_RECAP + 2):
        ong = f"$A{i}"
        ref = lambda cell: f'IFERROR(INDIRECT("\'"&{ong}&"\'!{cell}"),"⚠ onglet introuvable")'
        for nom, _, cle in COLS_RECAP:
            c = ws[f"{A[nom]}{i}"]
            c.border = BORDURE
            if cle is None:
                continue
            cell = PC[cle] if cle in PC else SC[cle]
            c.value = f'=IF({ong}="","",{ref("$" + cell[0] + "$" + cell[1:])})'
        ws[f"A{i}"].fill = FILL_SAISIE
        ws[f"{A['Contrôles']}{i}"] = (f'=IF({ong}="","",IF({A["ID bail"]}{i}="⚠ onglet introuvable","⚠ onglet introuvable",'
                                      f'IF({ref("$" + PC["controles"][0] + "$" + PC["controles"][1:])}<>"OK",{ref("$" + PC["controles"][0] + "$" + PC["controles"][1:])},'
                                      f'IF({A["ID bail"]}{i}<>{ong},"ID bail ≠ nom d\'onglet","OK"))))')
        for nom in ("Prise d'effet", "Date de fin", "Prochaine révision"):
            ws[f"{A[nom]}{i}"].number_format = FMT_DATE
        for nom in ("Loyer initial annuel HT", "Loyer actuel (annuel HT)", "Loyer actuel mensuel HT"):
            ws[f"{A[nom]}{i}"].number_format = FMT_EUR
        ws[f"{A['Consigne prochaine révision']}{i}"].alignment = Alignment(wrap_text=True, vertical="top")
    t = MAX_RECAP + 3
    ws[f"A{t}"] = "Total"
    ws[f"A{t}"].font = FONT_GRAS
    for nom in ("Loyer initial annuel HT", "Loyer actuel (annuel HT)", "Loyer actuel mensuel HT"):
        col = A[nom]
        c = ws[f"{col}{t}"]
        c.value = f"=SUM({col}2:{col}{MAX_RECAP + 1})"
        c.number_format, c.font = FMT_EUR, FONT_GRAS
    n = MAX_RECAP + 1
    ws.conditional_formatting.add(f"{A['Action']}2:{A['Action']}{n}", FormulaRule(formula=[f'LEFT({A["Action"]}2,1)="⚠"'], font=Font(color="C00000", bold=True), fill=PatternFill("solid", fgColor="FFC7CE")))
    ws.conditional_formatting.add(f"{A['Action']}2:{A['Action']}{n}", FormulaRule(formula=[f'LEFT({A["Action"]}2,8)="Révision"'], fill=PatternFill("solid", fgColor="FFEB9C")))
    ws.conditional_formatting.add(f"{A['Contrôles']}2:{A['Contrôles']}{n}", FormulaRule(formula=[f'AND({A["Contrôles"]}2<>"",{A["Contrôles"]}2<>"OK")'], font=Font(color="C00000", bold=True)))
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS_RECAP))}{n}"


# --- Indices : zones de collage ------------------------------------------------
def _feuille_indices(wb: Workbook) -> None:
    ws = wb.create_sheet("Indices")
    config = charger_config()["series"]
    for code in SERIES:
        c0 = colonne_zone(code)
        ws.cell(row=1, column=c0, value=code).font = FONT_ENTETE
        ws.cell(row=1, column=c0).fill = FILL_ENTETE
        ws.merge_cells(start_row=1, start_column=c0 + 1, end_row=1, end_column=c0 + 2)
        t = ws.cell(row=1, column=c0 + 1, value=f"{config[code]['libelle'].split(' - ')[0]} – idbank {config[code]['idbank']}")
        t.fill, t.font, t.alignment = FILL_ENTETE, FONT_ENTETE, Alignment(wrap_text=True, vertical="center")
        ws.merge_cells(start_row=2, start_column=c0, end_row=2, end_column=c0 + 2)
        n = ws.cell(row=2, column=c0, value="Coller ici les lignes de l'export insee.fr (Période / valeur / date JO), ou saisir. Doublons :")
        n.font, n.alignment = FONT_GRIS, Alignment(wrap_text=True, vertical="top")
        for j, nom in enumerate(("Période", "Valeur", "Parution JO")):
            h = ws.cell(row=3, column=c0 + j, value=nom)
            h.fill, h.font, h.alignment = FILL_SECTION, FONT_GRAS, Alignment(horizontal="center")
        col_per = get_column_letter(c0)
        per = f"${col_per}$4:${col_per}${3 + LIGNES_ZONE}"
        # contrôle des doublons de période dans la zone (affiché à droite de l'instruction)
        d = ws.cell(row=2, column=c0 + 3, value=f'=IF(SUMPRODUCT(({per}<>"")*(COUNTIF({per},{per})>1))>0,"⚠","OK")')
        d.font = FONT_GRAS
        for r in range(4, 4 + LIGNES_ZONE):
            for j in range(3):
                c = ws.cell(row=r, column=c0 + j)
                c.fill, c.border = FILL_SAISIE, BORDURE
            ws.cell(row=r, column=c0 + 1).number_format = "0" if code == "ICC" else FMT_INDICE
        ws.column_dimensions[col_per].width = 10
        ws.column_dimensions[get_column_letter(c0 + 1)].width = 10
        ws.column_dimensions[get_column_letter(c0 + 2)].width = 12
        ws.column_dimensions[get_column_letter(c0 + 3)].width = 4
    ws.row_dimensions[1].height = 44
    ws.row_dimensions[2].height = 44
    ws.freeze_panes = "A4"
    ws.conditional_formatting.add(f"A4:{get_column_letter(4 * len(SERIES))}{3 + LIGNES_ZONE}",
                                  FormulaRule(formula=['AND(A4<>"",NOT(ISNUMBER(SEARCH("-T",A4))),ISNUMBER(A4)=FALSE,COLUMN()=MATCH(INDEX($1:$1,1,COLUMN()),$1:$1,0))'],
                                              font=Font(color="7F7F7F", italic=True)))
    # feuille masquée : rang (année*4 + trimestre) de chaque période collée, pour « dernier indice connu »
    wr = wb.create_sheet("Grille_rang")
    wr["A1"], wr["B1"] = "Série", "Rang"
    r = 2
    for code in SERIES:
        c0 = get_column_letter(colonne_zone(code))
        for i in range(4, 4 + LIGNES_ZONE):
            cell = f"Indices!{c0}{i}"
            wr[f"A{r}"] = code
            wr[f"B{r}"] = (f'=IF(AND(LEN({cell})=7,MID({cell},5,2)="-T",ISNUMBER(VALUE(LEFT({cell},4))),ISNUMBER(INDEX(Indices!$A:$P,ROW({cell}),COLUMN({cell})+1))),'
                           f'VALUE(LEFT({cell},4))*4+VALUE(RIGHT({cell},1)),0)')
            r += 1
    wr.sheet_state = "hidden"


def _feuille_grille(wb: Workbook) -> None:
    ws = wb.create_sheet("Grille indices")
    config = charger_config()["series"]
    ws.column_dimensions["A"].width = 8
    for cell in ("A1", "A2"):
        ws[cell].fill, ws[cell].font = FILL_ENTETE, FONT_ENTETE
    ws["A1"] = "Année"
    for j, code in enumerate(SERIES):
        c0 = 2 + 4 * j
        ws.merge_cells(start_row=1, start_column=c0, end_row=1, end_column=c0 + 3)
        cell = ws.cell(row=1, column=c0, value=f"{code} – base {config[code]['base']}")
        cell.fill, cell.font, cell.alignment = FILL_ENTETE, FONT_ENTETE, Alignment(horizontal="center", wrap_text=True)
        for q in range(1, 5):
            c = ws.cell(row=2, column=c0 + q - 1, value=f"T{q}")
            c.fill, c.font, c.alignment = FILL_SECTION, FONT_GRAS, Alignment(horizontal="center")
            ws.column_dimensions[get_column_letter(c0 + q - 1)].width = 9
    ws.row_dimensions[1].height = 30
    for annee in range(ANNEE_MIN, ANNEE_MAX + 1):
        r = 3 + annee - ANNEE_MIN
        ws.cell(row=r, column=1, value=annee).font = FONT_GRAS
        for j, code in enumerate(SERIES):
            for q in range(1, 5):
                formule = _lookup_indice(f'"{code}"', f'"{annee}-T{q}"')
                c = ws.cell(row=r, column=2 + 4 * j + q - 1, value=f"={formule}")
                c.border = BORDURE
                c.number_format = "0" if code == "ICC" else FMT_INDICE
    ws.freeze_panes = "B3"
    ws.cell(row=3 + ANNEE_MAX - ANNEE_MIN + 2, column=1, value="Vue en lecture seule des valeurs collées dans la feuille Indices.").font = FONT_GRIS


def ecrire_indices(wb: Workbook, observations: list[Observation]) -> int:
    """Réécrit chaque zone de collage à partir des observations (du plus récent au plus ancien)."""
    ws = wb["Indices"]
    n = 0
    par_serie: dict[str, list[Observation]] = {s: [] for s in SERIES}
    for o in observations:
        if o.serie in par_serie:
            par_serie[o.serie].append(o)
    for code, obs in par_serie.items():
        if not obs:
            continue
        c0 = colonne_zone(code)
        for r in range(4, 4 + LIGNES_ZONE):
            for j in range(3):
                ws.cell(row=r, column=c0 + j).value = None
                ws.cell(row=r, column=c0 + j).fill = FILL_SAISIE
        for i, o in enumerate(sorted(obs, key=lambda o: o.periode, reverse=True)[:LIGNES_ZONE]):
            r = 4 + i
            ws.cell(row=r, column=c0, value=o.periode)
            ws.cell(row=r, column=c0 + 1, value=o.valeur)
            ws.cell(row=r, column=c0 + 2, value=o.statut_obs.replace("JO ", "") if o.statut_obs.startswith("JO ") else (o.source if o.source != "INSEE_SDMX" else o.statut_obs))
            if o.source == "FICTIF_DEMO":
                for j in range(3):
                    ws.cell(row=r, column=c0 + j).fill = FILL_DEMO
            n += 1
    return n


# --- Fiche de bail --------------------------------------------------------------
def _feuille_fiche(ws: Worksheet, max_ech: int, modele: bool = False) -> None:
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 2
    ws["B1"] = '=IF(C3="","Fiche de bail","Fiche de bail – "&C3&IF(C5<>""," – "&C5,""))'
    ws["B1"].font = FONT_TITRE
    if modele:
        ws["G1"] = "MODÈLE : dupliquer cet onglet (clic droit > Déplacer ou copier > Créer une copie), le renommer avec l'ID du bail, remplir les cellules jaunes, puis saisir le nom de l'onglet dans Récapitulatif."
        ws["G1"].font = Font(bold=True, color="C00000")
    # paramètres
    for cle, libelle, ligne, saisie in PARAMS:
        lab = ws.cell(row=ligne, column=2, value=libelle)
        lab.font, lab.border = FONT_GRAS, BORDURE
        c = ws.cell(row=ligne, column=3)
        c.border = BORDURE
        c.alignment = Alignment(vertical="top", wrap_text=(cle in ("adresse_locataire", "notes")))
        if saisie:
            c.fill = FILL_SAISIE
    ws.row_dimensions[PARAM_LIGNE["adresse_locataire"]].height = 30
    ws.row_dimensions[PARAM_LIGNE["notes"]].height = 45
    for cle in ("date_effet", "date_fin"):
        ws[PC[cle]].number_format = FMT_DATE
    for cle in ("loyer_initial_annuel_ht", "charges_annuelles_ht"):
        ws[PC[cle]].number_format = FMT_EUR
    ws[PC["date_fin"]] = f'=IF(OR({PC["date_effet"]}="",{PC["duree_ans"]}=""),"",EDATE({PC["date_effet"]},12*{PC["duree_ans"]}))'
    base_ok = _lookup_indice(PC["indice"], PC["trimestre_base"]) + '<>""'
    ws[PC["controles"]] = (
        f'=IF({PC["id"]}="","",IF(AND({PC["date_effet"]}<>"",ISNUMBER({PC["loyer_initial_annuel_ht"]}),{PC["indice"]}<>"",LEN({PC["trimestre_base"]})=7,{base_ok},'
        f'OR({PC["plafond_annuel_pct"]}="",ISNUMBER({PC["plafond_annuel_pct"]}))),"OK",TRIM('
        f'IF({PC["date_effet"]}="","Date d\'effet manquante. ","")'
        f'&IF(NOT(ISNUMBER({PC["loyer_initial_annuel_ht"]})),"Loyer initial manquant. ","")'
        f'&IF({PC["indice"]}="","Indice manquant. ","")'
        f'&IF(LEN({PC["trimestre_base"]})<>7,"Trimestre de base au format AAAA-Tn. ","")'
        f'&IF(AND(LEN({PC["trimestre_base"]})=7,{PC["indice"]}<>"",NOT({base_ok})),"Indice de base "&{PC["trimestre_base"]}&" absent de la feuille Indices. ","")'
        f'&IF(AND({PC["plafond_annuel_pct"]}<>"",NOT(ISNUMBER({PC["plafond_annuel_pct"]}))),"Plafond non numérique. ",""))))'
    )
    ws[PC["controles"]].font = FONT_GRAS
    for cle, liste in [("type_bail", TYPES_BAIL), ("periodicite_facturation", tuple(PERIODICITES_FACTURATION)),
                       ("indice", INDICES), ("methode", METHODES), ("periodicite_revision_ans", ("1", "2", "3"))]:
        dv = DataValidation(type="list", formula1='"' + ",".join(liste) + '"', allow_blank=True)
        dv.add(PC[cle])
        ws.add_data_validation(dv)
    # valeurs par défaut du modèle
    ws[PC["type_bail"]] = "Commercial"
    ws[PC["duree_ans"]] = 9
    ws[PC["tva_pct"]] = 20
    ws[PC["periodicite_facturation"]] = "Mensuelle"
    ws[PC["indice"]] = "ILC"
    ws[PC["periodicite_revision_ans"]] = 1
    ws[PC["methode"]] = "Chaînée"

    # situation actuelle (libellé G:J fusionné, valeur K)
    ws.cell(row=2, column=7, value="Situation actuelle").font = FONT_GRAS
    d0, d1 = ligne_revision(1), ligne_revision(max_ech)
    rng = lambda nom: f"${R[nom]}${d0}:${R[nom]}${d1}"
    n_eff = SC["n_eff"]
    formules = {
        "n_eff": f'=_xlfn.MAXIFS({rng("N°")},{rng("Effectif")},1)',
        "loyer_actuel": f'=IF({PC["loyer_initial_annuel_ht"]}="","",IF({n_eff}=0,{PC["loyer_initial_annuel_ht"]},INDEX({rng("Loyer retenu (annuel HT)")},{n_eff})))',
        "loyer_mensuel": f'=IF({SC["loyer_actuel"]}="","",{SC["loyer_actuel"]}/12)',
        "prochaine": f'=IF(_xlfn.MINIFS({rng("Date de révision")},{rng("Actif")},1,{rng("Date de révision")},">"&TODAY())=0,"—",_xlfn.MINIFS({rng("Date de révision")},{rng("Actif")},1,{rng("Date de révision")},">"&TODAY()))',
        "trim_attendu": f'=IF({n_eff}+1>{max_ech},"—",IF(INDEX({rng("Actif")},{n_eff}+1)=0,"—",INDEX({rng("Trim. référence")},{n_eff}+1)))',
        "indice_publie": f'=IF({SC["trim_attendu"]}="—","—",IF({_lookup_indice(PC["indice"], SC["trim_attendu"])}="","Non","Oui"))',
        "a_facturer": f'=COUNTIFS({rng("Statut")},"✔ Calculable",{rng("Appliqué ? (Oui/Non)")},"<>Oui")',
        "gelees": f'=COUNTIFS({rng("Statut")},"❄ Gelée")',
        "consigne_prochaine": f'=IF({n_eff}+1>{max_ech},"",INDEX({rng("Consigne (à faire)")},{n_eff}+1)&"")',
        "action": (f'=IF({PC["id"]}="","",IF({SC["a_facturer"]}>0,"⚠ Facturer la révision ("&{SC["a_facturer"]}&" en attente)",'
                   f'IF(AND({SC["prochaine"]}<>"—",{SC["prochaine"]}-TODAY()<=60),'
                   f'IF({SC["indice_publie"]}="Oui","Révision dans "&INT({SC["prochaine"]}-TODAY())&" j – indice publié","Révision dans "&INT({SC["prochaine"]}-TODAY())&" j – indice non publié"),"RAS")))'),
    }
    for cle, libelle, ligne in SITUATION:
        ws.merge_cells(start_row=ligne, start_column=7, end_row=ligne, end_column=10)
        lab = ws.cell(row=ligne, column=7, value=libelle)
        lab.font, lab.fill = FONT_GRAS, FILL_SECTION
        c = ws.cell(row=ligne, column=11, value=formules[cle])
        c.border = BORDURE
        c.alignment = Alignment(horizontal="left" if cle in ("action", "consigne_prochaine", "trim_attendu", "indice_publie") else "right")
    ws[SC["loyer_actuel"]].number_format = FMT_EUR
    ws[SC["loyer_mensuel"]].number_format = FMT_EUR
    ws[SC["prochaine"]].number_format = FMT_DATE
    ws.conditional_formatting.add(SC["action"], FormulaRule(formula=[f'LEFT({SC["action"]},1)="⚠"'], font=Font(color="C00000", bold=True)))
    ws.conditional_formatting.add(PC["controles"], FormulaRule(formula=[f'AND({PC["controles"]}<>"",{PC["controles"]}<>"OK")'], font=Font(color="C00000", bold=True)))

    # tableau des révisions
    ws.cell(row=LIGNE_TABLE - 1, column=COL_TABLE, value="Révisions").font = FONT_GRAS
    _entetes(ws, COLS_REV, ligne=LIGNE_TABLE, col0=COL_TABLE, figer=False)
    ws.column_dimensions["K"].width = 14
    for k in range(1, max_ech + 1):
        _ligne_revision(ws, k)
    col_statut, col_dec, col_app = R["Statut"], R["Décision"], R["Appliqué ? (Oui/Non)"]
    dv = DataValidation(type="list", formula1='"Oui,Non"', allow_blank=True)
    dv.add(f"{col_app}{d0}:{col_app}{d1}")
    ws.add_data_validation(dv)
    dv_dec = DataValidation(type="list", formula1='"' + ",".join(DECISIONS) + '"', allow_blank=True, promptTitle="Décision du bailleur",
                            prompt="Vide ou Appliquer = révision appliquée. Geler = loyer inchangé ; « rattrapage possible » repart de l'indice de la dernière révision appliquée.")
    dv_dec.showInputMessage = True
    dv_dec.add(f"{col_dec}{d0}:{col_dec}{d1}")
    ws.add_data_validation(dv_dec)
    plage = f"{col_statut}{d0}:{col_statut}{d1}"
    ws.conditional_formatting.add(plage, FormulaRule(formula=[f'LEFT({col_statut}{d0},1)="⚠"'], font=Font(color="C00000", bold=True), fill=PatternFill("solid", fgColor="FFC7CE")))
    ws.conditional_formatting.add(plage, FormulaRule(formula=[f'AND(LEFT({col_statut}{d0},1)="✔",{col_app}{d0}<>"Oui")'], font=Font(color="9C5700", bold=True), fill=PatternFill("solid", fgColor="FFEB9C")))
    ws.conditional_formatting.add(plage, FormulaRule(formula=[f'AND(LEFT({col_statut}{d0},1)="✔",{col_app}{d0}="Oui")'], font=Font(color="006100"), fill=PatternFill("solid", fgColor="C6EFCE")))
    ws.conditional_formatting.add(plage, FormulaRule(formula=[f'LEFT({col_statut}{d0},1)="❄"'], font=Font(color="1F4E79", bold=True), fill=PatternFill("solid", fgColor="DDEBF7")))
    ws.conditional_formatting.add(f"{R['N°']}{d0}:{R['Commentaire']}{d1}", FormulaRule(formula=[f'${R["Actif"]}{d0}=0'], font=Font(color="BFBFBF")))
    for nom in ("Effectif", "Actif"):
        ws.column_dimensions[R[nom]].hidden = True
    ws.freeze_panes = f"A{LIGNE_TABLE + 1}"
    ws.sheet_view.zoomScale = 90


def _ligne_revision(ws: Worksheet, k: int) -> None:
    i = ligne_revision(k)
    c = lambda nom: f"{R[nom]}{i}"
    prec = lambda nom: f"{R[nom]}{i - 1}"
    off = f'{c("Actif")}=0'
    p, base_t, methode, loyer0, plafond = PC["periodicite_revision_ans"], PC["trimestre_base"], PC["methode"], PC["loyer_initial_annuel_ht"], PC["plafond_annuel_pct"]
    gel = f'LEFT({c("Décision")},5)="Geler"'
    perf = PC["periodicite_facturation"]
    ech_par_an = f'IF({perf}="Mensuelle",12,IF({perf}="Trimestrielle",4,IF({perf}="Semestrielle",2,1)))'

    ws[c("N°")] = k
    ws[c("Date de révision")] = f'=IF(OR({PC["id"]}="",{PC["date_effet"]}="",{p}=""),"",EDATE({PC["date_effet"]},12*{p}*{c("N°")}))'
    ws[c("Actif")] = f'=IF({c("Date de révision")}="",0,IF(AND(ISNUMBER({PC["date_fin"]}),{c("Date de révision")}>{PC["date_fin"]}),0,1))'
    ws[c("Trim. référence")] = f'=IF({off},"",IFERROR(TEXT(VALUE(LEFT({base_t},4))+{c("N°")}*{p},"0")&RIGHT({base_t},3),""))'
    ws[c("Valeur indice réf.")] = f'=IF({off},"",{_lookup_indice(PC["indice"], c("Trim. référence"))})'
    if k == 1:
        ws[c("Trim. précédent")] = f'=IF({off},"",{base_t})'
    else:
        ws[c("Trim. précédent")] = (f'=IF({off},"",IF({methode}="Base fixe",{base_t},'
                                    f'IF({prec("Décision")}="Geler – rattrapage possible",{prec("Trim. précédent")},{prec("Trim. référence")})))')
    ws[c("Valeur indice préc.")] = f'=IF({off},"",{_lookup_indice(PC["indice"], c("Trim. précédent"))})'
    ws[c("Coefficient")] = f'=IF(OR({c("Valeur indice réf.")}="",{c("Valeur indice préc.")}=""),"",{c("Valeur indice réf.")}/{c("Valeur indice préc.")})'
    if k == 1:
        ws[c("Base de calcul (annuel HT)")] = f'=IF({off},"",{loyer0})'
        ws[c("Loyer précédent (annuel HT)")] = f'=IF({off},"",{loyer0})'
    else:
        ws[c("Base de calcul (annuel HT)")] = f'=IF({off},"",IF({methode}="Base fixe",{loyer0},{prec("Loyer retenu (annuel HT)")}))'
        ws[c("Loyer précédent (annuel HT)")] = f'=IF({off},"",{prec("Loyer retenu (annuel HT)")})'
    ws[c("Loyer révisé brut")] = f'=IF(OR({c("Coefficient")}="",{c("Base de calcul (annuel HT)")}=""),"",ROUND({c("Base de calcul (annuel HT)")}*{c("Coefficient")},2))'
    annees = f'IF({methode}="Base fixe",{c("N°")}*{p},VALUE(LEFT({c("Trim. référence")},4))-VALUE(LEFT({c("Trim. précédent")},4)))'
    ws[c("Loyer retenu (annuel HT)")] = (f'=IF({off},"",IF({gel},{c("Loyer précédent (annuel HT)")},IF({c("Loyer révisé brut")}="","",'
                                         f'IF({plafond}="",{c("Loyer révisé brut")},MIN({c("Loyer révisé brut")},ROUND({c("Base de calcul (annuel HT)")}*(1+{plafond}/100)^{annees},2))))))')
    ws[c("Variation")] = f'=IF(OR({c("Loyer retenu (annuel HT)")}="",{c("Loyer précédent (annuel HT)")}=""),"",{c("Loyer retenu (annuel HT)")}/{c("Loyer précédent (annuel HT)")}-1)'
    ws[c("Loyer mensuel HT")] = f'=IF({c("Loyer retenu (annuel HT)")}="","",{c("Loyer retenu (annuel HT)")}/12)'
    ws[c("Loyer / échéance HT")] = f'=IF({c("Loyer retenu (annuel HT)")}="","",{c("Loyer retenu (annuel HT)")}/{ech_par_an})'
    ws[c("Charges / échéance HT")] = f'=IF({off},"",IF({PC["charges_annuelles_ht"]}="",0,{PC["charges_annuelles_ht"]})/{ech_par_an})'
    ws[c("TVA / échéance")] = f'=IF({c("Loyer / échéance HT")}="","",({c("Loyer / échéance HT")}+{c("Charges / échéance HT")})*{PC["tva_pct"]}/100)'
    ws[c("Total TTC / échéance")] = f'=IF({c("Loyer / échéance HT")}="","",{c("Loyer / échéance HT")}+{c("Charges / échéance HT")}+{c("TVA / échéance")})'
    ws[c("Statut")] = (f'=IF({off},"",IF({gel},"❄ Gelée",IF({c("Valeur indice réf.")}="",IF({c("Date de révision")}<=TODAY(),"⚠ Indice attendu","À venir"),'
                       f'IF({c("Date de révision")}<=TODAY(),"✔ Calculable","Indice connu – à venir"))))')
    ws[c("Effectif")] = f'=IF(AND({c("Actif")}=1,{c("Date de révision")}<=TODAY(),{c("Loyer retenu (annuel HT)")}<>""),1,0)'

    for nom in ("Date de révision", "Date d'application", "Courrier envoyé le"):
        ws[c(nom)].number_format = FMT_DATE
    for nom in ("Valeur indice réf.", "Valeur indice préc."):
        ws[c(nom)].number_format = FMT_INDICE
    ws[c("Coefficient")].number_format = "0.000000"
    ws[c("Variation")].number_format = FMT_PCT
    for nom in ("Base de calcul (annuel HT)", "Loyer révisé brut", "Loyer retenu (annuel HT)", "Loyer précédent (annuel HT)",
                "Loyer mensuel HT", "Loyer / échéance HT", "Charges / échéance HT", "TVA / échéance", "Total TTC / échéance"):
        ws[c(nom)].number_format = FMT_EUR
    for _, nom in Saisies.CHAMPS:
        ws[c(nom)].fill = FILL_SAISIE
    ws[c("Consigne (à faire)")].alignment = Alignment(wrap_text=True, vertical="top")
    for nom, _ in COLS_REV:
        ws[c(nom)].border = BORDURE


def ecrire_parametres(ws: Worksheet, b: Bail) -> None:
    valeurs = {
        "id": b.id, "local": b.local, "locataire": b.locataire, "adresse_locataire": b.adresse_locataire or None, "type_bail": b.type_bail,
        "date_effet": b.date_effet, "duree_ans": b.duree_ans, "loyer_initial_annuel_ht": b.loyer_initial_annuel_ht,
        "charges_annuelles_ht": b.charges_annuelles_ht, "tva_pct": b.tva_pct, "periodicite_facturation": b.periodicite_facturation,
        "indice": b.indice, "trimestre_base": b.trimestre_base, "periodicite_revision_ans": b.periodicite_revision_ans,
        "methode": b.methode, "plafond_annuel_pct": b.plafond_annuel_pct, "pennylane_customer_id": b.pennylane_customer_id or None,
        "pennylane_product_id": b.pennylane_product_id or None, "pennylane_subscription_id": b.pennylane_subscription_id or None,
        "notes": b.notes or None,
    }
    for cle, v in valeurs.items():
        ws[PC[cle]] = v


def ecrire_saisies(ws: Worksheet, id_bail: str, saisies: Saisies, max_ech: int) -> None:
    for k in range(1, max_ech + 1):
        for attr, nom in Saisies.CHAMPS:
            v = getattr(saisies, attr).get((id_bail, k))
            if v is not None:
                ws[f"{R[nom]}{ligne_revision(k)}"] = v


# --- Pennylane ---------------------------------------------------------------
def _feuille_pennylane(wb: Workbook) -> None:
    ws = wb.create_sheet("Pennylane")
    _entetes(ws, COLS_PL)
    soc = lambda cle: f"Société!$B${SOC_ROW[cle]}"
    q = '""'
    for i in range(2, MAX_RECAP + 2):
        c = lambda nom: f"{P[nom]}{i}"
        ong = c("Onglet")
        fx = lambda cle: f'IFERROR(INDIRECT("\'"&{ong}&"\'!{PC[cle]}"),"")'
        vide = f'{ong}=""'
        per = c("Périodicité")
        ech_par_an = f'IF({per}="Mensuelle",12,IF({per}="Trimestrielle",4,IF({per}="Semestrielle",2,1)))'
        ws[ong] = f'=IF(Récapitulatif!$A{i}="","",Récapitulatif!$A{i})'
        ws[c("Locataire")] = f'=IF({vide},"",{fx("locataire")})'
        ws[c("customer_id")] = f'=IF({vide},"",IF({fx("pennylane_customer_id")}="","À RENSEIGNER",{fx("pennylane_customer_id")}))'
        ws[c("product_id")] = f'=IF({vide},"",{fx("pennylane_product_id")}&"")'
        ws[c("label (abonnement)")] = f'=IF({vide},"","Loyer "&{fx("local")})'
        ws[per] = f'=IF({vide},"",{fx("periodicite_facturation")})'
        ws[c("recurring_rule.type")] = f'=IF({vide},"",IF({per}="Annuelle","yearly","monthly"))'
        ws[c("interval")] = f'=IF({vide},"",IF({per}="Mensuelle",1,IF({per}="Trimestrielle",3,IF({per}="Semestrielle",6,1))))'
        ws[c("unit")] = f'=IF({vide},"",IF({per}="Mensuelle","mois",IF({per}="Trimestrielle","trimestre",IF({per}="Semestrielle","semestre","an"))))'
        ws[c("Prix unitaire HT / échéance")] = f'=IF({vide},"",IFERROR(ROUND(Récapitulatif!${A["Loyer actuel (annuel HT)"]}{i}/{ech_par_an},2),""))'
        ws[c("Charges HT / échéance")] = f'=IF({vide},"",IFERROR(ROUND(IF({fx("charges_annuelles_ht")}="",0,{fx("charges_annuelles_ht")})/{ech_par_an},2),""))'
        ws[c("TVA (%)")] = f'=IF({vide},"",{fx("tva_pct")})'
        ws[c("vat_rate")] = (f'=IF({vide},"",IF({c("TVA (%)")}=20,"FR_200",IF({c("TVA (%)")}=10,"FR_100",'
                             f'IF({c("TVA (%)")}=5.5,"FR_55",IF({c("TVA (%)")}=2.1,"FR_21","exempt")))))')
        ws[c("mode")] = f'=IF({vide},"",{soc("pl_mode")})'
        ws[c("payment_conditions")] = f'=IF({vide},"",{soc("pl_payment_conditions")})'
        ws[c("payment_method")] = f'=IF({vide},"",{soc("pl_payment_method")})'
        d = c("start (1er du mois suivant)")
        ws[d] = f'=IF({vide},"",DATE(YEAR(TODAY()),MONTH(TODAY())+1,1))'
        ws[c("subscription_id existant")] = f'=IF({vide},"",{fx("pennylane_subscription_id")}&"")'
        iso = f'YEAR({d})&"-"&TEXT(MONTH({d}),"00")&"-"&TEXT(DAY({d}),"00")'
        num = lambda ref: f'SUBSTITUTE(TEXT({ref},"0.00"),",",".")'
        ligne_loyer = (f'"{{{q}label{q}: {q}Loyer "&LOWER({per})&" – "&{fx("local")}&"{q}, {q}quantity{q}: 1, {q}unit{q}: {q}"&{c("unit")}&"{q}, '
                       f'{q}raw_currency_unit_price{q}: {q}"&{num(c("Prix unitaire HT / échéance"))}&"{q}, {q}vat_rate{q}: {q}"&{c("vat_rate")}&"{q}"'
                       f'&IF({c("product_id")}<>"",", {q}product_id{q}: "&{c("product_id")},"")&"}}"')
        ligne_charges = (f'IF({c("Charges HT / échéance")}>0,", {{{q}label{q}: {q}Provision sur charges{q}, {q}quantity{q}: 1, {q}unit{q}: {q}"&{c("unit")}&"{q}, '
                         f'{q}raw_currency_unit_price{q}: {q}"&{num(c("Charges HT / échéance"))}&"{q}, {q}vat_rate{q}: {q}"&{c("vat_rate")}&"{q}}}","")')
        rule = (f'"{{{q}type{q}: {q}"&{c("recurring_rule.type")}&"{q}, {q}interval{q}: "&{c("interval")}'
                f'&IF({c("recurring_rule.type")}="monthly",", {q}day_of_month{q}: 1","")&"}}"')
        ws[c("Corps JSON – POST /api/external/v2/billing_subscriptions")] = (
            f'=IF(OR({vide},{c("Prix unitaire HT / échéance")}=""),"","{{{q}customer_id{q}: "&{c("customer_id")}&", {q}label{q}: {q}"&{c("label (abonnement)")}&"{q}, {q}start{q}: {q}"&{iso}&"{q}, '
            f'{q}mode{q}: {{{q}type{q}: {q}"&{c("mode")}&"{q}}}, {q}payment_conditions{q}: {q}"&{c("payment_conditions")}&"{q}, '
            f'{q}payment_method{q}: {q}"&{c("payment_method")}&"{q}, {q}recurring_rule{q}: "&{rule}&", '
            f'{q}customer_invoice_data{q}: {{{q}currency{q}: {q}EUR{q}, {q}language{q}: {q}fr_FR{q}, {q}pdf_invoice_subject{q}: {q}"&{c("label (abonnement)")}&"{q}, '
            f'{q}invoice_lines{q}: ["&{ligne_loyer}&{ligne_charges}&"]}}}}")'
        )
        for nom in ("Prix unitaire HT / échéance", "Charges HT / échéance"):
            ws[c(nom)].number_format = FMT_EUR
        ws[d].number_format = FMT_DATE
        for nom, _ in COLS_PL:
            ws[c(nom)].border = BORDURE
    ws.cell(row=MAX_RECAP + 3, column=1, value="Schéma : OpenAPI Pennylane Company V2 (POST /billing_subscriptions, 2026-05-27). Réglages dans la feuille Société. "
                                              "Automatisation de l'envoi : étape ultérieure.").font = FONT_GRIS


# =============================================================================
# Lecture / mise à jour d'un classeur existant
# =============================================================================
def feuilles_baux(wb: Workbook) -> list[Worksheet]:
    return [ws for ws in wb.worksheets if ws.title not in FEUILLES_FIXES and ws.title != "Grille_rang"
            and ws["B3"].value == PARAMS[0][1]]


def lire_classeur(chemin: Path) -> tuple[Societe, list[Bail], Saisies]:
    """Société, baux (un par onglet de fiche, avec `onglet`) et saisies des tableaux de révisions."""
    wb = load_workbook(chemin, data_only=False)
    ws_s = wb["Société"]
    params = {ws_s.cell(row=r, column=1).value: ws_s.cell(row=r, column=2).value for r in range(1, len(LIB_SOC) + 2)}
    g = lambda cle, defaut="": params.get(LIB_SOC[cle]) if params.get(LIB_SOC[cle]) not in (None, "") else defaut
    societe = Societe(
        nom=str(g("nom", chemin.stem)), siren=str(g("siren")), forme=str(g("forme", "SCI")), regime_tva=str(g("regime_tva")),
        pennylane_company=str(g("pennylane_company")), contact=str(g("contact")), adresse=str(g("adresse")),
        signataire=str(g("signataire")), qualite_signataire=str(g("qualite_signataire", "Gérant")), ville_signature=str(g("ville_signature")),
        pl_mode=str(g("pl_mode", "awaiting_validation")), pl_payment_conditions=str(g("pl_payment_conditions", "upon_receipt")),
        pl_payment_method=str(g("pl_payment_method", "offline")), demo=str(g("demo", "non")).upper() == "OUI",
        max_echeances=int(g("max_echeances", MAX_ECHEANCES)),
    )
    baux: list[Bail] = []
    saisies = Saisies()
    for ws in feuilles_baux(wb):
        v = lambda cle: ws[PC[cle]].value
        if v("id") in (None, ""):
            log.warning("Onglet %s : ID bail vide, ignoré", ws.title)
            continue
        b = Bail(
            id=str(v("id")).strip(), local=str(v("local") or ""), locataire=str(v("locataire") or ""),
            adresse_locataire=str(v("adresse_locataire") or ""), type_bail=str(v("type_bail") or "Commercial"), date_effet=_en_date(v("date_effet")),
            duree_ans=int(v("duree_ans")) if v("duree_ans") not in (None, "") else None,
            loyer_initial_annuel_ht=float(v("loyer_initial_annuel_ht") or 0), charges_annuelles_ht=float(v("charges_annuelles_ht") or 0),
            tva_pct=float(v("tva_pct") if v("tva_pct") not in (None, "") else 20),
            periodicite_facturation=str(v("periodicite_facturation") or "Mensuelle"), indice=str(v("indice") or "ILC"),
            trimestre_base=str(v("trimestre_base") or ""), periodicite_revision_ans=int(v("periodicite_revision_ans") or 1),
            methode=str(v("methode") or "Chaînée"),
            plafond_annuel_pct=float(v("plafond_annuel_pct")) if v("plafond_annuel_pct") not in (None, "") else None,
            pennylane_customer_id=_texte_id(v("pennylane_customer_id")), pennylane_product_id=_texte_id(v("pennylane_product_id")),
            pennylane_subscription_id=_texte_id(v("pennylane_subscription_id")), notes=str(v("notes") or ""),
        )
        b.onglet = ws.title
        baux.append(b)
        for k in range(1, societe.max_echeances + 1):
            for attr, nom in Saisies.CHAMPS:
                val = ws[f"{R[nom]}{ligne_revision(k)}"].value
                if val not in (None, ""):
                    getattr(saisies, attr)[(b.id, k)] = val
    return societe, baux, saisies


def lire_indices_classeur(chemin: Path) -> list[Observation]:
    """Observations présentes dans les zones de collage de la feuille Indices."""
    wb = load_workbook(chemin, data_only=False)
    ws = wb["Indices"]
    obs: list[Observation] = []
    for code in SERIES:
        c0 = colonne_zone(code)
        for r in range(4, 4 + LIGNES_ZONE):
            per, val = ws.cell(row=r, column=c0).value, ws.cell(row=r, column=c0 + 1).value
            if isinstance(per, str) and re.match(r"^\d{4}-[TQ][1-4]$", per.strip()) and isinstance(val, (int, float)):
                obs.append(Observation(code, "", per.strip().replace("Q", "T"), float(val), "", "", "CLASSEUR", ""))
    return obs


def sauvegarder(chemin: Path) -> Path:
    s = chemin.with_suffix(f".{datetime.now():%Y%m%d-%H%M%S}.bak.xlsx")
    shutil.copy2(chemin, s)
    return s


def mettre_a_jour_indices(chemin: Path, observations: list[Observation]) -> int:
    """Réécrit les zones de collage des séries présentes dans `observations`, sans toucher au reste."""
    sauvegarder(chemin)
    wb = load_workbook(chemin)
    n = ecrire_indices(wb, observations)
    wb.calculation.fullCalcOnLoad = True
    wb.save(chemin)
    return n


def ecrire_cellules_revisions(chemin: Path, valeurs: dict[tuple[str, int], object], colonne: str) -> int:
    """Écrit `valeurs[(ID bail, n°)]` dans la colonne `colonne` du tableau de la fiche concernée."""
    societe, baux, _ = lire_classeur(chemin)
    sauvegarder(chemin)
    wb = load_workbook(chemin)
    n = 0
    for b in baux:
        ws = wb[b.onglet]
        for k in range(1, societe.max_echeances + 1):
            if (b.id, k) in valeurs:
                c = ws[f"{R[colonne]}{ligne_revision(k)}"]
                c.value = valeurs[(b.id, k)]
                if isinstance(c.value, date):
                    c.number_format = FMT_DATE
                n += 1
    wb.calculation.fullCalcOnLoad = True
    wb.save(chemin)
    return n


def ecrire_parametre_fiches(chemin: Path, valeurs: dict[str, object], cle: str) -> int:
    """Écrit `valeurs[ID bail]` dans le paramètre `cle` de chaque fiche concernée."""
    _, baux, _ = lire_classeur(chemin)
    sauvegarder(chemin)
    wb = load_workbook(chemin)
    n = 0
    for b in baux:
        if b.id in valeurs:
            wb[b.onglet][PC[cle]] = valeurs[b.id]
            n += 1
    wb.calculation.fullCalcOnLoad = True
    wb.save(chemin)
    return n


def _texte_id(v) -> str:
    if v in (None, ""):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _en_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(v.strip(), fmt).date()
            except ValueError:
                pass
    raise ValueError(f"Date illisible : {v!r}")

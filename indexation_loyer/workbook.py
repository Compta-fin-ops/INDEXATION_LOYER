"""Classeur Excel autonome de suivi des réindexations d'une société.

Le classeur fonctionne sans le script : tout est formules.
  Lisez-moi     mode d'emploi, derniers indices saisis, points de vigilance
  Société       paramètres (raison sociale, signataire, réglages Pennylane)
  Baux          registre saisi par le cabinet, N lignes prêtes (cellules jaunes)
  Indices       grille de saisie : une ligne par année, 4 colonnes (T1..T4) par série
  Révisions     N baux × K échéances pré-câblés ; une ligne s'active dès qu'un bail est saisi
  Alertes       une ligne par bail : loyer actuel, prochaine révision, action
  Pennylane     préparation des abonnements (aperçu JSON), une ligne par bail
  Indices_long  (masquée) la grille Indices en format long pour les SUMIFS

Le script sert ensuite à : créer le classeur (init), écrire les indices INSEE dans
la grille (refresh), générer les courriers, préparer / envoyer les abonnements.
Il ne régénère jamais les feuilles : les saisies du cabinet ne sont pas touchées.

Toutes les formules sont en syntaxe « fichier » (anglais, séparateur virgule) ;
Excel et LibreOffice les affichent localisées.
"""
from __future__ import annotations

import logging
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

# --- capacité par défaut -------------------------------------------------------
MAX_BAUX = 40            # lignes prêtes dans Baux
MAX_ECHEANCES = 12       # révisions pré-câblées par bail
ANNEE_MIN, ANNEE_MAX = 2000, 2035
SERIES = ("ILC", "ILAT", "ICC", "IRL")

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
    max_baux: int = MAX_BAUX
    max_echeances: int = MAX_ECHEANCES


@dataclass
class Saisies:
    """Colonnes saisies dans « Révisions », clé (ID bail, n° révision)."""
    decision: dict[tuple[str, int], str] = field(default_factory=dict)
    consigne: dict[tuple[str, int], str] = field(default_factory=dict)
    applique: dict[tuple[str, int], str] = field(default_factory=dict)
    date_application: dict[tuple[str, int], object] = field(default_factory=dict)
    courrier: dict[tuple[str, int], object] = field(default_factory=dict)
    commentaire: dict[tuple[str, int], str] = field(default_factory=dict)

    CHAMPS = (("decision", "Décision"), ("consigne", "Consigne (à faire)"), ("applique", "Appliqué ? (Oui/Non)"),
              ("date_application", "Date d'application"), ("courrier", "Courrier envoyé le"), ("commentaire", "Commentaire"))


# --- colonnes ------------------------------------------------------------------
COLS_BAUX = [
    ("ID bail", 10), ("Local (désignation / adresse)", 34), ("Locataire", 26), ("Adresse du locataire (courrier)", 30),
    ("Type de bail", 13), ("Date de prise d'effet", 13), ("Durée (ans)", 8), ("Date de fin", 13),
    ("Loyer initial annuel HT", 15), ("Charges annuelles HT", 13), ("TVA (%)", 7),
    ("Périodicité de facturation", 15), ("Indice", 7), ("Trimestre indice de base", 12),
    ("Périodicité de révision (ans)", 11), ("Méthode", 11), ("Plafond annuel (%) – optionnel", 12),
    ("Pennylane customer_id", 13), ("Pennylane product_id", 13), ("Pennylane subscription_id", 13), ("Notes", 30), ("Contrôles", 44),
]
B = {nom: get_column_letter(i + 1) for i, (nom, _) in enumerate(COLS_BAUX)}
B_ID, B_LOCAL, B_LOCATAIRE, B_ADRESSE, B_TYPE = B["ID bail"], B["Local (désignation / adresse)"], B["Locataire"], B["Adresse du locataire (courrier)"], B["Type de bail"]
B_EFFET, B_DUREE, B_FIN = B["Date de prise d'effet"], B["Durée (ans)"], B["Date de fin"]
B_LOYER, B_CHARGES, B_TVA = B["Loyer initial annuel HT"], B["Charges annuelles HT"], B["TVA (%)"]
B_PERFACT, B_INDICE, B_TBASE = B["Périodicité de facturation"], B["Indice"], B["Trimestre indice de base"]
B_PERREV, B_METHODE, B_PLAFOND = B["Périodicité de révision (ans)"], B["Méthode"], B["Plafond annuel (%) – optionnel"]
B_CUST, B_PROD, B_SUBSCR, B_NOTES, B_CTRL = B["Pennylane customer_id"], B["Pennylane product_id"], B["Pennylane subscription_id"], B["Notes"], B["Contrôles"]

COLS_REV = [
    ("ID bail", 9), ("Local", 26), ("N°", 4), ("Date de révision", 12), ("Indice", 6),
    ("Trim. référence", 10), ("Valeur indice réf.", 10), ("Trim. précédent", 10), ("Valeur indice préc.", 10),
    ("Coefficient", 10), ("Décision", 24), ("Base de calcul (annuel HT)", 14), ("Loyer révisé brut", 13),
    ("Loyer retenu (annuel HT)", 14), ("Loyer précédent (annuel HT)", 14), ("Variation", 8),
    ("Loyer mensuel HT", 12), ("Loyer / échéance HT", 12), ("Charges / échéance HT", 12),
    ("TVA / échéance", 11), ("Total TTC / échéance", 12), ("Statut", 20),
    ("Consigne (à faire)", 40), ("Appliqué ? (Oui/Non)", 10), ("Date d'application", 12), ("Courrier envoyé le", 12),
    ("Commentaire", 28), ("Clé", 10), ("Effectif", 6), ("Actif", 6),
]
R = {nom: get_column_letter(i + 1) for i, (nom, _) in enumerate(COLS_REV)}

COLS_ALERTES = [
    ("ID bail", 9), ("Local", 26), ("Locataire", 22), ("Indice", 6), ("Loyer initial annuel HT", 13),
    ("N° dernière révision effective", 11), ("Loyer actuel (annuel HT)", 14), ("Loyer actuel mensuel HT", 12),
    ("Prochaine révision", 12), ("Trimestre attendu", 10), ("Indice publié ?", 9),
    ("Révisions calculables non appliquées", 13), ("Révisions gelées", 8), ("Action", 34), ("Consigne prochaine révision", 40),
]
A = {nom: get_column_letter(i + 1) for i, (nom, _) in enumerate(COLS_ALERTES)}

COLS_PL = [
    ("ID bail", 9), ("Locataire", 22), ("customer_id", 12), ("product_id", 10), ("label (abonnement)", 30),
    ("Périodicité", 12), ("recurring_rule.type", 10), ("interval", 7), ("unit", 9),
    ("Prix unitaire HT / échéance", 13), ("Charges HT / échéance", 12), ("TVA (%)", 7), ("vat_rate", 9),
    ("mode", 16), ("payment_conditions", 15), ("payment_method", 13), ("start (1er du mois suivant)", 12),
    ("subscription_id existant", 12), ("Corps JSON – POST /api/external/v2/billing_subscriptions", 100),
]
P = {nom: get_column_letter(i + 1) for i, (nom, _) in enumerate(COLS_PL)}

LIB_SOC = {  # libellés de la feuille Société (clé -> libellé)
    "nom": "Raison sociale", "siren": "SIREN", "forme": "Forme", "regime_tva": "Régime TVA des loyers",
    "pennylane_company": "Identifiant société Pennylane", "contact": "Contact cabinet",
    "adresse": "Adresse du bailleur (courriers)", "signataire": "Signataire des courriers",
    "qualite_signataire": "Qualité du signataire", "ville_signature": "Ville de signature",
    "pl_mode": "Pennylane – mode des factures", "pl_payment_conditions": "Pennylane – conditions de paiement",
    "pl_payment_method": "Pennylane – moyen de paiement",
    "date": "Date de génération", "demo": "Mode démonstration", "max_baux": "Capacité – baux", "max_echeances": "Capacité – révisions par bail",
    "version": "Version du modèle",
}
LISTES_SOC = {
    "pl_mode": ("awaiting_validation", "finalized"),
    "pl_payment_conditions": ("upon_receipt", "7_days", "15_days", "30_days", "30_days_end_of_month", "45_days", "45_days_end_of_month", "60_days"),
    "pl_payment_method": ("offline", "gocardless_direct_debit", "pro_account_sepa_core"),
}
SOC_ROW = {cle: i for i, cle in enumerate(LIB_SOC, start=1)}


# --- géométrie -------------------------------------------------------------------
def ligne_baux(slot: int) -> int:
    """Ligne de la feuille Baux du bail n° `slot` (1..N)."""
    return slot + 1


def ligne_revision(slot: int, k: int, max_echeances: int = MAX_ECHEANCES) -> int:
    return 1 + (slot - 1) * max_echeances + k


def cellule_indice(serie: str, annee: int, trimestre: int) -> str:
    """Cellule de la grille Indices pour (série, année, trimestre)."""
    j = SERIES.index(serie)
    return f"{get_column_letter(2 + 4 * j + trimestre - 1)}{3 + annee - ANNEE_MIN}"


def ligne_indices_long(serie: str, annee: int, trimestre: int) -> int:
    j = SERIES.index(serie)
    return 2 + j * (ANNEE_MAX - ANNEE_MIN + 1) * 4 + (annee - ANNEE_MIN) * 4 + (trimestre - 1)


# =============================================================================
# Construction
# =============================================================================
def construire(societe: Societe, baux: list[Bail], observations: list[Observation], chemin: Path,
               saisies: Saisies | None = None, aujourdhui: date | None = None) -> Path:
    aujourdhui = aujourdhui or date.today()
    saisies = saisies or Saisies()
    if len(baux) > societe.max_baux:
        raise ValueError(f"{len(baux)} baux pour une capacité de {societe.max_baux} (init --max-baux)")
    wb = Workbook()
    wb.remove(wb.active)
    _feuille_lisezmoi(wb, societe, aujourdhui)
    _feuille_societe(wb, societe, aujourdhui)
    _feuille_baux(wb, baux, societe.max_baux)
    _feuille_indices(wb)
    _feuille_revisions(wb, societe.max_baux, societe.max_echeances)
    _feuille_alertes(wb, societe.max_baux)
    _feuille_pennylane(wb, societe.max_baux)
    _feuille_indices_long(wb)
    ecrire_indices(wb, observations)
    ecrire_saisies(wb, baux, saisies, societe.max_echeances)
    wb.calculation.fullCalcOnLoad = True
    chemin.parent.mkdir(parents=True, exist_ok=True)
    wb.save(chemin)
    log.info("Classeur écrit : %s", chemin)
    return chemin


def _entetes(ws: Worksheet, colonnes: list[tuple[str, int]], ligne: int = 1) -> None:
    for i, (nom, largeur) in enumerate(colonnes, start=1):
        c = ws.cell(row=ligne, column=i, value=nom)
        c.fill, c.font, c.border = FILL_ENTETE, FONT_ENTETE, BORDURE
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        ws.column_dimensions[get_column_letter(i)].width = largeur
    ws.row_dimensions[ligne].height = 42
    ws.freeze_panes = ws.cell(row=ligne + 1, column=1)


def _lookup_indice(serie_ref: str, periode_ref: str) -> str:
    """Valeur d'indice ("" si absente) depuis Indices_long."""
    s = f"SUMIFS(Indices_long!$C:$C,Indices_long!$A:$A,{serie_ref},Indices_long!$B:$B,{periode_ref})"
    return f'IF({periode_ref}="","",IF({s}=0,"",{s}))'


# --- Lisez-moi ---------------------------------------------------------------
def _feuille_lisezmoi(wb: Workbook, societe: Societe, aujourdhui: date) -> None:
    ws = wb.create_sheet("Lisez-moi")
    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 44
    for col in "CDEF":
        ws.column_dimensions[col].width = 20
    ws["B2"] = f"Suivi des réindexations de loyers – {societe.nom}"
    ws["B2"].font = FONT_TITRE
    ws["B3"] = f"Modèle v{__version__} généré le {aujourdhui:%d/%m/%Y} – capacité {societe.max_baux} baux × {societe.max_echeances} révisions"
    ws["B3"].font = FONT_GRIS
    l = 5
    if societe.demo:
        ws.cell(row=l, column=2, value="⚠ CLASSEUR DE DÉMONSTRATION : indices et baux FICTIFS. Ne pas utiliser pour un calcul réel.").font = Font(bold=True, color="C00000")
        for c in range(2, 7):
            ws.cell(row=l, column=c).fill = FILL_DEMO
        l += 2

    ws.cell(row=l, column=2, value="Derniers indices saisis (feuille Indices)").font = FONT_GRAS
    l += 1
    for i, nom in enumerate(["Série", "Dernier trimestre", "Valeur", "Variation annuelle"], start=2):
        c = ws.cell(row=l, column=i, value=nom)
        c.fill, c.font = FILL_SECTION, FONT_GRAS
    config = charger_config()["series"]
    for code in SERIES:
        l += 1
        n = f'_xlfn.MAXIFS(Indices_long!$D:$D,Indices_long!$A:$A,"{code}")'
        ws.cell(row=l, column=2, value=f"{code} – {config[code]['libelle']}")
        ws.cell(row=l, column=3, value=f'=IF({n}=0,"aucune valeur",INT(({n}-1)/4)&"-T"&({n}-INT(({n}-1)/4)*4))')
        ws.cell(row=l, column=4, value=f'=IF({n}=0,"",SUMIFS(Indices_long!$C:$C,Indices_long!$A:$A,"{code}",Indices_long!$D:$D,{n}))').number_format = FMT_INDICE
        ws.cell(row=l, column=5, value=f'=IFERROR(D{l}/SUMIFS(Indices_long!$C:$C,Indices_long!$A:$A,"{code}",Indices_long!$D:$D,{n}-4)-1,"")').number_format = FMT_PCT
    l += 2
    ws.cell(row=l, column=2, value="Mode d'emploi").font = FONT_GRAS
    for texte in [
        "1. Feuille « Indices » : saisir les valeurs publiées par l'INSEE dans la grille (une ligne par année, une colonne par trimestre, un bloc par série). Rien d'autre à faire : tout le classeur se recalcule.",
        "2. Feuille « Baux » : un local / bail par ligne, dans l'ordre d'arrivée (ne pas trier, ne pas insérer de ligne : les décisions de la feuille Révisions sont attachées à la position). Cellules jaunes = saisie. La colonne Contrôles doit afficher OK.",
        "3. Feuille « Révisions » : les échéances du bail apparaissent dès la saisie. Statut ✔ Calculable = révision échue et indice connu : facturer, puis renseigner « Appliqué ? » et la date.",
        "   Colonne « Décision » : vide ou Appliquer = révision appliquée ; « Geler – sans rattrapage » = le client renonce à la variation de l'année ; « Geler – rattrapage possible » = loyer inchangé mais la révision suivante repart de l'indice de la dernière révision appliquée.",
        "   Colonne « Consigne (à faire) » : instruction libre du dossier (ex. « gel décidé le 12/03, courrier à envoyer, ne pas facturer »), reprise dans la feuille Alertes.",
        "4. Feuille « Alertes » : pilotage par bail (loyer actuel, prochaine révision, indice publié ou non, action).",
        "5. Feuille « Pennylane » : corps des abonnements de facturation, prêt pour l'automatisation ultérieure. Les réglages communs sont dans la feuille Société.",
        "Trimestres : notation AAAA-Tn (2024-T2 = 2e trimestre 2024). Convention par défaut du trimestre de base : dernier indice publié à la prise d'effet (T-2) ; à caler sur la clause du bail.",
        "Filtrer la feuille Révisions sur Actif = 1 pour masquer les échéances hors bail ; les lignes sans bail restent vides en bas de feuille.",
    ]:
        l += 1
        ws.cell(row=l, column=2, value=texte)
    l += 2
    ws.cell(row=l, column=2, value="Points de vigilance juridiques (à confirmer par le juriste du dossier)").font = FONT_GRAS
    for texte in [
        "• Depuis la loi Pinel (n° 2014-626 du 18 juin 2014), l'ILC et l'ILAT sont les indices de référence des baux commerciaux (art. L145-34 et L145-38 C. com.) ; l'ICC subsiste dans les clauses des baux antérieurs.",
        "• Plafonnement légal de la variation de l'ILC à 3,5 % pour les PME (loi n° 2022-1158 du 16 août 2022, art. 14, puis prolongation) : période et champ à vérifier bail par bail ; utiliser la colonne « Plafond annuel (%) ».",
        "• Une clause d'indexation ne jouant qu'à la hausse est réputée non écrite (art. L112-1 C. mon. fin. et jurisprudence Cass. 3e civ.) : le classeur applique la baisse si l'indice recule.",
        "• Révision légale triennale (L145-38), déplafonnement et lissage : hors périmètre ; le classeur traite la clause d'échelle mobile contractuelle.",
        "• Le renoncement du bailleur à une indexation (gel) est un acte de gestion à documenter (consigne, courrier) ; sa portée juridique n'est pas tranchée par l'outil.",
    ]:
        l += 1
        ws.cell(row=l, column=2, value=texte)
    l += 2
    ws.cell(row=l, column=2, value="Légende : fond jaune = saisie ; autres cellules = formules. Ne pas insérer ni supprimer de colonnes.").font = FONT_GRIS


# --- Société -----------------------------------------------------------------
def _feuille_societe(wb: Workbook, s: Societe, aujourdhui: date) -> None:
    ws = wb.create_sheet("Société")
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 50
    valeurs = {**{k: getattr(s, k) for k in LIB_SOC if hasattr(s, k)},
               "date": aujourdhui, "demo": "OUI" if s.demo else "non", "version": __version__}
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


# --- Baux --------------------------------------------------------------------
def _feuille_baux(wb: Workbook, baux: list[Bail], max_baux: int) -> None:
    ws = wb.create_sheet("Baux")
    _entetes(ws, COLS_BAUX)
    derniere = ligne_baux(max_baux)
    for i in range(2, derniere + 1):
        ws[f"{B_FIN}{i}"] = f'=IF(OR({B_EFFET}{i}="",{B_DUREE}{i}=""),"",EDATE({B_EFFET}{i},12*{B_DUREE}{i}))'
        ws[f"{B_CTRL}{i}"] = _formule_controles(i, derniere)
        for nom, _ in COLS_BAUX:
            col = B[nom]
            c = ws[f"{col}{i}"]
            c.border = BORDURE
            if col not in (B_FIN, B_CTRL):
                c.fill = FILL_SAISIE
        ws[f"{B_EFFET}{i}"].number_format = FMT_DATE
        ws[f"{B_FIN}{i}"].number_format = FMT_DATE
        for col in (B_LOYER, B_CHARGES):
            ws[f"{col}{i}"].number_format = FMT_EUR
    for slot, b in enumerate(baux, start=1):
        ecrire_bail(ws, slot, b)
    plage = lambda col: f"{col}2:{col}{derniere}"
    for col, liste in [(B_TYPE, TYPES_BAIL), (B_PERFACT, tuple(PERIODICITES_FACTURATION)),
                       (B_INDICE, INDICES), (B_METHODE, METHODES), (B_PERREV, ("1", "2", "3"))]:
        dv = DataValidation(type="list", formula1='"' + ",".join(liste) + '"', allow_blank=True)
        dv.add(plage(col))
        ws.add_data_validation(dv)
    dv_date = DataValidation(type="date", operator="greaterThan", formula1="DATE(1990,1,1)", allow_blank=True)
    dv_date.add(plage(B_EFFET))
    ws.add_data_validation(dv_date)
    ws.conditional_formatting.add(plage(B_CTRL), FormulaRule(formula=[f'AND({B_CTRL}2<>"",{B_CTRL}2<>"OK")'],
                                                             font=Font(color="C00000", bold=True)))
    ws.auto_filter.ref = f"A1:{B_CTRL}{derniere}"


def _formule_controles(i: int, derniere: int) -> str:
    base_ok = f"SUMIFS(Indices_long!$C:$C,Indices_long!$A:$A,{B_INDICE}{i},Indices_long!$B:$B,{B_TBASE}{i})>0"
    return (
        f'=IF({B_ID}{i}="","",IF(AND({B_EFFET}{i}<>"",ISNUMBER({B_LOYER}{i}),{B_INDICE}{i}<>"",LEN({B_TBASE}{i})=7,{base_ok},'
        f'COUNTIF($A$2:$A${derniere},{B_ID}{i})=1,OR({B_PLAFOND}{i}="",ISNUMBER({B_PLAFOND}{i}))),"OK",TRIM('
        f'IF({B_EFFET}{i}="","Date d\'effet manquante. ","")'
        f'&IF(NOT(ISNUMBER({B_LOYER}{i})),"Loyer initial manquant. ","")'
        f'&IF({B_INDICE}{i}="","Indice manquant. ","")'
        f'&IF(LEN({B_TBASE}{i})<>7,"Trimestre de base au format AAAA-Tn. ","")'
        f'&IF(AND(LEN({B_TBASE}{i})=7,{B_INDICE}{i}<>"",NOT({base_ok})),"Indice de base "&{B_TBASE}{i}&" non saisi dans Indices. ","")'
        f'&IF(COUNTIF($A$2:$A${derniere},{B_ID}{i})>1,"ID bail en doublon. ","")'
        f'&IF(AND({B_PLAFOND}{i}<>"",NOT(ISNUMBER({B_PLAFOND}{i}))),"Plafond non numérique. ",""))))'
    )


def ecrire_bail(ws: Worksheet, slot: int, b: Bail) -> None:
    i = ligne_baux(slot)
    valeurs = {
        B_ID: b.id, B_LOCAL: b.local, B_LOCATAIRE: b.locataire, B_ADRESSE: b.adresse_locataire or None, B_TYPE: b.type_bail,
        B_EFFET: b.date_effet, B_DUREE: b.duree_ans, B_LOYER: b.loyer_initial_annuel_ht,
        B_CHARGES: b.charges_annuelles_ht, B_TVA: b.tva_pct, B_PERFACT: b.periodicite_facturation,
        B_INDICE: b.indice, B_TBASE: b.trimestre_base, B_PERREV: b.periodicite_revision_ans,
        B_METHODE: b.methode, B_PLAFOND: b.plafond_annuel_pct, B_CUST: b.pennylane_customer_id or None,
        B_PROD: b.pennylane_product_id or None, B_SUBSCR: b.pennylane_subscription_id or None, B_NOTES: b.notes or None,
    }
    for col, v in valeurs.items():
        ws[f"{col}{i}"] = v


# --- Indices (grille de saisie) -------------------------------------------------
def _feuille_indices(wb: Workbook) -> None:
    ws = wb.create_sheet("Indices")
    config = charger_config()["series"]
    ws.column_dimensions["A"].width = 8
    ws["A1"] = "Année"
    ws["A2"] = ""
    for j, code in enumerate(SERIES):
        c0 = 2 + 4 * j
        ws.merge_cells(start_row=1, start_column=c0, end_row=1, end_column=c0 + 3)
        cell = ws.cell(row=1, column=c0, value=f"{code} – base {config[code]['base']}")
        cell.fill, cell.font, cell.alignment = FILL_ENTETE, FONT_ENTETE, Alignment(horizontal="center", wrap_text=True)
        for q in range(1, 5):
            c = ws.cell(row=2, column=c0 + q - 1, value=f"T{q}")
            c.fill, c.font, c.alignment = FILL_SECTION, FONT_GRAS, Alignment(horizontal="center")
            ws.column_dimensions[get_column_letter(c0 + q - 1)].width = 9
    for cell in ("A1", "A2"):
        ws[cell].fill, ws[cell].font = FILL_ENTETE, FONT_ENTETE
    ws.row_dimensions[1].height = 30
    for annee in range(ANNEE_MIN, ANNEE_MAX + 1):
        r = 3 + annee - ANNEE_MIN
        ws.cell(row=r, column=1, value=annee).font = FONT_GRAS
        for j, code in enumerate(SERIES):
            for q in range(1, 5):
                c = ws.cell(row=r, column=2 + 4 * j + q - 1)
                c.fill, c.border = FILL_SAISIE, BORDURE
                c.number_format = "0" if code == "ICC" else FMT_INDICE
    ws.freeze_panes = "B3"
    note = 3 + ANNEE_MAX - ANNEE_MIN + 2
    ws.cell(row=note, column=1, value="Saisir les valeurs telles que publiées par l'INSEE (insee.fr, séries 001532540 ILC · 001617112 ILAT · 000008630 ICC · 001515333 IRL). "
                                       "Colonne = trimestre auquel se rapporte l'indice. Une cellule vide = indice non publié : les révisions concernées restent « en attente ».").font = FONT_GRIS
    ws.cell(row=note + 1, column=1, value="La commande  refresh  du script remplit cette grille depuis l'INSEE sans toucher au reste du classeur (les cellules en orange clair signalent une valeur fictive de démonstration).").font = FONT_GRIS


def _feuille_indices_long(wb: Workbook) -> None:
    ws = wb.create_sheet("Indices_long")
    _entetes(ws, [("Série", 8), ("Période", 10), ("Valeur", 10), ("Rang", 8)])
    for code in SERIES:
        for annee in range(ANNEE_MIN, ANNEE_MAX + 1):
            for q in range(1, 5):
                r = ligne_indices_long(code, annee, q)
                cell = cellule_indice(code, annee, q)
                ws.cell(row=r, column=1, value=code)
                ws.cell(row=r, column=2, value=f"{annee}-T{q}")
                ws.cell(row=r, column=3, value=f'=IF(Indices!{cell}="","",Indices!{cell})')
                ws.cell(row=r, column=4, value=f'=IF(C{r}="",0,{annee * 4 + q})')
    ws.sheet_state = "hidden"


def ecrire_indices(wb: Workbook, observations: list[Observation]) -> int:
    """Écrit les observations dans la grille Indices (cellules dans la plage d'années uniquement)."""
    ws = wb["Indices"]
    n = 0
    for o in observations:
        if o.serie not in SERIES:
            continue
        try:
            annee, q = int(o.periode[:4]), int(o.periode[-1])
        except ValueError:
            continue
        if not ANNEE_MIN <= annee <= ANNEE_MAX:
            continue
        c = ws[cellule_indice(o.serie, annee, q)]
        c.value = o.valeur
        c.fill = FILL_DEMO if o.source == "FICTIF_DEMO" else FILL_SAISIE
        n += 1
    return n


# --- Révisions ---------------------------------------------------------------
def _feuille_revisions(wb: Workbook, max_baux: int, max_ech: int) -> None:
    ws = wb.create_sheet("Révisions")
    _entetes(ws, COLS_REV)
    for slot in range(1, max_baux + 1):
        for k in range(1, max_ech + 1):
            _ligne_revision(ws, slot, k, max_ech)
    derniere = ligne_revision(max_baux, max_ech, max_ech)
    col_statut, col_dec, col_app = R["Statut"], R["Décision"], R["Appliqué ? (Oui/Non)"]
    dv = DataValidation(type="list", formula1='"Oui,Non"', allow_blank=True)
    dv.add(f"{col_app}2:{col_app}{derniere}")
    ws.add_data_validation(dv)
    dv_dec = DataValidation(type="list", formula1='"' + ",".join(DECISIONS) + '"', allow_blank=True,
                            promptTitle="Décision du bailleur",
                            prompt="Vide ou Appliquer = révision appliquée. Geler = loyer inchangé ; « rattrapage possible » repart de l'indice de la dernière révision appliquée.")
    dv_dec.showInputMessage = True
    dv_dec.add(f"{col_dec}2:{col_dec}{derniere}")
    ws.add_data_validation(dv_dec)
    plage_statut = f"{col_statut}2:{col_statut}{derniere}"
    ws.conditional_formatting.add(plage_statut, FormulaRule(formula=[f'LEFT({col_statut}2,1)="⚠"'], font=Font(color="C00000", bold=True), fill=PatternFill("solid", fgColor="FFC7CE")))
    ws.conditional_formatting.add(plage_statut, FormulaRule(formula=[f'AND(LEFT({col_statut}2,1)="✔",{col_app}2<>"Oui")'], font=Font(color="9C5700", bold=True), fill=PatternFill("solid", fgColor="FFEB9C")))
    ws.conditional_formatting.add(plage_statut, FormulaRule(formula=[f'AND(LEFT({col_statut}2,1)="✔",{col_app}2="Oui")'], font=Font(color="006100"), fill=PatternFill("solid", fgColor="C6EFCE")))
    ws.conditional_formatting.add(plage_statut, FormulaRule(formula=[f'LEFT({col_statut}2,1)="❄"'], font=Font(color="1F4E79", bold=True), fill=PatternFill("solid", fgColor="DDEBF7")))
    # lignes hors bail : texte grisé
    ws.conditional_formatting.add(f"A2:{R['Commentaire']}{derniere}",
                                  FormulaRule(formula=[f'AND($A2<>"",${R["Actif"]}2=0)'], font=Font(color="BFBFBF")))
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS_REV))}{derniere}"
    for nom in ("Clé", "Effectif"):
        ws.column_dimensions[R[nom]].hidden = True


def _ligne_revision(ws: Worksheet, slot: int, k: int, max_ech: int) -> None:
    i = ligne_revision(slot, k, max_ech)
    b = ligne_baux(slot)
    bx = lambda col: f"Baux!${col}${b}"
    c = lambda nom: f"{R[nom]}{i}"
    prec = lambda nom: f"{R[nom]}{i - 1}"
    actif = c("Actif")
    off = f'{actif}=0'                         # ligne hors bail / hors durée
    base_t, p, methode, loyer0, plafond = bx(B_TBASE), bx(B_PERREV), bx(B_METHODE), bx(B_LOYER), bx(B_PLAFOND)
    gel = f'LEFT({c("Décision")},5)="Geler"'
    ech_par_an = f'IF({bx(B_PERFACT)}="Mensuelle",12,IF({bx(B_PERFACT)}="Trimestrielle",4,IF({bx(B_PERFACT)}="Semestrielle",2,1)))'

    ws[c("ID bail")] = f'=IF({bx(B_ID)}="","",{bx(B_ID)})'
    ws[c("Local")] = f'=IF({c("ID bail")}="","",{bx(B_LOCAL)})'
    ws[c("N°")] = k
    ws[c("Date de révision")] = f'=IF(OR({c("ID bail")}="",{bx(B_EFFET)}="",{p}=""),"",EDATE({bx(B_EFFET)},12*{p}*{c("N°")}))'
    ws[c("Actif")] = (f'=IF(OR({c("ID bail")}="",{c("Date de révision")}=""),0,'
                      f'IF(AND(ISNUMBER({bx(B_FIN)}),{c("Date de révision")}>{bx(B_FIN)}),0,1))')
    ws[c("Indice")] = f'=IF({off},"",{bx(B_INDICE)})'
    ws[c("Trim. référence")] = f'=IF({off},"",IFERROR(TEXT(VALUE(LEFT({base_t},4))+{c("N°")}*{p},"0")&RIGHT({base_t},3),""))'
    ws[c("Valeur indice réf.")] = f'=IF({off},"",{_lookup_indice(c("Indice"), c("Trim. référence"))})'
    if k == 1:
        ws[c("Trim. précédent")] = f'=IF({off},"",{base_t})'
    else:
        ws[c("Trim. précédent")] = (f'=IF({off},"",IF({methode}="Base fixe",{base_t},'
                                    f'IF({prec("Décision")}="Geler – rattrapage possible",{prec("Trim. précédent")},{prec("Trim. référence")})))')
    ws[c("Valeur indice préc.")] = f'=IF({off},"",{_lookup_indice(c("Indice"), c("Trim. précédent"))})'
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
    ws[c("Charges / échéance HT")] = f'=IF({off},"",IF({bx(B_CHARGES)}="",0,{bx(B_CHARGES)})/{ech_par_an})'
    ws[c("TVA / échéance")] = f'=IF({c("Loyer / échéance HT")}="","",({c("Loyer / échéance HT")}+{c("Charges / échéance HT")})*{bx(B_TVA)}/100)'
    ws[c("Total TTC / échéance")] = f'=IF({c("Loyer / échéance HT")}="","",{c("Loyer / échéance HT")}+{c("Charges / échéance HT")}+{c("TVA / échéance")})'
    ws[c("Statut")] = (f'=IF({off},"",IF({gel},"❄ Gelée",IF({c("Valeur indice réf.")}="",IF({c("Date de révision")}<=TODAY(),"⚠ Indice attendu","À venir"),'
                       f'IF({c("Date de révision")}<=TODAY(),"✔ Calculable","Indice connu – à venir"))))')
    ws[c("Clé")] = f'=IF({c("ID bail")}="","",{c("ID bail")}&"#"&{c("N°")})'
    ws[c("Effectif")] = f'=IF(AND({actif}=1,{c("Date de révision")}<=TODAY(),{c("Loyer retenu (annuel HT)")}<>""),1,0)'

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


def ecrire_saisies(wb: Workbook, baux: list[Bail], saisies: Saisies, max_ech: int) -> None:
    ws = wb["Révisions"]
    for slot, b in enumerate(baux, start=1):
        for k in range(1, max_ech + 1):
            i = ligne_revision(slot, k, max_ech)
            for attr, nom in Saisies.CHAMPS:
                v = getattr(saisies, attr).get((b.id, k))
                if v is not None:
                    ws[f"{R[nom]}{i}"] = v


# --- Alertes -----------------------------------------------------------------
def _feuille_alertes(wb: Workbook, max_baux: int) -> None:
    ws = wb.create_sheet("Alertes")
    _entetes(ws, COLS_ALERTES)
    rv = lambda nom: f"Révisions!${R[nom]}:${R[nom]}"
    for slot in range(1, max_baux + 1):
        i, b = slot + 1, ligne_baux(slot)
        c = lambda nom: f"{A[nom]}{i}"
        bx = lambda col: f"Baux!${col}${b}"
        vide = f'{c("ID bail")}=""'
        idc = c("ID bail")
        ws[idc] = f'=IF({bx(B_ID)}="","",{bx(B_ID)})'
        ws[c("Local")] = f'=IF({vide},"",{bx(B_LOCAL)})'
        ws[c("Locataire")] = f'=IF({vide},"",{bx(B_LOCATAIRE)})'
        ws[c("Indice")] = f'=IF({vide},"",{bx(B_INDICE)})'
        ws[c("Loyer initial annuel HT")] = f'=IF({vide},"",{bx(B_LOYER)})'
        n = c("N° dernière révision effective")
        ws[n] = f'=IF({vide},"",_xlfn.MAXIFS({rv("N°")},{rv("ID bail")},{idc},{rv("Effectif")},1))'
        ws[c("Loyer actuel (annuel HT)")] = (f'=IF({vide},"",IF({n}=0,{c("Loyer initial annuel HT")},'
                                             f'SUMIFS({rv("Loyer retenu (annuel HT)")},{rv("ID bail")},{idc},{rv("N°")},{n})))')
        ws[c("Loyer actuel mensuel HT")] = f'=IF({vide},"",{c("Loyer actuel (annuel HT)")}/12)'
        prochaine = f'_xlfn.MINIFS({rv("Date de révision")},{rv("ID bail")},{idc},{rv("Actif")},1,{rv("Date de révision")},">"&TODAY())'
        ws[c("Prochaine révision")] = f'=IF({vide},"",IF({prochaine}=0,"—",{prochaine}))'
        ws[c("Trimestre attendu")] = f'=IF({vide},"",IFERROR(INDEX({rv("Trim. référence")},MATCH({idc}&"#"&({n}+1),{rv("Clé")},0))&"","—"))'
        ws[c("Indice publié ?")] = (f'=IF({vide},"",IF(OR({c("Trimestre attendu")}="—",{c("Trimestre attendu")}=""),"—",'
                                    f'IF(SUMIFS(Indices_long!$C:$C,Indices_long!$A:$A,{c("Indice")},Indices_long!$B:$B,{c("Trimestre attendu")})>0,"Oui","Non")))')
        ws[c("Révisions calculables non appliquées")] = (f'=IF({vide},"",COUNTIFS({rv("ID bail")},{idc},{rv("Statut")},"✔ Calculable",'
                                                         f'{rv("Appliqué ? (Oui/Non)")},"<>Oui"))')
        ws[c("Révisions gelées")] = f'=IF({vide},"",COUNTIFS({rv("ID bail")},{idc},{rv("Statut")},"❄ Gelée"))'
        ws[c("Consigne prochaine révision")] = f'=IF({vide},"",IFERROR(INDEX({rv("Consigne (à faire)")},MATCH({idc}&"#"&({n}+1),{rv("Clé")},0))&"",""))'
        pr = c("Prochaine révision")
        ws[c("Action")] = (f'=IF({vide},"",IF({c("Révisions calculables non appliquées")}>0,"⚠ Facturer la révision ("&{c("Révisions calculables non appliquées")}&" en attente)",'
                           f'IF(AND({pr}<>"—",{pr}-TODAY()<=60),'
                           f'IF({c("Indice publié ?")}="Oui","Révision dans "&INT({pr}-TODAY())&" j – indice publié","Révision dans "&INT({pr}-TODAY())&" j – indice non publié"),"RAS")))')
        for nom in ("Loyer initial annuel HT", "Loyer actuel (annuel HT)", "Loyer actuel mensuel HT"):
            ws[c(nom)].number_format = FMT_EUR
        ws[pr].number_format = FMT_DATE
        ws[c("Consigne prochaine révision")].alignment = Alignment(wrap_text=True, vertical="top")
        for nom, _ in COLS_ALERTES:
            ws[c(nom)].border = BORDURE
    n = max_baux + 1
    ws.conditional_formatting.add(f"{A['Action']}2:{A['Action']}{n}", FormulaRule(formula=[f'LEFT({A["Action"]}2,1)="⚠"'], font=Font(color="C00000", bold=True), fill=PatternFill("solid", fgColor="FFC7CE")))
    ws.conditional_formatting.add(f"{A['Action']}2:{A['Action']}{n}", FormulaRule(formula=[f'LEFT({A["Action"]}2,8)="Révision"'], fill=PatternFill("solid", fgColor="FFEB9C")))
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS_ALERTES))}{n}"


# --- Pennylane ---------------------------------------------------------------
def _feuille_pennylane(wb: Workbook, max_baux: int) -> None:
    ws = wb.create_sheet("Pennylane")
    _entetes(ws, COLS_PL)
    soc = lambda cle: f"Société!$B${SOC_ROW[cle]}"
    q = '""'
    for slot in range(1, max_baux + 1):
        i, b = slot + 1, ligne_baux(slot)
        c = lambda nom: f"{P[nom]}{i}"
        bx = lambda col: f"Baux!${col}${b}"
        al = lambda nom: f"Alertes!${A[nom]}${i}"
        vide = f'{c("ID bail")}=""'
        per = c("Périodicité")
        ech_par_an = f'IF({per}="Mensuelle",12,IF({per}="Trimestrielle",4,IF({per}="Semestrielle",2,1)))'
        ws[c("ID bail")] = f'=IF({bx(B_ID)}="","",{bx(B_ID)})'
        ws[c("Locataire")] = f'=IF({vide},"",{bx(B_LOCATAIRE)})'
        ws[c("customer_id")] = f'=IF({vide},"",IF({bx(B_CUST)}="","À RENSEIGNER",{bx(B_CUST)}))'
        ws[c("product_id")] = f'=IF(OR({vide},{bx(B_PROD)}=""),"",{bx(B_PROD)})'
        ws[c("label (abonnement)")] = f'=IF({vide},"","Loyer "&{bx(B_LOCAL)})'
        ws[per] = f'=IF({vide},"",{bx(B_PERFACT)})'
        ws[c("recurring_rule.type")] = f'=IF({vide},"",IF({per}="Annuelle","yearly","monthly"))'
        ws[c("interval")] = f'=IF({vide},"",IF({per}="Mensuelle",1,IF({per}="Trimestrielle",3,IF({per}="Semestrielle",6,1))))'
        ws[c("unit")] = f'=IF({vide},"",IF({per}="Mensuelle","mois",IF({per}="Trimestrielle","trimestre",IF({per}="Semestrielle","semestre","an"))))'
        ws[c("Prix unitaire HT / échéance")] = f'=IF({vide},"",ROUND({al("Loyer actuel (annuel HT)")}/{ech_par_an},2))'
        ws[c("Charges HT / échéance")] = f'=IF({vide},"",ROUND(IF({bx(B_CHARGES)}="",0,{bx(B_CHARGES)})/{ech_par_an},2))'
        ws[c("TVA (%)")] = f'=IF({vide},"",{bx(B_TVA)})'
        ws[c("vat_rate")] = (f'=IF({vide},"",IF({c("TVA (%)")}=20,"FR_200",IF({c("TVA (%)")}=10,"FR_100",'
                             f'IF({c("TVA (%)")}=5.5,"FR_55",IF({c("TVA (%)")}=2.1,"FR_21","exempt")))))')
        ws[c("mode")] = f'=IF({vide},"",{soc("pl_mode")})'
        ws[c("payment_conditions")] = f'=IF({vide},"",{soc("pl_payment_conditions")})'
        ws[c("payment_method")] = f'=IF({vide},"",{soc("pl_payment_method")})'
        ws[c("start (1er du mois suivant)")] = f'=IF({vide},"",DATE(YEAR(TODAY()),MONTH(TODAY())+1,1))'
        ws[c("subscription_id existant")] = f'=IF(OR({vide},{bx(B_SUBSCR)}=""),"",{bx(B_SUBSCR)})'
        d = c("start (1er du mois suivant)")
        iso = f'YEAR({d})&"-"&TEXT(MONTH({d}),"00")&"-"&TEXT(DAY({d}),"00")'
        num = lambda ref: f'SUBSTITUTE(TEXT({ref},"0.00"),",",".")'
        ligne_loyer = (f'"{{{q}label{q}: {q}Loyer "&LOWER({per})&" – "&{bx(B_LOCAL)}&"{q}, {q}quantity{q}: 1, {q}unit{q}: {q}"&{c("unit")}&"{q}, '
                       f'{q}raw_currency_unit_price{q}: {q}"&{num(c("Prix unitaire HT / échéance"))}&"{q}, {q}vat_rate{q}: {q}"&{c("vat_rate")}&"{q}"'
                       f'&IF({c("product_id")}<>"",", {q}product_id{q}: "&{c("product_id")},"")&"}}"')
        ligne_charges = (f'IF({c("Charges HT / échéance")}>0,", {{{q}label{q}: {q}Provision sur charges{q}, {q}quantity{q}: 1, {q}unit{q}: {q}"&{c("unit")}&"{q}, '
                         f'{q}raw_currency_unit_price{q}: {q}"&{num(c("Charges HT / échéance"))}&"{q}, {q}vat_rate{q}: {q}"&{c("vat_rate")}&"{q}}}","")')
        rule = (f'"{{{q}type{q}: {q}"&{c("recurring_rule.type")}&"{q}, {q}interval{q}: "&{c("interval")}'
                f'&IF({c("recurring_rule.type")}="monthly",", {q}day_of_month{q}: 1","")&"}}"')
        ws[c("Corps JSON – POST /api/external/v2/billing_subscriptions")] = (
            f'=IF({vide},"","{{{q}customer_id{q}: "&{c("customer_id")}&", {q}label{q}: {q}"&{c("label (abonnement)")}&"{q}, {q}start{q}: {q}"&{iso}&"{q}, '
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
    n = max_baux + 3
    ws.cell(row=n, column=1, value="Schéma : OpenAPI Pennylane Company V2 (POST /billing_subscriptions, 2026-05-27). Réglages dans la feuille Société. "
                                    "Automatisation de l'envoi : étape ultérieure (commande pennylane --push).").font = FONT_GRIS


# =============================================================================
# Lecture / mise à jour d'un classeur existant
# =============================================================================
def lire_classeur(chemin: Path) -> tuple[Societe, list[Bail], Saisies]:
    """Société, baux (avec leur position `ligne_baux`) et saisies de la feuille Révisions."""
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
        max_baux=int(g("max_baux", MAX_BAUX)), max_echeances=int(g("max_echeances", MAX_ECHEANCES)),
    )
    ws_b = wb["Baux"]
    entetes = [ws_b.cell(row=1, column=c).value for c in range(1, len(COLS_BAUX) + 1)]
    attendus = [n for n, _ in COLS_BAUX]
    if entetes != attendus:
        raise ValueError("La feuille Baux a été modifiée (colonnes déplacées ou renommées) : "
                         f"attendu {attendus}, trouvé {entetes}")
    baux: list[Bail] = []
    for slot in range(1, societe.max_baux + 1):
        r = ligne_baux(slot)
        v = lambda col: ws_b[f"{col}{r}"].value
        if v(B_ID) in (None, ""):
            continue
        b = Bail(
            id=str(v(B_ID)).strip(), local=str(v(B_LOCAL) or ""), locataire=str(v(B_LOCATAIRE) or ""),
            adresse_locataire=str(v(B_ADRESSE) or ""), type_bail=str(v(B_TYPE) or "Commercial"), date_effet=_en_date(v(B_EFFET)),
            duree_ans=int(v(B_DUREE)) if v(B_DUREE) not in (None, "") else None,
            loyer_initial_annuel_ht=float(v(B_LOYER) or 0), charges_annuelles_ht=float(v(B_CHARGES) or 0),
            tva_pct=float(v(B_TVA) if v(B_TVA) not in (None, "") else 20),
            periodicite_facturation=str(v(B_PERFACT) or "Mensuelle"), indice=str(v(B_INDICE) or "ILC"),
            trimestre_base=str(v(B_TBASE) or ""), periodicite_revision_ans=int(v(B_PERREV) or 1),
            methode=str(v(B_METHODE) or "Chaînée"),
            plafond_annuel_pct=float(v(B_PLAFOND)) if v(B_PLAFOND) not in (None, "") else None,
            pennylane_customer_id=_texte_id(v(B_CUST)), pennylane_product_id=_texte_id(v(B_PROD)),
            pennylane_subscription_id=_texte_id(v(B_SUBSCR)), notes=str(v(B_NOTES) or ""),
        )
        b.ligne_baux = r
        baux.append(b)
    saisies = Saisies()
    ws_r = wb["Révisions"]
    for b in baux:
        slot = b.ligne_baux - 1
        for k in range(1, societe.max_echeances + 1):
            r = ligne_revision(slot, k, societe.max_echeances)
            for attr, nom in Saisies.CHAMPS:
                val = ws_r[f"{R[nom]}{r}"].value
                if val not in (None, ""):
                    getattr(saisies, attr)[(b.id, k)] = val
    return societe, baux, saisies


def lire_indices_classeur(chemin: Path) -> list[Observation]:
    """Observations présentes dans la grille Indices (ce que voit le cabinet)."""
    wb = load_workbook(chemin, data_only=False)
    ws = wb["Indices"]
    obs: list[Observation] = []
    for code in SERIES:
        for annee in range(ANNEE_MIN, ANNEE_MAX + 1):
            for q in range(1, 5):
                v = ws[cellule_indice(code, annee, q)].value
                if isinstance(v, (int, float)):
                    obs.append(Observation(code, "", f"{annee}-T{q}", float(v), "", "", "CLASSEUR", ""))
    return obs


def sauvegarder(chemin: Path) -> Path:
    s = chemin.with_suffix(f".{datetime.now():%Y%m%d-%H%M%S}.bak.xlsx")
    shutil.copy2(chemin, s)
    return s


def mettre_a_jour_indices(chemin: Path, observations: list[Observation]) -> int:
    """Écrit les observations dans la grille Indices du classeur existant, sans toucher au reste."""
    sauvegarder(chemin)
    wb = load_workbook(chemin)
    n = ecrire_indices(wb, observations)
    wb.calculation.fullCalcOnLoad = True
    wb.save(chemin)
    return n


def ecrire_cellules_revisions(chemin: Path, valeurs: dict[tuple[str, int], object], colonne: str, max_echeances: int | None = None) -> int:
    """Écrit `valeurs[(ID bail, n°)]` dans la colonne `colonne` de Révisions (sauvegarde préalable)."""
    societe, baux, _ = lire_classeur(chemin)
    sauvegarder(chemin)
    wb = load_workbook(chemin)
    ws = wb["Révisions"]
    n = 0
    for b in baux:
        for k in range(1, societe.max_echeances + 1):
            if (b.id, k) in valeurs:
                c = ws[f"{R[colonne]}{ligne_revision(b.ligne_baux - 1, k, societe.max_echeances)}"]
                c.value = valeurs[(b.id, k)]
                if isinstance(c.value, date):
                    c.number_format = FMT_DATE
                n += 1
    wb.calculation.fullCalcOnLoad = True
    wb.save(chemin)
    return n


def ecrire_colonne_baux(chemin: Path, valeurs: dict[str, object], colonne: str) -> int:
    """Écrit `valeurs[ID bail]` dans la colonne `colonne` de Baux (sauvegarde préalable)."""
    _, baux, _ = lire_classeur(chemin)
    sauvegarder(chemin)
    wb = load_workbook(chemin)
    ws = wb["Baux"]
    n = 0
    for b in baux:
        if b.id in valeurs:
            ws[f"{colonne}{b.ligne_baux}"] = valeurs[b.id]
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

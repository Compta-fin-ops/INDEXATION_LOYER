"""Moteur de calcul Python des révisions.

Il reproduit *exactement* la logique des formules du classeur Excel (feuille
« Révisions »). Il sert :
  - de contre-épreuve : un test recalcule le classeur avec LibreOffice et
    compare cellule à cellule ;
  - de source pour la préparation des abonnements Pennylane sans dépendre des
    valeurs mises en cache par Excel.

Règles (identiques aux formules) :
  coefficient      = indice(trimestre de référence) / indice(trimestre précédent)
  base de calcul   = loyer initial si 1re révision ou méthode « Base fixe »,
                     sinon loyer retenu de la révision précédente
  loyer brut       = ARRONDI(base × coefficient ; 2)
  loyer retenu     = MIN(brut ; base × (1 + plafond)^(années couvertes)) si un plafond est saisi
                     (années couvertes = écart d'années entre l'indice de référence et l'indice de départ)
  gel              = loyer retenu = loyer précédent ; statut « ❄ Gelée »
  loyer précédent  = loyer initial si 1re révision, sinon loyer retenu précédent
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .baux import DECISION_APPLIQUER, DECISION_GEL_RATTRAPAGE, Bail, Echeance, est_gel
from .insee import Observation

STATUT_CALCULABLE = "✔ Calculable"
STATUT_GELEE = "❄ Gelée"
STATUT_ATTENDU = "⚠ Indice attendu"
STATUT_A_VENIR = "À venir"
STATUT_CONNU_A_VENIR = "Indice connu – à venir"


@dataclass
class LigneRevision:
    echeance: Echeance
    decision: str
    trimestre_precedent_effectif: str
    indice_reference: float | None
    indice_precedent: float | None
    coefficient: float | None
    base_calcul: float | None
    loyer_brut: float | None
    loyer_retenu: float | None
    loyer_precedent: float | None
    statut: str

    @property
    def variation_pct(self) -> float | None:
        if self.loyer_retenu is None or not self.loyer_precedent:
            return None
        return self.loyer_retenu / self.loyer_precedent - 1


def arrondi_excel(x: float, decimales: int = 2) -> float:
    q = Decimal(1).scaleb(-decimales)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP))


def table_indices(observations: list[Observation]) -> dict[tuple[str, str], float]:
    return {o.cle: o.valeur for o in observations}


def calculer(bail: Bail, indices: dict[tuple[str, str], float], horizon: date,
             aujourdhui: date | None = None,
             decisions: dict[tuple[str, int], str] | None = None) -> list[LigneRevision]:
    """``decisions`` : {(id bail, n° révision): "Appliquer" | "Geler – ..."} ; absent = Appliquer.

    Gel « sans rattrapage » : le loyer reste inchangé et la révision suivante repart de
    l'indice de référence de l'échéance gelée (la hausse de l'année est perdue).
    Gel « rattrapage possible » : le loyer reste inchangé mais la révision suivante repart
    de l'indice de la dernière révision appliquée (rattrapage automatique).
    En méthode « Base fixe », le rattrapage est automatique dans les deux cas."""
    aujourdhui = aujourdhui or date.today()
    decisions = decisions or {}
    lignes: list[LigneRevision] = []
    precedent_retenu: float | None = None
    prec_ref: str | None = None
    prec_eff: str | None = None
    prec_decision = DECISION_APPLIQUER
    for ech in bail.calendrier(horizon):
        decision = (decisions.get((bail.id, ech.numero)) or DECISION_APPLIQUER).strip()
        if ech.numero == 1 or bail.methode == "Base fixe":
            t_prec_eff = ech.trimestre_precedent
        elif prec_decision == DECISION_GEL_RATTRAPAGE:
            t_prec_eff = prec_eff
        else:
            t_prec_eff = prec_ref
        i_ref = indices.get((bail.indice, ech.trimestre_reference))
        i_prec = indices.get((bail.indice, t_prec_eff))
        if ech.numero == 1 or bail.methode == "Base fixe":
            base = bail.loyer_initial_annuel_ht
        else:
            base = precedent_retenu
        loyer_prec = bail.loyer_initial_annuel_ht if ech.numero == 1 else precedent_retenu

        coef = brut = retenu = None
        if est_gel(decision):
            retenu = loyer_prec
            if i_ref is not None and i_prec is not None and base is not None:
                coef = i_ref / i_prec
                brut = arrondi_excel(base * coef)
        elif i_ref is not None and i_prec is not None and base is not None:
            coef = i_ref / i_prec
            brut = arrondi_excel(base * coef)
            retenu = brut
            if bail.plafond_annuel_pct is not None:
                # années couvertes par le coefficient : depuis l'indice de base (base fixe) ou depuis
                # le trimestre précédent effectif (chaînée ; = 2 périodes après un gel avec rattrapage)
                annees = ech.numero * bail.periodicite_revision_ans if bail.methode == "Base fixe" \
                    else int(ech.trimestre_reference[:4]) - int(t_prec_eff[:4])
                plafond = base * (1 + bail.plafond_annuel_pct / 100) ** annees
                retenu = min(brut, arrondi_excel(plafond))

        if est_gel(decision):
            statut = STATUT_GELEE
        elif i_ref is None:
            statut = STATUT_ATTENDU if ech.date_revision <= aujourdhui else STATUT_A_VENIR
        else:
            statut = STATUT_CALCULABLE if ech.date_revision <= aujourdhui else STATUT_CONNU_A_VENIR

        lignes.append(LigneRevision(ech, decision, t_prec_eff, i_ref, i_prec, coef, base, brut, retenu, loyer_prec, statut))
        precedent_retenu = retenu
        prec_ref, prec_eff, prec_decision = ech.trimestre_reference, t_prec_eff, decision
    return lignes


def loyer_actuel(bail: Bail, lignes: list[LigneRevision], aujourdhui: date | None = None) -> float:
    """Dernier loyer annuel HT calculable à la date du jour (le loyer initial sinon)."""
    aujourdhui = aujourdhui or date.today()
    valeur = bail.loyer_initial_annuel_ht
    for l in lignes:
        if l.echeance.date_revision <= aujourdhui and l.loyer_retenu is not None:
            valeur = l.loyer_retenu
    return valeur

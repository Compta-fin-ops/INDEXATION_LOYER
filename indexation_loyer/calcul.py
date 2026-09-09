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
  loyer précédent  = loyer initial si 1re révision, sinon loyer retenu précédent
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .baux import Bail, Echeance
from .insee import Observation

STATUT_CALCULABLE = "✔ Calculable"
STATUT_ATTENDU = "⚠ Indice attendu"
STATUT_A_VENIR = "À venir"
STATUT_CONNU_A_VENIR = "Indice connu – à venir"


@dataclass
class LigneRevision:
    echeance: Echeance
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
             aujourdhui: date | None = None) -> list[LigneRevision]:
    aujourdhui = aujourdhui or date.today()
    lignes: list[LigneRevision] = []
    precedent_retenu: float | None = None
    for ech in bail.calendrier(horizon):
        i_ref = indices.get((bail.indice, ech.trimestre_reference))
        i_prec = indices.get((bail.indice, ech.trimestre_precedent))
        if ech.numero == 1 or bail.methode == "Base fixe":
            base = bail.loyer_initial_annuel_ht
        else:
            base = precedent_retenu
        loyer_prec = bail.loyer_initial_annuel_ht if ech.numero == 1 else precedent_retenu

        coef = brut = retenu = None
        if i_ref is not None and i_prec is not None and base is not None:
            coef = i_ref / i_prec
            brut = arrondi_excel(base * coef)
            retenu = brut
            if bail.plafond_annuel_pct is not None:
                annees = ech.numero * bail.periodicite_revision_ans if bail.methode == "Base fixe" \
                    else bail.periodicite_revision_ans
                plafond = base * (1 + bail.plafond_annuel_pct / 100) ** annees
                retenu = min(brut, arrondi_excel(plafond))

        if i_ref is None:
            statut = STATUT_ATTENDU if ech.date_revision <= aujourdhui else STATUT_A_VENIR
        else:
            statut = STATUT_CALCULABLE if ech.date_revision <= aujourdhui else STATUT_CONNU_A_VENIR

        lignes.append(LigneRevision(ech, i_ref, i_prec, coef, base, brut, retenu, loyer_prec, statut))
        precedent_retenu = retenu
    return lignes


def loyer_actuel(bail: Bail, lignes: list[LigneRevision], aujourdhui: date | None = None) -> float:
    """Dernier loyer annuel HT calculable à la date du jour (le loyer initial sinon)."""
    aujourdhui = aujourdhui or date.today()
    valeur = bail.loyer_initial_annuel_ht
    for l in lignes:
        if l.statut == STATUT_CALCULABLE and l.loyer_retenu is not None:
            valeur = l.loyer_retenu
    return valeur

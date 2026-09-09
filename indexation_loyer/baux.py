"""Modèle d'un bail et calendrier théorique de ses révisions.

Le classeur Excel calcule les loyers par formules (pour rester auditable par
un réviseur). Ce module ne sert qu'à :
  - lire / valider la feuille « Baux » saisie par le cabinet ;
  - produire la *structure* du calendrier (numéro, dates, trimestres de référence)
    que le générateur transforme ensuite en lignes de formules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterator

from .periodes import Trimestre

INDICES = ("ILC", "ILAT", "ICC", "IRL")
TYPES_BAIL = ("Commercial", "Professionnel", "Habitation", "Dérogatoire", "Autre")
PERIODICITES_FACTURATION = {"Mensuelle": 12, "Trimestrielle": 4, "Semestrielle": 2, "Annuelle": 1}
METHODES = ("Chaînée", "Base fixe")

# Décision du bailleur sur une échéance donnée (saisie dans la feuille Révisions).
DECISION_APPLIQUER = "Appliquer"
DECISION_GEL_SANS = "Geler – sans rattrapage"       # l'indice avance, la hausse de l'année est abandonnée
DECISION_GEL_RATTRAPAGE = "Geler – rattrapage possible"  # l'indice de départ reste celui de la dernière révision appliquée
DECISIONS = (DECISION_APPLIQUER, DECISION_GEL_SANS, DECISION_GEL_RATTRAPAGE)


def est_gel(decision: str | None) -> bool:
    return bool(decision) and str(decision).strip().startswith("Geler")


@dataclass
class Bail:
    id: str
    local: str
    locataire: str
    date_effet: date
    loyer_initial_annuel_ht: float
    adresse_locataire: str = ""          # adresse de correspondance pour les courriers (défaut : le local)
    indice: str = "ILC"
    trimestre_base: str = ""             # ex. 2023-T2 ; vide -> déduit de date_effet (T-2)
    periodicite_revision_ans: int = 1    # 1 = annuelle (clause d'échelle mobile), 3 = triennale
    methode: str = "Chaînée"
    periodicite_facturation: str = "Mensuelle"
    tva_pct: float = 20.0
    charges_annuelles_ht: float = 0.0
    plafond_annuel_pct: float | None = None
    duree_ans: int | None = 9
    date_fin: date | None = None
    type_bail: str = "Commercial"
    pennylane_customer_id: str = ""
    pennylane_product_id: str = ""
    notes: str = ""
    erreurs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.indice not in INDICES:
            self.erreurs.append(f"Indice inconnu {self.indice!r} (attendu {', '.join(INDICES)})")
        if self.methode not in METHODES:
            self.erreurs.append(f"Méthode inconnue {self.methode!r}")
        if self.periodicite_facturation not in PERIODICITES_FACTURATION:
            self.erreurs.append(f"Périodicité de facturation inconnue {self.periodicite_facturation!r}")
        if self.periodicite_revision_ans not in (1, 2, 3):
            self.erreurs.append(f"Périodicité de révision inhabituelle : {self.periodicite_revision_ans} an(s)")
        if not self.trimestre_base:
            # Convention par défaut : « dernier indice publié à la date de prise d'effet »,
            # soit T-2 par rapport au trimestre de la date d'effet. À confirmer dans le bail.
            self.trimestre_base = Trimestre.de_date(self.date_effet).plus_trimestres(-2).fr
        try:
            Trimestre.parse(self.trimestre_base)
        except ValueError as e:
            self.erreurs.append(str(e))
        if self.date_fin is None and self.duree_ans:
            self.date_fin = _ajouter_annees(self.date_effet, self.duree_ans)

    @property
    def adresse_courrier(self) -> str:
        return self.adresse_locataire or self.local

    @property
    def echeances_par_an(self) -> int:
        return PERIODICITES_FACTURATION.get(self.periodicite_facturation, 12)

    def calendrier(self, horizon: date) -> Iterator["Echeance"]:
        """Révisions théoriques, de la 1re à la dernière antérieure à ``horizon``
        (et à la fin du bail si connue)."""
        base = Trimestre.parse(self.trimestre_base)
        p = self.periodicite_revision_ans
        k = 1
        while True:
            d = _ajouter_annees(self.date_effet, k * p)
            if d > horizon or (self.date_fin and d > self.date_fin):
                return
            yield Echeance(
                bail_id=self.id, numero=k, date_revision=d,
                trimestre_reference=base.plus_annees(k * p).fr,
                trimestre_precedent=(base.plus_annees((k - 1) * p) if self.methode == "Chaînée" else base).fr,
            )
            k += 1


@dataclass(frozen=True)
class Echeance:
    bail_id: str
    numero: int
    date_revision: date
    trimestre_reference: str
    trimestre_precedent: str


def _ajouter_annees(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 29 février
        return d.replace(year=d.year + n, day=28)

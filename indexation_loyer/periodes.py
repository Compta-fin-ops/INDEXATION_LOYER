"""Manipulation des trimestres au format français ``AAAA-Tn`` (ex. ``2024-T2``).

L'INSEE renvoie les périodes en SDMX (``2024-Q2``). Tout le projet travaille
en ``2024-T2`` ; la conversion se fait à l'ingestion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_RE_FR = re.compile(r"^(\d{4})-T([1-4])$")
_RE_SDMX = re.compile(r"^(\d{4})-Q([1-4])$")
_RE_MOIS = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass(frozen=True, order=True)
class Trimestre:
    annee: int
    numero: int  # 1..4

    def __post_init__(self) -> None:
        if not 1 <= self.numero <= 4:
            raise ValueError(f"Numéro de trimestre invalide : {self.numero}")

    # --- constructeurs -----------------------------------------------------
    @classmethod
    def parse(cls, texte: str) -> "Trimestre":
        texte = str(texte).strip().upper()
        m = _RE_FR.match(texte) or _RE_SDMX.match(texte)
        if not m:
            raise ValueError(f"Trimestre illisible : {texte!r} (attendu AAAA-Tn ou AAAA-Qn)")
        return cls(int(m.group(1)), int(m.group(2)))

    @classmethod
    def de_date(cls, d: date) -> "Trimestre":
        return cls(d.year, (d.month - 1) // 3 + 1)

    # --- conversions -------------------------------------------------------
    @property
    def fr(self) -> str:
        return f"{self.annee}-T{self.numero}"

    @property
    def sdmx(self) -> str:
        return f"{self.annee}-Q{self.numero}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.fr

    # --- arithmétique ------------------------------------------------------
    def plus_annees(self, n: int) -> "Trimestre":
        return Trimestre(self.annee + n, self.numero)

    def plus_trimestres(self, n: int) -> "Trimestre":
        idx = self.annee * 4 + (self.numero - 1) + n
        return Trimestre(idx // 4, idx % 4 + 1)

    def precedent(self) -> "Trimestre":
        return self.plus_trimestres(-1)

    def debut(self) -> date:
        return date(self.annee, 3 * (self.numero - 1) + 1, 1)


def periode_sdmx_vers_fr(periode: str) -> str:
    """Convertit une période SDMX en clé française.

    Trimestres : ``2024-Q2`` -> ``2024-T2``.
    Mois (séries mensuelles, non utilisées ici mais tolérées) : ``2024-07`` -> ``2024-07``.
    Années : ``2024`` -> ``2024``.
    """
    periode = periode.strip()
    m = _RE_SDMX.match(periode)
    if m:
        return f"{m.group(1)}-T{m.group(2)}"
    return periode


def dernier_trimestre_publiable(aujourdhui: date | None = None) -> Trimestre:
    """Dernier trimestre dont l'indice peut être publié à la date donnée.

    Règle empirique : l'INSEE publie les indices trimestriels (ILC, ILAT, ICC)
    environ trois mois après la fin du trimestre. À la date J, le dernier
    trimestre *possiblement* publié est donc T-2 par rapport au trimestre
    courant. Cette fonction ne remplace pas la vérification de présence
    réelle de la valeur dans le cache.
    """
    aujourdhui = aujourdhui or date.today()
    return Trimestre.de_date(aujourdhui).plus_trimestres(-2)

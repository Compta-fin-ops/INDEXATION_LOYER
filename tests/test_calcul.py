from datetime import date

from indexation_loyer import calcul
from indexation_loyer.baux import DECISION_GEL_RATTRAPAGE, DECISION_GEL_SANS, Bail

INDICES = {("ILC", "2020-T4"): 100.0, ("ILC", "2021-T4"): 102.0, ("ILC", "2022-T4"): 105.06, ("ILC", "2023-T4"): 110.0}
BAIL = dict(id="B", local="L", locataire="X", date_effet=date(2021, 1, 1), loyer_initial_annuel_ht=10000, trimestre_base="2020-T4")
AUJ = date(2026, 9, 9)


def _retenus(bail, decisions):
    return [(l.loyer_retenu, l.statut, l.trimestre_precedent_effectif)
            for l in calcul.calculer(bail, INDICES, horizon=date(2024, 6, 1), aujourdhui=AUJ, decisions=decisions)]


def test_chainee_sans_gel():
    assert [r for r, _, _ in _retenus(Bail(**BAIL), {})] == [10200.0, 10506.0, 11000.0]


def test_gel_sans_rattrapage_perd_la_hausse():
    r = _retenus(Bail(**BAIL), {("B", 2): DECISION_GEL_SANS})
    assert r[1] == (10200.0, "❄ Gelée", "2021-T4")
    assert r[2] == (round(10200 * 110 / 105.06, 2), "✔ Calculable", "2022-T4")   # repart de l'indice 2022-T4


def test_gel_avec_rattrapage_repart_de_l_indice_applique():
    r = _retenus(Bail(**BAIL), {("B", 2): DECISION_GEL_RATTRAPAGE})
    assert r[1][0] == 10200.0
    assert r[2] == (round(10200 * 110 / 102, 2), "✔ Calculable", "2021-T4")      # rattrapage sur 2 ans


def test_base_fixe_rattrape_toujours():
    b = Bail(**BAIL, methode="Base fixe")
    r = _retenus(b, {("B", 2): DECISION_GEL_SANS})
    assert r[1][0] == 10200.0 and r[2][0] == 11000.0


def test_gel_sans_indice_publie_reste_chiffrable():
    b = Bail(**BAIL)
    lignes = calcul.calculer(b, {("ILC", "2020-T4"): 100.0}, horizon=date(2022, 6, 1), aujourdhui=AUJ, decisions={("B", 1): DECISION_GEL_SANS})
    assert lignes[0].loyer_retenu == 10000 and lignes[0].statut == "❄ Gelée"
    assert calcul.loyer_actuel(b, lignes, AUJ) == 10000

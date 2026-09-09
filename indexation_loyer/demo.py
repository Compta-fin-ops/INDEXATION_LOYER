"""Classeur de démonstration : baux et indices FICTIFS (source FICTIF_DEMO).

Les valeurs d'indices sont volontairement irréalistes (progression régulière)
pour qu'aucune confusion avec une valeur INSEE ne soit possible.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .baux import Bail
from .insee import Observation
from .periodes import Trimestre
from .baux import DECISION_GEL_RATTRAPAGE, DECISION_GEL_SANS
from .workbook import Saisies, Societe, construire


def indices_fictifs(aujourdhui: date) -> list[Observation]:
    """Séries fictives de 2018-T1 au dernier trimestre publiable à `aujourdhui`."""
    dernier = Trimestre.de_date(aujourdhui).plus_trimestres(-2)
    obs: list[Observation] = []
    for code, idbank, depart, pas in (("ILC", "001532540", 110.0, 0.60), ("ILAT", "001617112", 108.0, 0.55),
                                       ("ICC", "000008630", 1700.0, 8.0), ("IRL", "001515333", 128.0, 0.40)):
        t, v = Trimestre(2018, 1), depart
        while t <= dernier:
            # une baisse fictive en 2021 pour montrer la bilatéralité de la clause
            delta = -pas if t.annee == 2021 else pas
            obs.append(Observation(code, idbank, t.fr, round(v, 2), "A" if t < dernier else "P",
                                   aujourdhui.isoformat(), "FICTIF_DEMO", aujourdhui.isoformat()))
            v += delta
            t = t.plus_trimestres(1)
    return obs


def baux_fictifs() -> list[Bail]:
    return [
        Bail(id="B01", local="Boutique 12 rue de la Paix", locataire="Boulangerie Dupont SAS", date_effet=date(2021, 4, 1),
             adresse_locataire="12 rue de la Paix\n75002 Paris",
             loyer_initial_annuel_ht=24000, charges_annuelles_ht=2400, indice="ILC", trimestre_base="2020-T4",
             periodicite_revision_ans=1, methode="Chaînée", periodicite_facturation="Mensuelle",
             pennylane_customer_id="100001", notes="Clause d'échelle mobile annuelle, ILC même trimestre"),
        Bail(id="B02", local="Bureaux 2e étage – 8 av. Foch", locataire="Cabinet Martin (SELARL)", date_effet=date(2022, 10, 1),
             loyer_initial_annuel_ht=36000, indice="ILAT", trimestre_base="2022-T2", periodicite_revision_ans=3,
             methode="Base fixe", periodicite_facturation="Trimestrielle", pennylane_customer_id="100002",
             notes="Révision triennale indexée ILAT, indice de base fixe"),
        Bail(id="B03", local="Entrepôt ZA des Landes", locataire="Logistix SARL", date_effet=date(2019, 1, 15),
             loyer_initial_annuel_ht=60000, charges_annuelles_ht=6000, indice="ICC", trimestre_base="2018-T3",
             periodicite_revision_ans=1, methode="Chaînée", periodicite_facturation="Trimestrielle",
             plafond_annuel_pct=3.5, notes="Bail antérieur à 2014 renouvelé, clause ICC ; plafond contractuel 3,5 %"),
        Bail(id="B04", local="Local 45 bd Haussmann", locataire="Nouveau preneur (à compléter)", date_effet=date(2025, 7, 1),
             loyer_initial_annuel_ht=18000, indice="ILC", trimestre_base="2025-T1", periodicite_revision_ans=1,
             methode="Chaînée", periodicite_facturation="Mensuelle", notes="customer_id Pennylane à créer"),
    ]


def saisies_fictives() -> Saisies:
    s = Saisies()
    # B01 : le client renonce à la hausse 2025 (locataire en difficulté) sans rattrapage
    s.decision[("B01", 4)] = DECISION_GEL_SANS
    s.consigne[("B01", 4)] = "Gel décidé par la gérance le 12/03/2025 (difficultés du preneur). Ne pas facturer la hausse. Courrier d'information envoyé."
    s.applique[("B01", 1)] = s.applique[("B01", 2)] = s.applique[("B01", 3)] = "Oui"
    s.date_application[("B01", 3)] = date(2024, 4, 1)
    s.courrier[("B01", 4)] = date(2025, 3, 20)
    # B03 : gel négocié un an avec rattrapage à la révision suivante
    s.decision[("B03", 6)] = DECISION_GEL_RATTRAPAGE
    s.consigne[("B03", 6)] = "Gel d'un an négocié (avenant du 10/01/2025), rattrapage à la révision 2026 : recalculer sur l'indice 2023-T3."
    for n in range(1, 6):
        s.applique[("B03", n)] = "Oui"
    s.consigne[("B03", 7)] = "Appliquer le rattrapage : coefficient calculé sur 2 ans d'indice. Prévenir le locataire avant facturation."
    return s


def generer_demo(dossier: Path, aujourdhui: date | None = None) -> Path:
    aujourdhui = aujourdhui or date.today()
    societe = Societe(nom="SCI EXEMPLE (DÉMO)", siren="000000000", forme="SCI", demo=True,
                      contact="cabinet – démonstration", adresse="1 place de la Bourse\n75002 Paris",
                      signataire="Mme Claire Exemple", qualite_signataire="Gérante", ville_signature="Paris")
    chemin = dossier / "DEMO_SCI_EXEMPLE_valeurs_fictives.xlsx"
    return construire(societe, baux_fictifs(), indices_fictifs(aujourdhui), chemin, saisies=saisies_fictives(), aujourdhui=aujourdhui)

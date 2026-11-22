"""Portefeuille FICTIF pour faire tourner la console sans Pennylane : dix sociétés bailleresses,
leurs baux, et un état de facturation au format de l'API, semé d'anomalies réalistes.

Tout est déterministe pour une date et une graine données : les tests s'appuient dessus.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from .pennylane_api import normaliser_abonnement, normaliser_facture
from .rapprochement import ECHEANCES_PAR_AN, INTERVALLE_MOIS, ajouter_mois

SOCIETES = [("HALLES", "SCI des Halles", 12), ("PARC", "SCI du Parc", 15), ("QUAIS", "SCI des Quais", 6),
            ("REMPARTS", "SCI des Remparts", 9), ("TILLEULS", "SCI Les Tilleuls", 3), ("MOULIN", "SARL Foncière du Moulin", 14),
            ("BELVEDERE", "SCI Belvédère", 5), ("ORANGERIE", "SCI de l'Orangerie", 4), ("CANAL", "SAS Immobilière du Canal", 8),
            ("VERGER", "SCI du Verger", 2)]
LOCATAIRES = ["Boulangerie Dupont", "Cabinet Martin", "Logistix", "Pharmacie du Centre", "Optique Lumière", "Atelier Vélo",
              "Agence Horizon", "Coiffure Élégance", "Restaurant Le Passage", "Librairie des Arts", "Fleurs & Jardins",
              "Kiné Santé", "Garage Central", "Épicerie Fine", "Studio Graphique", "Notaires Associés", "Cave du Marché",
              "Pressing Net", "Assurances Sud", "Micro-crèche Les Lutins"]
RUES = ["rue de la Paix", "avenue Foch", "place du Marché", "boulevard Carnot", "rue Nationale", "quai des Arts",
        "rue du Commerce", "allée des Tilleuls", "zone d'activité des Landes", "cours Gambetta"]
SCENARIOS = [("ok", 66), ("revision_non_repercutee", 6), ("non_borne", 5), ("impaye", 6), ("brouillon", 4),
             ("terme_echu", 3), ("sans_abonnement", 2), ("echeance_manquante", 2), ("indice_attendu", 2),
             ("suivant_cree", 4)]


class ClientSimule:
    def __init__(self, donnees: dict[str, dict[str, list[dict]]]):
        self.donnees = donnees

    def abonnements(self, dossier: str) -> list[dict]:
        return [normaliser_abonnement(a) for a in self.donnees.get(dossier, {}).get("abonnements", [])]

    def factures(self, dossier: str) -> list[dict]:
        return [normaliser_facture(f) for f in self.donnees.get(dossier, {}).get("factures", [])]


def _anniversaires(effet: date, aujourdhui: date) -> tuple[date, date]:
    """(dernière date anniversaire échue, prochaine)."""
    k = 1
    while ajouter_mois(effet, 12 * (k + 1)) <= aujourdhui:
        k += 1
    derniere = ajouter_mois(effet, 12 * k)
    if derniere > aujourdhui:           # bail de moins d'un an
        return effet, derniere
    return derniere, ajouter_mois(effet, 12 * (k + 1))


def generer(aujourdhui: date, graine: int = 2026) -> tuple[list[dict], dict[str, list[dict]], ClientSimule]:
    """Retourne (dossiers, baux par dossier, client simulé)."""
    rnd = random.Random(graine)
    dossiers, baux_par_dossier, donnees = [], {}, {}
    id_abo, id_fac, id_client = 5000, 90000, 1000
    tirage = [s for s, poids in SCENARIOS for _ in range(poids)]
    for code, nom, nb in SOCIETES:
        dossiers.append({"code": code, "nom": nom, "siren": f"{rnd.randrange(300000000, 899999999)}",
                         "variable_jeton": f"PENNYLANE_TOKEN_{code}"})
        baux, abos, facts = [], [], []
        for i in range(1, nb + 1):
            id_client += 1
            periodicite = rnd.choices(["Mensuelle", "Trimestrielle"], weights=[3, 2])[0]
            n, pas = ECHEANCES_PAR_AN[periodicite], INTERVALLE_MOIS[periodicite]
            effet = date(rnd.randint(2017, 2025), rnd.randint(1, 12), 1)
            if effet >= aujourdhui:
                effet = date(2024, effet.month, 1)
            derniere, prochaine = _anniversaires(effet, aujourdhui)
            initial = float(rnd.randrange(6, 60) * 1000)
            annees = max((derniere - effet).days // 365, 0)
            attendu = round(initial * 1.024 ** annees, 2)
            charges = float(rnd.choice([0, 0, 600, 1200, 2400, 3600]))
            scenario = rnd.choice(tirage) if annees >= 1 else "ok"
            bail = {
                "code": f"B{i:02d}", "local": f"{rnd.randint(1, 80)} {rnd.choice(RUES)}",
                "locataire": rnd.choice(LOCATAIRES), "customer_id": id_client, "subscription_id": None,
                "indice": rnd.choices(["ILC", "ILAT", "ICC"], weights=[6, 3, 1])[0], "periodicite": periodicite,
                "tva_pct": 20.0, "date_effet": effet, "date_fin": ajouter_mois(effet, 108),
                "loyer_initial_annuel": initial, "loyer_attendu_annuel": attendu, "charges_annuelles": charges,
                "date_derniere_revision": derniere if annees >= 1 else None, "date_prochaine_revision": prochaine,
                # indice de la prochaine révision publié si elle est à moins de 4 mois, sauf un bail sur six
                "loyer_prochain_annuel": round(attendu * 1.021, 2)
                if (prochaine - aujourdhui).days <= 120 and id_client % 6 else None,
                "date_revision_suivante": ajouter_mois(prochaine, 12),
                "statut_revision": "✔ Calculable" if annees >= 1 else "À venir", "revisions_non_appliquees": 0,
            }
            echeance = round((attendu + charges) / n, 2)
            ancien = round((attendu / 1.024 + charges) / n, 2)
            debut, montant, borne = derniere, echeance, True
            if scenario == "revision_non_repercutee":
                debut, montant, borne = ajouter_mois(derniere, -12), ancien, False
                bail["revisions_non_appliquees"] = 1
            elif scenario == "non_borne":
                borne = False
            elif scenario == "indice_attendu":
                bail["statut_revision"], bail["loyer_attendu_annuel"] = "⚠ Indice attendu", round(attendu / 1.024, 2)
                montant, debut, borne = ancien, ajouter_mois(derniere, -12), False
                bail["date_derniere_revision"] = ajouter_mois(derniere, -12)
            elif scenario == "terme_echu":
                debut = ajouter_mois(derniere, -12)
            if scenario == "sans_abonnement":
                baux.append(bail)
                continue
            # occurrences de l'abonnement, de son début à aujourd'hui
            occurrences, d = [], debut
            fin_theorique = ajouter_mois(debut, 12 - pas)
            while d <= aujourdhui and (not borne or d <= fin_theorique):
                occurrences.append(d)
                d = ajouter_mois(d, pas)
            termine = borne and fin_theorique < aujourdhui and ajouter_mois(fin_theorique, pas) <= aujourdhui
            id_abo += 1
            abo = {
                "id": id_abo, "customer": {"id": id_client}, "label": f"Loyer {bail['local']}",
                "status": "finished" if termine else "in_progress", "mode": "awaiting_validation",
                "start": debut.isoformat(), "finish": fin_theorique.isoformat() if borne else None,
                "next_occurrence": None if termine else d.isoformat() if not borne or d <= fin_theorique else None,
                "prev_occurrence": occurrences[-1].isoformat() if occurrences else None,
                "recurring_rule": {"rule_type": "monthly", "interval": pas, "count": 12 // pas if borne else None},
                "customer_invoice_data": {"currency": "EUR", "currency_amount_before_tax": f"{montant:.2f}"},
            }
            abos.append(abo)
            if scenario == "suivant_cree" and borne and not termine:
                id_abo += 1
                abos.append({**abo, "id": id_abo, "status": "not_started", "start": prochaine.isoformat(),
                             "finish": ajouter_mois(prochaine, 12 - pas).isoformat(), "prev_occurrence": None,
                             "next_occurrence": prochaine.isoformat(),
                             "customer_invoice_data": {"currency": "EUR",
                                                       "currency_amount_before_tax": f"{echeance * 1.02:.2f}"}})
            for rang, occ in enumerate(reversed(occurrences[-8:])):
                if scenario == "echeance_manquante" and rang == 0:
                    continue
                id_fac += 1
                ttc = round(montant * 1.2, 2)
                statut, restant = "paid", 0.0
                if rang == 0 and scenario == "brouillon" and aujourdhui - occ > timedelta(days=8):
                    statut, restant = "draft", ttc
                elif scenario == "impaye" and rang in (1, 2):
                    statut, restant = "late", ttc
                elif rang == 0 and aujourdhui - occ < timedelta(days=12):
                    statut, restant = "upcoming", ttc
                facts.append({"id": id_fac, "customer": {"id": id_client}, "billing_subscription": {"id": abo["id"]},
                              "invoice_number": "" if statut == "draft" else f"F-{occ:%Y%m}-{id_fac % 1000:03d}",
                              "label": f"Loyer {bail['local']}", "date": occ.isoformat(),
                              "deadline": (occ + timedelta(days=10)).isoformat(), "status": statut,
                              "currency_amount_before_tax": f"{montant:.2f}", "currency_amount": f"{ttc:.2f}",
                              "remaining_amount_with_tax": f"{restant:.2f}"})
            baux.append(bail)
        if code in ("PARC", "MOULIN"):          # un abonnement sans bail au référentiel
            id_abo += 1
            id_client += 1
            abos.append({"id": id_abo, "customer": {"id": id_client}, "label": "Loyer emplacement de parking n° 7",
                         "status": "in_progress", "mode": "finalized", "start": "2025-03-01", "finish": None,
                         "next_occurrence": ajouter_mois(date(aujourdhui.year, aujourdhui.month, 1), 1).isoformat(),
                         "prev_occurrence": None, "recurring_rule": {"rule_type": "monthly", "interval": 1},
                         "customer_invoice_data": {"currency_amount_before_tax": "95.00"}})
        baux_par_dossier[code] = baux
        donnees[code] = {"abonnements": abos, "factures": facts}
    return dossiers, baux_par_dossier, ClientSimule(donnees)


def charger_demo(cx, aujourdhui: date, graine: int = 2026) -> dict:
    """Vide la base et la remplit avec le portefeuille fictif."""
    from . import db, depot
    db.vider(cx)
    dossiers, baux, client = generer(aujourdhui, graine)
    for d in dossiers:
        depot.enregistrer_dossier(cx, d["code"], d["nom"], d["siren"], d["variable_jeton"], source="portefeuille fictif")
        depot.enregistrer_baux(cx, d["code"], baux[d["code"]])
    bilan = depot.synchroniser(cx, client, source="simulation")
    return {"dossiers": len(dossiers), "baux": sum(len(b) for b in baux.values()),
            "abonnements": sum(b.get("abonnements", 0) for b in bilan), "factures": sum(b.get("factures", 0) for b in bilan)}

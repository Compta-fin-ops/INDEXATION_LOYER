"""Lecture de Pennylane : abonnements de facturation et factures clients d'un dossier.

Deux sources interchangeables derrière la même interface :
  - ClientReel    : API Entreprise v2, un jeton par dossier, LECTURE SEULE ;
  - ClientSimule  : jeu de données fictif au même format (console/simulation.py).

État de la vérification du client réel : la forme d'un abonnement est celle de l'OpenAPI
« Create a billing subscription » fournie par le cabinet. La pagination des listes et les
champs exacts d'une facture client N'ONT PAS été vérifiés (documentation inaccessible depuis
l'environnement de développement) : les fonctions `normaliser_*` lisent plusieurs noms de
champs plausibles et sont le seul endroit à corriger au premier essai réel.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator, Protocol

log = logging.getLogger(__name__)
BASE_V2 = "https://app.pennylane.com/api/external/v2"
PAUSE_ENTRE_APPELS = 0.25            # 4 appels par seconde, sous la limite annoncée de 5


class Client(Protocol):
    def abonnements(self, dossier: str) -> list[dict]: ...
    def factures(self, dossier: str) -> list[dict]: ...


def _premier(d: dict, *cles, defaut=None):
    for c in cles:
        v = d
        for part in c.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if v not in (None, ""):
            return v
    return defaut


def _nombre(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normaliser_abonnement(a: dict) -> dict:
    regle = a.get("recurring_rule") or {}
    donnees = a.get("customer_invoice_data") or {}
    return {
        "id": int(a["id"]),
        "customer_id": _premier(a, "customer.id", "customer_id"),
        "label": a.get("label") or donnees.get("label") or donnees.get("pdf_invoice_subject") or "",
        "statut": a.get("status", ""),
        "mode": a.get("mode") if isinstance(a.get("mode"), str) else (a.get("mode") or {}).get("type", ""),
        "debut": a.get("start"),
        "fin": a.get("finish"),
        "prochaine_occurrence": a.get("next_occurrence"),
        "occurrence_precedente": a.get("prev_occurrence"),
        "type_regle": regle.get("rule_type") or regle.get("type") or "monthly",
        "intervalle": int(regle.get("interval") or 1),
        "montant_ht": _nombre(_premier(donnees, "currency_amount_before_tax", "amount_before_tax")),
    }


def normaliser_facture(f: dict) -> dict:
    return {
        "id": int(f["id"]),
        "customer_id": _premier(f, "customer.id", "customer_id"),
        "subscription_id": _premier(f, "billing_subscription.id", "billing_subscription_id"),
        "numero": _premier(f, "invoice_number", "number", defaut=""),
        "libelle": _premier(f, "label", "pdf_invoice_subject", defaut=""),
        "date": _premier(f, "date", "issue_date"),
        "echeance": _premier(f, "deadline", "due_date"),
        "montant_ht": _nombre(_premier(f, "currency_amount_before_tax", "amount_before_tax")),
        "montant_ttc": _nombre(_premier(f, "currency_amount", "amount")),
        "restant_du": _nombre(_premier(f, "remaining_amount_with_tax", "remaining_amount")),
        "statut": _premier(f, "status", defaut="draft" if f.get("draft") else ""),
    }


class ClientReel:
    """API Entreprise v2. Le jeton de chaque dossier est lu dans la variable d'environnement
    déclarée pour ce dossier (table dossiers.variable_jeton) : il n'est jamais stocké."""

    def __init__(self, variables_jeton: dict[str, str], base_url: str = BASE_V2):
        self.variables_jeton = variables_jeton
        self.base_url = base_url.rstrip("/")

    def _jeton(self, dossier: str) -> str:
        variable = self.variables_jeton.get(dossier, "")
        jeton = os.environ.get(variable, "") if variable else ""
        if not jeton:
            raise RuntimeError(f"Dossier {dossier} : variable d'environnement {variable or '(non déclarée)'} absente")
        return jeton

    def _pages(self, dossier: str, chemin: str) -> Iterator[dict]:
        """Parcourt une liste paginée. Gère la pagination par curseur (items, has_more,
        next_cursor) et, à défaut, par numéro de page (total_pages, current_page)."""
        params: dict[str, Any] = {"limit": 100}
        while True:
            url = f"{self.base_url}{chemin}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._jeton(dossier)}",
                                                       "Accept": "application/json"})
            for essai in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        corps = json.loads(resp.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429 and essai < 3:      # limite d'appels : on attend ce que demande l'API
                        time.sleep(float(e.headers.get("retry-after", 2)))
                        continue
                    raise RuntimeError(f"Pennylane a répondu {e.code} sur {chemin} ({dossier})") from e
            lignes = corps.get("items") or corps.get("data") or next(
                (v for v in corps.values() if isinstance(v, list)), [])
            yield from lignes
            if corps.get("has_more") and corps.get("next_cursor"):
                params = {"limit": 100, "cursor": corps["next_cursor"]}
            elif corps.get("total_pages") and corps.get("current_page", 1) < corps["total_pages"]:
                params = {"limit": 100, "page": corps.get("current_page", 1) + 1}
            else:
                return
            time.sleep(PAUSE_ENTRE_APPELS)

    def abonnements(self, dossier: str) -> list[dict]:
        return [normaliser_abonnement(a) for a in self._pages(dossier, "/billing_subscriptions")]

    def factures(self, dossier: str) -> list[dict]:
        return [normaliser_facture(f) for f in self._pages(dossier, "/customer_invoices")]

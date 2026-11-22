"""Lecture de Pennylane : abonnements de facturation et factures clients d'un dossier.

Deux sources interchangeables derrière la même interface :
  - ClientReel    : API Entreprise v2, un jeton par dossier, LECTURE SEULE ;
  - ClientSimule  : jeu de données fictif au même format (console/simulation.py).

État de la vérification du client réel, contre les OpenAPI fournies par le cabinet :
  - abonnements : « List billing subscriptions » (pagination par curseur items / has_more /
    next_cursor, limite de 100, champs et valeurs nulles), conforme ;
  - factures : « List customer invoices » (mêmes règles de pagination, champs lus ci-dessous,
    filtre sur la date), conforme. La liste contient aussi les avoirs, les proformas et les devis :
    ils sont collectés avec leur statut et écartés du rapprochement.
Reste inconnu : le signe des montants d'un avoir. Aucun appel réel n'a encore été exécuté.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
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
    """Champs de « List customer invoices ». `customer` et `billing_subscription` peuvent être nuls,
    `label` et `remaining_amount_with_tax` aussi. Les montants sont des chaînes."""
    return {
        "id": int(f["id"]),
        "customer_id": _premier(f, "customer.id"),
        "subscription_id": _premier(f, "billing_subscription.id"),
        "numero": f.get("invoice_number") or "",
        "libelle": f.get("label") or f.get("pdf_invoice_subject") or "",
        "date": f.get("date"),
        "echeance": f.get("deadline"),
        "montant_ht": _nombre(f.get("currency_amount_before_tax")),
        "montant_ttc": _nombre(f.get("currency_amount")),
        "restant_du": 0.0 if f.get("paid") else _nombre(f.get("remaining_amount_with_tax")),
        "statut": f.get("status") or ("draft" if f.get("draft") else ""),
    }


class ClientReel:
    """API Entreprise v2. Le jeton de chaque dossier est lu dans la variable d'environnement
    déclarée pour ce dossier (table dossiers.variable_jeton) : il n'est jamais stocké."""

    def __init__(self, variables_jeton: dict[str, str], base_url: str = BASE_V2, profondeur_mois: int = 36):
        self.variables_jeton = variables_jeton
        self.base_url = base_url.rstrip("/")
        self.profondeur_mois = profondeur_mois           # factures plus anciennes : non collectées

    def _jeton(self, dossier: str) -> str:
        variable = self.variables_jeton.get(dossier, "")
        jeton = os.environ.get(variable, "") if variable else ""
        if not jeton:
            raise RuntimeError(f"Dossier {dossier} : variable d'environnement {variable or '(non déclarée)'} absente")
        return jeton

    def _pages(self, dossier: str, chemin: str, filtre: list[dict] | None = None) -> Iterator[dict]:
        """Parcourt une liste paginée. Gère la pagination par curseur (items, has_more,
        next_cursor) et, à défaut, par numéro de page (total_pages, current_page)."""
        fixes: dict[str, Any] = {"limit": 100}
        if filtre:
            fixes["filter"] = json.dumps(filtre)
        params = dict(fixes)
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
                params = {**fixes, "cursor": corps["next_cursor"]}
            elif corps.get("total_pages") and corps.get("current_page", 1) < corps["total_pages"]:
                params = {**fixes, "page": corps.get("current_page", 1) + 1}
            else:
                return
            time.sleep(PAUSE_ENTRE_APPELS)

    def abonnements(self, dossier: str) -> list[dict]:
        return [normaliser_abonnement(a) for a in self._pages(dossier, "/billing_subscriptions")]

    def factures(self, dossier: str) -> list[dict]:
        aujourdhui = date.today()
        mois = aujourdhui.year * 12 + aujourdhui.month - 1 - self.profondeur_mois
        depuis = date(mois // 12, mois % 12 + 1, 1)
        filtre = [{"field": "date", "operator": "gteq", "value": depuis.isoformat()}]
        return [normaliser_facture(f) for f in self._pages(dossier, "/customer_invoices", filtre)]

"""Lecture de Pennylane : abonnements de facturation et factures clients d'un dossier.

Deux sources interchangeables derrière la même interface :
  - ClientReel    : API Entreprise v2, un jeton par dossier, LECTURE SEULE ;
  - ClientSimule  : jeu de données fictif au même format (console/simulation.py).

État de la vérification du client réel, contre les OpenAPI fournies par le cabinet :
  - abonnements : « List billing subscriptions » (pagination par curseur items / has_more /
    next_cursor, limite de 100, champs et valeurs nulles), conforme ;
  - factures : « List customer invoices » (mêmes règles de pagination, champs lus ci-dessous,
    filtre sur la date), conforme. La liste contient aussi les avoirs, les proformas et les devis :
    ils sont collectés avec leur statut et écartés du rapprochement ;
  - lignes et sections d'un abonnement : « List invoice lines for a billing subscription » et
    « List the invoice line sections of a billing subscription », conformes, jamais exécutées.
Reste inconnu : le signe des montants d'un avoir.
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

from . import jetons

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


def _remise(r) -> dict | None:
    """Remise d'un abonnement ou d'une ligne, seulement si elle n'est pas nulle."""
    if not isinstance(r, dict) or _nombre(r.get("value")) == 0:
        return None
    return {"type": r.get("type") or "absolute", "value": str(r["value"])}


def normaliser_ligne(l: dict) -> dict:
    """Ligne de « List invoice lines for a billing subscription ». Quantité et prix sont des chaînes ;
    `unit`, `product`, `section_rank` peuvent être nuls."""
    return {"label": l.get("label") or "", "unit": l.get("unit") or "", "quantite": _nombre(l.get("quantity")) or 1.0,
            "prix": str(l.get("raw_currency_unit_price") or "0"), "tva": l.get("vat_rate") or "",
            "description": l.get("description") or "", "produit": _premier(l, "product.id"),
            "section": l.get("section_rank"), "remise": _remise(l.get("discount")),
            "montant_ht": _nombre(l.get("currency_amount_before_tax"))}


def parametres_abonnement(a: dict) -> dict:
    """Ce qu'un nouvel abonnement ou une facture peut reprendre de celui-ci. Les lignes se lisent à part."""
    donnees = a.get("customer_invoice_data") or {}
    return {"conditions": a.get("payment_conditions") or "", "paiement": a.get("payment_method") or "",
            "devise": donnees.get("currency") or "", "langue": donnees.get("language") or "",
            "modele": _premier(donnees, "customer_invoice_template.id"),
            "objet": donnees.get("pdf_invoice_subject") or "", "texte_libre": donnees.get("pdf_invoice_free_text") or "",
            "description": donnees.get("pdf_description") or "", "mention": donnees.get("special_mention") or "",
            "remise": _remise(donnees.get("discount")), "lignes": None, "sections": []}


def normaliser_abonnement(a: dict) -> dict:
    regle = a.get("recurring_rule") or {}
    donnees = a.get("customer_invoice_data") or {}
    return {
        "parametres": parametres_abonnement(a),
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
        "reference": f.get("external_reference") or "",
    }


def marques_sandbox(corps, chemin: str = "") -> list[str]:
    """Chemins des valeurs de la réponse qui commencent par « sandbox » (nom du dossier, immatriculation…)."""
    trouvees: list[str] = []
    if isinstance(corps, dict):
        for cle, valeur in corps.items():
            trouvees += marques_sandbox(valeur, f"{chemin}.{cle}" if chemin else str(cle))
    elif isinstance(corps, list):
        for i, valeur in enumerate(corps):
            trouvees += marques_sandbox(valeur, f"{chemin}[{i}]")
    elif isinstance(corps, str) and corps.strip().lower().startswith("sandbox"):
        trouvees.append(f"{chemin} = {corps}")
    return trouvees


def a_detailler(abos: list[dict]) -> list[dict]:
    """Abonnements dont on lit les lignes : ceux qui ne sont pas terminés, et le dernier terminé de chaque client.
    Ce sont les seuls dont la console reprend les paramètres ; les autres ne coûtent aucun appel."""
    retenus = [a for a in abos if a.get("statut") not in ("finished", "stopped")]
    derniers: dict = {}
    for a in abos:
        if a.get("statut") in ("finished", "stopped"):
            d = derniers.get(a.get("customer_id"))
            if d is None or (a.get("fin") or "") > (d.get("fin") or ""):
                derniers[a.get("customer_id")] = a
    return retenus + list(derniers.values())


class ClientReel:
    """API Entreprise v2. Le jeton de chaque dossier est lu dans une variable d'environnement, à défaut
    dans le coffre du système (console/jetons.py), sous le nom porté par dossiers.variable_jeton.
    Il n'est jamais dans la base."""

    def __init__(self, variables_jeton: dict[str, str], base_url: str = BASE_V2, profondeur_mois: int = 36):
        self.variables_jeton = variables_jeton
        self.base_url = base_url.rstrip("/")
        self.profondeur_mois = profondeur_mois           # factures plus anciennes : non collectées

    def _jeton(self, dossier: str) -> str:
        variable = self.variables_jeton.get(dossier, "")
        jeton = jetons.lire(variable)                    # variable d'environnement, sinon coffre du système
        if not jeton:
            raise RuntimeError(f"Dossier {dossier} : aucun jeton Pennylane enregistré pour ce dossier sur ce poste")
        return jeton

    def _pages(self, dossier: str, chemin: str, filtre: list[dict] | None = None, tri: str = "") -> Iterator[dict]:
        """Parcourt une liste paginée. Gère la pagination par curseur (items, has_more,
        next_cursor) et, à défaut, par numéro de page (total_pages, current_page)."""
        fixes: dict[str, Any] = {"limit": 100}
        if filtre:
            fixes["filter"] = json.dumps(filtre)
        if tri:
            fixes["sort"] = tri
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
                    try:                                 # le motif donné par Pennylane aide au premier essai réel
                        detail = json.loads(e.read().decode("utf-8"))
                        motif = str(detail.get("error") or detail.get("message") or "")[:200]
                    except Exception:
                        motif = ""
                    raise RuntimeError(f"Pennylane a répondu {e.code} sur {chemin}" + (f" : {motif}" if motif else "")) from e
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

    def _lire(self, dossier: str, chemin: str, params: dict | None = None) -> tuple[int | None, Any, str]:
        """Une seule lecture, sans rien lever : (code HTTP ou None, corps décodé ou None, message d'erreur)."""
        url = f"{self.base_url}{chemin}" + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._jeton(dossier)}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8")), ""
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8"))
                message = str(detail.get("error") or detail.get("message") or detail)[:300]
            except Exception:
                message = e.reason if isinstance(e.reason, str) else ""
            return e.code, None, message
        except Exception as e:                           # réseau, délai dépassé, réponse illisible
            return None, None, f"{type(e).__name__} : {e}"[:300]

    def facture_existe(self, dossier: str, reference: str) -> bool | None:
        """Une facture porte-t-elle déjà cette référence externe ? None si Pennylane n'a pas répondu clairement.

        Le filtre `external_reference` (opérateur `eq`) est celui de l'OpenAPI « List customer invoices »."""
        filtre = json.dumps([{"field": "external_reference", "operator": "eq", "value": reference}])
        code, corps, _ = self._lire(dossier, "/customer_invoices", {"limit": 1, "filter": filtre})
        if code != 200 or not isinstance(corps, dict) or not isinstance(corps.get("items"), list):
            return None
        return bool(corps["items"])

    def tester_connexion(self, dossier: str) -> dict:
        """Deux lectures, aucune écriture : l'identité du jeton (GET /me), puis une page d'abonnements.

        `sandbox` est vrai si l'identité renvoyée porte la marque d'un environnement de test Pennylane :
        le centre d'aide indique qu'il est créé sous le nom « Sandbox - votre email ». Le schéma de /me
        n'ayant pas été vérifié, la recherche porte sur toutes les valeurs de la réponse, affichée telle quelle."""
        bilan: dict = {"variable": self.variables_jeton.get(dossier, ""), "jeton_present": False, "sandbox": False,
                       "marques": [], "identite": None, "me": None, "abonnements": None}
        try:
            self._jeton(dossier)
        except RuntimeError as exc:
            bilan["erreur"] = str(exc)
            return bilan
        bilan["jeton_present"] = True
        code, corps, message = self._lire(dossier, "/me")
        bilan["me"] = {"code": code, "message": message}
        if corps is not None:
            bilan["identite"] = corps
            bilan["marques"] = marques_sandbox(corps)
            bilan["sandbox"] = bool(bilan["marques"])
        code, corps, message = self._lire(dossier, "/billing_subscriptions", {"limit": 1})
        bilan["abonnements"] = {"code": code, "message": message,
                                "lisible": isinstance(corps, dict) and isinstance(corps.get("items"), list)}
        return bilan

    def clients(self, dossier: str, maximum: int = 300) -> tuple[list[dict], bool]:
        """Clients du dossier dans Pennylane : (liste, tronquée ?). Lecture seule.

        Sert à retrouver l'identifiant numérique à inscrire sur un bail. La route est déduite de l'adresse
        `…/customers/42` que renvoient les abonnements ; le schéma de la liste n'a pas été vérifié, d'où une
        lecture tolérante du nom."""
        sortie: list[dict] = []
        for c in self._pages(dossier, "/customers"):
            if len(sortie) >= maximum:
                return sortie, True
            nom = _premier(c, "name", "label", "company_name") or " ".join(
                str(c.get(k) or "") for k in ("first_name", "last_name")).strip()
            courriels = c.get("emails") if isinstance(c.get("emails"), list) else [c.get("email")] if c.get("email") else []
            sortie.append({"id": c.get("id"), "nom": nom or "(sans nom)", "courriel": ", ".join(str(x) for x in courriels[:2]),
                           "immatriculation": _premier(c, "reg_no", "registration_number", defaut="")})
        return sortie, False

    def abonnements(self, dossier: str) -> list[dict]:
        """Abonnements du dossier. Les lignes de facture ne sont pas dans la liste : elles se lisent abonnement
        par abonnement, et seulement pour ceux qui peuvent servir de modèle (voir `a_detailler`)."""
        abos = [normaliser_abonnement(a) for a in self._pages(dossier, "/billing_subscriptions")]
        for a in a_detailler(abos):
            p = a["parametres"]
            try:                                         # tri croissant : l'ordre des lignes de la facture
                p["lignes"] = [normaliser_ligne(l) for l in self._pages(
                    dossier, f"/billing_subscriptions/{a['id']}/invoice_lines", tri="id")]
                if any(l["section"] is not None for l in p["lignes"]):
                    p["sections"] = [{"title": s.get("title") or "", "description": s.get("description") or "", "rank": s.get("rank")}
                                     for s in self._pages(dossier, f"/billing_subscriptions/{a['id']}/invoice_line_sections", tri="id")]
            except RuntimeError as exc:                  # les lignes manquent : l'abonnement reste lu, sans modèle de lignes
                log.warning("Lignes de l'abonnement %s illisibles : %s", a["id"], exc)
                p["lignes"], p["erreur_lignes"] = None, str(exc)
            time.sleep(PAUSE_ENTRE_APPELS)
        return abos

    def factures(self, dossier: str) -> list[dict]:
        aujourdhui = date.today()
        mois = aujourdhui.year * 12 + aujourdhui.month - 1 - self.profondeur_mois
        depuis = date(mois // 12, mois % 12 + 1, 1)
        filtre = [{"field": "date", "operator": "gteq", "value": depuis.isoformat()}]
        return [normaliser_facture(f) for f in self._pages(dossier, "/customer_invoices", filtre)]

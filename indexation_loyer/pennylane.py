"""Préparation (et, à terme, envoi) des abonnements de facturation Pennylane.

Cible : ``POST {base_url}/billing_subscriptions`` — un abonnement par bail, au
loyer *actuel* (dernière révision calculable), ligne loyer + ligne charges.

Sécurités :
  - par défaut, ``--dry-run`` : les corps JSON sont écrits dans ``out/`` et rien
    n'est envoyé ;
  - l'envoi réel exige ``schema_valide: true`` dans ``config/pennylane_mapping.json``
    (à basculer après relecture de la documentation) et un jeton dans la variable
    d'environnement ``PENNYLANE_API_TOKEN`` ;
  - un bail sans ``customer_id`` Pennylane est ignoré avec un avertissement.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .baux import Bail
from .calcul import calculer, loyer_actuel, table_indices
from .insee import Observation

log = logging.getLogger(__name__)
RACINE = Path(__file__).resolve().parent.parent
CHEMIN_MAPPING = RACINE / "config" / "pennylane_mapping.json"


@dataclass
class Abonnement:
    bail_id: str
    locataire: str
    corps: dict[str, Any]
    avertissements: list[str]


def charger_mapping(chemin: Path = CHEMIN_MAPPING) -> dict:
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def premier_du_mois_suivant(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _remplir(gabarit: Any, valeurs: dict[str, Any]) -> Any:
    """Remplace les ``{jetons}`` par les valeurs typées ; supprime les champs dont la valeur est None."""
    if isinstance(gabarit, dict):
        out = {}
        for k, v in gabarit.items():
            r = _remplir(v, valeurs)
            if r is not None:
                out[k] = r
        return out
    if isinstance(gabarit, list):
        return [_remplir(v, valeurs) for v in gabarit]
    if isinstance(gabarit, str) and gabarit.startswith("{") and gabarit.endswith("}") and gabarit[1:-1] in valeurs:
        return valeurs[gabarit[1:-1]]
    return gabarit


def construire_abonnement(bail: Bail, observations: list[Observation], mapping: dict,
                          aujourdhui: date | None = None) -> Abonnement:
    aujourdhui = aujourdhui or date.today()
    avert: list[str] = []
    lignes_rev = calculer(bail, table_indices(observations), horizon=bail.date_fin or aujourdhui, aujourdhui=aujourdhui)
    annuel = loyer_actuel(bail, lignes_rev, aujourdhui)
    n = bail.echeances_par_an
    en_attente = [l for l in lignes_rev if l.statut == "⚠ Indice attendu"]
    if en_attente:
        avert.append(f"{len(en_attente)} révision(s) échue(s) sans indice publié : loyer figé au dernier calculable")
    if not bail.pennylane_customer_id:
        avert.append("customer_id Pennylane manquant : abonnement non envoyable")

    taux = f"{bail.tva_pct:g}"
    vat = mapping["vat_rate_par_taux"].get(taux)
    if vat is None:
        avert.append(f"Taux de TVA {taux} % sans code Pennylane connu")
        vat = "exempt"
    valeurs = {
        "customer_id": int(bail.pennylane_customer_id) if bail.pennylane_customer_id.isdigit() else bail.pennylane_customer_id or None,
        "date_debut": premier_du_mois_suivant(aujourdhui).isoformat(),
        "recurrence": mapping["recurrence_par_periodicite"].get(bail.periodicite_facturation),
        "libelle": f"Loyer {bail.local} – échéance {bail.periodicite_facturation.lower()}",
        "prix_unitaire_ht": round(annuel / n, 2),
        "charges_ht": round(bail.charges_annuelles_ht / n, 2),
        "vat_rate": vat,
        "product_id": int(bail.pennylane_product_id) if bail.pennylane_product_id.isdigit() else (bail.pennylane_product_id or None),
    }
    lignes = [_remplir(mapping["ligne_loyer"], valeurs)]
    if bail.charges_annuelles_ht:
        lignes.append(_remplir(mapping["ligne_charges"], valeurs))
    valeurs["lignes"] = lignes
    corps = _remplir(mapping["corps"], valeurs)
    return Abonnement(bail.id, bail.locataire, corps, avert)


def ecrire_dry_run(abonnements: list[Abonnement], dossier: Path, societe: str) -> list[Path]:
    dossier.mkdir(parents=True, exist_ok=True)
    chemins = []
    for a in abonnements:
        p = dossier / f"{_slug(societe)}__{_slug(a.bail_id)}__billing_subscription.json"
        p.write_text(json.dumps({"_bail": a.bail_id, "_locataire": a.locataire, "_avertissements": a.avertissements,
                                 "body": a.corps}, ensure_ascii=False, indent=2), encoding="utf-8")
        chemins.append(p)
    return chemins


def envoyer(abonnements: list[Abonnement], mapping: dict, token: str, timeout: int = 60) -> list[tuple[str, int, str]]:
    if not mapping.get("schema_valide"):
        raise RuntimeError("Envoi refusé : config/pennylane_mapping.json -> schema_valide est false. "
                           "Valider le schéma sur la documentation Pennylane puis basculer à true.")
    url = mapping["base_url"].rstrip("/") + mapping["endpoint"]
    resultats = []
    for a in abonnements:
        if any("customer_id" in m for m in a.avertissements):
            log.warning("Bail %s ignoré (customer_id manquant)", a.bail_id)
            continue
        data = json.dumps(a.corps).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            mapping["auth_header"]: mapping["auth_prefix"] + token,
            "Content-Type": "application/json", "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resultats.append((a.bail_id, resp.status, resp.read().decode("utf-8", "replace")))
        except urllib.error.HTTPError as e:
            resultats.append((a.bail_id, e.code, e.read().decode("utf-8", "replace")))
    return resultats


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_")

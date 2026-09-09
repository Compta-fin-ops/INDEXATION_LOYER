"""Abonnements de facturation Pennylane – POST /api/external/v2/billing_subscriptions.

Schéma : OpenAPI « Company V2 » fournie par le cabinet (voir config/pennylane_mapping.json).
Un abonnement par bail, au loyer *actuel* (dernière révision effective, gels compris) :
ligne loyer + ligne provision sur charges, mention d'indexation sur la facture.

Sécurités :
  - dry-run par défaut : les corps JSON sont écrits dans ``out/``, rien n'est envoyé ;
  - l'envoi exige ``PENNYLANE_API_TOKEN`` et un ``customer_id`` par bail ;
  - un bail dont « Pennylane subscription_id » est déjà renseigné est ignoré à l'envoi
    (sauf ``--remplacer``), pour ne jamais créer deux abonnements actifs ;
  - après un 201, l'identifiant renvoyé est inscrit dans la feuille Baux.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .baux import Bail
from .calcul import STATUT_ATTENDU, calculer, loyer_actuel, table_indices
from .insee import Observation

log = logging.getLogger(__name__)
RACINE = Path(__file__).resolve().parent.parent
CHEMIN_MAPPING = RACINE / "config" / "pennylane_mapping.json"


@dataclass
class ReglagesPennylane:
    """Réglages communs à la société (feuille Société)."""
    mode: str = "awaiting_validation"
    payment_conditions: str = "upon_receipt"
    payment_method: str = "offline"


@dataclass
class Abonnement:
    bail_id: str
    locataire: str
    corps: dict[str, Any]
    avertissements: list[str] = field(default_factory=list)
    subscription_id_existant: str = ""

    @property
    def envoyable(self) -> bool:
        return not any(a.startswith("customer_id") for a in self.avertissements)


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


def _entier(s: str) -> int | str | None:
    s = (s or "").strip()
    if not s:
        return None
    return int(float(s)) if s.replace(".", "", 1).isdigit() else s


def construire_abonnement(bail: Bail, observations: list[Observation], mapping: dict,
                          reglages: ReglagesPennylane | None = None, aujourdhui: date | None = None,
                          decisions: dict[tuple[str, int], str] | None = None) -> Abonnement:
    aujourdhui = aujourdhui or date.today()
    reglages = reglages or ReglagesPennylane()
    avert: list[str] = []
    lignes_rev = calculer(bail, table_indices(observations), horizon=bail.date_fin or aujourdhui,
                          aujourdhui=aujourdhui, decisions=decisions)
    annuel = loyer_actuel(bail, lignes_rev, aujourdhui)
    n = bail.echeances_par_an
    en_attente = [l for l in lignes_rev if l.statut == STATUT_ATTENDU]
    if en_attente:
        avert.append(f"{len(en_attente)} révision(s) échue(s) sans indice publié : loyer figé au dernier connu")
    if not bail.pennylane_customer_id:
        avert.append("customer_id Pennylane manquant : abonnement non envoyable")
    for champ, val in (("mode", reglages.mode), ("payment_conditions", reglages.payment_conditions), ("payment_method", reglages.payment_method)):
        if val not in mapping["enums"][champ]:
            avert.append(f"{champ} = {val!r} hors énumération Pennylane {mapping['enums'][champ]}")
    if reglages.mode == "email":
        avert.append("mode « email » : exige email_settings.recipients et un modèle d'e-mail Pennylane ; non géré, préférer awaiting_validation")

    taux = f"{bail.tva_pct:g}"
    vat = mapping["vat_rate_par_taux"].get(taux)
    if vat is None:
        avert.append(f"Taux de TVA {taux} % sans code Pennylane connu")
        vat = "exempt"
    derniere = next((l for l in reversed(lignes_rev) if l.echeance.date_revision <= aujourdhui and l.loyer_retenu is not None), None)
    if derniere is None:
        mention = f"Loyer indexé sur l'{bail.indice} (indice de base {bail.trimestre_base})."
    else:
        mention = (f"Loyer indexé sur l'{bail.indice} – révision du {derniere.echeance.date_revision:%d/%m/%Y} "
                   f"(indice {derniere.echeance.trimestre_reference}"
                   + (f" : {derniere.indice_reference:g}" if derniere.indice_reference else "") + ").")
    valeurs = {
        "customer_id": _entier(bail.pennylane_customer_id),
        "label": f"Loyer {bail.local}",
        "libelle_loyer": f"Loyer {bail.periodicite_facturation.lower()} – {bail.local}",
        "date_debut": premier_du_mois_suivant(aujourdhui).isoformat(),
        "mode": reglages.mode, "payment_conditions": reglages.payment_conditions, "payment_method": reglages.payment_method,
        "recurring_rule": mapping["recurring_rule_par_periodicite"].get(bail.periodicite_facturation),
        "unite": mapping["unite_par_periodicite"].get(bail.periodicite_facturation, "mois"),
        "prix_unitaire_ht": f"{annuel / n:.2f}",             # chaîne, cf. schéma raw_currency_unit_price
        "charges_ht": f"{bail.charges_annuelles_ht / n:.2f}",
        "vat_rate": vat,
        "product_id": _entier(bail.pennylane_product_id),
        "mention_indexation": mention,
    }
    lignes = [_remplir(mapping["ligne_loyer"], valeurs)]
    if bail.charges_annuelles_ht:
        lignes.append(_remplir(mapping["ligne_charges"], valeurs))
    valeurs["lignes"] = lignes
    corps = _remplir(mapping["corps"], valeurs)
    return Abonnement(bail.id, bail.locataire, corps, avert, subscription_id_existant=bail.pennylane_subscription_id)


def ecrire_dry_run(abonnements: list[Abonnement], dossier: Path, societe: str) -> list[Path]:
    dossier.mkdir(parents=True, exist_ok=True)
    chemins = []
    for a in abonnements:
        p = dossier / f"{_slug(societe)}__{_slug(a.bail_id)}__billing_subscription.json"
        p.write_text(json.dumps({"_bail": a.bail_id, "_locataire": a.locataire, "_avertissements": a.avertissements,
                                 "_subscription_id_existant": a.subscription_id_existant or None, "body": a.corps},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        chemins.append(p)
    return chemins


def envoyer(abonnements: list[Abonnement], mapping: dict, token: str, remplacer: bool = False,
            timeout: int = 60) -> list[tuple[str, int, str, str]]:
    """POST chaque abonnement envoyable. Retourne (bail_id, statut HTTP, id créé ou "", réponse brute)."""
    if not mapping.get("schema_valide"):
        raise RuntimeError("Envoi refusé : config/pennylane_mapping.json -> schema_valide est false.")
    url = mapping["base_url"].rstrip("/") + mapping["endpoint"]
    resultats = []
    for a in abonnements:
        if not a.envoyable:
            log.warning("Bail %s ignoré (customer_id manquant)", a.bail_id)
            continue
        if a.subscription_id_existant and not remplacer:
            log.warning("Bail %s ignoré : abonnement %s déjà enregistré (utiliser --remplacer après avoir arrêté l'ancien dans Pennylane)",
                        a.bail_id, a.subscription_id_existant)
            continue
        req = urllib.request.Request(url, data=json.dumps(a.corps).encode("utf-8"), method="POST", headers={
            mapping["auth_header"]: mapping["auth_prefix"] + token,
            "Content-Type": "application/json", "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                brut = resp.read().decode("utf-8", "replace")
                try:
                    cree = str(json.loads(brut).get("id", ""))
                except ValueError:
                    cree = ""
                resultats.append((a.bail_id, resp.status, cree, brut))
        except urllib.error.HTTPError as e:
            resultats.append((a.bail_id, e.code, "", e.read().decode("utf-8", "replace")))
    return resultats


def enregistrer_subscription_ids(chemin_classeur: Path, ids: dict[str, str]) -> int:
    """Inscrit les identifiants d'abonnement créés dans la colonne « Pennylane subscription_id » de Baux."""
    from .workbook import B_SUBSCR, ecrire_colonne_baux
    return ecrire_colonne_baux(chemin_classeur, ids, B_SUBSCR)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_")

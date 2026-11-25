"""Les deux seules écritures de la console dans Pennylane : créer un abonnement, créer une facture en brouillon.

Règle de construction : ne rien faire qui puisse mettre des données en danger.
  - La console ne contient aucun code de modification, d'arrêt ni de suppression. Elle ne sait que
    CRÉER : un abonnement nouveau, ou une facture nouvelle. Les données existantes ne sont jamais touchées.
  - Tout ce qu'elle crée reste à valider dans Pennylane : les factures d'un abonnement naissent en
    brouillon (mode « awaiting_validation » imposé ici, réglable nulle part), une facture de rappel
    est créée avec `draft: true` imposé de même.
  - L'écriture est désactivée par défaut. Elle exige CONSOLE_ECRITURE=oui ET un jeton d'écriture
    distinct du jeton de lecture (nom du jeton de lecture suivi de _ECRITURE).
  - Un envoi n'a lieu que si TOUS les contrôles bloquants passent, sur des données relues à l'instant.
  - Chaque tentative est journalisée avant l'appel, avec le corps exact et son empreinte.
  - Un bail à la fois, après confirmation saisie à la main. Aucun envoi en lot.
  - Un seul point du code émet vers Pennylane : la fonction `_poster`, en POST.

État de vérification : aucun appel réel d'écriture n'a été exécuté. Les schémas d'entrée sont ceux des
OpenAPI « Create a billing subscription » et « Create a customer invoice » fournies par le cabinet.
Le premier ne permet pas de demander que l'ABONNEMENT lui-même naisse en brouillon : son statut de
création est lu dans la réponse et affiché, à constater sur un environnement de test.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import jetons, referentiel
from .pennylane_api import BASE_V2
from .rapprochement import INTERVALLE_MOIS, SituationBail, _d, _eur, _fr

MODE_IMPOSE = "awaiting_validation"
VARIATION_MAX = 0.15              # écart toléré avec le montant de l'abonnement précédent
FRAICHEUR_MAX = timedelta(hours=24)
MOIS_MAX = 12                     # un abonnement ne couvre jamais plus d'une période annuelle


@dataclass
class Controle:
    libelle: str
    ok: bool
    detail: str = ""
    bloquant: bool = True


def ecriture_activee() -> bool:
    return os.environ.get("CONSOLE_ECRITURE", "").strip().lower() == "oui"


def variable_jeton_ecriture(variable_lecture: str) -> str:
    return f"{variable_lecture}_ECRITURE" if variable_lecture else ""


def empreinte(corps: dict) -> str:
    """Empreinte du corps exact : ce qui a été affiché est ce qui est envoyé, sinon l'envoi est refusé."""
    return hashlib.sha256(json.dumps(corps, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def cle_idempotence(dossier: str, bail_code: str, debut: str) -> str:
    return f"{dossier}|{bail_code}|{debut}"


def imposer_brouillon(corps: dict) -> dict:
    """Retourne le corps avec le mode des factures forcé à brouillon, quoi qu'il contienne."""
    return {**corps, "mode": {"type": MODE_IMPOSE}}


def controler(sit: SituationBail, proposition: dict, dossier: dict, envois: list[dict], aujourdhui: date,
              simulation: bool, maintenant: datetime | None = None) -> list[Controle]:
    """Tous les contrôles préalables à la création. Un seul contrôle bloquant en échec interdit l'envoi."""
    maintenant = maintenant or datetime.now()
    bail, corps = sit.bail, proposition["corps"]
    c: list[Controle] = []
    debut = _d(proposition["debut"])
    regle = corps.get("recurring_rule", {})
    pas = INTERVALLE_MOIS.get(bail.get("periodicite"), 1)
    lignes = corps.get("customer_invoice_data", {}).get("invoice_lines", [])
    montant = round(sum(float(l["raw_currency_unit_price"]) * l.get("quantity", 1) for l in lignes), 2)

    if simulation:
        c.append(Controle("Mode simulation", True, "Aucun appel à Pennylane : l'envoi est joué dans la base locale."))
    else:
        variable = variable_jeton_ecriture(dossier.get("variable_jeton", ""))
        c.append(Controle("Écriture activée sur ce poste", ecriture_activee(),
                          "Variable CONSOLE_ECRITURE=oui" + ("" if ecriture_activee() else " absente : la console reste en lecture seule.")))
        c.append(Controle("Jeton d'écriture distinct du jeton de lecture", bool(jetons.lire(variable)),
                          f"Jeton {variable or '(dossier sans nom de jeton)'}"))
        synchro = dossier.get("derniere_synchro")
        frais = bool(synchro) and maintenant - datetime.fromisoformat(synchro) <= FRAICHEUR_MAX
        c.append(Controle("État Pennylane relu depuis moins de 24 heures", frais,
                          f"Dernière collecte : {synchro or 'jamais'}"))

    c.append(Controle("Client Pennylane identifié", isinstance(corps.get("customer_id"), int),
                      f"customer_id = {corps.get('customer_id')}"))
    c.append(Controle("Factures générées en brouillon", corps.get("mode") == {"type": MODE_IMPOSE},
                      "Mode imposé : chaque facture devra être validée dans Pennylane avant tout envoi au locataire."))
    c.append(Controle("Début strictement postérieur à aujourd'hui", bool(debut and debut > aujourdhui),
                      f"Début le {_fr(debut)} : rien n'est généré avant cette date, ce qui laisse le temps de vérifier."))
    count = regle.get("count")
    borne = isinstance(count, int) and 0 < count * pas <= MOIS_MAX
    c.append(Controle("Abonnement borné à une période d'indexation", borne,
                      f"{count} occurrence(s), dernière le {_fr(_d(proposition.get('derniere_occurrence')))}" if count
                      else "Aucun nombre d'occurrences : un abonnement sans terme est refusé."))
    c.append(Controle("Taux de TVA reconnu", proposition.get("tva_connue", True),
                      "Code Pennylane " + str(lignes[0].get("vat_rate")) + "." if lignes and proposition.get("tva_connue", True) else
                      f"Le taux de {bail.get('tva_pct')} % du bail n'a pas de code Pennylane connu de la console : "
                      "corriger le taux du bail. Aucune facture n'est créée « exonérée » par défaut."))
    c.append(Controle("Loyer de la période connu", bool(proposition.get("loyer_connu")),
                      ("Loyer révisé de " + _eur(proposition["loyer_annuel"]) + " par an, calculé sur l'indice publié."
                       if proposition.get("periode_revisee") else "Loyer en vigueur, aucune révision avant le terme proposé.")
                      if proposition.get("loyer_connu") else
                      "L'indice de la révision n'est pas publié : la console ne crée pas d'abonnement à un loyer "
                      "qu'elle ne connaît pas."))

    fin_courant = _d(sit.courant.get("fin")) if sit.courant else None
    libre = sit.suivant is None and (sit.courant is None or (fin_courant is not None and debut is not None and fin_courant < debut))
    if sit.suivant is not None:
        detail = f"L'abonnement n° {sit.suivant['id']} est déjà programmé au {_fr(_d(sit.suivant.get('debut')))}."
    elif sit.courant is not None and not libre:
        detail = (f"L'abonnement n° {sit.courant['id']} " + ("n'a pas de terme" if fin_courant is None else f"court jusqu'au {_fr(fin_courant)}")
                  + ". La console n'arrête jamais un abonnement : le clore dans Pennylane, puis actualiser.")
    else:
        detail = "Aucun abonnement ne couvre la période proposée."
    c.append(Controle("Aucun chevauchement avec un abonnement existant", libre, detail))

    cle = cle_idempotence(bail["dossier"], bail["code"], proposition["debut"])
    synchro_dt = datetime.fromisoformat(dossier["derniere_synchro"]) if dossier.get("derniere_synchro") else None
    deja = [e for e in envois if e["cle"] == cle and (
        e["etat"] == "créé" or (e["etat"] in ("en cours", "incertain")
                                and (synchro_dt is None or synchro_dt <= datetime.fromisoformat(e["horodatage"]))))]
    c.append(Controle("Aucun envoi antérieur pour ce bail et cette période", not deja,
                      "Envoi n° " + ", ".join(str(e["id"]) for e in deja) + " déjà journalisé"
                      + (" : issue incertaine, vérifier dans Pennylane puis actualiser." if any(e["etat"] != "créé" for e in deja) else ".")
                      if deja else "Le journal ne contient aucun envoi pour cette clé."))

    reference = sit.courant or sit.dernier_termine
    if reference and reference.get("montant_ht"):
        variation = montant / reference["montant_ht"] - 1
        c.append(Controle(f"Montant cohérent avec l'abonnement précédent (± {VARIATION_MAX:.0%})".replace("%", " %"),
                          abs(variation) <= VARIATION_MAX and montant > 0,
                          f"{_eur(montant)} par occurrence, contre {_eur(reference['montant_ht'])} : {variation:+.1%}".replace("%", " %")
                          .replace(".", ",")))
    else:
        c.append(Controle("Montant strictement positif", montant > 0, f"{_eur(montant)} par occurrence, sans abonnement de référence."))

    if (bail.get("statut_revision") or "").startswith("⚠"):
        c.append(Controle("Indice de la dernière révision publié", False,
                          "Indice attendu : le loyer facturé sera provisoire, un rappel sera dû.", bloquant=False))
    return c


def envoi_possible(controles: list[Controle]) -> bool:
    return all(c.ok for c in controles if c.bloquant)


# ---------------------------------------------------------------------------
# Journal des envois
# ---------------------------------------------------------------------------
def journal(cx: sqlite3.Connection, dossier: str | None = None) -> list[dict]:
    filtre, args = ("WHERE dossier=?", (dossier,)) if dossier else ("", ())
    return [dict(r) for r in cx.execute(f"SELECT * FROM envois {filtre} ORDER BY id DESC", args)]


def ouvrir_envoi(cx: sqlite3.Connection, bail: dict, proposition: dict, corps: dict, mode: str, nature: str = "abonnement") -> int:
    """Journalise la tentative AVANT l'appel : un envoi dont la réponse se perd laisse une trace."""
    cur = cx.execute("""INSERT INTO envois (horodatage, dossier, bail_id, bail_code, locataire, cle, empreinte, corps, mode, etat, auteur, nature)
                        VALUES (?,?,?,?,?,?,?,?,?, 'en cours', ?, ?)""",
                     (datetime.now().isoformat(timespec="seconds"), bail["dossier"], bail["id"], bail["code"],
                      bail.get("locataire", ""), cle_idempotence(bail["dossier"], bail["code"], proposition["debut"]),
                      empreinte(corps), json.dumps(corps, ensure_ascii=False, indent=2), mode, referentiel.utilisateur(), nature))
    cx.commit()
    return cur.lastrowid


def clore_envoi(cx: sqlite3.Connection, envoi_id: int, etat: str, statut_http: int | None = None,
                subscription_id: int | None = None, statut_abonnement: str = "", reponse: str = "",
                facture_id: int | None = None) -> None:
    cx.execute("UPDATE envois SET etat=?, statut_http=?, subscription_id=?, statut_abonnement=?, reponse=?, facture_id=? WHERE id=?",
               (etat, statut_http, subscription_id, statut_abonnement, reponse[:4000], facture_id, envoi_id))
    cx.commit()


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------
def _poster(dossier: dict, chemin: str, corps: dict, timeout: int = 60) -> tuple[str, int | None, dict | None, str]:
    """L'unique émission de la console vers Pennylane. Retourne (état, statut HTTP, réponse, texte brut).

    état : « créé » (201), « refusé » (réponse d'erreur de Pennylane : rien n'a été créé),
    « incertain » (pas de réponse : l'objet a pu être créé, à vérifier avant tout nouvel essai)."""
    if not ecriture_activee():
        raise PermissionError("Écriture désactivée : CONSOLE_ECRITURE=oui est requis")
    jeton = jetons.lire(variable_jeton_ecriture(dossier.get("variable_jeton", "")))
    if not jeton:
        raise PermissionError("Jeton d'écriture absent pour ce dossier")
    req = urllib.request.Request(f"{BASE_V2}{chemin}", data=json.dumps(corps).encode("utf-8"),
                                 method="POST", headers={"Authorization": f"Bearer {jeton}",
                                                         "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            brut = resp.read().decode("utf-8", "replace")
            return ("créé" if resp.status == 201 else "incertain"), resp.status, json.loads(brut), brut
    except urllib.error.HTTPError as e:                     # Pennylane a répondu par un refus : rien n'est créé
        brut = e.read().decode("utf-8", "replace")
        return ("refusé" if 400 <= e.code < 500 else "incertain"), e.code, None, brut
    except Exception as e:                                  # coupure, délai dépassé : issue inconnue
        return "incertain", None, None, f"{type(e).__name__} : {e}"


def creer_dans_pennylane(dossier: dict, corps: dict, timeout: int = 60) -> tuple[str, int | None, dict | None, str]:
    """Crée un abonnement (POST /billing_subscriptions) dont les factures naîtront en brouillon."""
    if corps.get("mode") != {"type": MODE_IMPOSE}:
        raise PermissionError("Envoi refusé : seul le mode brouillon est autorisé")
    return _poster(dossier, "/billing_subscriptions", corps, timeout)


def creer_facture_dans_pennylane(dossier: dict, corps: dict, timeout: int = 60) -> tuple[str, int | None, dict | None, str]:
    """Crée une facture client en brouillon (POST /customer_invoices). Jamais une facture finalisée."""
    if corps.get("draft") is not True:
        raise PermissionError("Envoi refusé : seule une facture en brouillon est autorisée")
    return _poster(dossier, "/customer_invoices", corps, timeout)


# ---------------------------------------------------------------------------
# Contrôles d'une facture de rappel
# ---------------------------------------------------------------------------
def controler_rappel(sit: SituationBail, proposition: dict | None, dossier: dict, envois: list[dict], simulation: bool,
                     existe_deja: bool | None = None, maintenant: datetime | None = None) -> list[Controle]:
    """Contrôles préalables à la création d'une facture de rappel en brouillon.

    `existe_deja` : réponse de Pennylane à « une facture porte-t-elle déjà cette référence ? » ; None si inconnue."""
    maintenant = maintenant or datetime.now()
    c: list[Controle] = []
    if proposition is None:
        return [Controle("Révision non répercutée sur un abonnement en cours", False,
                         "Aucun rappel à facturer : il faut un abonnement en cours et une révision passée.")]
    corps, bail = proposition["corps"], sit.bail
    if simulation:
        c.append(Controle("Mode simulation", True, "Aucun appel à Pennylane : l'envoi est joué dans la base locale."))
    else:
        variable = variable_jeton_ecriture(dossier.get("variable_jeton", ""))
        c.append(Controle("Écriture activée sur ce poste", ecriture_activee(),
                          "Variable CONSOLE_ECRITURE=oui" + ("" if ecriture_activee() else " absente : la console reste en lecture seule.")))
        c.append(Controle("Jeton d'écriture distinct du jeton de lecture", bool(jetons.lire(variable)),
                          f"Jeton {variable or '(dossier sans nom de jeton)'}"))
        synchro = dossier.get("derniere_synchro")
        frais = bool(synchro) and maintenant - datetime.fromisoformat(synchro) <= FRAICHEUR_MAX
        c.append(Controle("État Pennylane relu depuis moins de 24 heures", frais, f"Dernière collecte : {synchro or 'jamais'}"))
        c.append(Controle("Aucune facture ne porte déjà cette référence dans Pennylane", existe_deja is False,
                          f"Référence {proposition['reference']} : " + ("absente de Pennylane." if existe_deja is False else
                          "déjà présente, la facture existe." if existe_deja else "Pennylane n'a pas répondu, vérification impossible.")))
    c.append(Controle("Révision non répercutée constatée", any(e.type == "REVISION_NON_REPERCUTEE" for e in sit.ecarts),
                      f"Révision du {_fr(_d(proposition['date_revision']))}, abonnement n° {sit.courant['id'] if sit.courant else '—'} "
                      "resté à l'ancien loyer."))
    c.append(Controle("Client Pennylane identifié", isinstance(corps.get("customer_id"), int), f"customer_id = {corps.get('customer_id')}"))
    c.append(Controle("Facture créée en brouillon", corps.get("draft") is True,
                      "Brouillon imposé : la facture devra être relue et validée dans Pennylane avant tout envoi au locataire."))
    c.append(Controle("Abonnement en cours borné : le rappel est complet", proposition["borne"],
                      f"{proposition['echeances']} échéance(s), du {_fr(_d(proposition['du']))} au {_fr(_d(proposition['au']))}."
                      if proposition["borne"] else
                      "L'abonnement en cours n'a pas de terme : il continuerait à l'ancien loyer et le rappel serait incomplet. "
                      "Le borner dans Pennylane, puis actualiser."))
    c.append(Controle("Rappel strictement positif", proposition["echeances"] > 0 and proposition["ecart_par_echeance"] > 0,
                      f"{proposition['echeances']} × {_eur(proposition['ecart_par_echeance'])} = {_eur(proposition['montant_ht'])} hors taxes."
                      if proposition["ecart_par_echeance"] > 0 else
                      "Le loyer révisé est inférieur à l'ancien : c'est un avoir qu'il faut établir, à la main dans Pennylane."))
    c.append(Controle("Taux de TVA reconnu", proposition["tva_connue"],
                      f"Code Pennylane {corps['invoice_lines'][0]['vat_rate']}." if proposition["tva_connue"] else
                      f"Le taux de {bail.get('tva_pct')} % du bail n'a pas de code Pennylane connu de la console."))
    cle = cle_idempotence(bail["dossier"], bail["code"], proposition["debut"])
    deja = [e for e in envois if e["cle"] == cle and e["etat"] in ("créé", "en cours", "incertain")]
    c.append(Controle("Aucun rappel déjà envoyé pour ce bail et cette révision", not deja,
                      "Envoi n° " + ", ".join(str(e["id"]) for e in deja) + " déjà journalisé"
                      + (" : issue incertaine, vérifier dans Pennylane." if any(e["etat"] != "créé" for e in deja) else ".")
                      if deja else "Le journal ne contient aucun rappel pour cette révision."))
    return c

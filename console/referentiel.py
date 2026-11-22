"""Les baux vivent dans la console : paramètres saisis, loyer et dates recalculés à chaque lecture.

Le moteur est celui du paquet ``indexation_loyer`` (``calcul.calculer``), inchangé et déjà éprouvé
contre les formules du classeur. Ce module fait le pont entre une ligne de la table ``baux`` et ce
moteur, valide une saisie de formulaire, et tient l'historique des modifications.

Choix de gestion, à connaître : les révisions continuent après le terme contractuel du bail tant
qu'aucune date de sortie du locataire n'est saisie. Le terme reste signalé comme une alerte. Un
renouvellement qui refixe le loyer se traite en créant un nouveau bail.
"""
from __future__ import annotations

import os
import sqlite3
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path

from indexation_loyer import calcul, insee
from indexation_loyer.baux import DECISIONS, INDICES, METHODES, PERIODICITES_FACTURATION, TYPES_BAIL, Bail
from indexation_loyer.periodes import Trimestre

from .rapprochement import CODES_TVA

NATURES_PIECE = ("Bail", "Avenant", "Courrier", "État des lieux", "Autre")
# paramètres saisis d'un bail (hors identifiants techniques et champs calculés)
CHAMPS_BAIL = ("code", "local", "locataire", "adresse_locataire", "type_bail", "date_effet", "duree_ans", "date_fin",
               "date_sortie", "loyer_initial_annuel", "charges_annuelles", "tva_pct", "periodicite", "indice",
               "trimestre_base", "periodicite_revision_ans", "methode", "plafond_annuel_pct", "customer_id",
               "subscription_id", "notes")
CHAMPS_CALCULES = ("loyer_attendu_annuel", "date_derniere_revision", "date_prochaine_revision", "loyer_prochain_annuel",
                   "date_revision_suivante", "statut_revision", "loyer_precedent_annuel")
LIBELLES = {
    "code": "Code du bail", "local": "Local", "locataire": "Locataire", "adresse_locataire": "Adresse de correspondance",
    "type_bail": "Type de bail", "date_effet": "Prise d'effet", "duree_ans": "Durée", "date_fin": "Terme contractuel",
    "date_sortie": "Sortie du locataire", "loyer_initial_annuel": "Loyer initial annuel HT",
    "charges_annuelles": "Provision sur charges annuelle HT", "tva_pct": "TVA", "periodicite": "Facturation",
    "indice": "Indice", "trimestre_base": "Trimestre de l'indice de départ", "periodicite_revision_ans": "Révision tous les",
    "methode": "Méthode", "plafond_annuel_pct": "Plafond annuel", "customer_id": "Client Pennylane",
    "subscription_id": "Abonnement Pennylane", "notes": "Notes",
}

_cache_indices: dict = {}


auteur_courant: ContextVar[str] = ContextVar("auteur_courant", default="")


def utilisateur() -> str:
    """Auteur porté à l'historique : le compte connecté ; en ligne de commande, le poste."""
    return auteur_courant.get() or os.environ.get("CONSOLE_UTILISATEUR", "").strip() or "poste local"


def lire_indices(chemin: Path | None = None) -> list[insee.Observation]:
    """Indices INSEE du cache du dépôt, relus seulement quand le fichier change."""
    chemin = Path(chemin or insee.CHEMIN_CACHE)
    marque = chemin.stat().st_mtime_ns if chemin.exists() else None
    cle = str(chemin)
    if _cache_indices.get(cle, (None,))[0] != marque:
        _cache_indices[cle] = (marque, insee.lire_cache(chemin))
    return _cache_indices[cle][1]


def _d(v) -> date | None:
    if v in (None, ""):
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _annees(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def vers_bail(ligne: dict) -> Bail:
    """Ligne de la table ``baux`` vers l'objet du moteur. Le calendrier n'est borné que par la sortie
    du locataire : ``duree_ans`` est neutralisé pour que le terme contractuel n'arrête pas les révisions."""
    return Bail(
        id=ligne["code"], local=ligne.get("local") or "", locataire=ligne.get("locataire") or "",
        date_effet=_d(ligne["date_effet"]), loyer_initial_annuel_ht=float(ligne.get("loyer_initial_annuel") or 0),
        adresse_locataire=ligne.get("adresse_locataire") or "", indice=ligne.get("indice") or "ILC",
        trimestre_base=ligne.get("trimestre_base") or "",
        periodicite_revision_ans=int(ligne.get("periodicite_revision_ans") or 1),
        methode=ligne.get("methode") or "Chaînée", periodicite_facturation=ligne.get("periodicite") or "Mensuelle",
        tva_pct=float(ligne.get("tva_pct") if ligne.get("tva_pct") is not None else 20.0),
        charges_annuelles_ht=float(ligne.get("charges_annuelles") or 0),
        plafond_annuel_pct=ligne.get("plafond_annuel_pct"), duree_ans=None, date_fin=_d(ligne.get("date_sortie")),
        type_bail=ligne.get("type_bail") or "Commercial", notes=ligne.get("notes") or "")


def reviser(ligne: dict, indices: dict, decisions: dict[int, str], aujourdhui: date) -> tuple[Bail, list[calcul.LigneRevision]]:
    """Toutes les révisions du bail, des origines aux deux prochaines échéances."""
    bail = vers_bail(ligne)
    horizon = _annees(aujourdhui, 2 * bail.periodicite_revision_ans)
    lignes = calcul.calculer(bail, indices, horizon=horizon, aujourdhui=aujourdhui,
                             decisions={(bail.id, n): d for n, d in decisions.items()})
    return bail, lignes


def deriver(ligne: dict, indices: dict, decisions: dict[int, str], aujourdhui: date) -> dict:
    """Champs calculés d'un bail géré dans la console : loyer en vigueur, révisions passée et à venir."""
    bail, lignes = reviser(ligne, indices, decisions, aujourdhui)
    echues = [l for l in lignes if l.echeance.date_revision <= aujourdhui]
    effectives = [l for l in echues if l.loyer_retenu is not None]
    a_venir = [l for l in lignes if l.echeance.date_revision > aujourdhui]
    return {
        "loyer_attendu_annuel": calcul.loyer_actuel(bail, lignes, aujourdhui),
        "loyer_precedent_annuel": effectives[-1].loyer_precedent if effectives else None,
        "date_derniere_revision": effectives[-1].echeance.date_revision.isoformat() if effectives else None,
        "date_prochaine_revision": a_venir[0].echeance.date_revision.isoformat() if a_venir else None,
        "loyer_prochain_annuel": a_venir[0].loyer_retenu if a_venir else None,
        "date_revision_suivante": a_venir[1].echeance.date_revision.isoformat() if len(a_venir) > 1 else None,
        "statut_revision": echues[-1].statut if echues else calcul.STATUT_A_VENIR,
        "trimestre_attendu": next((l.echeance.trimestre_reference for l in lignes
                                   if l.indice_reference is None and l.statut != calcul.STATUT_GELEE), None),
    }


def decisions_du_bail(cx: sqlite3.Connection, bail_id: int) -> dict[int, dict]:
    return {r["numero"]: dict(r) for r in cx.execute("SELECT * FROM decisions WHERE bail_id=?", (bail_id,))}


# ---------------------------------------------------------------------------
# Saisie : validation d'un formulaire de bail
# ---------------------------------------------------------------------------
def _nombre(texte: str, nom: str, erreurs: list[str], obligatoire: bool = False, entier: bool = False):
    texte = (texte or "").replace(" ", "").replace("\xa0", "").replace(" ", "").replace("€", "").replace("%", "").replace(",", ".")
    if not texte:
        if obligatoire:
            erreurs.append(f"{LIBELLES[nom]} : valeur obligatoire.")
        return None
    try:
        valeur = float(texte)
    except ValueError:
        erreurs.append(f"{LIBELLES[nom]} : « {texte} » n'est pas un nombre.")
        return None
    if valeur < 0:
        erreurs.append(f"{LIBELLES[nom]} : valeur négative.")
        return None
    if entier:
        if valeur != int(valeur):
            erreurs.append(f"{LIBELLES[nom]} : nombre entier attendu.")
            return None
        return int(valeur)
    return valeur


def _date(texte: str, nom: str, erreurs: list[str], obligatoire: bool = False) -> str | None:
    texte = (texte or "").strip()
    if not texte:
        if obligatoire:
            erreurs.append(f"{LIBELLES[nom]} : date obligatoire.")
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texte, fmt).date().isoformat()
        except ValueError:
            pass
    erreurs.append(f"{LIBELLES[nom]} : date illisible « {texte} » (attendu JJ/MM/AAAA).")
    return None


def valider(champs: dict[str, str]) -> tuple[dict, list[str]]:
    """Formulaire de bail vers les colonnes de la table. Retourne (valeurs, erreurs)."""
    e: list[str] = []
    t = lambda nom: (champs.get(nom) or "").strip()
    v: dict = {"code": t("code").upper(), "local": t("local"), "locataire": t("locataire"),
               "adresse_locataire": t("adresse_locataire"), "notes": t("notes")}
    for nom in ("code", "local", "locataire"):
        if not v[nom]:
            e.append(f"{LIBELLES[nom]} : champ obligatoire.")
    if v["code"] and not all(c.isalnum() or c in "-_" for c in v["code"]):
        e.append("Code du bail : lettres, chiffres, tiret et souligné seulement.")
    for nom, permis in (("type_bail", TYPES_BAIL), ("periodicite", tuple(PERIODICITES_FACTURATION)),
                        ("indice", INDICES), ("methode", METHODES)):
        v[nom] = t(nom) or permis[0]
        if v[nom] not in permis:
            e.append(f"{LIBELLES[nom]} : valeur inconnue « {v[nom]} ».")
    v["date_effet"] = _date(t("date_effet"), "date_effet", e, obligatoire=True)
    v["date_fin"] = _date(t("date_fin"), "date_fin", e)
    v["date_sortie"] = _date(t("date_sortie"), "date_sortie", e)
    v["duree_ans"] = _nombre(t("duree_ans"), "duree_ans", e, entier=True)
    v["loyer_initial_annuel"] = _nombre(t("loyer_initial_annuel"), "loyer_initial_annuel", e, obligatoire=True)
    v["charges_annuelles"] = _nombre(t("charges_annuelles"), "charges_annuelles", e) or 0.0
    tva = _nombre(t("tva_pct"), "tva_pct", e)
    v["tva_pct"] = 20.0 if tva is None else tva
    if v["tva_pct"] not in CODES_TVA:
        e.append("TVA : taux attendu parmi " + ", ".join(f"{x:g}".replace(".", ",") for x in sorted(CODES_TVA)) + " %.")
    v["periodicite_revision_ans"] = _nombre(t("periodicite_revision_ans"), "periodicite_revision_ans", e, entier=True) or 1
    if v["periodicite_revision_ans"] not in (1, 2, 3):
        e.append("Révision : périodicité de 1, 2 ou 3 ans.")
    v["plafond_annuel_pct"] = _nombre(t("plafond_annuel_pct"), "plafond_annuel_pct", e)
    v["customer_id"] = _nombre(t("customer_id"), "customer_id", e, entier=True)
    v["subscription_id"] = _nombre(t("subscription_id"), "subscription_id", e, entier=True)
    if v["loyer_initial_annuel"] == 0:
        e.append("Loyer initial annuel HT : valeur nulle.")
    v["trimestre_base"] = t("trimestre_base").upper().replace("Q", "T")
    if v["date_effet"]:
        effet = _d(v["date_effet"])
        if not v["trimestre_base"]:                    # convention par défaut, à confirmer dans le bail
            v["trimestre_base"] = Trimestre.de_date(effet).plus_trimestres(-2).fr
        if v["date_fin"] is None and v["duree_ans"]:
            v["date_fin"] = _annees(effet, v["duree_ans"]).isoformat()
        for nom in ("date_fin", "date_sortie"):
            if v[nom] and v[nom] <= v["date_effet"]:
                e.append(f"{LIBELLES[nom]} : antérieure à la prise d'effet.")
    if v["trimestre_base"]:
        try:
            Trimestre.parse(v["trimestre_base"])
        except ValueError:
            e.append(f"Trimestre de l'indice de départ : « {v['trimestre_base']} » illisible (attendu AAAA-Tn, par exemple 2023-T2).")
    return v, e


def controler_indice_de_depart(valeurs: dict, indices: dict) -> str | None:
    """Avertissement, non bloquant : l'indice de départ doit exister dans la série."""
    cle = (valeurs.get("indice"), valeurs.get("trimestre_base"))
    if cle[1] and cle not in indices:
        return (f"L'indice {cle[0]} du {cle[1]} est absent des séries relevées : aucune révision ne pourra être "
                "calculée tant que ce trimestre n'est pas corrigé.")
    return None


# ---------------------------------------------------------------------------
# Écritures dans la base, toujours tracées
# ---------------------------------------------------------------------------
def _aff(v) -> str:
    """Valeur lisible pour l'historique : dates en JJ/MM/AAAA, nombres à la française."""
    if v in (None, ""):
        return "—"
    if isinstance(v, float):
        return f"{v:.12g}".replace(".", ",")
    texte = str(v)
    if len(texte) == 10 and texte[4] == texte[7] == "-" and texte[:4].isdigit():
        return f"{texte[8:]}/{texte[5:7]}/{texte[:4]}"
    return texte


def tracer(cx: sqlite3.Connection, action: str, detail: str = "", dossier: str = "", bail_id: int | None = None) -> None:
    cx.execute("INSERT INTO historique (horodatage, auteur, dossier, bail_id, action, detail) VALUES (?,?,?,?,?,?)",
               (datetime.now().isoformat(timespec="seconds"), utilisateur(), dossier, bail_id, action, detail))


def enregistrer_bail(cx: sqlite3.Connection, dossier: str, valeurs: dict, bail_id: int | None = None) -> int:
    """Crée ou modifie un bail géré par la console. La modification liste les champs changés à l'historique."""
    colonnes = [c for c in CHAMPS_BAIL if c in valeurs]
    if bail_id is None:
        cur = cx.execute(f"INSERT INTO baux (dossier, calcule, {', '.join(colonnes)}) VALUES (?, 1, {', '.join('?' * len(colonnes))})",
                         [dossier, *(valeurs[c] for c in colonnes)])
        bail_id = cur.lastrowid
        tracer(cx, "Bail créé", f"{valeurs['code']} · {valeurs['locataire']} · loyer initial annuel "
                                + f"{valeurs['loyer_initial_annuel']:,.2f} € HT".replace(",", " ").replace(".", ","),
               dossier, bail_id)
    else:
        avant = dict(cx.execute("SELECT * FROM baux WHERE id=?", (bail_id,)).fetchone())
        changes = [f"{LIBELLES[c]} : {_aff(avant.get(c))} → {_aff(valeurs[c])}"
                   for c in colonnes if (avant.get(c) if avant.get(c) != "" else None) != (valeurs[c] if valeurs[c] != "" else None)]
        cx.execute(f"UPDATE baux SET calcule=1, {', '.join(f'{c}=?' for c in colonnes)} WHERE id=?",
                   [*(valeurs[c] for c in colonnes), bail_id])
        if not avant.get("calcule"):
            changes.insert(0, "Loyer désormais calculé par la console")
        if changes:
            tracer(cx, "Bail modifié", " ; ".join(changes), dossier, bail_id)
    cx.commit()
    return bail_id


def enregistrer_decision(cx: sqlite3.Connection, bail: dict, numero: int, decision: str, commentaire: str = "",
                         courrier_le: str | None = None) -> None:
    if decision not in DECISIONS:
        raise ValueError(f"Décision inconnue : {decision}")
    avant = cx.execute("SELECT * FROM decisions WHERE bail_id=? AND numero=?", (bail["id"], numero)).fetchone()
    cx.execute("""INSERT INTO decisions (bail_id, numero, decision, commentaire, courrier_le, auteur, horodatage)
                  VALUES (?,?,?,?,?,?,?) ON CONFLICT(bail_id, numero) DO UPDATE SET decision=excluded.decision,
                  commentaire=excluded.commentaire, courrier_le=excluded.courrier_le, auteur=excluded.auteur,
                  horodatage=excluded.horodatage""",
               (bail["id"], numero, decision, commentaire, courrier_le, utilisateur(), datetime.now().isoformat(timespec="seconds")))
    ancienne = avant["decision"] if avant else DECISIONS[0]
    morceaux = [f"révision n° {numero}"]
    if ancienne != decision:
        morceaux.append(f"{ancienne} → {decision}")
    if commentaire and (not avant or avant["commentaire"] != commentaire):
        morceaux.append(f"commentaire : {commentaire}")
    if courrier_le and (not avant or avant["courrier_le"] != courrier_le):
        morceaux.append(f"courrier envoyé le {_aff(courrier_le)}")
    if len(morceaux) > 1:
        tracer(cx, "Décision de révision", " ; ".join(morceaux), bail["dossier"], bail["id"])
    cx.commit()

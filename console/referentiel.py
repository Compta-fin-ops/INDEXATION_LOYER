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

from .rapprochement import CODES_TVA, ECHEANCES_PAR_AN, UNITE

NATURES_PIECE = ("Bail", "Avenant", "Courrier", "État des lieux", "Autre")
# paramètres saisis d'un bail (hors identifiants techniques et champs calculés)
CHAMPS_BAIL = ("code", "local", "locataire", "adresse_locataire", "type_bail", "date_effet", "duree_ans", "date_fin",
               "date_sortie", "loyer_initial_annuel", "charges_annuelles", "tva_pct", "periodicite", "indice",
               "trimestre_base", "periodicite_revision_ans", "methode", "plafond_annuel_pct", "customer_id",
               "subscription_id", "notes", "reprise_date", "reprise_loyer_annuel", "reprise_trimestre", "mention_historique")
CHAMPS_CALENDRIER = ("date_effet", "periodicite_revision_ans", "reprise_date")   # ils fixent la numérotation des révisions
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
    "reprise_date": "Reprise : date de la dernière révision", "reprise_loyer_annuel": "Reprise : loyer annuel HT à cette date",
    "reprise_trimestre": "Reprise : trimestre de l'indice appliqué",
    "mention_historique": "Historique à reporter sur les factures",
}
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre")
FREQUENCE = {1: "annuelle", 2: "biennale", 3: "triennale"}
LONGUEUR_HISTORIQUE = 1500

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


def vers_bail(ligne: dict, affichage: bool = False) -> Bail:
    """Ligne de la table ``baux`` vers l'objet du moteur. Le calendrier n'est borné que par la sortie
    du locataire : ``duree_ans`` est neutralisé pour que le terme contractuel n'arrête pas les révisions.

    Bail repris en cours : le moteur part du point de reprise (date, loyer et indice de la dernière
    révision appliquée) et ne recalcule rien avant. ``affichage`` rend la vraie date d'effet, pour un courrier."""
    reprise = bool(ligne.get("reprise_date")) and not affichage
    return Bail(
        id=ligne["code"], local=ligne.get("local") or "", locataire=ligne.get("locataire") or "",
        date_effet=_d(ligne["reprise_date"] if reprise else ligne["date_effet"]),
        loyer_initial_annuel_ht=float((ligne.get("reprise_loyer_annuel") if reprise else ligne.get("loyer_initial_annuel")) or 0),
        adresse_locataire=ligne.get("adresse_locataire") or "", indice=ligne.get("indice") or "ILC",
        trimestre_base=(ligne.get("reprise_trimestre") if reprise else ligne.get("trimestre_base")) or "",
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
        "date_derniere_revision": effectives[-1].echeance.date_revision.isoformat() if effectives
        else (str(ligne["reprise_date"])[:10] if ligne.get("reprise_date") else None),       # à défaut, celle de la reprise
        "date_prochaine_revision": a_venir[0].echeance.date_revision.isoformat() if a_venir else None,
        "loyer_prochain_annuel": a_venir[0].loyer_retenu if a_venir else None,
        "date_revision_suivante": a_venir[1].echeance.date_revision.isoformat() if len(a_venir) > 1 else None,
        "statut_revision": echues[-1].statut if echues else calcul.STATUT_A_VENIR,
        "trimestre_attendu": next((l.echeance.trimestre_reference for l in lignes
                                   if l.indice_reference is None and l.statut != calcul.STATUT_GELEE), None),
        "suivi": suivi_revisions(ligne, indices, lignes),
    }


def _euros(x: float) -> str:
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def _indice(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def suivi_revisions(ligne: dict, indices: dict, lignes: list[calcul.LigneRevision]) -> dict:
    """Matière de la mention portée sur les factures : d'où vient le loyer, révision par révision.

    Les montants sont ceux d'une échéance (le loyer annuel divisé par le nombre d'échéances), comme sur la facture.
    `entete` : indice et loyer de départ ; `revisions` : une phrase par révision, datée, passée ou à venir.
    Bail repris en cours : la console ne connaît pas les révisions d'avant la reprise. Elle reporte le texte saisi
    sur le bail (`mention_historique`), à défaut le loyer constaté à la reprise."""
    n = ECHEANCES_PAR_AN.get(ligne.get("periodicite"), 12)
    unite = UNITE.get(ligne.get("periodicite"), "mois")
    nom = ligne.get("indice") or "ILC"
    par = lambda annuel: f"{_euros(annuel / n)} HT par {unite}"
    tri = lambda t: (t or "").replace("-", " ")
    entete = []
    base = indices.get((nom, ligne.get("trimestre_base")))
    if ligne.get("trimestre_base"):
        entete.append(f"{nom} {tri(ligne['trimestre_base'])}" + (f" : {_indice(base)}" if base is not None else ""))
    entete.append(f"Loyer de base : {par(float(ligne.get('loyer_initial_annuel') or 0))}")
    effet = _d(ligne.get("date_effet"))
    if effet:
        entete.append(f"Premier loyer : {MOIS[effet.month - 1]} {effet.year}")
    if ligne.get("reprise_date"):
        historique = (ligne.get("mention_historique") or "").strip()
        entete += [x.strip() for x in historique.splitlines() if x.strip()] if historique else [
            f"Loyer au {_aff(str(ligne['reprise_date'])[:10])} : {par(float(ligne.get('reprise_loyer_annuel') or 0))} "
            f"({nom} {tri(ligne.get('reprise_trimestre'))})."]
    revisions = []
    for l in lignes:
        quand = _aff(l.echeance.date_revision.isoformat())
        rapport = (f"{nom} {tri(l.echeance.trimestre_reference)} : {_indice(l.indice_reference)} contre "
                   f"{tri(l.trimestre_precedent_effectif)} : {_indice(l.indice_precedent)}"
                   if l.indice_reference is not None and l.indice_precedent is not None else "")
        variation = f"{(l.coefficient - 1) * 100:+.2f} %".replace(".", ",") if l.coefficient is not None else ""
        if l.statut == calcul.STATUT_GELEE:
            texte = f"Révision au {quand} : non appliquée, loyer maintenu à {par(l.loyer_retenu)}." if l.loyer_retenu is not None else ""
        elif l.loyer_retenu is None:
            texte = f"Révision au {quand} : {nom} {tri(l.echeance.trimestre_reference)} non encore publié, loyer provisoire."
        elif l.loyer_brut is not None and l.loyer_retenu < l.loyer_brut:
            plafond = f"{ligne.get('plafond_annuel_pct'):g}".replace(".", ",")
            texte = (f"Révision au {quand} : variation de l'indice de {variation} ({rapport}), limitée par le plafond "
                     f"du bail de {plafond} % par an, soit {par(l.loyer_retenu)}.")
        else:
            texte = f"Révision au {quand} : {rapport}, soit {variation} ; loyer porté à {par(l.loyer_retenu)}."
        revisions.append({"date": l.echeance.date_revision.isoformat(), "texte": texte,
                          "loyer": par(l.loyer_retenu) if l.loyer_retenu is not None else ""})
    return {"entete": entete, "revisions": revisions,
            "frequence": FREQUENCE.get(int(ligne.get("periodicite_revision_ans") or 1), "")}


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
               "adresse_locataire": t("adresse_locataire"), "notes": t("notes"),
               "mention_historique": "\n".join(x.strip() for x in t("mention_historique").splitlines() if x.strip())}
    if len(v["mention_historique"]) > LONGUEUR_HISTORIQUE:
        e.append(f"Historique à reporter sur les factures : {LONGUEUR_HISTORIQUE} caractères au plus.")
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
    # point de reprise d'un bail en cours : trois champs, tous ou aucun
    v["reprise_date"] = _date(t("reprise_date"), "reprise_date", e)
    v["reprise_loyer_annuel"] = _nombre(t("reprise_loyer_annuel"), "reprise_loyer_annuel", e)
    v["reprise_trimestre"] = t("reprise_trimestre").upper().replace("Q", "T") or None
    saisis = [v[c] is not None for c in ("reprise_date", "reprise_loyer_annuel", "reprise_trimestre")]
    if any(saisis) and not all(saisis):
        e.append("Reprise d'un bail en cours : renseigner les trois champs (date, loyer, trimestre de l'indice), ou aucun.")
    elif all(saisis):
        if v["reprise_loyer_annuel"] == 0:
            e.append("Reprise : loyer annuel nul.")
        if v["date_effet"] and v["reprise_date"] < v["date_effet"]:
            e.append("Reprise : date antérieure à la prise d'effet du bail.")
        try:
            Trimestre.parse(v["reprise_trimestre"])
        except ValueError:
            e.append(f"Reprise : trimestre « {v['reprise_trimestre']} » illisible (attendu AAAA-Tn, par exemple 2024-T2).")
    if v["trimestre_base"]:
        try:
            Trimestre.parse(v["trimestre_base"])
        except ValueError:
            e.append(f"Trimestre de l'indice de départ : « {v['trimestre_base']} » illisible (attendu AAAA-Tn, par exemple 2023-T2).")
    return v, e


def controler_indice_de_depart(valeurs: dict, indices: dict) -> str | None:
    """Avertissement, non bloquant : l'indice de départ (ou celui de la reprise) doit exister dans la série."""
    cle = (valeurs.get("indice"), valeurs.get("reprise_trimestre") or valeurs.get("trimestre_base"))
    if cle[1] and cle not in indices:
        return (f"L'indice {cle[0]} du {cle[1]} est absent des séries relevées : aucune révision ne pourra être "
                "calculée tant que ce trimestre n'est pas corrigé.")
    if valeurs.get("reprise_date") and valeurs.get("reprise_trimestre"):
        try:
            t, r = Trimestre.parse(valeurs["reprise_trimestre"]), Trimestre.de_date(_d(valeurs["reprise_date"]))
        except ValueError:
            return None
        ecart = (r.annee * 4 + r.numero) - (t.annee * 4 + t.numero)
        if ecart > 4 or ecart < 0:
            return (f"Reprise : l'indice du {t.fr} est éloigné de la révision du {_aff(str(valeurs['reprise_date'])[:10])}. "
                    "Ce trimestre doit être celui appliqué à cette révision : la suivante prendra le même trimestre, "
                    "une période plus tard. Un gel à rattraper se saisit dans le tableau des révisions, pas ici.")
    return None


def gels_enregistres(cx: sqlite3.Connection, bail_id: int) -> int:
    return cx.execute("SELECT COUNT(*) AS n FROM decisions WHERE bail_id=? AND decision<>?", (bail_id, DECISIONS[0])).fetchone()["n"]


def calendrier_modifie(avant: dict, valeurs: dict) -> bool:
    """Vrai si la modification déplace ou renumérote les révisions : les gels saisis ne viseraient plus la bonne année."""
    norme = lambda v: None if v in (None, "") else str(v)[:10]
    return any(norme(avant.get(c)) != norme(valeurs.get(c)) for c in CHAMPS_CALENDRIER)


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


def enregistrer_bail(cx: sqlite3.Connection, dossier: str, valeurs: dict, bail_id: int | None = None,
                     valider: bool = True) -> int:
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
    if valider:                                      # l'import en lot valide tout d'un bloc
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

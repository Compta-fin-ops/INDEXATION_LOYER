"""Comptes utilisateurs de la console : mot de passe, session, rôles.

Le mot de passe n'est jamais stocké : la base garde son empreinte scrypt, salée. Le jeton de
session n'est pas stocké non plus, seulement son SHA-256 : une copie de la base ne permet ni de
retrouver un mot de passe ni de reprendre une session.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta

ROLES = ("lecture", "gestion", "validation", "administrateur")       # du moins au plus de droits
LIBELLES_ROLE = {
    "lecture": "Lecture : consulte tout, ne modifie rien",
    "gestion": "Gestion : saisit les baux, gèle une révision, joint des pièces, édite les courriers",
    "validation": "Validation : gestion, plus la création d'abonnements dans Pennylane",
    "administrateur": "Administrateur : validation, plus la gestion des comptes",
}
LONGUEUR_MIN = 12
ECHECS_MAX = 5                       # au-delà, le compte est bloqué quelques minutes
BLOCAGE = timedelta(minutes=15)
DUREE_SESSION = timedelta(hours=12)
_N, _R, _P = 2 ** 14, 8, 1           # paramètres scrypt
_LEURRE = None                       # empreinte calculée pour un identifiant inconnu : même durée de réponse


def hacher(mot_de_passe: str) -> str:
    sel = secrets.token_bytes(16)
    cle = hashlib.scrypt(mot_de_passe.encode("utf-8"), salt=sel, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${sel.hex()}${cle.hex()}"


def verifier(mot_de_passe: str, stocke: str) -> bool:
    try:
        _, n, r, p, sel, cle = stocke.split("$")
        calcule = hashlib.scrypt(mot_de_passe.encode("utf-8"), salt=bytes.fromhex(sel), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(calcule, bytes.fromhex(cle))


def controler_mot_de_passe(mot_de_passe: str, identifiant: str = "") -> list[str]:
    erreurs = []
    if len(mot_de_passe) < LONGUEUR_MIN:
        erreurs.append(f"Mot de passe : {LONGUEUR_MIN} caractères au moins. Une phrase de quelques mots convient très bien.")
    if len(set(mot_de_passe)) < 5:
        erreurs.append("Mot de passe : trop répétitif.")
    if identifiant and identifiant.lower().split("@")[0] in mot_de_passe.lower():
        erreurs.append("Mot de passe : il ne doit pas contenir l'identifiant.")
    return erreurs


def normaliser(identifiant: str) -> str:
    return (identifiant or "").strip().lower()


def _maintenant() -> datetime:
    return datetime.now()


def creer(cx: sqlite3.Connection, identifiant: str, nom: str, role: str, mot_de_passe: str, doit_changer: bool = False) -> int:
    identifiant = normaliser(identifiant)
    if role not in ROLES:
        raise ValueError(f"Rôle inconnu : {role}")
    if not identifiant or any(c.isspace() for c in identifiant) or len(identifiant) > 120:
        raise ValueError("Identifiant : obligatoire, sans espace.")
    erreurs = controler_mot_de_passe(mot_de_passe, identifiant)
    if erreurs:
        raise ValueError(" ".join(erreurs))
    if cx.execute("SELECT 1 FROM utilisateurs WHERE identifiant=?", (identifiant,)).fetchone():
        raise ValueError(f"Identifiant : {identifiant} existe déjà.")
    cur = cx.execute("""INSERT INTO utilisateurs (identifiant, nom, role, mot_de_passe, doit_changer, cree_le)
                        VALUES (?,?,?,?,?,?)""",
                     (identifiant, nom.strip() or identifiant, role, hacher(mot_de_passe), int(doit_changer),
                      _maintenant().isoformat(timespec="seconds")))
    cx.commit()
    return cur.lastrowid


def changer_mot_de_passe(cx: sqlite3.Connection, utilisateur_id: int, mot_de_passe: str, doit_changer: bool = False) -> None:
    u = cx.execute("SELECT identifiant FROM utilisateurs WHERE id=?", (utilisateur_id,)).fetchone()
    erreurs = controler_mot_de_passe(mot_de_passe, u["identifiant"] if u else "")
    if u is None or erreurs:
        raise ValueError(" ".join(erreurs) or "Compte inconnu.")
    cx.execute("UPDATE utilisateurs SET mot_de_passe=?, doit_changer=?, echecs=0, bloque_jusqu=NULL WHERE id=?",
               (hacher(mot_de_passe), int(doit_changer), utilisateur_id))
    cx.execute("DELETE FROM sessions WHERE utilisateur_id=?", (utilisateur_id,))     # toutes les sessions tombent
    cx.commit()


def authentifier(cx: sqlite3.Connection, identifiant: str, mot_de_passe: str) -> tuple[dict | None, str]:
    """(compte, message d'erreur). Le message ne dit jamais si c'est l'identifiant ou le mot de passe qui est faux."""
    global _LEURRE
    maintenant = _maintenant()
    refus = "Identifiant ou mot de passe incorrect."
    u = cx.execute("SELECT * FROM utilisateurs WHERE identifiant=?", (normaliser(identifiant),)).fetchone()
    if u is None or not u["actif"]:
        _LEURRE = _LEURRE or hacher("leurre de durée constante")
        verifier(mot_de_passe, _LEURRE)
        return None, refus
    if u["bloque_jusqu"] and datetime.fromisoformat(u["bloque_jusqu"]) > maintenant:
        return None, f"Compte bloqué après {ECHECS_MAX} essais. Réessayer dans quelques minutes, ou demander à un administrateur."
    if not verifier(mot_de_passe, u["mot_de_passe"]):
        echecs = (u["echecs"] or 0) + 1
        bloque = (maintenant + BLOCAGE).isoformat(timespec="seconds") if echecs >= ECHECS_MAX else None
        cx.execute("UPDATE utilisateurs SET echecs=?, bloque_jusqu=? WHERE id=?", (0 if bloque else echecs, bloque, u["id"]))
        cx.commit()
        return None, refus
    cx.execute("UPDATE utilisateurs SET echecs=0, bloque_jusqu=NULL, derniere_connexion=? WHERE id=?",
               (maintenant.isoformat(timespec="seconds"), u["id"]))
    cx.commit()
    return dict(u), ""


def _empreinte(jeton: str) -> str:
    return hashlib.sha256(jeton.encode("ascii", "ignore")).hexdigest()


def ouvrir_session(cx: sqlite3.Connection, utilisateur_id: int) -> str:
    jeton = secrets.token_urlsafe(32)
    maintenant = _maintenant()
    cx.execute("DELETE FROM sessions WHERE expire_le < ?", (maintenant.isoformat(timespec="seconds"),))
    cx.execute("INSERT INTO sessions (empreinte, utilisateur_id, cree_le, expire_le) VALUES (?,?,?,?)",
               (_empreinte(jeton), utilisateur_id, maintenant.isoformat(timespec="seconds"),
                (maintenant + DUREE_SESSION).isoformat(timespec="seconds")))
    cx.commit()
    return jeton


def session(cx: sqlite3.Connection, jeton: str | None) -> dict | None:
    """Le compte actif auquel appartient ce jeton de session, ou rien."""
    if not jeton:
        return None
    r = cx.execute("""SELECT u.id, u.identifiant, u.nom, u.role, u.doit_changer FROM sessions s
                      JOIN utilisateurs u ON u.id = s.utilisateur_id
                      WHERE s.empreinte=? AND s.expire_le > ? AND u.actif=1""",
                   (_empreinte(jeton), _maintenant().isoformat(timespec="seconds"))).fetchone()
    return dict(r) if r else None


def fermer_session(cx: sqlite3.Connection, jeton: str | None) -> None:
    if jeton:
        cx.execute("DELETE FROM sessions WHERE empreinte=?", (_empreinte(jeton),))
        cx.commit()


def peut(role: str | None, requis: str) -> bool:
    return role in ROLES and ROLES.index(role) >= ROLES.index(requis)


def nb_administrateurs(cx: sqlite3.Connection, sauf: int | None = None) -> int:
    return cx.execute("SELECT COUNT(*) AS n FROM utilisateurs WHERE role='administrateur' AND actif=1 AND id<>?",
                      (sauf if sauf is not None else -1,)).fetchone()["n"]

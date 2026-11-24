"""Où vivent les jetons Pennylane : jamais dans la base, jamais dans le dépôt.

Deux sources, dans cet ordre :
  1. une variable d'environnement (serveur, hébergement) ;
  2. le coffre du système d'exploitation, via la bibliothèque ``keyring`` : sous Windows, le
     Gestionnaire d'identification, protégé par la session de l'utilisateur.
Le coffre permet de saisir un jeton une fois dans la console, sans toucher aux variables du poste.
La console ne sait qu'y déposer, tester la présence et retirer : elle ne réaffiche jamais un jeton.
"""
from __future__ import annotations

import os
import re

SERVICE = "console-loyers"
RE_VARIABLE = re.compile(r"^PENNYLANE_TOKEN_[A-Z0-9_]+$")
_INCONNU = object()
_coffre = _INCONNU


class CoffreIndisponible(RuntimeError):
    pass


def _detecter():
    """Le coffre du système, ou None s'il n'y en a pas d'utilisable (serveur sans session graphique, par exemple)."""
    try:
        import keyring
        from keyring.backends import fail
        trouve = keyring.get_keyring()
        return None if isinstance(trouve, fail.Keyring) else trouve
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:                                # bibliothèque absente ou coffre défaillant : pas de coffre
        return None


def coffre():
    global _coffre
    if _coffre is _INCONNU:
        _coffre = _detecter()
    return _coffre


def definir_coffre(objet) -> None:
    """Remplace le coffre (tests) ; ``None`` le déclare indisponible."""
    global _coffre
    _coffre = objet


def disponible() -> bool:
    return coffre() is not None


def nom_du_coffre() -> str:
    return "Gestionnaire d'identification de Windows" if os.name == "nt" else "coffre du système"


def source(variable: str) -> str:
    """D'où viendrait le jeton : « environnement », « coffre » ou « » s'il est absent. Ne renvoie jamais le jeton."""
    if not variable:
        return ""
    if os.environ.get(variable):
        return "environnement"
    try:
        return "coffre" if disponible() and coffre().get_password(SERVICE, variable) else ""
    except Exception:
        return ""


def lire(variable: str) -> str:
    if not variable:
        return ""
    valeur = os.environ.get(variable, "")
    if valeur:
        return valeur
    try:
        return (coffre().get_password(SERVICE, variable) or "") if disponible() else ""
    except Exception:
        return ""


def controler(jeton: str) -> str | None:
    """Message d'erreur si ce qui est saisi ne peut pas être un jeton, sinon None."""
    if not jeton:
        return "Jeton vide."
    if any(c.isspace() for c in jeton):
        return "Le jeton ne contient pas d'espace : le copier en entier, sans retour à la ligne."
    if not 20 <= len(jeton) <= 2000:
        return "Longueur inattendue pour un jeton : le copier en entier depuis Pennylane."
    if RE_VARIABLE.fullmatch(jeton):
        return "Ceci est un nom de variable, pas un jeton."
    return None


def enregistrer(variable: str, jeton: str) -> None:
    if not RE_VARIABLE.fullmatch(variable or ""):
        raise ValueError("Nom de jeton invalide.")
    erreur = controler(jeton)
    if erreur:
        raise ValueError(erreur)
    if not disponible():
        raise CoffreIndisponible("Aucun coffre de système utilisable sur cette machine : déclarer le jeton par une variable d'environnement.")
    coffre().set_password(SERVICE, variable, jeton)
    if coffre().get_password(SERVICE, variable) != jeton:                # relecture : on ne prétend pas avoir enregistré
        raise CoffreIndisponible("Le coffre n'a pas restitué le jeton après l'enregistrement.")


def retirer(variable: str) -> bool:
    if not disponible() or not variable:
        return False
    try:
        if coffre().get_password(SERVICE, variable) is None:
            return False
        coffre().delete_password(SERVICE, variable)
        return True
    except Exception:
        return False

"""Réglages communs des tests."""
import pytest

from console import jetons


class CoffreEnMemoire:
    """Remplace le coffre du système : les tests ne lisent ni n'écrivent jamais le vrai."""

    def __init__(self):
        self.contenu = {}

    def get_password(self, service, nom):
        return self.contenu.get((service, nom))

    def set_password(self, service, nom, valeur):
        self.contenu[(service, nom)] = valeur

    def delete_password(self, service, nom):
        del self.contenu[(service, nom)]


@pytest.fixture(autouse=True)
def coffre():
    """Un coffre vide en mémoire pour chaque test."""
    faux = CoffreEnMemoire()
    jetons.definir_coffre(faux)
    yield faux
    jetons.definir_coffre(None)

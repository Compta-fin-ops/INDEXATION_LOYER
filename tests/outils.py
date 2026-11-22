"""Outils de test de la console : un client web déjà connecté."""
from fastapi.testclient import TestClient

from console import comptes, db

MOT_DE_PASSE = "phrase de test assez longue"


def client_connecte(app, chemin_db, role: str = "administrateur", identifiant: str | None = None) -> TestClient:
    """Crée le compte s'il manque, se connecte, et envoie l'en-tête Origin qu'un navigateur envoie."""
    identifiant = identifiant or f"test.{role}"
    cx = db.connecter(chemin_db)
    if not cx.execute("SELECT 1 FROM utilisateurs WHERE identifiant=?", (identifiant,)).fetchone():
        comptes.creer(cx, identifiant, f"Test {role}", role, MOT_DE_PASSE)
    c = TestClient(app, headers={"origin": "http://testserver"})
    r = c.post("/connexion", data={"identifiant": identifiant, "mot_de_passe": MOT_DE_PASSE}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c

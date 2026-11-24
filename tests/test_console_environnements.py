"""Base de production et base de test : marquage, copie, sessions distinctes, aucune écriture depuis le test."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from console import db, ecriture
from console.__main__ import main
from console.app import creer_app
from tests.outils import MOT_DE_PASSE, client_connecte

AUJ = date(2026, 11, 22)
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
BAIL = {"code": "B01", "locataire": "Locataire", "local": "1 rue Test", "date_effet": "2023-04-01", "loyer_initial_annuel": "12000",
        "indice": "ILC", "trimestre_base": "2022-T4", "customer_id": "77"}


@pytest.fixture
def production(tmp_path):
    chemin = tmp_path / "data" / "cabinet.sqlite"
    c = client_connecte(creer_app(chemin, AUJ), chemin)
    c.post("/dossiers", data={"code": "HALLES", "nom": "SCI des Halles", "variable_jeton": "PENNYLANE_TOKEN_HALLES"})
    c.post("/dossiers/HALLES/baux", data=BAIL)
    c.post("/baux/1/pieces", data={"nature": "Bail"}, files={"fichier": ("bail.pdf", PDF, "application/pdf")})
    return chemin, c, tmp_path / "data" / "test" / "test.sqlite"


def test_copie_vers_le_test_et_marquage(production):
    prod, c, test = production
    assert "base de production" in c.get("/").text and "Base de test" not in c.get("/").text
    bilan = db.copier_vers_test(prod, test)
    assert (bilan["dossiers"], bilan["baux"], bilan["pieces"], bilan["utilisateurs"]) == (1, 1, 1, 1)
    assert db.environnement(db.connecter(test)) == "test" and db.environnement(db.connecter(prod)) == "production"
    assert db.connecter(test).execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0     # aucune session ne suit la copie
    app_test = creer_app(test, AUJ)
    anonyme = TestClient(app_test, headers={"origin": "http://testserver"})
    assert "Base de test" in anonyme.get("/connexion").text                  # visible avant même de saisir son mot de passe
    t = client_connecte(app_test, test)                                      # même compte, même mot de passe que la production
    page = t.get("/baux/1").text
    assert "Base de test" in page and "[TEST]" in page and "Locataire" in page
    assert t.get("/pieces/1").content == PDF                                 # les pièces ont suivi
    # les deux consoles tournent sur le même poste : leurs sessions ne se marchent pas dessus
    assert "console_session_test" in t.cookies and "console_session_test" not in c.cookies and "console_session" in c.cookies
    croise = TestClient(app_test, headers={"origin": "http://testserver"}, cookies={"console_session_test": c.cookies["console_session"]})
    assert croise.get("/baux/1", follow_redirects=False).status_code == 303  # une session de production ne vaut rien dans le test
    # ce qu'on saisit dans le test ne touche pas la production
    t.post("/dossiers/HALLES/baux", data={**BAIL, "code": "B02", "locataire": "Essai"})
    assert db.connecter(test).execute("SELECT COUNT(*) AS n FROM baux").fetchone()["n"] == 2
    assert db.connecter(prod).execute("SELECT COUNT(*) AS n FROM baux").fetchone()["n"] == 1


def test_base_de_test_ne_cree_rien_dans_pennylane(production, monkeypatch):
    """Même avec l'écriture activée et un jeton d'écriture présent, une base de test simule la création."""
    prod, c, test = production
    db.copier_vers_test(prod, test)
    monkeypatch.setenv("CONSOLE_ECRITURE", "oui")
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES", "jeton-lecture")
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES_ECRITURE", "jeton-ecriture")

    def interdit(*a, **k):
        raise AssertionError("appel réseau depuis une base de test")

    monkeypatch.setattr(ecriture.urllib.request, "urlopen", interdit)
    monkeypatch.setattr(ecriture, "creer_dans_pennylane", interdit)
    t = client_connecte(creer_app(test, AUJ), test)
    page = t.get("/baux/1/abonnement").text
    assert "Simuler la création" in page and "Créer l’abonnement dans Pennylane" not in page
    empreinte = page.split('name="empreinte" value="')[1].split('"')[0]
    r = t.post("/baux/1/abonnement", data={"empreinte": empreinte, "confirmation": "B01"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/envois/")
    e = db.connecter(test).execute("SELECT mode, etat FROM envois").fetchone()
    assert (e["mode"], e["etat"]) == ("simulation", "créé")
    assert db.connecter(prod).execute("SELECT COUNT(*) AS n FROM envois").fetchone()["n"] == 0


def test_une_base_de_production_n_est_jamais_ecrasee(production, tmp_path, capsys):
    prod, c, test = production
    autre_prod = tmp_path / "data" / "autre.sqlite"
    db.connecter(autre_prod).close()
    with pytest.raises(RuntimeError, match="jamais écrasée"):
        db.copier_vers_test(prod, autre_prod, remplacer=True)
    with pytest.raises(RuntimeError, match="elle-même"):
        db.copier_vers_test(prod, prod, remplacer=True)
    db.copier_vers_test(prod, test)
    with pytest.raises(RuntimeError, match="existe déjà"):
        db.copier_vers_test(prod, test)                                      # pas de remplacement sans le demander
    with pytest.raises(RuntimeError, match="déjà une base de test"):
        db.copier_vers_test(test, tmp_path / "data" / "test2.sqlite")
    # le test diverge, puis on le rafraîchit : il redevient la copie de la production
    cx = db.connecter(test)
    cx.execute("UPDATE baux SET locataire='Modifié dans le test'")
    cx.commit()
    cx.close()
    assert main(["--base", str(prod), "copier-vers-test", "--vers", str(test), "--remplacer"]) == 0
    assert db.connecter(test).execute("SELECT locataire FROM baux").fetchone()["locataire"] == "Locataire"
    assert db.connecter(prod).execute("SELECT locataire FROM baux").fetchone()["locataire"] == "Locataire"
    # une base de test ne redevient pas une base de production
    assert main(["--base", str(test), "environnement", "production"]) == 2
    assert main(["--base", str(test), "environnement"]) == 0 and "TEST" in capsys.readouterr().out


def test_preparer_test_est_idempotent(production, tmp_path, capsys):
    prod, c, test = production
    assert main(["--base", str(prod), "preparer-test", "--vers", str(test)]) == 0 and test.exists()
    cx = db.connecter(test)
    cx.execute("UPDATE baux SET locataire='Travail en cours'")
    cx.commit()
    cx.close()
    assert main(["--base", str(prod), "preparer-test", "--vers", str(test)]) == 0        # déjà là : rien n'est touché
    assert db.connecter(test).execute("SELECT locataire FROM baux").fetchone()["locataire"] == "Travail en cours"
    # sans production à copier : base de test vide, et aucune base de production créée par mégarde
    absente, vide = tmp_path / "ailleurs" / "cabinet.sqlite", tmp_path / "ailleurs" / "test" / "test.sqlite"
    assert main(["--base", str(absente), "preparer-test", "--vers", str(vide)]) == 0
    assert db.environnement(db.connecter(vide)) == "test" and not absente.exists()


class _Reponse:
    status = 200

    def __init__(self, corps):
        self.corps = corps

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        import json
        return json.dumps(self.corps).encode()


def test_tester_la_connexion_pennylane_lecture_seule(production, monkeypatch):
    import io
    import urllib.error
    from console import pennylane_api
    prod, c, _ = production
    # sans jeton sur le poste : aucun appel, et la marche à suivre
    monkeypatch.delenv("PENNYLANE_TOKEN_HALLES", raising=False)
    monkeypatch.setattr(pennylane_api.urllib.request, "urlopen", lambda *a, **k: pytest.fail("appel sans jeton"))
    page = c.post("/dossiers/HALLES/connexion-pennylane").text
    assert "PENNYLANE_TOKEN_HALLES" in page and "variables d" in page
    # jeton d'un environnement de test : deux lectures GET, la marque Sandbox est reconnue, le jeton n'est jamais affiché
    appels = []

    def faux(req, timeout=30):
        appels.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        if req.full_url.endswith("/me"):
            return _Reponse({"user": {"id": 7, "email": "moi@exemple.fr"}, "company": {"id": 99, "name": "Sandbox - moi@exemple.fr"}})
        return _Reponse({"items": [], "has_more": False, "next_cursor": None})

    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES", "jeton-secret-de-test")
    monkeypatch.setattr(pennylane_api.urllib.request, "urlopen", faux)
    page = c.post("/dossiers/HALLES/connexion-pennylane").text
    assert [m for m, _, _ in appels] == ["GET", "GET"] and all(a == "Bearer jeton-secret-de-test" for _, _, a in appels)
    assert appels[0][1].endswith("/api/external/v2/me") and "/billing_subscriptions?limit=1" in appels[1][1]
    assert "jeton-secret-de-test" not in page and "company.name = Sandbox - moi@exemple.fr" in page
    assert "Reconnu" in page and "Possible" in page
    # dossier réel : aucune marque, la console le dit
    monkeypatch.setattr(pennylane_api.urllib.request, "urlopen", lambda req, timeout=30: _Reponse(
        {"company": {"id": 5, "name": "SCI des Halles"}} if req.full_url.endswith("/me") else {"items": []}))
    assert "à considérer comme celui d" in c.post("/dossiers/HALLES/connexion-pennylane").text
    # jeton refusé : l'échec est affiché, rien ne plante
    def refuse(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error": "The access token is invalid", "status": 401}'))
    monkeypatch.setattr(pennylane_api.urllib.request, "urlopen", refuse)
    page = c.post("/dossiers/HALLES/connexion-pennylane").text
    assert "HTTP 401" in page and "The access token is invalid" in page and "refuse le jeton" in page
    assert pennylane_api.marques_sandbox({"a": [{"n": "sandbox-123"}], "b": "Bac à sable", "c": 3}) == ["a[0].n = sandbox-123"]
    h = [r["detail"] for r in db.connecter(prod).execute("SELECT detail FROM historique WHERE action='Connexion Pennylane testée'")]
    assert len(h) == 4 and "environnement de test reconnu" in h[1] and "jeton-secret" not in " ".join(h)
    lecteur = client_connecte(creer_app(prod, AUJ), prod, "lecture")
    assert lecteur.post("/dossiers/HALLES/connexion-pennylane").status_code == 403

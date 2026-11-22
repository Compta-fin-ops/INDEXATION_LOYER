"""Comptes de la console : identification par mot de passe, session, rôles, traçabilité nominative."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from console import comptes, db, simulation
from console.app import creer_app
from tests.outils import MOT_DE_PASSE, client_connecte

AUJ = date(2026, 11, 22)
ORIGINE = {"origin": "http://testserver"}
BAIL = {"code": "B01", "locataire": "Locataire", "local": "1 rue Test", "date_effet": "2023-04-01", "loyer_initial_annuel": "12000",
        "indice": "ILC", "trimestre_base": "2022-T4", "customer_id": "77"}


@pytest.fixture
def base(tmp_path):
    chemin = tmp_path / "base.sqlite"
    app = creer_app(chemin, AUJ, stockage=tmp_path / "pieces")
    admin = client_connecte(app, chemin, "administrateur")
    assert admin.post("/dossiers", data={"code": "HALLES", "nom": "SCI des Halles"}, follow_redirects=False).status_code == 303
    assert admin.post("/dossiers/HALLES/baux", data=BAIL, follow_redirects=False).headers["location"] == "/baux/1"
    return app, chemin, admin, db.connecter(chemin)


def test_rien_n_est_servi_sans_session(base):
    app, chemin, _, cx = base
    anonyme = TestClient(app, headers=ORIGINE)
    r = anonyme.get("/baux/1?message=x", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connexion?suite=%2Fbaux%2F1%3Fmessage%3Dx"
    for url in ("/", "/dossiers/HALLES", "/indices", "/envois", "/utilisateurs", "/pieces/1", "/openapi.json"):
        assert anonyme.get(url, follow_redirects=False).status_code == 303, url
    for url in ("/api/synthese", "/api/ecarts", "/api/docs"):
        assert anonyme.get(url).status_code == 401, url
    assert anonyme.post("/baux/1/revisions/1", data={"decision": "Geler – sans rattrapage"}).status_code == 401
    assert anonyme.post("/dossiers", data={"code": "PARC", "nom": "SCI du Parc"}).status_code == 401
    assert cx.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"] == 0
    assert cx.execute("SELECT COUNT(*) AS n FROM dossiers").fetchone()["n"] == 1
    assert anonyme.get("/static/console.css").status_code == 200 and anonyme.get("/connexion").status_code == 200
    assert "Locataire" not in anonyme.get("/connexion").text                  # la page de connexion ne montre aucune donnée


def test_connexion_temoin_et_deconnexion(base):
    app, chemin, _, cx = base
    c = TestClient(app, headers=ORIGINE)
    r = c.post("/connexion", data={"identifiant": "Test.Administrateur ", "mot_de_passe": MOT_DE_PASSE, "suite": "/baux/1"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/baux/1"
    temoin = r.headers["set-cookie"].lower()
    assert "httponly" in temoin and "samesite=lax" in temoin and "path=/" in temoin
    jeton = c.cookies["console_session"]
    stocke = [tuple(l) for l in cx.execute("SELECT empreinte FROM sessions")]
    assert stocke and all(jeton not in e for (e,) in stocke)                  # le jeton de session n'est pas en base
    mdp = cx.execute("SELECT mot_de_passe FROM utilisateurs").fetchone()["mot_de_passe"]
    assert mdp.startswith("scrypt$") and MOT_DE_PASSE not in mdp
    assert c.get("/baux/1").status_code == 200 and "Test administrateur" in c.get("/").text
    assert c.post("/deconnexion", follow_redirects=False).headers["location"] == "/connexion"
    assert c.get("/baux/1", follow_redirects=False).status_code == 303
    # un jeton volé avant la déconnexion ne vaut plus rien
    voleur = TestClient(app, headers=ORIGINE, cookies={"console_session": jeton})
    assert voleur.get("/baux/1", follow_redirects=False).status_code == 303
    # une redirection vers un autre site est neutralisée
    r = c.post("/connexion", data={"identifiant": "test.administrateur", "mot_de_passe": MOT_DE_PASSE, "suite": "//exemple.org/x"},
               follow_redirects=False)
    assert r.headers["location"] == "/"


def test_mauvais_mot_de_passe_et_blocage(base):
    app, chemin, _, cx = base
    c = TestClient(app, headers=ORIGINE)
    for identifiant in ("test.administrateur", "inconnu"):                    # même réponse, compte existant ou non
        r = c.post("/connexion", data={"identifiant": identifiant, "mot_de_passe": "mauvais mot de passe"})
        assert r.status_code == 401 and "Identifiant ou mot de passe incorrect" in r.text
    for _ in range(comptes.ECHECS_MAX - 1):
        c.post("/connexion", data={"identifiant": "test.administrateur", "mot_de_passe": "mauvais mot de passe"})
    r = c.post("/connexion", data={"identifiant": "test.administrateur", "mot_de_passe": MOT_DE_PASSE})
    assert r.status_code == 401 and "bloqué" in r.text                        # le bon mot de passe ne passe plus
    cx.execute("UPDATE utilisateurs SET bloque_jusqu='2000-01-01T00:00:00'")
    cx.commit()
    assert c.post("/connexion", data={"identifiant": "test.administrateur", "mot_de_passe": MOT_DE_PASSE},
                  follow_redirects=False).status_code == 303


def test_roles(base):
    app, chemin, admin, cx = base
    lecteur, gestionnaire, valideur = (client_connecte(app, chemin, r) for r in ("lecture", "gestion", "validation"))
    gel = {"decision": "Geler – sans rattrapage", "commentaire": "essai"}
    for url in ("/", "/dossiers/HALLES", "/baux/1", "/indices", "/envois", "/echeancier", "/compte"):
        assert lecteur.get(url).status_code == 200, url
    r = lecteur.post("/baux/1/revisions/1", data=gel)
    assert r.status_code == 403 and "Rien n" in r.text and cx.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"] == 0
    assert lecteur.post("/dossiers/HALLES/baux", data={**BAIL, "code": "B02"}).status_code == 403
    assert lecteur.post("/actions/synchroniser").status_code == 403
    assert gestionnaire.post("/baux/1/revisions/1", data=gel, follow_redirects=False).status_code == 303
    d = cx.execute("SELECT * FROM decisions").fetchone()
    assert d["decision"] == gel["decision"] and d["auteur"] == "test.gestion"
    h = cx.execute("SELECT auteur FROM historique WHERE action='Décision de révision'").fetchone()
    assert h["auteur"] == "test.gestion"                                      # l'historique porte le nom, plus « poste local »
    # créer un abonnement dans Pennylane : validation au moins
    assert gestionnaire.post("/baux/1/abonnement", data={"confirmation": "B01"}).status_code == 403
    assert gestionnaire.get("/baux/1/abonnement").status_code == 200          # voir la proposition reste permis
    assert valideur.post("/baux/1/abonnement", data={"confirmation": "B01"}).status_code != 403
    for c in (lecteur, gestionnaire, valideur):
        assert c.get("/utilisateurs").status_code == 403
        assert c.post("/utilisateurs", data={"identifiant": "intrus", "nom": "Intrus", "role": "administrateur",
                                             "mot_de_passe": MOT_DE_PASSE}).status_code == 403
    assert admin.get("/utilisateurs").status_code == 200
    assert cx.execute("SELECT COUNT(*) AS n FROM utilisateurs WHERE identifiant='intrus'").fetchone()["n"] == 0


def test_cycle_de_vie_d_un_compte(base):
    app, chemin, admin, cx = base
    assert admin.post("/utilisateurs", data={"identifiant": "court", "nom": "Court", "role": "gestion", "mot_de_passe": "trop court"}).status_code == 400
    r = admin.post("/utilisateurs", data={"identifiant": "Claire.Martin", "nom": "Claire Martin", "role": "gestion",
                                          "mot_de_passe": "provisoire du lundi matin"})
    assert r.status_code == 200 and "Compte créé" in r.text
    claire = TestClient(app, headers=ORIGINE)
    assert claire.post("/connexion", data={"identifiant": "claire.martin", "mot_de_passe": "provisoire du lundi matin"},
                       follow_redirects=False).status_code == 303
    # mot de passe provisoire : rien d'autre que la page du compte tant qu'il n'est pas changé
    assert claire.get("/baux/1", follow_redirects=False).headers["location"] == "/compte"
    assert claire.post("/baux/1/revisions/1", data={"decision": "Appliquer"}, follow_redirects=False).headers["location"] == "/compte"
    for donnees, attendu in (({"actuel": "faux", "nouveau": "une phrase bien à moi", "confirmation": "une phrase bien à moi"}, "actuel incorrect"),
                             ({"actuel": "provisoire du lundi matin", "nouveau": "une phrase bien à moi", "confirmation": "autre chose encore"}, "diffèrent"),
                             ({"actuel": "provisoire du lundi matin", "nouveau": "court", "confirmation": "court"}, "12 caractères")):
        r = claire.post("/compte", data=donnees)
        assert r.status_code == 400 and attendu in r.text
    assert claire.post("/compte", data={"actuel": "provisoire du lundi matin", "nouveau": "une phrase bien à moi",
                                        "confirmation": "une phrase bien à moi"}, follow_redirects=False).status_code == 303
    assert claire.get("/baux/1").status_code == 200
    # l'administrateur change le rôle : effet immédiat, la session tombe
    id_claire = cx.execute("SELECT id FROM utilisateurs WHERE identifiant='claire.martin'").fetchone()["id"]
    assert admin.post(f"/utilisateurs/{id_claire}", data={"role": "lecture", "actif": "1"}).status_code == 200
    assert claire.get("/baux/1", follow_redirects=False).status_code == 303
    # désactivé : plus de connexion possible
    admin.post(f"/utilisateurs/{id_claire}", data={"role": "lecture", "actif": "0"})
    assert claire.post("/connexion", data={"identifiant": "claire.martin", "mot_de_passe": "une phrase bien à moi"}).status_code == 401
    # il reste toujours un administrateur actif
    id_admin = cx.execute("SELECT id FROM utilisateurs WHERE identifiant='test.administrateur'").fetchone()["id"]
    for donnees in ({"role": "gestion", "actif": "1"}, {"role": "administrateur", "actif": "0"}):
        r = admin.post(f"/utilisateurs/{id_admin}", data=donnees)
        assert r.status_code == 400 and "au moins un administrateur" in r.text
    assert admin.get("/utilisateurs").status_code == 200
    actions = [r["action"] for r in cx.execute("SELECT action FROM historique WHERE action LIKE 'Compte%' OR action LIKE 'Mot de passe%'")]
    assert actions.count("Compte créé") == 1 and "Compte modifié" in actions and "Mot de passe changé" in actions


def test_formulaire_sans_origine_refuse_et_demo_conserve_les_comptes(base, tmp_path):
    app, chemin, admin, cx = base
    sans_origine = TestClient(app, cookies={"console_session": admin.cookies["console_session"]})
    assert sans_origine.get("/baux/1").status_code == 200
    assert sans_origine.post("/baux/1/revisions/1", data={"decision": "Geler – sans rattrapage"}).status_code == 403
    assert sans_origine.post("/baux/1/revisions/1", data={"decision": "Appliquer"}, headers={"referer": "http://testserver/baux/1"},
                             follow_redirects=False).status_code == 303          # Referer accepté à défaut d'Origin
    demo = db.connecter(tmp_path / "demo.sqlite")
    comptes.creer(demo, "moi", "Moi", "administrateur", MOT_DE_PASSE)
    simulation.charger_demo(demo, AUJ)
    assert demo.execute("SELECT COUNT(*) AS n FROM utilisateurs").fetchone()["n"] == 1   # recharger la démo ne retire pas les comptes


def test_ligne_de_commande_premier_compte(tmp_path, monkeypatch, capsys):
    from console.__main__ import main
    chemin = str(tmp_path / "cli.sqlite")
    saisies = iter(["mauvais", MOT_DE_PASSE, MOT_DE_PASSE])                   # 1er essai trop court, puis deux saisies identiques
    monkeypatch.setattr("getpass.getpass", lambda invite="": next(saisies))
    assert main(["--base", chemin, "utilisateur", "creer", "jm", "--nom", "JM"]) == 0
    u = db.connecter(chemin).execute("SELECT * FROM utilisateurs").fetchone()
    assert (u["identifiant"], u["role"], u["doit_changer"]) == ("jm", "administrateur", 0)   # le premier compte administre
    assert comptes.verifier(MOT_DE_PASSE, u["mot_de_passe"]) and MOT_DE_PASSE not in capsys.readouterr().out
    assert main(["--base", chemin, "utilisateur", "lister"]) == 0 and "jm" in capsys.readouterr().out

"""Création d'abonnement : la seule écriture de la console, et tous ses verrous."""
import io
import json
import urllib.error
import urllib.parse
from datetime import date

import pytest
from fastapi.testclient import TestClient

from console import db, depot, ecriture, rapprochement as rp, simulation
from console.app import creer_app
from tests.outils import client_connecte
from tests.test_console import AUJ, _abo, _bail, _facture


def _controles(bail, abos, envois=(), simulation_=True, dossier=None):
    sit = rp.analyser_bail(bail, abos, [_facture()], AUJ)
    prop = rp.proposer_abonnement(bail, sit.courant, AUJ)
    prop["corps"] = ecriture.imposer_brouillon(prop["corps"])
    return ecriture.controler(sit, prop, dossier or {"code": "D", "variable_jeton": "PENNYLANE_TOKEN_D"}, list(envois), AUJ, simulation_), prop


def _echecs(controles):
    return {c.libelle for c in controles if not c.ok and c.bloquant}


def test_cas_nominal_abonnement_borne_periode_suivante():
    bail = _bail(loyer_prochain_annuel=12900.0)                      # indice publié : loyer révisé connu
    controles, prop = _controles(bail, [_abo()])                     # abonnement en cours borné au 01/03/2027
    assert ecriture.envoi_possible(controles), _echecs(controles)
    assert prop["debut"] == "2027-04-01" and prop["corps"]["mode"] == {"type": "awaiting_validation"}
    assert prop["corps"]["recurring_rule"]["count"] == 12 and prop["derniere_occurrence"] == "2028-03-01"
    assert prop["corps"]["customer_invoice_data"]["invoice_lines"][0]["raw_currency_unit_price"] == "1075.00"
    # sans loyer révisé connu, la même situation est bloquée : on ne facture pas un loyer inconnu
    controles, _ = _controles(_bail(), [_abo()])
    assert _echecs(controles) == {"Loyer de la période connu"}


def test_refus_chevauchement_abonnement_sans_terme_ou_suivant_existant():
    controles, _ = _controles(_bail(), [_abo(fin=None)])
    assert "Aucun chevauchement avec un abonnement existant" in _echecs(controles)
    detail = next(c.detail for c in controles if c.libelle.startswith("Aucun chevauchement"))
    assert "n'arrête jamais" in detail
    suivant = _abo(id=501, statut="not_started", debut="2027-04-01", fin="2028-03-01")
    controles, _ = _controles(_bail(), [_abo(), suivant])
    assert "Aucun chevauchement avec un abonnement existant" in _echecs(controles)


def test_refus_client_absent_montant_incoherent_et_revision_intercalee():
    controles, _ = _controles(_bail(customer_id=None), [])
    assert "Client Pennylane identifié" in _echecs(controles)
    controles, _ = _controles(_bail(loyer_prochain_annuel=20000.0), [_abo()])      # +56 % sur l'abonnement en cours
    assert any(l.startswith("Montant cohérent") for l in _echecs(controles))
    b = _bail(date_derniere_revision="2025-12-01", date_prochaine_revision="2026-12-01")
    controles, _ = _controles(b, [])
    assert "Loyer de la période connu" in _echecs(controles)


def test_refus_si_deja_envoye_ou_issue_incertaine():
    _bail_connu = lambda: _bail(loyer_prochain_annuel=12900.0)
    _, prop = _controles(_bail_connu(), [_abo()])
    cle = ecriture.cle_idempotence("D", "B01", prop["debut"])
    cree = {"id": 1, "cle": cle, "etat": "créé", "horodatage": "2026-11-20T10:00:00"}
    controles, _ = _controles(_bail_connu(), [_abo()], envois=[cree])
    assert "Aucun envoi antérieur pour ce bail et cette période" in _echecs(controles)
    incertain = {**cree, "etat": "incertain"}
    avant = {"code": "D", "variable_jeton": "V", "derniere_synchro": "2026-11-20T09:00:00"}
    apres = {"code": "D", "variable_jeton": "V", "derniere_synchro": "2026-11-20T11:00:00"}
    assert "Aucun envoi antérieur pour ce bail et cette période" in _echecs(_controles(_bail_connu(), [_abo()], [incertain], dossier=avant)[0])
    # une relecture de Pennylane postérieure à la tentative lève le blocage : l'état réel fait foi
    assert "Aucun envoi antérieur pour ce bail et cette période" not in _echecs(_controles(_bail_connu(), [_abo()], [incertain], dossier=apres)[0])


def test_mode_reel_verrouille_par_defaut(monkeypatch):
    monkeypatch.delenv("CONSOLE_ECRITURE", raising=False)
    controles, prop = _controles(_bail(), [_abo()], simulation_=False)
    assert {"Écriture activée sur ce poste", "Jeton d'écriture distinct du jeton de lecture",
            "État Pennylane relu depuis moins de 24 heures"} <= _echecs(controles)

    def interdit(*a, **k):
        raise AssertionError("aucun appel réseau ne doit partir")
    monkeypatch.setattr(ecriture.urllib.request, "urlopen", interdit)
    with pytest.raises(PermissionError, match="CONSOLE_ECRITURE"):
        ecriture.creer_dans_pennylane({"variable_jeton": "PENNYLANE_TOKEN_D"}, prop["corps"])
    monkeypatch.setenv("CONSOLE_ECRITURE", "oui")
    with pytest.raises(PermissionError, match="brouillon"):          # un corps au mode « finalisé » est refusé
        ecriture.creer_dans_pennylane({"variable_jeton": "V"}, {**prop["corps"], "mode": {"type": "finalized"}})
    with pytest.raises(PermissionError, match="Jeton d'écriture"):   # le jeton de lecture ne sert jamais à écrire
        monkeypatch.setenv("PENNYLANE_TOKEN_D", "jeton-de-lecture")
        ecriture.creer_dans_pennylane({"variable_jeton": "PENNYLANE_TOKEN_D"}, prop["corps"])
    assert ecriture.imposer_brouillon({"mode": {"type": "email"}})["mode"] == {"type": "awaiting_validation"}


@pytest.fixture()
def console_simulee(tmp_path):
    chemin = tmp_path / "demo.sqlite"
    cx = db.connecter(chemin)
    simulation.charger_demo(cx, AUJ)
    return cx, client_connecte(creer_app(chemin, AUJ), chemin)


def _formulaire(page: str, confirmation: str) -> str:
    emp = page.split('name="empreinte" value="')[1].split('"')[0]
    return urllib.parse.urlencode({"empreinte": emp, "confirmation": confirmation})


def test_parcours_simule_de_bout_en_bout(console_simulee):
    cx, c = console_simulee
    cible = next(e for e in c.get("/api/ecarts").json() if e["type"] == "RENOUVELLEMENT" and "Loyer révisé connu" in e["detail"])
    inconnu = next(e for e in c.get("/api/ecarts").json() if e["type"] == "RENOUVELLEMENT" and "non encore connu" in e["detail"])
    assert "Création impossible" in c.get(f"/baux/{inconnu['bail_id']}/abonnement").text
    url = f"/baux/{cible['bail_id']}/abonnement"
    page = c.get(url)
    assert page.status_code == 200 and "Contrôles préalables" in page.text and "Simuler la création" in page.text
    nb_abos = cx.execute("SELECT COUNT(*) AS n FROM abonnements").fetchone()["n"]
    entetes = {"content-type": "application/x-www-form-urlencoded"}

    assert c.post(url, content=_formulaire(page.text, "MAUVAIS"), headers=entetes).status_code == 400
    faux = urllib.parse.urlencode({"empreinte": "0" * 64, "confirmation": cible["bail_code"]})
    assert c.post(url, content=faux, headers=entetes).status_code == 409
    assert cx.execute("SELECT COUNT(*) AS n FROM envois").fetchone()["n"] == 0       # aucun refus n'écrit quoi que ce soit

    ok = c.post(url, content=_formulaire(page.text, cible["bail_code"].lower()), headers=entetes, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/envois/1"
    e = dict(cx.execute("SELECT * FROM envois").fetchone())
    assert (e["etat"], e["mode"], e["statut_http"]) == ("créé", "simulation", 201)
    assert json.loads(e["corps"])["mode"] == {"type": "awaiting_validation"}
    assert cx.execute("SELECT COUNT(*) AS n FROM abonnements").fetchone()["n"] == nb_abos + 1
    cree = dict(cx.execute("SELECT * FROM abonnements WHERE id=?", (e["subscription_id"],)).fetchone())
    assert cree["statut"] == "not_started" and cree["fin"] is not None

    # l'existant n'a pas bougé : seul un abonnement a été ajouté
    assert cx.execute("SELECT COUNT(*) AS n FROM abonnements WHERE id<800000").fetchone()["n"] == nb_abos
    # second essai : la page n'offre plus de formulaire et un POST direct est refusé
    seconde = c.get(url).text
    assert 'name="empreinte"' not in seconde and "Création impossible" in seconde
    rejoue = c.post(url, content=urllib.parse.urlencode({"empreinte": e["empreinte"], "confirmation": cible["bail_code"]}), headers=entetes)
    assert rejoue.status_code == 409 and cx.execute("SELECT COUNT(*) AS n FROM envois").fetchone()["n"] == 1
    assert c.get("/envois").status_code == 200 and "Envoi simulé" in c.get("/envois/1").text
    assert "RENOUVELLEMENT" not in {x["type"] for x in c.get("/api/ecarts").json() if x["bail_id"] == cible["bail_id"]}


class _FauxPennylane:
    """Remplace urlopen : listes vides en lecture, et une réponse configurable à la création."""
    def __init__(self, a_la_creation):
        self.a_la_creation, self.requetes = a_la_creation, []

    def __call__(self, req, timeout=60):
        self.requetes.append(req)
        if req.get_method() == "GET":
            return _Reponse(200, {"items": [], "has_more": False})
        return self.a_la_creation(req)


class _Reponse(io.BytesIO):
    def __init__(self, statut, corps):
        super().__init__(json.dumps(corps).encode("utf-8"))
        self.status = statut

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def console_reelle(tmp_path, monkeypatch):
    chemin = tmp_path / "reel.sqlite"
    cx = db.connecter(chemin)
    depot.enregistrer_dossier(cx, "HALLES", "SCI des Halles", variable_jeton="PENNYLANE_TOKEN_HALLES", source="classeur.xlsx")
    depot.enregistrer_baux(cx, "HALLES", [_bail(code="B01")])
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES", "jeton-lecture")
    return cx, client_connecte(creer_app(chemin, AUJ), chemin), {"content-type": "application/x-www-form-urlencoded"}


def test_mode_reel_sans_activation_aucune_ecriture(console_reelle, monkeypatch):
    cx, c, entetes = console_reelle
    monkeypatch.delenv("CONSOLE_ECRITURE", raising=False)
    faux = _FauxPennylane(lambda req: (_ for _ in ()).throw(AssertionError("création interdite")))
    monkeypatch.setattr("urllib.request.urlopen", faux)
    page = c.get("/baux/1/abonnement").text
    assert "Création impossible" in page and "CONSOLE_ECRITURE" in page and 'name="empreinte"' not in page
    r = c.post("/baux/1/abonnement", content="empreinte=x&confirmation=B01", headers=entetes)
    assert r.status_code == 409 and all(q.get_method() == "GET" for q in faux.requetes)
    assert cx.execute("SELECT COUNT(*) AS n FROM envois").fetchone()["n"] == 0


def test_mode_reel_active_creation_puis_reponse_perdue(console_reelle, monkeypatch):
    cx, c, entetes = console_reelle
    monkeypatch.setenv("CONSOLE_ECRITURE", "oui")
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES_ECRITURE", "jeton-ecriture")
    faux = _FauxPennylane(lambda req: _Reponse(201, {"id": 4242, "status": "not_started"}))
    monkeypatch.setattr("urllib.request.urlopen", faux)
    c.post("/actions/synchroniser")                                              # première collecte
    page = c.get("/baux/1/abonnement").text
    assert "Créer l’abonnement dans Pennylane" in page
    r = c.post("/baux/1/abonnement", content=_formulaire(page, "B01"), headers=entetes, follow_redirects=False)
    assert r.status_code == 303
    creations = [q for q in faux.requetes if q.get_method() == "POST"]
    assert len(creations) == 1 and creations[0].full_url.endswith("/api/external/v2/billing_subscriptions")
    assert creations[0].get_header("Authorization") == "Bearer jeton-ecriture"   # jamais le jeton de lecture
    envoye = json.loads(creations[0].data)
    assert envoye["mode"] == {"type": "awaiting_validation"} and envoye["recurring_rule"]["count"] == 4
    assert {q.get_method() for q in faux.requetes} == {"GET", "POST"}            # aucune modification, aucune suppression
    e = dict(cx.execute("SELECT * FROM envois").fetchone())
    assert (e["etat"], e["subscription_id"], e["statut_abonnement"]) == ("créé", 4242, "not_started")

    # réponse perdue : l'issue est journalisée « incertain » et tout nouvel essai est bloqué
    cx.execute("DELETE FROM envois"); cx.commit()
    faux.a_la_creation = lambda req: (_ for _ in ()).throw(TimeoutError("délai dépassé"))
    page = c.get("/baux/1/abonnement").text
    c.post("/baux/1/abonnement", content=_formulaire(page, "B01"), headers=entetes, follow_redirects=False)
    assert cx.execute("SELECT etat FROM envois").fetchone()["etat"] == "incertain"
    assert "Issue incertaine" in c.get("/envois/2").text
    bloque = c.get("/baux/1/abonnement").text
    assert "Création impossible" in bloque and "issue incertaine" in bloque

    # refus explicite de Pennylane : rien n'est créé, l'état est « refusé »
    cx.execute("DELETE FROM envois"); cx.commit()
    faux.a_la_creation = lambda req: (_ for _ in ()).throw(urllib.error.HTTPError(
        req.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"error": "customer not found", "status": 422}')))
    page = c.get("/baux/1/abonnement").text
    c.post("/baux/1/abonnement", content=_formulaire(page, "B01"), headers=entetes, follow_redirects=False)
    e = dict(cx.execute("SELECT * FROM envois").fetchone())
    assert e["etat"] == "refusé" and e["statut_http"] == 422 and "customer not found" in e["reponse"]


def test_la_console_ne_contient_aucun_verbe_destructeur():
    """Garde-fou de construction : ni PUT, ni PATCH, ni DELETE vers Pennylane dans le code de la console."""
    from pathlib import Path
    import console
    for fichier in Path(console.__file__).parent.glob("*.py"):
        source = fichier.read_text(encoding="utf-8")
        for verbe in ('method="PUT"', 'method="PATCH"', 'method="DELETE"', "method='PUT'", "method='PATCH'", "method='DELETE'"):
            assert verbe not in source, f"{fichier.name} contient {verbe}"
    assert Path(console.__file__).parent.joinpath("ecriture.py").read_text(encoding="utf-8").count('method="POST"') == 1

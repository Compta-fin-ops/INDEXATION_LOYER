"""Console de pilotage : règles de rapprochement, collecte simulée, import d'un classeur, pages web."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from console import db, depot, pennylane_api, rapprochement as rp, simulation
from console.app import creer_app

AUJ = date(2026, 11, 22)


def _bail(**kw):
    b = {"id": 1, "dossier": "D", "code": "B01", "local": "1 rue Test", "locataire": "Locataire", "customer_id": 10,
         "subscription_id": None, "indice": "ILC", "periodicite": "Mensuelle", "tva_pct": 20.0,
         "date_effet": "2022-04-01", "date_fin": "2031-04-01", "loyer_initial_annuel": 12000.0,
         "loyer_attendu_annuel": 12600.0, "charges_annuelles": 1200.0, "date_derniere_revision": "2026-04-01",
         "date_prochaine_revision": "2027-04-01", "statut_revision": "✔ Calculable", "revisions_non_appliquees": 0}
    return {**b, **kw}


def _abo(**kw):
    a = {"id": 500, "customer_id": 10, "label": "Loyer", "statut": "in_progress", "mode": "awaiting_validation",
         "debut": "2026-04-01", "fin": "2027-03-01", "prochaine_occurrence": "2026-12-01",
         "occurrence_precedente": "2026-11-01", "type_regle": "monthly", "intervalle": 1, "montant_ht": 1150.0}
    return {**a, **kw}


def _facture(**kw):
    f = {"id": 900, "customer_id": 10, "subscription_id": 500, "numero": "F-1", "date": "2026-11-01",
         "echeance": "2026-11-11", "montant_ht": 1150.0, "montant_ttc": 1380.0, "restant_du": 0.0, "statut": "paid"}
    return {**f, **kw}


def _types(s):
    return {e.type for e in s.ecarts}


def test_bail_conforme():
    s = rp.analyser_bail(_bail(), [_abo()], [_facture()], AUJ)
    assert s.ecarts == [] and s.gravite == "ok"
    assert s.attendu_annuel == 13800.0 and s.facture_annuel == 13800.0


def test_revision_non_repercutee_et_rappel():
    ancien = _abo(debut="2025-04-01", fin=None, montant_ht=1125.0)       # 12 300 + 1 200 de charges, ancien loyer
    s = rp.analyser_bail(_bail(), [ancien], [_facture(montant_ht=1125.0)], AUJ)
    assert _types(s) == {"REVISION_NON_REPERCUTEE"}                       # pas de double signalement « non borné »
    e = s.ecarts[0]
    assert e.gravite == "bloquant" and e.montant == 300.0                 # 25 € × 12
    # avril à novembre 2026 : 8 échéances facturées à l'ancien loyer
    assert s.rappel == {"echeances": 8, "ecart_par_echeance": 25.0, "montant": 200.0, "depuis": "2026-04-01"}
    p = s.proposition
    assert p["debut"] == "2026-12-01" and p["occurrences"] == 4 and p["derniere_occurrence"] == "2027-03-01"
    regle = p["corps"]["recurring_rule"]
    assert regle == {"type": "monthly", "interval": 1, "day_of_month": 1, "count": 4}
    lignes = p["corps"]["customer_invoice_data"]["invoice_lines"]
    assert [l["raw_currency_unit_price"] for l in lignes] == ["1050.00", "100.00"]
    assert lignes[0]["label"].startswith("Loyer mensuel –")


def test_proposition_quand_la_revision_precede_le_debut():
    b = _bail(date_derniere_revision="2025-12-01", date_prochaine_revision="2026-12-01")
    p = rp.proposer_abonnement(b, None, AUJ)                 # début proposé le 01/12/2026, jour de la révision
    assert p["a_recalculer"] and p["occurrences"] == 12 and "mettre à jour le classeur" in p["note"]


def test_abonnement_non_borne():
    s = rp.analyser_bail(_bail(), [_abo(fin=None)], [_facture()], AUJ)
    assert _types(s) == {"NON_BORNE"} and s.gravite == "a_traiter"


def test_terme_echu_puis_sans_abonnement():
    termine = _abo(statut="finished", debut="2025-04-01", fin="2026-03-01", occurrence_precedente=None)
    s = rp.analyser_bail(_bail(), [termine], [], AUJ)
    assert _types(s) == {"TERME_ECHU"} and s.ecarts[0].montant == 13800.0 and s.proposition is not None
    assert _types(rp.analyser_bail(_bail(), [], [], AUJ)) == {"SANS_ABONNEMENT"}


def test_renouvellement_mesure_sur_le_debut_de_la_periode_suivante():
    b = _bail(date_prochaine_revision="2027-01-01", date_derniere_revision="2026-01-01")
    proche = _abo(debut="2026-01-01", fin="2026-12-01")                   # période suivante au 01/01/2027 : 40 jours
    assert "RENOUVELLEMENT" in _types(rp.analyser_bail(b, [proche], [_facture()], AUJ))
    suivant = _abo(id=501, statut="not_started", debut="2027-01-01", fin="2027-12-01")
    assert "RENOUVELLEMENT" not in _types(rp.analyser_bail(b, [proche, suivant], [_facture()], AUJ))
    lointain = rp.analyser_bail(_bail(), [_abo()], [_facture()], AUJ)     # période suivante au 01/04/2027
    assert "RENOUVELLEMENT" not in _types(lointain)


def test_factures_impayee_brouillon_et_echeance_manquante():
    s = rp.analyser_bail(_bail(), [_abo()], [
        _facture(id=1, date="2026-11-01"),
        _facture(id=2, date="2026-08-01", echeance="2026-08-11", statut="late", restant_du=1380.0),
        _facture(id=3, date="2026-10-01", statut="draft", restant_du=1380.0)], AUJ)
    assert _types(s) == {"IMPAYE", "FACTURE_BROUILLON"}
    impaye = next(e for e in s.ecarts if e.type == "IMPAYE")
    assert impaye.gravite == "bloquant" and impaye.montant == 1380.0      # plus de 60 jours de retard
    sans_facture = rp.analyser_bail(_bail(), [_abo()], [_facture(date="2026-10-01")], AUJ)
    assert "ECHEANCE_MANQUANTE" in _types(sans_facture)


def test_trimestriel_et_doublon():
    b = _bail(periodicite="Trimestrielle")
    trimestriel = _abo(intervalle=3, montant_ht=3450.0, fin="2027-01-01")
    assert rp.analyser_bail(b, [trimestriel], [_facture()], AUJ).ecarts == []
    mensuel = rp.analyser_bail(b, [_abo(montant_ht=1150.0)], [_facture()], AUJ)
    assert "PERIODICITE" in _types(mensuel)
    doublon = rp.analyser_bail(_bail(), [_abo(), _abo(id=777)], [_facture()], AUJ)
    assert "DOUBLON" in _types(doublon)


def test_rattachement_et_orphelin():
    baux = [_bail(id=1, code="B01", loyer_attendu_annuel=12600.0), _bail(id=2, code="B02", loyer_attendu_annuel=30000.0)]
    abos = [_abo(id=1, montant_ht=2600.0), _abo(id=2, montant_ht=1150.0), _abo(id=3, customer_id=99)]
    par_bail, orphelins = rp.rattacher(baux, abos)
    assert [a["id"] for a in par_bail[1]] == [2] and [a["id"] for a in par_bail[2]] == [1]   # au loyer le plus proche
    assert [a["id"] for a in orphelins] == [3]
    explicite, _ = rp.rattacher([_bail(id=1, subscription_id=3)], abos)
    assert 3 in [a["id"] for a in explicite[1]]
    analyse = rp.analyser_dossier({"code": "D", "nom": "Dossier"}, baux, abos, [], AUJ)
    assert any(e.type == "ORPHELIN" for e in analyse["ecarts"])


def test_normalisation_du_format_api():
    brut = {"id": 42, "status": "in_progress", "mode": "awaiting_validation", "start": "2026-01-01", "finish": "2026-12-01",
            "next_occurrence": "2026-12-01", "prev_occurrence": "2026-11-01", "label": "Loyer", "customer": {"id": 7, "url": "x"},
            "recurring_rule": {"rule_type": "monthly", "interval": 3, "count": 4},
            "customer_invoice_data": {"currency_amount_before_tax": "196.32", "currency": "EUR"}}
    a = pennylane_api.normaliser_abonnement(brut)
    assert (a["id"], a["customer_id"], a["intervalle"], a["montant_ht"], a["fin"]) == (42, 7, 3, 196.32, "2026-12-01")
    assert rp.occurrences_par_an(a) == 4
    f = pennylane_api.normaliser_facture({"id": 1, "customer": {"id": 7}, "invoice_number": "F-1", "date": "2026-11-01",
                                          "deadline": "2026-11-11", "currency_amount_before_tax": "100.0",
                                          "currency_amount": "120.0", "remaining_amount_with_tax": "120.0", "status": "late"})
    assert (f["numero"], f["montant_ttc"], f["restant_du"], f["statut"]) == ("F-1", 120.0, 120.0, "late")


def test_client_reel_refuse_sans_jeton(monkeypatch):
    monkeypatch.delenv("PENNYLANE_TOKEN_X", raising=False)
    client = pennylane_api.ClientReel({"X": "PENNYLANE_TOKEN_X"})
    with pytest.raises(RuntimeError, match="PENNYLANE_TOKEN_X"):
        client.abonnements("X")
    cx = db.connecter(":memory:")
    depot.enregistrer_dossier(cx, "X", "Dossier X", variable_jeton="PENNYLANE_TOKEN_X")
    bilan = depot.synchroniser(cx, client)                     # l'échec d'un dossier est tracé, pas propagé
    assert bilan == [{"dossier": "X", "ok": False, "message": bilan[0]["message"]}]
    assert cx.execute("SELECT message FROM synchros").fetchone()["message"].startswith("ÉCHEC")


@pytest.fixture(scope="module")
def base_demo(tmp_path_factory):
    chemin = tmp_path_factory.mktemp("console") / "demo.sqlite"
    bilan = simulation.charger_demo(db.connecter(chemin), AUJ)
    assert bilan["dossiers"] == 10 and bilan["baux"] == 78
    return chemin


def test_simulation_deterministe_et_coherente(base_demo):
    cx = db.connecter(base_demo)
    analyses = [rp.analyser_dossier(x["dossier"], x["baux"], x["abonnements"], x["factures"], AUJ) for x in depot.charger(cx)]
    types = {e.type for a in analyses for e in a["ecarts"]}
    assert {"REVISION_NON_REPERCUTEE", "TERME_ECHU", "SANS_ABONNEMENT", "NON_BORNE", "IMPAYE", "ORPHELIN"} <= types
    attendu, facture = sum(a["attendu_annuel"] for a in analyses), sum(a["facture_annuel"] for a in analyses)
    assert 0 < facture < attendu
    # un bail au scénario nominal est conforme : aucune fausse alerte de structure
    assert sum(a["nb_ok"] for a in analyses) > 30
    d1, b1, _ = simulation.generer(AUJ)
    d2, b2, _ = simulation.generer(AUJ)
    assert d1 == d2 and b1 == b2


def test_pages_et_api(base_demo):
    c = TestClient(creer_app(base_demo, AUJ))
    accueil = c.get("/")
    assert accueil.status_code == 200 and "Données simulées" in accueil.text and "SCI du Parc" in accueil.text
    for url in ("/ecarts", "/ecarts?gravite=bloquant&dossier=PARC", "/echeancier?horizon=365", "/dossiers/PARC"):
        assert c.get(url).status_code == 200, url
    assert c.get("/dossiers/INCONNU").status_code == 404 and c.get("/baux/99999").status_code == 404
    s = c.get("/api/synthese").json()
    assert s["nb_baux"] == 78 and len(s["dossiers"]) == 10 and s["attendu"] > s["facture"]
    bloquants = c.get("/api/ecarts?gravite=bloquant").json()
    assert bloquants and all(e["gravite"] == "bloquant" for e in bloquants)
    cible = next(e for e in bloquants if e["type"] == "REVISION_NON_REPERCUTEE")
    page = c.get(f"/baux/{cible['bail_id']}").text
    assert "billing_subscriptions" in page and "Rappel de loyer" in page and "recurring_rule" in page
    assert c.post("/actions/synchroniser", follow_redirects=False).status_code == 303


def test_import_d_un_classeur_d_indexation(tmp_path):
    """Le référentiel de la console se nourrit du classeur : loyer en vigueur, dates et statut de révision."""
    from indexation_loyer import demo as demo_indexation
    classeur = demo_indexation.generer_demo(tmp_path, aujourdhui=date(2026, 9, 9))
    cx = db.connecter(":memory:")
    assert depot.importer_classeur(cx, classeur, "EXEMPLE", "PENNYLANE_TOKEN_EXEMPLE", aujourdhui=date(2026, 9, 9)) == 4
    baux = {b["code"]: b for b in depot.charger(cx, "EXEMPLE")[0]["baux"]}
    b01, b03 = baux["B01"], baux["B03"]
    assert b01["customer_id"] == 100001 and b01["periodicite"] == "Mensuelle"
    assert b01["loyer_attendu_annuel"] == pytest.approx(24978.23, abs=0.01)      # gel 2025 sans rattrapage
    assert (b01["date_derniere_revision"], b01["date_prochaine_revision"]) == ("2026-04-01", "2027-04-01")
    assert b01["revisions_non_appliquees"] == 1                                  # la révision 2026 n'est pas cochée
    assert b03["loyer_attendu_annuel"] == pytest.approx(65594.40, abs=0.01)      # rattrapage plafonné
    assert cx.execute("SELECT variable_jeton FROM dossiers").fetchone()["variable_jeton"] == "PENNYLANE_TOKEN_EXEMPLE"

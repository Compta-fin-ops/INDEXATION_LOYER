"""Console de pilotage : règles de rapprochement, collecte simulée, import d'un classeur, pages web."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from console import db, depot, pennylane_api, rapprochement as rp, simulation
from console.app import creer_app
from tests.outils import client_connecte

AUJ = date(2026, 11, 22)


def _bail(**kw):
    b = {"id": 1, "dossier": "D", "code": "B01", "local": "1 rue Test", "locataire": "Locataire", "customer_id": 10,
         "subscription_id": None, "indice": "ILC", "periodicite": "Mensuelle", "tva_pct": 20.0,
         "date_effet": "2022-04-01", "date_fin": "2031-04-01", "loyer_initial_annuel": 12000.0,
         "loyer_attendu_annuel": 12600.0, "charges_annuelles": 1200.0, "date_derniere_revision": "2026-04-01",
         "date_prochaine_revision": "2027-04-01", "loyer_prochain_annuel": None, "date_revision_suivante": None,
         "statut_revision": "✔ Calculable", "revisions_non_appliquees": 0}
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
    assert p["periode_revisee"] and not p["loyer_connu"] and p["occurrences"] == 12 and "Loyer révisé inconnu" in p["note"]
    connu = rp.proposer_abonnement({**b, "loyer_prochain_annuel": 12900.0}, None, AUJ)
    assert connu["loyer_connu"] and connu["derniere_occurrence"] == "2027-11-01"
    assert connu["corps"]["customer_invoice_data"]["invoice_lines"][0]["raw_currency_unit_price"] == "1075.00"
    assert "révisé le 01/12/2026" in connu["corps"]["customer_invoice_data"]["special_mention"]


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
    with pytest.raises(RuntimeError, match="aucun jeton Pennylane enregistré"):
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
    c = client_connecte(creer_app(base_demo, AUJ), base_demo)
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


def test_reprise_d_un_classeur_d_indexation(tmp_path):
    """Migration : les baux, leurs paramètres et les gels passent du classeur à la console, qui recalcule seule."""
    from indexation_loyer import demo as demo_indexation, insee
    from indexation_loyer.workbook import lire_indices_classeur
    jour = date(2026, 9, 9)
    classeur = demo_indexation.generer_demo(tmp_path, aujourdhui=jour)
    indices = tmp_path / "indices.csv"
    insee.ecrire_cache(lire_indices_classeur(classeur), indices)             # les indices fictifs du classeur
    cx = db.connecter(":memory:")
    assert depot.importer_classeur(cx, classeur, "EXEMPLE", "PENNYLANE_TOKEN_EXEMPLE", aujourdhui=jour) == 4
    stocke = {r["code"]: dict(r) for r in cx.execute("SELECT * FROM baux")}
    baux = {b["code"]: b for b in depot.charger(cx, "EXEMPLE", aujourdhui=jour, chemin_indices=indices)[0]["baux"]}
    b01, b03 = baux["B01"], baux["B03"]
    assert b01["calcule"] == 1 and b01["trimestre_base"] and b01["customer_id"] == 100001
    assert b01["loyer_attendu_annuel"] == pytest.approx(24978.23, abs=0.01)      # gel 2025 sans rattrapage
    assert (b01["date_derniere_revision"], b01["date_prochaine_revision"]) == ("2026-04-01", "2027-04-01")
    assert b03["loyer_attendu_annuel"] == pytest.approx(65574.12, abs=0.01)      # rattrapage plafonné une seule fois
    # le recalcul de la console retrouve, bail par bail, ce que le classeur avait calculé
    for code, b in baux.items():
        assert b["loyer_attendu_annuel"] == pytest.approx(stocke[code]["loyer_attendu_annuel"], abs=0.005), code
    assert cx.execute("SELECT COUNT(*) AS n FROM decisions WHERE decision LIKE 'Geler%'").fetchone()["n"] >= 1
    assert cx.execute("SELECT variable_jeton FROM dossiers").fetchone()["variable_jeton"] == "PENNYLANE_TOKEN_EXEMPLE"


def test_page_des_baux_par_societe_filtres_et_recherche(tmp_path):
    """Tous les baux, regroupés par société ; les tuiles filtrent, la recherche réduit, les sociétés se déplient."""
    chemin = tmp_path / "demo.sqlite"
    simulation.charger_demo(db.connecter(chemin), AUJ)
    c = client_connecte(creer_app(chemin, AUJ), chemin, "lecture")
    page = c.get("/baux").text
    nb = db.connecter(chemin).execute("SELECT COUNT(*) FROM baux").fetchone()[0]
    assert f"{nb} bail/baux affiché(s)" in page and page.count('<details class="carte societe"') == 10
    assert '<details class="carte societe" open' not in page                  # repliées par défaut
    assert "Modifier la société" in page and "/baux/1/modifier" in page and c.get("/").text.count('<details class="carte societe"') == 10
    corriger = c.get("/baux?filtre=corriger").text
    compte = int(page.split('À corriger</div><div class="valeur">')[1].split("<")[0])
    assert compte > 0 and f"{compte} bail/baux affiché(s)" in corriger and '<details class="carte societe" open' in corriger
    assert "Bloquant" in corriger and "Conforme</span>" not in corriger
    for f in ("regulariser", "preparer", "attente"):
        assert c.get(f"/baux?filtre={f}").status_code == 200
    assert c.get("/baux?filtre=nimporte").text.count('<details class="carte societe"') == 10      # filtre inconnu ignoré
    recherche = c.get("/baux?q=halles").text
    assert recherche.count('<details class="carte societe"') == 1 and "SCI des Halles" in recherche
    assert "Aucun bail ne correspond" in c.get("/baux?q=zzzz-introuvable").text
    # réindexation dans la période choisie, et disponibilité de son indice
    annee = c.get("/?periode=annee").text
    n_annee = int(annee.split(" bail/baux affiché(s)")[0].rsplit(">", 1)[1])
    assert 0 < n_annee < nb and "Réindexations du 01/01/2026 au 31/12/2026" in annee and "Réindexation de la période" in annee
    mois = c.get("/?periode=mois").text
    assert "du 01/11/2026 au 30/11/2026" in mois
    publie = c.get("/?periode=annee&indice=publie").text
    attendu = c.get("/?periode=annee&indice=attendu").text
    n = lambda page: int(page.split(" bail/baux affiché(s)")[0].rsplit(">", 1)[1])
    assert n(publie) + n(attendu) == n_annee and "Indice attendu</span>" not in publie
    perso = c.get("/?periode=perso&du=2027-01-01&au=2027-03-31").text
    assert "du 01/01/2027 au 31/03/2027" in perso and 'name="du" value="2027-01-01"' in perso
    assert c.get("/?periode=perso&du=pas-une-date").status_code == 200            # date illisible : filtre ignoré
    # les filtres se combinent, et les tuiles les conservent
    combine = c.get("/?filtre=preparer&periode=trimestre&indice=publie").text
    assert "periode=trimestre" in combine.split('class="tuiles"')[1].split("</div>\n</div>")[0]


def test_controles_d_enchainement_client_tva_mode_et_sortie():
    # abonnement suivant qui chevauche l'abonnement en cours
    s = rp.analyser_bail(_bail(), [_abo(), _abo(id=501, statut="not_started", debut="2027-03-01", fin="2028-02-01")], [_facture()], AUJ)
    assert "CONTINUITE" in _types(s) and "chevauche" in next(e.titre for e in s.ecarts if e.type == "CONTINUITE")
    # trou d'une échéance entre les deux
    s = rp.analyser_bail(_bail(), [_abo(), _abo(id=501, statut="not_started", debut="2027-05-01", fin="2028-02-01")], [_facture()], AUJ)
    assert "01/04/2027 n'est couverte" in next(e.detail for e in s.ecarts if e.type == "CONTINUITE")
    # enchaînement exact, au loyer révisé connu : conforme ; au mauvais montant : signalé
    bail = _bail(loyer_prochain_annuel=13200.0)
    s = rp.analyser_bail(bail, [_abo(), _abo(id=501, statut="not_started", debut="2027-04-01", fin="2028-03-01", montant_ht=1200.0)], [_facture()], AUJ)
    assert not {"CONTINUITE", "MONTANT_SUIVANT"} & _types(s)
    s = rp.analyser_bail(bail, [_abo(), _abo(id=501, statut="not_started", debut="2027-04-01", fin="2028-03-01")], [_facture()], AUJ)
    assert "MONTANT_SUIVANT" in _types(s) and "loyer révisé" in next(e.titre for e in s.ecarts if e.type == "MONTANT_SUIVANT")
    # autre client, TVA différente, factures finalisées
    lignes = [{"label": "Loyer", "tva": "FR_100", "prix": "1150", "quantite": 1.0}]
    s = rp.analyser_bail(_bail(), [_abo(customer_id=11, mode="finalized", parametres={"lignes": lignes})], [_facture()], AUJ)
    assert {"CLIENT", "TVA", "MODE"} <= _types(s)
    # locataire sorti : abonnement encore actif signalé ; sans abonnement, aucune fausse alerte
    s = rp.analyser_bail(_bail(date_sortie="2026-10-31"), [_abo()], [_facture()], AUJ)
    assert "APRES_SORTIE" in _types(s)
    s = rp.analyser_bail(_bail(date_sortie="2026-10-31"), [], [], AUJ)
    assert not {"SANS_ABONNEMENT", "TERME_ECHU"} & _types(s)


def test_avertissement_jour_de_facturation_et_revision():
    from console import ecriture
    sit = rp.analyser_bail(_bail(date_prochaine_revision="2026-12-01", loyer_prochain_annuel=12900.0, date_revision_suivante="2027-12-01"),
                           [_abo(debut="2025-12-30", fin="2026-11-30", prochaine_occurrence="2026-11-30")], [_facture()], AUJ)
    prop = rp.proposer_abonnement(sit.bail, sit.courant, AUJ, None, sit.courant)
    assert prop["debut"] == "2026-12-30" and prop["date_revision"] == "2026-12-01"
    prop["corps"] = ecriture.imposer_brouillon(prop["corps"])
    controles = {c.libelle: c for c in ecriture.controler(sit, prop, {}, [], AUJ, True)}
    jour = controles["Jour de facturation calé sur la date de révision"]
    assert not jour.ok and not jour.bloquant and "L'échéance du 30/11/2026 reste à l'ancien loyer" in jour.detail
    assert not controles["Jour de facturation présent dans tous les mois"].ok


def test_releve_et_courrier_de_regularisation(tmp_path):
    from console import editions
    factures = [_facture(id=1, date="2025-10-01", montant_ttc=100.0, restant_du=0.0),
                _facture(id=2, date="2026-10-01", numero="F-2", echeance="2026-10-10", montant_ttc=1380.0, restant_du=0.0, statut="paid"),
                _facture(id=3, date="2026-11-01", numero="F-3", echeance="2026-11-10", montant_ttc=1380.0, restant_du=80.0, statut="late"),
                _facture(id=4, date="2026-11-02", montant_ttc=500.0, restant_du=500.0, statut="draft"),
                _facture(id=5, date="2026-11-03", montant_ttc=-50.0, restant_du=0.0, statut="credit_note")]
    lignes = editions.lignes_releve(factures, date(2025, 12, 1))
    assert [l["numero"] for l in lignes] == ["F-2", "F-3"] and lignes[1]["regle"] == 1300.0 and lignes[1]["statut"] == "En retard"
    pdf = editions.releve({"nom": "SCI"}, {"code": "B01", "locataire": "X", "local": "L"}, lignes, date(2025, 12, 1), AUJ,
                          courrier=True, attendu_ttc=1380.0)
    assert pdf.startswith(b"%PDF")
    # depuis la page du bail : un bail du portefeuille fictif avec une facture impayée
    chemin = tmp_path / "demo.sqlite"
    simulation.charger_demo(db.connecter(chemin), AUJ)
    c = client_connecte(creer_app(chemin, AUJ), chemin, "lecture")
    cx = db.connecter(chemin)
    impayes = [e for x in depot.charger(cx, aujourdhui=AUJ)
               for e in rp.analyser_dossier(x["dossier"], x["baux"], x["abonnements"], x["factures"], AUJ)["ecarts"] if e.type == "IMPAYE"]
    bail_id = impayes[0].bail_id
    page = c.get(f"/baux/{bail_id}").text
    assert "Courrier de demande de régularisation" in page and "non lettré" in page
    r = c.get(f"/baux/{bail_id}/releve.pdf?courrier=1")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "regularisation" in r.headers["content-disposition"]
    assert c.get(f"/baux/{bail_id}/releve.pdf?depuis=n-importe").status_code == 400
    assert c.get(f"/baux/{bail_id}/releve.pdf?depuis=2030-01-01").status_code == 404

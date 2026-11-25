"""Facture de rappel en brouillon : proposition, contrôles, simulation, parcours réel contre un faux Pennylane."""
import io
import json
import urllib.error
import urllib.parse
from datetime import date

import pytest

from console import db, ecriture, rapprochement as rp
from console.app import creer_app
from indexation_loyer import insee
from tests.outils import client_connecte

AUJ = date(2026, 11, 22)
ILC = {"2022-T4": 126.05, "2023-T4": 132.63, "2024-T4": 135.30, "2025-T4": 137.29}
BAIL = {"code": "B01", "locataire": "Boulangerie Martin", "local": "12 rue des Halles, Tours", "date_effet": "2023-04-01",
        "loyer_initial_annuel": "24000", "charges_annuelles": "1200", "tva_pct": "8,5", "periodicite": "Mensuelle", "indice": "ILC",
        "trimestre_base": "2022-T4", "customer_id": "4242"}
ANCIEN = 2246.77          # (25 761,21 + 1 200) / 12 : le loyer d'avant la révision du 01/04/2026
ECART = 31.57             # (26 140,11 + 1 200) / 12 = 2 278,34 ; moins 2 246,77


def _abonnement(cx, fin="2026-12-01", montant=ANCIEN):
    cx.execute("DELETE FROM abonnements")
    cx.execute("""INSERT INTO abonnements (id, dossier, customer_id, label, statut, mode, debut, fin, prochaine_occurrence,
                  occurrence_precedente, type_regle, intervalle, montant_ht) VALUES (700, 'HALLES', 4242, 'Loyer', 'in_progress',
                  'awaiting_validation', '2026-04-01', ?, '2026-12-01', NULL, 'monthly', 1, ?)""", (fin, montant))
    cx.commit()


@pytest.fixture
def base(tmp_path):
    indices = tmp_path / "indices.csv"
    insee.ecrire_cache([insee.Observation("ILC", "001532540", t, v, "A", "2026-06-20", "TEST", "2026-11-16") for t, v in ILC.items()], indices)
    chemin = tmp_path / "base.sqlite"

    def ouvrir(test: bool):
        if test:
            db.definir_environnement(db.connecter(chemin), "test")
        app = creer_app(chemin, AUJ, chemin_indices=indices, stockage=tmp_path / "pieces")
        c = client_connecte(app, chemin)
        c.post("/dossiers", data={"code": "HALLES", "nom": "SCI des Halles", "variable_jeton": "PENNYLANE_TOKEN_HALLES"})
        c.post("/dossiers/HALLES/baux", data=BAIL)
        cx = db.connecter(chemin)
        _abonnement(cx)
        return app, c, cx, chemin
    return ouvrir


def test_proposition_conforme_au_schema_de_creation_de_facture(base):
    _, c, cx, _ = base(True)
    page = c.get("/baux/1").text
    assert "Révision du 01/04/2026 non répercutée" in page and "/baux/1/rappel" in page
    apercu = c.get("/baux/1/rappel").text
    corps = json.loads(apercu.split("POST /api/external/v2/customer_invoices\n")[1].split("</pre>")[0].replace("&#34;", '"'))
    # champs requis d'une « Draft Customer Invoice » : date, deadline, customer_id, draft, invoice_lines
    assert corps["draft"] is True and corps["customer_id"] == 4242 and corps["date"] == corps["deadline"] == "2026-11-22"
    ligne = corps["invoice_lines"][0]
    assert set(ligne) == {"label", "quantity", "unit", "raw_currency_unit_price", "vat_rate"}      # ligne « standard », sans produit
    assert (ligne["quantity"], ligne["raw_currency_unit_price"], ligne["vat_rate"], ligne["unit"]) == (9, "31.57", "FR_85", "mois")
    assert corps["external_reference"] == "console-HALLES-B01-rappel-2026-04-01"
    assert "échéances du 01/04/2026 au 01/12/2026" in ligne["label"] and "284,13" in apercu      # 9 × 31,57


def test_parcours_simule_dans_la_base_de_test(base):
    _, c, cx, chemin = base(True)
    apercu = c.get("/baux/1/rappel").text
    assert "Simuler la création" in apercu and "Mode simulation" in apercu
    empreinte = apercu.split('name="empreinte" value="')[1].split('"')[0]
    assert c.post("/baux/1/rappel", data={"empreinte": empreinte, "confirmation": "B99"}).status_code == 400
    assert c.post("/baux/1/rappel", data={"empreinte": "autre", "confirmation": "B01"}).status_code == 409
    assert cx.execute("SELECT COUNT(*) AS n FROM envois").fetchone()["n"] == 0
    r = c.post("/baux/1/rappel", data={"empreinte": empreinte, "confirmation": "b01"}, follow_redirects=False)
    assert r.status_code == 303
    e = cx.execute("SELECT * FROM envois").fetchone()
    assert (e["nature"], e["mode"], e["etat"], e["auteur"]) == ("facture", "simulation", "créé", "test.administrateur")
    f = cx.execute("SELECT * FROM factures").fetchone()
    assert (f["statut"], f["montant_ht"], f["id"]) == ("draft", 284.13, e["facture_id"]) and f["montant_ttc"] == pytest.approx(308.28, abs=0.01)
    assert "Facture créée" in c.get(r.headers["location"]).text and "rappel, révision du 01/04/2026" in c.get("/envois").text
    # un seul rappel par bail et par révision
    apercu = c.get("/baux/1/rappel").text
    assert "Création impossible" in apercu and "déjà journalisé" in apercu
    assert c.post("/baux/1/rappel", data={"empreinte": empreinte, "confirmation": "B01"}).status_code == 409
    assert cx.execute("SELECT COUNT(*) AS n FROM factures").fetchone()["n"] == 1
    # une gestionnaire prépare, seule la validation peut créer
    gestion = client_connecte(creer_app(chemin, AUJ), chemin, "gestion")
    assert gestion.post("/baux/1/rappel", data={"empreinte": empreinte, "confirmation": "B01"}).status_code == 403


def test_rappel_refuse_si_incomplet_ou_negatif(base):
    _, c, cx, _ = base(True)
    _abonnement(cx, fin=None)                                # abonnement sans terme : le rappel serait incomplet
    page = c.get("/baux/1/rappel").text
    assert "Création impossible" in page and "pas de terme" in page
    _abonnement(cx, montant=2400.00)                         # facturé au-dessus du loyer attendu : pas un rappel
    assert "Création impossible" in c.get("/baux/1/rappel").text
    _abonnement(cx, montant=2278.34)                         # abonnement au bon loyer : aucun rappel
    assert "Aucun rappel à facturer" in c.get("/baux/1/rappel").text
    with pytest.raises(PermissionError, match="brouillon"):   # jamais de facture finalisée, même appelée directement
        ecriture.creer_facture_dans_pennylane({"variable_jeton": "X"}, {"draft": False})


class _Pennylane:
    """Faux Pennylane : répond aux lectures, enregistre les écritures."""

    def __init__(self):
        self.ecritures, self.existe, self.perdre = [], False, False

    def __call__(self, req, timeout=60):
        url, methode = req.full_url, req.get_method()
        if methode == "POST":
            self.ecritures.append((url, req.get_header("Authorization"), json.loads(req.data.decode("utf-8"))))
            if self.perdre:
                raise TimeoutError("réponse perdue")
            return _Reponse({"id": 555, "status": "draft", "draft": True}, 201)
        if "/billing_subscriptions" in url:
            return _Reponse({"items": [{"id": 700, "status": "in_progress", "mode": "awaiting_validation", "start": "2026-04-01",
                                        "finish": "2026-12-01", "next_occurrence": "2026-12-01", "prev_occurrence": None, "label": "Loyer",
                                        "customer": {"id": 4242, "url": "x"}, "recurring_rule": {"rule_type": "monthly", "interval": 1},
                                        "customer_invoice_data": {"currency_amount_before_tax": str(ANCIEN)}}],
                             "has_more": False, "next_cursor": None})
        if "external_reference" in urllib.parse.unquote(url):
            return _Reponse({"items": [{"id": 9}] if self.existe else [], "has_more": False, "next_cursor": None})
        return _Reponse({"items": [], "has_more": False, "next_cursor": None})


class _Reponse:
    def __init__(self, corps, statut=200):
        self.corps, self.status = corps, statut

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.corps).encode()


def test_parcours_reel_contre_un_faux_pennylane(base, monkeypatch):
    from console import pennylane_api
    _, c, cx, _ = base(False)                                 # base de production
    faux = _Pennylane()
    monkeypatch.setattr(urllib.request, "urlopen", faux)
    monkeypatch.setattr(pennylane_api.time, "sleep", lambda s: None)
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES", "jeton-de-lecture-suffisamment-long")
    monkeypatch.delenv("CONSOLE_ECRITURE", raising=False)
    c.post("/actions/synchroniser")
    # sans activation ni jeton d'écriture : aucun formulaire de confirmation, aucune écriture
    page = c.get("/baux/1/rappel").text
    assert "Création impossible" in page and "CONSOLE_ECRITURE" in page and 'name="empreinte"' not in page and not faux.ecritures
    monkeypatch.setenv("CONSOLE_ECRITURE", "oui")
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES_ECRITURE", "jeton-d-ecriture-suffisamment-long")
    # une facture porte déjà la référence dans Pennylane : refus
    faux.existe = True
    assert "déjà présente" in c.get("/baux/1/rappel").text and "Création impossible" in c.get("/baux/1/rappel").text
    faux.existe = False
    page = c.get("/baux/1/rappel").text
    assert "Créer le brouillon dans Pennylane" in page
    empreinte = page.split('name="empreinte" value="')[1].split('"')[0]
    r = c.post("/baux/1/rappel", data={"empreinte": empreinte, "confirmation": "B01"}, follow_redirects=False)
    assert r.status_code == 303 and len(faux.ecritures) == 1
    url, autorisation, corps = faux.ecritures[0]
    assert url == "https://app.pennylane.com/api/external/v2/customer_invoices"
    assert autorisation == "Bearer jeton-d-ecriture-suffisamment-long"                       # jamais le jeton de lecture
    assert corps["draft"] is True and corps["invoice_lines"][0]["raw_currency_unit_price"] == "31.57"
    e = cx.execute("SELECT * FROM envois").fetchone()
    assert (e["nature"], e["mode"], e["etat"], e["statut_http"], e["facture_id"]) == ("facture", "pennylane", "créé", 201, 555)
    # rien ne repart pour la même révision
    assert c.post("/baux/1/rappel", data={"empreinte": empreinte, "confirmation": "B01"}).status_code == 409 and len(faux.ecritures) == 1


def test_reponse_perdue_issue_incertaine_et_blocage(base, monkeypatch):
    from console import pennylane_api
    _, c, cx, _ = base(False)
    faux = _Pennylane()
    faux.perdre = True
    monkeypatch.setattr(urllib.request, "urlopen", faux)
    monkeypatch.setattr(pennylane_api.time, "sleep", lambda s: None)
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES", "jeton-de-lecture-suffisamment-long")
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES_ECRITURE", "jeton-d-ecriture-suffisamment-long")
    monkeypatch.setenv("CONSOLE_ECRITURE", "oui")
    c.post("/actions/synchroniser")
    page = c.get("/baux/1/rappel").text
    empreinte = page.split('name="empreinte" value="')[1].split('"')[0]
    r = c.post("/baux/1/rappel", data={"empreinte": empreinte, "confirmation": "B01"}, follow_redirects=False)
    e = cx.execute("SELECT * FROM envois").fetchone()
    assert e["etat"] == "incertain" and e["facture_id"] is None and "Issue incertaine" in c.get(r.headers["location"]).text
    faux.perdre = False
    assert "issue incertaine" in c.get("/baux/1/rappel").text and "Création impossible" in c.get("/baux/1/rappel").text
    assert c.post("/baux/1/rappel", data={"empreinte": empreinte, "confirmation": "B01"}).status_code == 409 and len(faux.ecritures) == 1

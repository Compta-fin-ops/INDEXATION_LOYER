"""Les baux vivent dans la console : saisie, calcul, gel, courrier, pièces jointes, garde-fous."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from console import db, depot, pennylane_api, pieces, rapprochement as rp, referentiel, simulation
from console.app import creer_app
from tests.outils import client_connecte
from indexation_loyer import insee

AUJ = date(2026, 11, 22)
ILC = {"2022-T4": 126.05, "2023-T4": 132.63, "2024-T4": 135.30, "2025-T4": 137.29}     # valeurs de test, arbitraires
BAIL = {"code": "B01", "locataire": "Boulangerie Martin", "local": "12 rue des Halles, Tours", "date_effet": "2023-04-01",
        "duree_ans": "9", "loyer_initial_annuel": "24 000,00", "charges_annuelles": "1200", "tva_pct": "20",
        "periodicite": "Mensuelle", "indice": "ILC", "trimestre_base": "2022-T4", "periodicite_revision_ans": "1",
        "methode": "Chaînée", "type_bail": "Commercial", "customer_id": "4242"}
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def indices(tmp_path):
    chemin = tmp_path / "indices.csv"
    insee.ecrire_cache([insee.Observation("ILC", "001532540", t, v, "A", "2026-06-20", "TEST", "2026-11-16")
                        for t, v in ILC.items()], chemin)
    return chemin


@pytest.fixture
def console(tmp_path, indices):
    app = creer_app(tmp_path / "base.sqlite", AUJ, chemin_indices=indices, stockage=tmp_path / "pieces")
    c = client_connecte(app, tmp_path / "base.sqlite")
    r = c.post("/dossiers", data={"code": "halles", "nom": "SCI des Halles", "siren": "123 456 789", "adresse": "1 place du Marché\n37000 Tours",
                                  "signataire": "A. Bailleur", "variable_jeton": "PENNYLANE_TOKEN_HALLES"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dossiers/HALLES"
    r = c.post("/dossiers/HALLES/baux", data=BAIL, follow_redirects=False)
    assert r.status_code == 303
    return c, db.connecter(tmp_path / "base.sqlite"), int(r.headers["location"].rsplit("/", 1)[1]), tmp_path


def _bail(cx, bail_id, indices):
    return next(b for b in depot.charger(cx, "HALLES", AUJ, indices)[0]["baux"] if b["id"] == bail_id)


def test_saisie_d_un_bail_et_calcul_par_la_console(console, indices):
    c, cx, bail_id, _ = console
    b = _bail(cx, bail_id, indices)
    assert b["calcule"] == 1 and b["date_fin"] == "2032-04-01" and b["loyer_initial_annuel"] == 24000.0
    # 24 000 × 132,63 / 126,05 = 25 252,84 ; × 135,30 / 132,63 = 25 761,21 ; × 137,29 / 135,30 = 26 140,11
    assert b["loyer_attendu_annuel"] == pytest.approx(26140.11, abs=0.01)
    assert b["loyer_precedent_annuel"] == pytest.approx(25761.21, abs=0.01)
    assert (b["date_derniere_revision"], b["date_prochaine_revision"]) == ("2026-04-01", "2027-04-01")
    assert b["loyer_prochain_annuel"] is None and b["trimestre_attendu"] == "2026-T4"     # indice non publié
    page = c.get(f"/baux/{bail_id}").text
    assert "calculé par la console" in page and "Révisions" in page and "26 140,11" in page
    assert "Bail créé" in page                                                              # historique
    assert c.get("/indices").status_code == 200 and "ILC 2026-T4" in c.get("/indices").text


def test_la_revision_calculee_alimente_le_rapprochement(console, indices):
    """Abonnement resté au loyer d'avant révision : la console le reconnaît même s'il a démarré à la date anniversaire."""
    c, cx, bail_id, _ = console
    x = depot.charger(cx, "HALLES", AUJ, indices)[0]
    ancien = {"id": 700, "customer_id": 4242, "label": "Loyer", "statut": "in_progress", "mode": "awaiting_validation",
              "debut": "2026-04-01", "fin": "2027-03-01", "prochaine_occurrence": "2026-12-01", "occurrence_precedente": None,
              "type_regle": "monthly", "intervalle": 1, "montant_ht": round((25761.21 + 1200) / 12, 2)}
    s = rp.analyser_dossier(x["dossier"], x["baux"], [ancien], [], AUJ)["situations"][0]
    assert "REVISION_NON_REPERCUTEE" in {e.type for e in s.ecarts} and s.rappel["echeances"] == 8
    a_jour = {**ancien, "montant_ht": round((26140.11 + 1200) / 12, 2)}
    s = rp.analyser_dossier(x["dossier"], x["baux"], [a_jour], [], AUJ)["situations"][0]
    assert not {e.type for e in s.ecarts} & {"REVISION_NON_REPERCUTEE", "MONTANT"}


def test_gel_avec_commentaire_puis_retour(console, indices):
    c, cx, bail_id, _ = console
    r = c.post(f"/baux/{bail_id}/revisions/3", data={"decision": "Geler – sans rattrapage", "commentaire": "Accord du gérant, travaux",
                                                      "courrier_le": "2026-03-15"}, follow_redirects=False)
    assert r.status_code == 303
    b = _bail(cx, bail_id, indices)
    assert b["loyer_attendu_annuel"] == pytest.approx(25761.21, abs=0.01) and b["statut_revision"].startswith("❄")
    page = c.get(f"/baux/{bail_id}").text
    assert "Accord du gérant, travaux" in page and "Décision de révision" in page and "courrier envoyé le 15/03/2026" in page
    c.post(f"/baux/{bail_id}/revisions/3", data={"decision": "Appliquer"})
    assert _bail(cx, bail_id, indices)["loyer_attendu_annuel"] == pytest.approx(26140.11, abs=0.01)
    r = c.post(f"/baux/{bail_id}/revisions/3", data={"decision": "N'importe quoi"}, follow_redirects=False)
    assert "erreur=" in r.headers["location"]
    assert cx.execute("SELECT decision FROM decisions WHERE bail_id=? AND numero=3", (bail_id,)).fetchone()["decision"] == "Appliquer"


def test_courrier_pdf_de_revision(console):
    c, cx, bail_id, _ = console
    r = c.get(f"/baux/{bail_id}/revisions/3/courrier.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert "HALLES_B01_2026-04-01_revision.pdf" in r.headers["content-disposition"]
    assert c.get(f"/baux/{bail_id}/revisions/4/courrier.pdf").status_code == 409           # 2027 : indice non publié, rien à dire
    assert c.get(f"/baux/{bail_id}/revisions/99/courrier.pdf").status_code == 404
    assert cx.execute("SELECT COUNT(*) AS n FROM historique WHERE action='Courrier généré'").fetchone()["n"] == 1


def test_piece_jointe_bail_signe(console):
    c, cx, bail_id, tmp = console
    r = c.post(f"/baux/{bail_id}/pieces", data={"nature": "Bail"}, files={"fichier": ("Bail signé 2023.pdf", PDF, "application/pdf")},
               follow_redirects=False)
    assert r.status_code == 303 and "message=" in r.headers["location"]
    p = cx.execute("SELECT * FROM pieces").fetchone()
    assert p["nom"] == "Bail signé 2023.pdf" and p["taille"] == len(PDF)
    assert pieces.chemin_fichier(tmp / "pieces", p["sha256"]).read_bytes() == PDF           # rangé sous son empreinte
    t = c.get(f"/pieces/{p['id']}")
    assert t.content == PDF and "attachment" in t.headers["content-disposition"] and t.headers["x-content-type-options"] == "nosniff"
    assert "Bail signé 2023.pdf" in c.get(f"/baux/{bail_id}").text
    for nom, contenu in (("virus.exe", b"MZ..."), ("faux.pdf", b"<html>pas un pdf</html>"), ("vide.pdf", b""),
                         ("Bail signé 2023.pdf", PDF)):                                    # format, contenu, vide, doublon
        r = c.post(f"/baux/{bail_id}/pieces", data={"nature": "Bail"}, files={"fichier": (nom, contenu, "application/pdf")},
                   follow_redirects=False)
        assert "erreur=" in r.headers["location"], nom
    assert cx.execute("SELECT COUNT(*) AS n FROM pieces").fetchone()["n"] == 1
    assert c.get("/pieces/999").status_code == 404


def test_saisies_refusees(console):
    c, cx, bail_id, _ = console
    r = c.post("/dossiers/HALLES/baux", data={**BAIL, "code": "B02", "loyer_initial_annuel": "abc", "date_effet": "",
                                              "trimestre_base": "2022-T9"})
    assert r.status_code == 400 and "pas un nombre" in r.text and "date obligatoire" in r.text
    assert c.post("/dossiers/HALLES/baux", data=BAIL).status_code == 400                    # code B01 déjà pris
    assert cx.execute("SELECT COUNT(*) AS n FROM baux").fetchone()["n"] == 1
    # un jeton collé dans le champ du nom de variable est refusé et n'est ni stocké ni réaffiché
    r = c.post("/dossiers", data={"code": "PARC", "nom": "SCI du Parc", "variable_jeton": "sk_live_abcdef0123456789"})
    assert r.status_code == 400 and "jamais" in r.text and "sk_live" not in r.text.lower()
    assert cx.execute("SELECT COUNT(*) AS n FROM dossiers").fetchone()["n"] == 1
    assert c.post("/dossiers/HALLES", data={"nom": "SCI des Halles", "variable_jeton": "abcdef"}).status_code == 400
    # un formulaire posté depuis un autre site est refusé
    r = c.post(f"/baux/{bail_id}/revisions/3", data={"decision": "Geler – sans rattrapage"}, headers={"origin": "https://exemple.org"})   # remplace l'en-tête du client
    assert r.status_code == 403 and cx.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"] == 0


def test_modification_tracee_et_revisions_apres_le_terme(console, indices):
    c, cx, bail_id, _ = console
    for url in (f"/baux/{bail_id}/modifier", "/nouveau-dossier", "/dossiers/HALLES/modifier", "/dossiers/HALLES/baux/nouveau",
                "/dossiers/HALLES", "/", "/echeancier", "/ecarts", f"/baux/{bail_id}/abonnement"):
        assert c.get(url).status_code == 200, url
    r = c.post(f"/baux/{bail_id}", data={**BAIL, "duree_ans": "3", "date_fin": "", "plafond_annuel_pct": "1,5"}, follow_redirects=False)
    assert r.status_code == 303
    b = _bail(cx, bail_id, indices)
    assert b["date_fin"] == "2026-04-01"                         # terme dépassé : le bail reste révisé, faute de date de sortie
    assert b["date_prochaine_revision"] == "2027-04-01"
    # plafond de 1,5 % atteint en 2024 et 2025 (24 360,00 puis 24 725,40) ; en 2026 l'indice donne +1,47 %, sous le plafond
    assert b["loyer_attendu_annuel"] == pytest.approx(25089.06, abs=0.01)
    h = cx.execute("SELECT detail FROM historique WHERE action='Bail modifié'").fetchone()["detail"]
    assert "Plafond annuel : — → 1,5" in h and "Durée : 9 → 3" in h and "Terme contractuel : 01/04/2032 → 01/04/2026" in h
    c.post(f"/baux/{bail_id}", data={**BAIL, "date_sortie": "2026-06-30"})
    assert _bail(cx, bail_id, indices)["date_prochaine_revision"] is None    # sortie du locataire : plus de révision


def test_base_reelle_jamais_ecrasee_et_sauvegarde(console, tmp_path):
    c, cx, bail_id, _ = console
    with pytest.raises(RuntimeError, match="ne peut pas être vidée"):
        simulation.charger_demo(cx, AUJ)
    assert cx.execute("SELECT COUNT(*) AS n FROM baux").fetchone()["n"] == 1
    copie = db.sauvegarder(cx, tmp_path / "sauvegardes")
    assert db.connecter(copie).execute("SELECT locataire FROM baux").fetchone()["locataire"] == "Boulangerie Martin"
    assert "Données simulées" not in c.get("/").text                        # un dossier réel : plus de bandeau de simulation
    assert c.post("/actions/synchroniser", follow_redirects=False).status_code == 303      # sans jeton : échec tracé, pas d'erreur
    assert cx.execute("SELECT message FROM synchros ORDER BY id DESC").fetchone()["message"].startswith("ÉCHEC")


def test_facture_conforme_au_schema_officiel():
    """Extrait d'un élément de « List customer invoices » : champs requis du schéma, valeurs nulles comprises."""
    f = {"id": 42, "label": None, "invoice_number": "F20260011", "currency": "EUR", "amount": "1380.00",
         "currency_amount": "1380.00", "currency_amount_before_tax": "1150.00", "date": "2026-11-01", "deadline": "2026-11-11",
         "paid": False, "status": "late", "remaining_amount_with_tax": "1380.00", "draft": False,
         "customer": {"id": 4242, "url": "https://app.pennylane.com/api/external/v2/customers/4242"},
         "billing_subscription": {"id": 700}, "credited_invoice": None, "pdf_invoice_subject": "Loyer 12 rue des Halles"}
    n = pennylane_api.normaliser_facture(f)
    assert n == {"id": 42, "customer_id": 4242, "subscription_id": 700, "numero": "F20260011", "libelle": "Loyer 12 rue des Halles",
                 "date": "2026-11-01", "echeance": "2026-11-11", "montant_ht": 1150.0, "montant_ttc": 1380.0, "restant_du": 1380.0,
                 "statut": "late"}
    hors = pennylane_api.normaliser_facture({**f, "customer": None, "billing_subscription": None, "remaining_amount_with_tax": None,
                                             "status": "credit_note"})
    assert hors["customer_id"] is None and hors["subscription_id"] is None and hors["restant_du"] == 0.0
    # un avoir ou un devis de la liste ne vaut ni facture d'échéance ni impayé
    bail = {"id": 1, "dossier": "D", "code": "B01", "locataire": "L", "customer_id": 4242, "periodicite": "Mensuelle",
            "loyer_attendu_annuel": 13800.0, "charges_annuelles": 0.0, "date_prochaine_revision": "2027-04-01"}
    abo = {"id": 700, "customer_id": 4242, "statut": "in_progress", "debut": "2026-04-01", "fin": "2027-03-01",
           "occurrence_precedente": "2026-11-01", "type_regle": "monthly", "intervalle": 1, "montant_ht": 1150.0}
    avoir = {**n, "statut": "credit_note", "restant_du": 1380.0}
    s = rp.analyser_bail(bail, [abo], [avoir], AUJ)
    assert {e.type for e in s.ecarts} == {"ECHEANCE_MANQUANTE"}


def test_filtre_de_date_envoye_a_pennylane(monkeypatch):
    import json, urllib.parse
    vus = []

    class Reponse:
        def __init__(self, corps): self.corps = corps
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(self.corps).encode()

    def faux(req, timeout=60):
        vus.append(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query))
        return Reponse({"items": [], "has_more": len(vus) == 1, "next_cursor": "abc" if len(vus) == 1 else None})

    monkeypatch.setenv("PENNYLANE_TOKEN_X", "jeton-de-test")
    monkeypatch.setattr(pennylane_api.urllib.request, "urlopen", faux)
    monkeypatch.setattr(pennylane_api.time, "sleep", lambda s: None)
    assert pennylane_api.ClientReel({"X": "PENNYLANE_TOKEN_X"}).factures("X") == []
    filtre = json.loads(vus[0]["filter"][0])
    assert filtre[0]["field"] == "date" and filtre[0]["operator"] == "gteq" and len(filtre[0]["value"]) == 10
    assert vus[1]["cursor"] == ["abc"] and vus[1]["filter"] == vus[0]["filter"] and vus[1]["limit"] == ["100"]


def test_abonnement_propose_cale_sur_les_termes_du_bail():
    """Bail trimestriel sans abonnement : le début proposé tombe sur un terme du bail, jamais à cheval sur la révision."""
    bail = {"id": 1, "dossier": "D", "code": "B01", "local": "Local", "customer_id": 1, "periodicite": "Trimestrielle", "tva_pct": 20,
            "date_effet": "2021-07-01", "loyer_attendu_annuel": 40000.0, "charges_annuelles": 0.0, "indice": "ILC",
            "date_derniere_revision": "2026-07-01", "date_prochaine_revision": "2027-07-01"}
    p = rp.proposer_abonnement(bail, None, AUJ)
    assert (p["debut"], p["occurrences"], p["derniere_occurrence"]) == ("2027-01-01", 2, "2027-04-01")
    assert p["corps"]["recurring_rule"] == {"type": "monthly", "interval": 3, "day_of_month": 1, "count": 2}
    mensuel = rp.proposer_abonnement({**bail, "periodicite": "Mensuelle"}, None, AUJ)
    assert (mensuel["debut"], mensuel["occurrences"]) == ("2026-12-01", 7)

"""Les baux vivent dans la console : saisie, calcul, gel, courrier, pièces jointes, garde-fous."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from console import db, depot, pennylane_api, pieces, rapprochement as rp, referentiel, simulation
from console.app import creer_app
from tests.outils import client_connecte
from indexation_loyer import calcul, insee

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
    assert "HALLES_B01_Boulangerie_Martin_2026-04-01_revision.pdf" in r.headers["content-disposition"]
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
    assert "Plafond par révision : — → 1,5" in h and "Durée : 9 → 3" in h and "Terme contractuel : 01/04/2032 → 01/04/2026" in h
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
    assert c.post("/actions/synchroniser", follow_redirects=False).status_code == 303      # sans jeton : rien n'est lu, rien ne casse
    assert cx.execute("SELECT COUNT(*) AS n FROM synchros").fetchone()["n"] == 0


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
                 "statut": "late", "reference": ""}
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


def test_tva_des_departements_d_outre_mer_et_taux_inconnu(console, indices):
    """8,5 % se saisit et part sous le code FR_85 ; un taux sans code connu n'est jamais envoyé « exonéré »."""
    from console import ecriture
    c, cx, bail_id, _ = console
    assert 'value="8.5"' in c.get(f"/baux/{bail_id}/modifier").text
    assert c.post(f"/baux/{bail_id}", data={**BAIL, "tva_pct": "8,5"}, follow_redirects=False).status_code == 303
    b = _bail(cx, bail_id, indices)
    assert b["tva_pct"] == 8.5
    p = rp.proposer_abonnement(b, None, AUJ)
    assert {l["vat_rate"] for l in p["corps"]["customer_invoice_data"]["invoice_lines"]} == {"FR_85"} and p["envoyable"]
    assert rp.proposer_abonnement({**b, "tva_pct": 0}, None, AUJ)["corps"]["customer_invoice_data"]["invoice_lines"][0]["vat_rate"] == "exempt"
    assert c.post(f"/baux/{bail_id}", data={**BAIL, "tva_pct": "7"}).status_code == 400          # taux hors liste : saisie refusée
    inconnu = rp.proposer_abonnement({**b, "tva_pct": 7.0}, None, AUJ)                            # bail repris d'ailleurs
    assert not inconnu["envoyable"] and inconnu["corps"]["customer_invoice_data"]["invoice_lines"][0]["vat_rate"] is None
    sit = rp.analyser_bail({**b, "tva_pct": 7.0}, [], [], AUJ)
    controles = ecriture.controler(sit, inconnu, {"code": "HALLES", "variable_jeton": ""}, [], AUJ, simulation=True)
    assert not ecriture.envoi_possible(controles)
    assert any(x.libelle == "Taux de TVA reconnu" and not x.ok for x in controles)


def _bail_de(cx, dossier, bail_id, indices):
    return next(b for b in depot.charger(cx, dossier, AUJ, indices)[0]["baux"] if b["id"] == bail_id)


def _classeur(lignes):
    """Le modèle de la console, feuille « Baux » remplie avec les lignes données (dictionnaires par titre de colonne)."""
    import io
    import openpyxl
    from console import import_tableur
    wb = openpyxl.load_workbook(io.BytesIO(import_tableur.modele_xlsx()))
    titres = [c.value for c in wb["Baux"][1]]
    for l in lignes:
        wb["Baux"].append([l.get(t) for t in titres])
    tampon = io.BytesIO()
    wb.save(tampon)
    return tampon.getvalue()


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
LIGNE = {"Code dossier": "PARC", "Raison sociale": "SCI du Parc", "SIREN": "987654321", "Locataire": "Pharmacie du Parc",
         "Local": "3 avenue du Parc", "Prise d'effet": date(2023, 4, 1), "Loyer initial annuel HT": 24000, "TVA (%)": 8.5,
         "Indice": "ILC", "Trimestre de départ": "2022-T4"}


def test_import_en_lot_apercu_puis_confirmation(console, indices):
    c, cx, bail_id, _ = console
    assert c.get("/import").status_code == 200 and c.get("/import/modele.xlsx").content.startswith(b"PK")
    fichier = _classeur([LIGNE, {**LIGNE, "Locataire": "Opticien", "Loyer initial annuel HT": 12000, "Facturation": "Trimestrielle"},
                         {"Code dossier": "HALLES", "Code bail": "B01", "Locataire": "Déjà là", "Local": "x",
                          "Prise d'effet": date(2020, 1, 1), "Loyer initial annuel HT": 1},                    # bail existant : ignoré
                         {"Code dossier": "HALLES", "Locataire": "Fleuriste", "Local": "14 rue des Halles",
                          "Prise d'effet": "01/04/2023", "Loyer initial annuel HT": "9 600,50", "Trimestre de départ": "2022-T4"}])
    r = c.post("/import", files={"fichier": ("portefeuille.xlsx", fichier, XLSX)}, follow_redirects=False)
    assert r.status_code == 303
    apercu = c.get(r.headers["location"]).text
    assert cx.execute("SELECT COUNT(*) AS n FROM baux").fetchone()["n"] == 1          # l'analyse ne crée rien
    assert "26\u202f140,11" in apercu and "Déjà présent" in apercu and "Créer 3 bail/baux et 1 société(s)" in apercu
    empreinte = cx.execute("SELECT empreinte FROM imports").fetchone()["empreinte"]
    assert c.post(r.headers["location"], data={"empreinte": "autre", "nb": "3"}).status_code == 409
    assert c.post(r.headers["location"], data={"empreinte": empreinte, "nb": "3"}, follow_redirects=False).status_code == 303
    baux = {(b["dossier"], b["code"]): dict(b) for b in cx.execute("SELECT * FROM baux")}
    assert set(baux) == {("HALLES", "B01"), ("HALLES", "B02"), ("PARC", "B01"), ("PARC", "B02")}
    assert baux[("HALLES", "B01")]["locataire"] == "Boulangerie Martin"                # l'existant n'est pas touché
    assert baux[("PARC", "B01")]["tva_pct"] == 8.5 and baux[("PARC", "B02")]["periodicite"] == "Trimestrielle"
    assert baux[("HALLES", "B02")]["loyer_initial_annuel"] == 9600.5 and baux[("HALLES", "B02")]["calcule"] == 1
    assert cx.execute("SELECT nom, siren FROM dossiers WHERE code='PARC'").fetchone()["siren"] == "987654321"
    pharmacie = next(b["id"] for b in baux.values() if b["locataire"] == "Pharmacie du Parc")
    assert _bail_de(cx, "PARC", pharmacie, indices)["loyer_attendu_annuel"] == pytest.approx(26140.11, abs=0.01)
    assert c.post(r.headers["location"], data={"empreinte": empreinte, "nb": "0"}).status_code == 409   # jamais deux fois
    assert cx.execute("SELECT COUNT(*) AS n FROM baux").fetchone()["n"] == 4
    assert cx.execute("SELECT auteur FROM historique WHERE action='Import en lot'").fetchone()["auteur"] == "test.administrateur"


def test_import_tout_ou_rien_et_fichiers_refuses(console):
    c, cx, bail_id, _ = console
    fichier = _classeur([LIGNE, {**LIGNE, "Locataire": "", "Loyer initial annuel HT": "abc", "TVA (%)": 7},
                         {**LIGNE, "Code bail": "B07"}, {**LIGNE, "Code bail": "B07"}])
    r = c.post("/import", files={"fichier": ("erreurs.xlsx", fichier, XLSX)}, follow_redirects=False)
    apercu = c.get(r.headers["location"]).text
    assert "2 ligne(s) en erreur" in apercu and "pas un nombre" in apercu and "en double dans le fichier" in apercu
    assert "Confirmer" not in apercu
    empreinte = cx.execute("SELECT empreinte FROM imports").fetchone()["empreinte"]
    assert c.post(r.headers["location"], data={"empreinte": empreinte, "nb": "2"}).status_code == 409
    assert cx.execute("SELECT COUNT(*) AS n FROM baux").fetchone()["n"] == 1 and cx.execute("SELECT COUNT(*) AS n FROM dossiers").fetchone()["n"] == 1
    csv_ok = ("Code dossier;Raison sociale;Locataire;Local;Prise d'effet;Loyer initial annuel HT;TVA (%)\r\n"
              "MARCHE;SCI du Marché;Café de la Gare;2 place de la Gare;01/04/2023;15 000,00;8,5\r\n").encode("cp1252")
    r = c.post("/import", files={"fichier": ("export.csv", csv_ok, "text/csv")}, follow_redirects=False)
    assert r.status_code == 303 and "SCI du Marché" in c.get(r.headers["location"]).text      # CSV d'Excel : point-virgule, cp1252
    for nom, contenu, attendu in (("x.xls", b"abc", "Format refusé"), ("x.xlsx", b"pas un classeur", "pas un classeur Excel"),
                                  ("x.csv", b"Code dossier;Locataire;Truc\nA;B;C\n", "Colonne(s) inconnue(s) : Truc"),
                                  ("x.csv", b"Code dossier;Locataire\nAB;C\n", "obligatoire(s) absente(s)"),
                                  ("x.xlsx", _classeur([]), "feuille « Baux » est vide")):
        r = c.post("/import", files={"fichier": (nom, contenu, "application/octet-stream")})
        assert r.status_code == 400 and attendu in r.text, nom


def test_reprise_d_un_bail_en_cours(console, indices):
    """Historique non recalculable (gel ancien, loyer renégocié) : la console part du loyer constaté à la dernière révision."""
    from indexation_loyer import calcul, courriers
    from indexation_loyer.workbook import Societe
    c, cx, bail_id, _ = console
    repris = {**BAIL, "code": "B09", "date_effet": "2019-04-01", "loyer_initial_annuel": "20000", "trimestre_base": "2018-T4",
              "reprise_date": "2025-04-01", "reprise_loyer_annuel": "25 000", "reprise_trimestre": "2024-T4"}
    for partiel in ({"reprise_loyer_annuel": ""}, {"reprise_trimestre": "2024-T7"}, {"reprise_date": "2018-01-01"}):
        assert c.post("/dossiers/HALLES/baux", data={**repris, **partiel}).status_code == 400
    r = c.post("/dossiers/HALLES/baux", data=repris, follow_redirects=False)
    assert r.status_code == 303
    id9 = int(r.headers["location"].rsplit("/", 1)[1])
    b = _bail(cx, id9, indices)
    # l'indice 2018-T4 n'existe pas dans la série de test : sans reprise, rien ne serait calculable
    assert b["loyer_attendu_annuel"] == pytest.approx(25367.70, abs=0.01)          # 25 000 × 137,29 / 135,30
    assert (b["date_derniere_revision"], b["date_prochaine_revision"]) == ("2026-04-01", "2027-04-01")
    page = c.get(f"/baux/{id9}").text
    assert "Bail repris en cours" in page and "25 367,70" in page and "01/04/2019" in page
    assert c.get(f"/baux/{id9}/revisions/1/courrier.pdf").status_code == 200
    _, lignes = referentiel.reviser(b, calcul.table_indices(referentiel.lire_indices(indices)), {}, AUJ)
    v = courriers.valeurs_courrier(Societe(nom="SCI"), referentiel.vers_bail(b, affichage=True), lignes[0])
    assert v["date_effet_bail"] == "01/04/2019" and v["date_revision"] == "01/04/2026"   # le courrier cite la vraie date d'effet
    # gel 2026 à rattraper : la reprise reste à la dernière révision APPLIQUÉE (2025), le gel se saisit sur la révision 2026
    c.post(f"/baux/{id9}/revisions/1", data={"decision": "Geler – rattrapage possible", "commentaire": "accord du bailleur"})
    b = _bail(cx, id9, indices)
    assert b["loyer_attendu_annuel"] == 25000.0 and b["statut_revision"].startswith("❄")
    _, lignes = referentiel.reviser(b, calcul.table_indices(referentiel.lire_indices(indices)), {1: "Geler – rattrapage possible"}, AUJ)
    assert (lignes[1].echeance.trimestre_reference, lignes[1].trimestre_precedent_effectif) == ("2026-T4", "2024-T4")
    # un trimestre de reprise incohérent avec la date est signalé, sans bloquer
    assert "éloigné de la révision" in referentiel.controler_indice_de_depart(
        {"indice": "ILC", "reprise_date": "2026-04-01", "reprise_trimestre": "2024-T4"}, {("ILC", "2024-T4"): 135.30})
    assert referentiel.controler_indice_de_depart({"indice": "ILC", "reprise_date": "2025-04-01", "reprise_trimestre": "2024-T4"},
                                                  {("ILC", "2024-T4"): 135.30}) is None
    # un gel saisi protège le calendrier : on ne déplace pas les révisions sous une décision existante
    r = c.post(f"/baux/{id9}", data={**repris, "reprise_date": "2024-04-01", "reprise_trimestre": "2023-T4"})
    assert r.status_code == 400 and "gelées" in r.text
    assert c.post(f"/baux/{id9}", data={**repris, "notes": "modification sans effet sur le calendrier"},
                  follow_redirects=False).status_code == 303
    # même reprise par le fichier d'import
    fichier = _classeur([{**LIGNE, "Prise d'effet": date(2019, 4, 1), "Trimestre de départ": "2018-T4",
                          "Reprise : date de la dernière révision": date(2025, 4, 1), "Reprise : loyer annuel HT à cette date": 25000,
                          "Reprise : trimestre de l'indice appliqué": "2024-T4"}])
    r = c.post("/import", files={"fichier": ("reprise.xlsx", fichier, XLSX)}, follow_redirects=False)
    assert "25 367,70" in c.get(r.headers["location"]).text


def test_accueil_suit_chaque_reindexation(console, indices):
    """La page d'accueil (baux par société) suit la réindexation : indice attendu, publié, courrier, abonnement suivant."""
    c, cx, bail_id, _ = console
    accueil = c.get("/").text
    assert "Baux par société" in accueil and "Tableau de bord" not in accueil and "Loyers facturés" not in accueil
    page = c.get("/?periode=annee_suivante").text
    assert "01/04/2027" in page and "ILC 2026 T4" in page and "Indice attendu" in page
    assert "Aucun bail ne correspond" in c.get("/?periode=annee_suivante&indice=publie").text
    # l'indice paraît : nouveau loyer, courrier à éditer, abonnement à créer
    insee.ecrire_cache([insee.Observation("ILC", "001532540", t, v, "A", "2026-12-20", "TEST", "2026-12-21")
                        for t, v in {**ILC, "2026-T4": 139.00}.items()], indices)
    page = c.get("/?periode=annee_suivante&indice=publie").text
    assert "Indice publié" in page and "26\u202f465,70" in page                     # 26 140,11 × 139,00 / 137,29
    assert f'/baux/{bail_id}/revisions/4/courrier.pdf' in page and "Abonnement suivant à créer" in page
    c.post(f"/baux/{bail_id}/revisions/4", data={"decision": "Appliquer", "courrier_le": "2026-12-22"})
    for ident, debut, fin, statut in ((801, "2026-04-01", "2027-03-01", "in_progress"), (802, "2027-04-01", "2028-03-01", "not_started")):
        cx.execute("""INSERT INTO abonnements (id, dossier, customer_id, label, statut, mode, debut, fin, type_regle, intervalle, montant_ht)
                      VALUES (?, 'HALLES', 4242, 'Loyer', ?, 'awaiting_validation', ?, ?, 'monthly', 1, 2278.34)""", (ident, statut, debut, fin))
    cx.commit()
    page = c.get("/?periode=annee_suivante").text
    assert "courrier envoyé le 22/12/2026" in page and "Suivant créé" in page and "Abonnement suivant à créer" not in page
    # une révision gelée
    c.post(f"/baux/{bail_id}/revisions/4", data={"decision": "Geler – sans rattrapage", "commentaire": "accord 2027"})
    assert "Gel décidé" in c.get("/?periode=annee_suivante").text


def test_courriers_en_lot_et_etat_annuel(console, indices):
    """Tous les courriers « à préparer » en un ZIP, datés comme envoyés d'un geste ; état annuel des révisions en PDF."""
    import io
    import zipfile
    c, cx, bail_id, _ = console
    c.post("/dossiers/HALLES/baux", data={**BAIL, "code": "B02", "locataire": "Opticien Durand", "loyer_initial_annuel": "12000"})
    assert "Aucun courrier à éditer" in c.get("/courriers?horizon=180").text     # indice 2026-T4 non publié : rien à dire
    assert c.get("/courriers/lot.zip?horizon=180").status_code == 404
    insee.ecrire_cache([insee.Observation("ILC", "001532540", t, v, "A", "2026-12-20", "TEST", "2026-12-21")
                        for t, v in {**ILC, "2026-T4": 139.00}.items()], indices)
    c.post(f"/baux/{bail_id}/revisions/4", data={"decision": "Geler – sans rattrapage", "commentaire": "accord du bailleur"})
    page = c.get("/courriers?horizon=180").text
    assert "2 courrier(s)" in page and "Opticien Durand" in page and "Gel" in page
    r = c.get("/courriers/lot.zip?horizon=180")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    noms = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert noms == ["HALLES_B01_Boulangerie_Martin_2027-04-01_gel.pdf", "HALLES_B02_Opticien_Durand_2027-04-01_revision.pdf"]
    assert all(zipfile.ZipFile(io.BytesIO(r.content)).read(n).startswith(b"%PDF") for n in noms)
    # on date seulement le courrier coché ; la décision de gel et son commentaire sont conservés
    r = c.post("/courriers", data={"horizon": "180", "dossier": "", "envoye_le": "2026-12-22", "revision": [f"{bail_id}:4"]}, follow_redirects=False)
    assert r.status_code == 303 and "1+courrier" in r.headers["location"]
    d = cx.execute("SELECT * FROM decisions WHERE bail_id=? AND numero=4", (bail_id,)).fetchone()
    assert (d["decision"], d["commentaire"], d["courrier_le"]) == ("Geler – sans rattrapage", "accord du bailleur", "2026-12-22")
    page = c.get("/courriers?horizon=180").text
    assert "1 courrier(s)" in page and "Boulangerie Martin" not in page
    assert "illisible" in c.post("/courriers", data={"envoye_le": "demain", "revision": ["2:4"]}, follow_redirects=False).headers["location"]
    assert c.get("/courriers?dossier=HALLES&horizon=180").status_code == 200
    # état annuel : une ligne par bail, la révision de l'année et la suite donnée
    from console import editions, referentiel
    from indexation_loyer import calcul
    r = c.get("/dossiers/HALLES/etat-revisions.pdf?annee=2026")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "HALLES_revisions_2026.pdf" in r.headers["content-disposition"]
    assert c.get("/dossiers/HALLES/etat-revisions.pdf?annee=1990").status_code == 400
    assert c.get("/dossiers/HALLES/etat-revisions.pdf?annee=2022").status_code == 404          # aucun bail en vigueur cette année-là
    table = calcul.table_indices(referentiel.lire_indices(indices))
    baux = []
    for b in map(dict, cx.execute("SELECT * FROM baux ORDER BY code").fetchall()):
        dec = referentiel.decisions_du_bail(cx, b["id"])
        baux.append({"bail": b, "decisions": dec,
                     "revisions": referentiel.reviser(b, table, {n: x["decision"] for n, x in dec.items()}, date(2027, 12, 31))[1]})
    e26, e27 = editions.lignes_etat(baux, 2026, AUJ), editions.lignes_etat(baux, 2027, date(2027, 12, 31))
    assert editions.lignes_etat(baux, 2027, AUJ)[1]["nature"] == "À venir, indice publié"      # vue du 22/11/2026
    assert [(x["bail"]["code"], x["nature"]) for x in e26] == [("B01", "Appliquée"), ("B02", "Appliquée")]
    assert e26[0]["loyer_debut"] == pytest.approx(25761.21, abs=0.01) and e26[0]["loyer_fin"] == pytest.approx(26140.11, abs=0.01)
    assert e27[0]["nature"] == "Gelée, sans rattrapage" and e27[0]["commentaire"] == "accord du bailleur" and e27[0]["courrier_le"] == "2026-12-22"
    assert e27[0]["loyer_fin"] == pytest.approx(26140.11, abs=0.01)                               # gelé : loyer maintenu
    assert e27[1]["nature"] == "Appliquée" and e27[1]["loyer_fin"] == pytest.approx(13070.06 * 139.00 / 137.29, abs=0.02)


def test_situation_des_loyers_pour_le_gerant(console, indices):
    """À date : loyers réindexés sur douze mois, réindexations à venir avec indice publié, en attente d'indice, autres baux."""
    from console import editions
    c, cx, bail_id, _ = console
    c.post("/dossiers/HALLES/baux", data={**BAIL, "code": "B02", "locataire": "Opticien Durand", "loyer_initial_annuel": "12000",
                                          "date_effet": "2023-01-15"})
    c.post("/dossiers/HALLES/baux", data={**BAIL, "code": "B03", "locataire": "Parti", "date_sortie": "2026-06-30"})
    r = c.get("/dossiers/HALLES/situation-loyers.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "HALLES_situation_loyers_2026-11-22.pdf" in r.headers["content-disposition"]
    table = calcul.table_indices(referentiel.lire_indices(indices))
    baux = []
    for b in depot.charger(cx, "HALLES", AUJ, indices)[0]["baux"]:
        if not b.get("date_sortie"):
            baux.append({"bail": b, "revisions": referentiel.reviser(b, table, {}, AUJ)[1]})
    g = editions.lignes_situation(baux, AUJ)
    # B01 réindexé le 01/04/2026 ; B02 attend l'ILC 2026-T3 pour le 15/01/2027 ; aucun indice 2026-T4 pour B01 en 2027
    assert [(l["bail"]["code"], l["date"]) for l in g["revises"]] == [("B02", date(2026, 1, 15)), ("B01", date(2026, 4, 1))]
    revises = {l["bail"]["code"]: l for l in g["revises"]}
    assert revises["B01"]["date"] == date(2026, 4, 1)
    assert revises["B01"]["avant"] == pytest.approx(25761.21, abs=0.01) and revises["B01"]["apres"] == pytest.approx(26140.11, abs=0.01)
    assert all(not l["retard"] for l in g["attente"]) and {l["bail"]["code"] for l in g["attente"]} == {"B01", "B02"}
    assert all(l["date"] > AUJ for l in g["attente"]) and g["connus"] == [] and g["autres"] == []
    # l'indice paraît : la réindexation de B01 passe dans les « à venir, indice publié », avec son nouveau loyer
    insee.ecrire_cache([insee.Observation("ILC", "001532540", t, v, "A", "2026-12-20", "TEST", "2026-12-21")
                        for t, v in {**ILC, "2026-T4": 139.00}.items()], indices)
    table = calcul.table_indices(referentiel.lire_indices(indices))
    g = editions.lignes_situation([{**x, "revisions": referentiel.reviser(x["bail"], table, {}, AUJ)[1]} for x in baux], AUJ)
    b01 = next(l for l in g["connus"] if l["bail"]["code"] == "B01")
    assert b01["date"] == date(2027, 4, 1) and b01["apres"] == pytest.approx(26140.11 * 139.00 / 137.29, abs=0.02)
    pdf = editions.situation_loyers({"nom": "SCI des Halles", "siren": "123456789"},
                                    {**g, "autres": [{"bail": {"code": "B09", "locataire": "Sans révision", "local": "x"},
                                                      "loyer": 7000.0, "prochaine": None}]}, AUJ)
    assert pdf.startswith(b"%PDF") and len(pdf) > 2500
    assert c.get("/dossiers/INCONNU/situation-loyers.pdf").status_code == 404
    assert "Situation des loyers à ce jour" in c.get("/dossiers/HALLES").text

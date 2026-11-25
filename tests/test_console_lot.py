"""Créations en lot, reprise des paramètres de l'abonnement existant, mention de suivi des révisions, chaînage des factures."""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import pytest

from console import db, ecriture, pennylane_api, rapprochement as rp
from console.app import creer_app
from indexation_loyer import insee
from tests.outils import client_connecte

AUJ = date(2026, 11, 22)
ILC = {"2022-T4": 126.05, "2023-T4": 132.63, "2024-T4": 135.30, "2025-T4": 137.29}
BAIL = {"code": "B01", "locataire": "Boulangerie Martin", "local": "12 rue des Halles, Tours", "date_effet": "2023-04-01",
        "loyer_initial_annuel": "24000", "charges_annuelles": "1200", "tva_pct": "8,5", "periodicite": "Mensuelle", "indice": "ILC",
        "trimestre_base": "2022-T4", "customer_id": "4242"}
BAIL2 = {**BAIL, "code": "B02", "locataire": "Cabinet Durand", "local": "14 rue des Halles, Tours", "customer_id": "4343"}
ANCIEN, REVISE = 2246.77, 2278.34          # loyer et charges par mois, avant et après la révision du 01/04/2026
PARAMETRES = {"conditions": "30_days", "paiement": "gocardless_direct_debit", "devise": "EUR", "langue": "fr_FR", "modele": 77,
              "objet": "Loyer boulangerie", "texte_libre": "Contact : gestion locative", "description": "",
              "mention": "Ancienne mention saisie à la main", "remise": None, "sections": [],
              "lignes": [{"label": "Loyer mensuel", "unit": "mois", "quantite": 1.0, "prix": "2146.77", "tva": "FR_85",
                          "description": "", "produit": 31, "section": None, "remise": None, "montant_ht": 2146.77},
                         {"label": "Provision sur charges", "unit": "mois", "quantite": 1.0, "prix": "100.00", "tva": "FR_85",
                          "description": "", "produit": None, "section": None, "remise": None, "montant_ht": 100.0}]}


def _abonnement(cx, numero, client, montant, parametres=None):
    cx.execute("""INSERT INTO abonnements (id, dossier, customer_id, label, statut, mode, debut, fin, prochaine_occurrence,
                  occurrence_precedente, type_regle, intervalle, montant_ht, parametres) VALUES (?, 'HALLES', ?, 'Loyer Halles',
                  'in_progress', 'awaiting_validation', '2026-04-01', '2026-12-01', '2026-12-01', NULL, 'monthly', 1, ?, ?)""",
               (numero, client, montant, json.dumps(parametres) if parametres else ""))
    cx.commit()


@pytest.fixture
def base(tmp_path):
    indices = tmp_path / "indices.csv"
    insee.ecrire_cache([insee.Observation("ILC", "001532540", t, v, "A", "2026-06-20", "TEST", "2026-11-16") for t, v in ILC.items()], indices)
    chemin = tmp_path / "base.sqlite"

    def ouvrir(test: bool, parametres=None):
        if test:
            db.definir_environnement(db.connecter(chemin), "test")
        app = creer_app(chemin, AUJ, chemin_indices=indices, stockage=tmp_path / "pieces")
        c = client_connecte(app, chemin)
        c.post("/dossiers", data={"code": "HALLES", "nom": "SCI des Halles", "variable_jeton": "PENNYLANE_TOKEN_HALLES"})
        c.post("/dossiers/HALLES/baux", data=BAIL)
        c.post("/dossiers/HALLES/baux", data=BAIL2)
        cx = db.connecter(chemin)
        _abonnement(cx, 700, 4242, ANCIEN, parametres)       # B01 : resté à l'ancien loyer, rappel dû
        _abonnement(cx, 701, 4343, REVISE)                   # B02 : au bon loyer, période suivante à préparer
        return app, c, cx, chemin
    return ouvrir


def _corps(page: str, route: str) -> dict:
    return json.loads(page.split(f"POST /api/external/v2/{route}\n")[1].split("</pre>")[0].replace("&#34;", '"').replace("&#39;", "'"))


# ------------------------------------------------------------------ mention de suivi des révisions
def test_mention_de_suivi_sur_l_abonnement_et_sur_le_rappel(base):
    _, c, cx, _ = base(True)
    mention = _corps(c.get("/baux/1/abonnement").text, "billing_subscriptions")["customer_invoice_data"]["special_mention"]
    lignes = mention.split("\n")
    assert lignes[:3] == ["ILC 2022 T4 : 126,05", "Loyer de base : 2 000,00 € HT par mois", "Premier loyer : avril 2023"]
    assert lignes[3].startswith("Révision au 01/04/2024 : ILC 2023 T4 : 132,63 contre 2022 T4 : 126,05, soit +5,22 %")
    assert "Révision au 01/04/2026" in lignes[5] and "2 178,34 € HT par mois" in lignes[5]
    assert lignes[-1] == "Prochaine ré-indexation annuelle du loyer au 01/04/2027." and len(lignes) == 7
    rappel = _corps(c.get("/baux/1/rappel").text, "customer_invoices")["special_mention"]
    assert rappel.startswith("Rappel de loyer consécutif à la révision du 01/04/2026") and "Loyer de base : 2 000,00 € HT par mois" in rappel
    assert "Mention portée sur les factures" in c.get("/baux/1").text


def test_mention_d_un_bail_repris_gel_et_resume(base):
    _, c, cx, _ = base(True)
    historique = "Révision au 01/04/2024 : application du plafonnement de l'ILC.\n\n  Avenant du 12/06/2024.  "
    assert c.post("/baux/1", data={**BAIL, "reprise_date": "2025-04-01", "reprise_loyer_annuel": "25000", "reprise_trimestre": "2024-T4",
                                   "mention_historique": historique}, follow_redirects=False).status_code == 303
    assert cx.execute("SELECT mention_historique FROM baux WHERE id=1").fetchone()[0] == "Révision au 01/04/2024 : application du plafonnement de l'ILC.\nAvenant du 12/06/2024."
    page = c.get("/baux/1").text
    assert "Avenant du 12/06/2024." in page and "Révision au 01/04/2026" in page and "Révision au 01/04/2025" not in page
    # au-delà de quatre révisions, les plus anciennes sont résumées par le loyer atteint
    suivi = {"entete": ["Loyer de base : 100,00 € HT par mois"], "frequence": "annuelle",
             "revisions": [{"date": f"{a}-01-01", "texte": f"Révision au 01/01/{a}.", "loyer": f"{a} €"} for a in range(2020, 2028)]}
    lignes = rp.mention_speciale({"suivi": suivi}, date(2026, 6, 1)).split("\n")
    assert lignes == ["Loyer de base : 100,00 € HT par mois", "Révisions jusqu'au 01/01/2023 : loyer porté à 2023 €.",
                      "Révision au 01/01/2024.", "Révision au 01/01/2025.", "Révision au 01/01/2026.",
                      "Prochaine ré-indexation annuelle du loyer au 01/01/2027."]
    assert rp.mention_speciale({}, AUJ) == ""                # bail non calculé : pas de suivi


# ------------------------------------------------------------------ reprise des paramètres de l'abonnement existant
def test_abonnement_propose_reprend_l_abonnement_existant(base):
    _, c, cx, _ = base(True, PARAMETRES)
    page = c.get("/baux/1/abonnement").text
    corps = _corps(page, "billing_subscriptions")
    assert (corps["payment_conditions"], corps["payment_method"], corps["label"]) == ("30_days", "gocardless_direct_debit", "Loyer Halles")
    assert corps["mode"] == {"type": "awaiting_validation"}                     # le brouillon reste imposé
    donnees = corps["customer_invoice_data"]
    assert (donnees["customer_invoice_template_id"], donnees["pdf_invoice_subject"], donnees["pdf_invoice_free_text"]) == (
        77, "Loyer boulangerie", "Contact : gestion locative")
    assert donnees["invoice_lines"] == [
        {"label": "Loyer mensuel", "quantity": 1, "unit": "mois", "raw_currency_unit_price": "2178.34", "vat_rate": "FR_85", "product_id": 31},
        {"label": "Provision sur charges", "quantity": 1, "unit": "mois", "raw_currency_unit_price": "100.00", "vat_rate": "FR_85"}]
    assert donnees["special_mention"].startswith("ILC 2022 T4") and "Ancienne mention saisie à la main" in page
    assert "Lignes recopiées, seul le loyer change" in page and "Mode de paiement reconduit" in page
    # la facture de rappel reprend l'échéance à 30 jours, le modèle, et l'unité, la TVA et le produit de la ligne de loyer
    rappel = _corps(c.get("/baux/1/rappel").text, "customer_invoices")
    assert (rappel["date"], rappel["deadline"], rappel["customer_invoice_template_id"]) == ("2026-11-22", "2026-12-22", 77)
    assert rappel["invoice_lines"][0]["product_id"] == 31 and rappel["invoice_lines"][0]["raw_currency_unit_price"] == "31.57"


def test_lignes_non_reprises_si_la_ligne_de_loyer_est_incertaine(base):
    sans_charges = {**PARAMETRES, "lignes": [{**PARAMETRES["lignes"][0], "prix": "2246.77"}]}
    _, c, cx, _ = base(True, sans_charges)
    page = c.get("/baux/1/abonnement").text
    corps = _corps(page, "billing_subscriptions")
    # aucune ligne ne vaut la provision du bail : la console reconstruit ses lignes, et le dit sans bloquer
    assert [l["raw_currency_unit_price"] for l in corps["customer_invoice_data"]["invoice_lines"]] == ["2178.34", "100.00"]
    assert "ne vaut la provision sur charges du bail" in page and 'name="empreinte"' in page
    assert corps["payment_conditions"] == "30_days"                             # les autres paramètres restent repris
    bail = {"periodicite": "Mensuelle", "charges_annuelles": 0}
    refus = lambda p: rp.reprendre_lignes({"id": 1, "parametres": p}, bail, 10.0)
    assert "n'ont pas été lues" in refus({"lignes": None})["motif"] and "remise globale" in refus({**PARAMETRES, "remise": {"type": "relative", "value": "5"}})["motif"]
    assert "quantité ou une remise" in refus({"lignes": [{**PARAMETRES["lignes"][0], "quantite": 2.0}]})["motif"]
    autre = rp.reprendre_lignes({"id": 1, "parametres": {"lignes": PARAMETRES["lignes"]}}, bail, 10.0)      # bail sans charges
    assert [d["role"] for d in autre["detail"]] == ["loyer", "autre"] and autre["lignes"][1]["raw_currency_unit_price"] == "100.00"


def test_lecture_des_lignes_d_abonnement_conforme_au_schema():
    """Élément de « List invoice lines for a billing subscription » : chaînes pour la quantité et le prix, valeurs nulles."""
    l = pennylane_api.normaliser_ligne({
        "id": 444, "label": "Loyer mensuel", "unit": None, "quantity": "1", "amount": "2329.25", "currency_amount": "2329.25",
        "description": "", "product": {"id": 3049, "url": "x"}, "vat_rate": "FR_85", "currency_amount_before_tax": "2146.77",
        "currency_tax": "182.48", "tax": "182.48", "raw_currency_unit_price": "2146.77", "discount": {"type": "relative", "value": "0"},
        "section_rank": None, "imputation_dates": None, "created_at": "x", "updated_at": "x"})
    assert l == {"label": "Loyer mensuel", "unit": "", "quantite": 1.0, "prix": "2146.77", "tva": "FR_85", "description": "",
                 "produit": 3049, "section": None, "remise": None, "montant_ht": 2146.77}
    a = pennylane_api.normaliser_abonnement({
        "id": 42, "status": "in_progress", "mode": "awaiting_validation", "start": "2026-01-01", "finish": None, "label": None,
        "payment_conditions": "30_days_end_of_month", "payment_method": "offline", "customer": {"id": 7, "url": "x"},
        "recurring_rule": {"rule_type": "monthly", "interval": 1},
        "customer_invoice_data": {"currency": "EUR", "language": "fr_FR", "currency_amount_before_tax": "100.0",
                                  "customer_invoice_template": {"id": 9}, "discount": {"type": "absolute", "value": None},
                                  "pdf_invoice_subject": "Loyer", "pdf_invoice_free_text": "", "pdf_description": None,
                                  "special_mention": "ILC 2019 T1 : 114,64"}})
    assert a["parametres"] == {"conditions": "30_days_end_of_month", "paiement": "offline", "devise": "EUR", "langue": "fr_FR",
                               "modele": 9, "objet": "Loyer", "texte_libre": "", "description": "", "mention": "ILC 2019 T1 : 114,64",
                               "remise": None, "lignes": None, "sections": []}
    # lignes lues pour les abonnements vivants et pour le dernier terminé de chaque client, pas pour les anciens
    abos = [{"id": 1, "statut": "finished", "customer_id": 7, "fin": "2024-12-01"}, {"id": 2, "statut": "finished", "customer_id": 7, "fin": "2025-12-01"},
            {"id": 3, "statut": "in_progress", "customer_id": 7}, {"id": 4, "statut": "stopped", "customer_id": 8, "fin": "2025-01-01"}]
    assert sorted(a["id"] for a in pennylane_api.a_detailler(abos)) == [2, 3, 4]
    assert rp.echeance_paiement(date(2026, 11, 22), "30_days_end_of_month") == date(2026, 12, 31)
    assert rp.echeance_paiement(date(2026, 11, 22), "upon_receipt") == rp.echeance_paiement(date(2026, 11, 22), None) == date(2026, 11, 22)


def test_abonnement_d_un_bail_a_revision_triennale_couvre_trois_ans(base):
    """Le bail fixe la période : un abonnement de 27 mois est admis sur un bail révisé tous les trois ans, refusé sur un bail annuel."""
    _, c, cx, _ = base(True)
    c.post("/dossiers/HALLES/baux", data={**BAIL, "code": "B03", "locataire": "Optique Lumière", "customer_id": "4444",
                                          "periodicite_revision_ans": "3"})
    _abonnement(cx, 702, 4444, REVISE)
    page = c.get("/baux/3/abonnement").text
    assert "27, la dernière le 01/03/2029" in page and 'name="empreinte"' in page
    assert "Prochaine ré-indexation triennale du loyer au 01/04/2029." in page
    sit = type("S", (), {"bail": {"dossier": "HALLES", "code": "B01", "periodicite": "Mensuelle", "periodicite_revision_ans": 1},
                         "courant": None, "suivant": None, "dernier_termine": None})()
    prop = {"debut": "2027-01-01", "derniere_occurrence": "2029-03-01", "loyer_connu": True, "loyer_annuel": 1.0,
            "corps": {"customer_id": 1, "mode": {"type": "awaiting_validation"}, "recurring_rule": {"count": 27},
                      "customer_invoice_data": {"invoice_lines": [{"raw_currency_unit_price": "10", "vat_rate": "FR_200"}]}}}
    borne = next(x for x in ecriture.controler(sit, prop, {}, [], AUJ, True) if x.libelle.startswith("Abonnement borné"))
    assert not borne.ok and "plus que la période de 1 an(s)" in borne.detail


# ------------------------------------------------------------------ créations en lot
def _choix(page: str) -> list[str]:
    return [m.split('"')[0] for m in page.split('name="choix" value="')[1:]]


def test_lot_simule_dans_la_base_de_test(base):
    _, c, cx, chemin = base(True, PARAMETRES)
    page = c.get("/creations").text
    choix = _choix(page)
    assert "3 création(s) prête(s) sur 3 proposée(s)" in page and len(choix) == 3
    assert sorted(x.split(":")[1] for x in choix) == ["abonnement", "abonnement", "facture"]
    # le nombre saisi doit être celui des cases cochées ; rien ne part sinon
    assert c.post("/creations", data={"choix": choix, "confirmation": "2"}).status_code == 400
    assert c.post("/creations", data={"choix": [], "confirmation": "0"}).status_code == 400
    assert c.post("/creations", data={"choix": ["1:virement:x"], "confirmation": "1"}).status_code == 400
    assert cx.execute("SELECT COUNT(*) AS n FROM envois").fetchone()["n"] == 0
    r = c.post("/creations", data={"choix": choix, "confirmation": "3"})
    assert r.status_code == 200 and "Résultat du lot" in r.text
    envois = [dict(e) for e in cx.execute("SELECT * FROM envois ORDER BY id")]
    assert [(e["etat"], e["mode"]) for e in envois] == [("créé", "simulation")] * 3 and len({e["lot"] for e in envois}) == 1 and envois[0]["lot"]
    assert sorted(e["nature"] for e in envois) == ["abonnement", "abonnement", "facture"]
    assert "Lot " + envois[0]["lot"] in c.get("/envois?lot=" + envois[0]["lot"]).text
    # chaînage : la facture créée porte la référence du bail et de la révision, la page du bail dit d'où elle vient
    f = cx.execute("SELECT * FROM factures").fetchone()
    assert f["reference"] == "console-HALLES-B01-rappel-2026-04-01"
    bail = c.get("/baux/1").text
    assert "Rappel de la révision du 01/04/2026, créé par la console" in bail and "Déjà créé" in bail
    assert "Rappel de la révision" not in c.get("/baux/2").text
    # l'abonnement simulé garde ses paramètres : il servira de modèle à la période suivante
    cree = cx.execute("SELECT * FROM abonnements WHERE id>=800000 AND customer_id=4242").fetchone()
    assert json.loads(cree["parametres"])["lignes"][0]["prix"] == "2178.34" and json.loads(cree["parametres"])["modele"] == 77
    # rejouer le même lot ne crée rien : la situation a changé, chaque ligne est écartée
    r = c.post("/creations", data={"choix": choix, "confirmation": "3"})
    assert r.text.count("Non envoyé") == 3 and cx.execute("SELECT COUNT(*) AS n FROM envois").fetchone()["n"] == 3
    assert "Rien à créer" in c.get("/creations").text
    # seule la validation peut lancer un lot
    gestion = client_connecte(creer_app(chemin, AUJ), chemin, "gestion")
    assert gestion.post("/creations", data={"choix": choix, "confirmation": "3"}).status_code == 403


class _Reponse:
    def __init__(self, corps, statut=200):
        self.corps, self.status = corps, statut

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.corps).encode()


class _Pennylane:
    """Faux Pennylane : deux abonnements avec leurs lignes, écritures enregistrées, panne programmable."""

    def __init__(self):
        self.ecritures, self.lectures, self.perdre_a_partir_de, self.montant_cree = [], [], None, None

    def __call__(self, req, timeout=60):
        url, methode = urllib.parse.unquote(req.full_url), req.get_method()
        page = lambda items: _Reponse({"items": items, "has_more": False, "next_cursor": None})
        if methode == "POST":
            corps = json.loads(req.data.decode("utf-8"))
            self.ecritures.append((url, corps))
            if self.perdre_a_partir_de is not None and len(self.ecritures) >= self.perdre_a_partir_de:
                raise TimeoutError("réponse perdue")
            lignes = corps.get("invoice_lines") or corps["customer_invoice_data"]["invoice_lines"]
            montant = self.montant_cree or f"{sum(float(l['raw_currency_unit_price']) * l['quantity'] for l in lignes):.2f}"
            if "customer_invoices" in url:
                return _Reponse({"id": 9000 + len(self.ecritures), "status": "draft", "currency_amount_before_tax": montant}, 201)
            return _Reponse({"id": 8000 + len(self.ecritures), "status": "not_started",
                             "customer_invoice_data": {"currency_amount_before_tax": montant}}, 201)
        self.lectures.append(url)
        if "/invoice_line_sections" in url:
            return page([])
        if "/billing_subscriptions/700/invoice_lines" in url:
            return page([{"label": "Provision sur charges", "unit": "mois", "quantity": "1", "raw_currency_unit_price": "100.00",
                          "vat_rate": "FR_85", "description": "", "product": None, "discount": {"type": "absolute", "value": "0"},
                          "section_rank": None, "currency_amount_before_tax": "100.00"},
                         {"label": "Loyer mensuel", "unit": "mois", "quantity": "1", "raw_currency_unit_price": "2146.77",
                          "vat_rate": "FR_85", "description": "", "product": {"id": 31, "url": "x"},
                          "discount": {"type": "absolute", "value": "0"}, "section_rank": None, "currency_amount_before_tax": "2146.77"}])
        if "/invoice_lines" in url:
            return page([])
        if "/billing_subscriptions" in url:
            abo = lambda numero, client, montant: {
                "id": numero, "status": "in_progress", "mode": "awaiting_validation", "start": "2026-04-01", "finish": "2026-12-01",
                "next_occurrence": "2026-12-01", "prev_occurrence": None, "label": "Loyer Halles", "payment_conditions": "30_days",
                "payment_method": "offline", "customer": {"id": client, "url": "x"}, "recurring_rule": {"rule_type": "monthly", "interval": 1},
                "customer_invoice_data": {"currency": "EUR", "language": "fr_FR", "currency_amount_before_tax": str(montant),
                                          "customer_invoice_template": {"id": 77}, "special_mention": None}}
            return page([abo(700, 4242, ANCIEN), abo(701, 4343, REVISE)])
        return page([])


def _reel(base, monkeypatch):
    _, c, cx, _ = base(False)
    faux = _Pennylane()
    monkeypatch.setattr(urllib.request, "urlopen", faux)
    monkeypatch.setattr(pennylane_api.time, "sleep", lambda s: None)
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES", "jeton-de-lecture-suffisamment-long")
    monkeypatch.setenv("PENNYLANE_TOKEN_HALLES_ECRITURE", "jeton-d-ecriture-suffisamment-long")
    monkeypatch.setenv("CONSOLE_ECRITURE", "oui")
    c.post("/actions/synchroniser")
    return c, cx, faux


def test_lot_reel_contre_un_faux_pennylane(base, monkeypatch):
    c, cx, faux = _reel(base, monkeypatch)
    # la collecte a lu les lignes des deux abonnements vivants, dans l'ordre de la facture
    lues = json.loads(cx.execute("SELECT parametres FROM abonnements WHERE id=700").fetchone()["parametres"])
    assert [l["label"] for l in lues["lignes"]] == ["Provision sur charges", "Loyer mensuel"] and lues["modele"] == 77
    assert any("/billing_subscriptions/700/invoice_lines" in u and "sort=id" in u for u in faux.lectures)
    page = c.get("/creations").text
    choix = _choix(page)
    assert len(choix) == 3 and not faux.ecritures
    avant = len(faux.lectures)
    r = c.post("/creations", data={"choix": choix, "confirmation": "3"})
    assert "Résultat du lot" in r.text and len(faux.ecritures) == 3
    urls = sorted(u.rsplit("/", 1)[1] for u, _ in faux.ecritures)
    assert urls == ["billing_subscriptions", "billing_subscriptions", "customer_invoices"]
    for url, corps in faux.ecritures:                        # rien que des brouillons
        assert corps.get("draft") is True if "customer_invoices" in url else corps["mode"] == {"type": "awaiting_validation"}
    abonnement = next(corps for url, corps in faux.ecritures if "billing_subscriptions" in url and corps["customer_id"] == 4242)
    assert [l["raw_currency_unit_price"] for l in abonnement["customer_invoice_data"]["invoice_lines"]] == ["100.00", "2178.34"]
    assert abonnement["customer_invoice_data"]["customer_invoice_template_id"] == 77 and abonnement["payment_conditions"] == "30_days"
    # Pennylane relu une fois par dossier avant le lot et une fois après, pas à chaque création
    assert sum(1 for u in faux.lectures[avant:] if u.endswith("/billing_subscriptions?limit=100")) == 2
    envois = [dict(e) for e in cx.execute("SELECT * FROM envois ORDER BY id")]
    assert all(e["etat"] == "créé" and e["verification"].startswith("Conforme") for e in envois)
    assert sorted(e["subscription_id"] or e["facture_id"] for e in envois) == sorted(
        (9000 if "customer_invoices" in u else 8000) + i for i, (u, _) in enumerate(faux.ecritures, 1))
    # la facture de rappel a été vérifiée dans Pennylane avant d'être créée
    assert any("external_reference" in u for u in faux.lectures[avant:])


def test_lot_arrete_a_la_premiere_reponse_perdue(base, monkeypatch):
    c, cx, faux = _reel(base, monkeypatch)
    choix = _choix(c.get("/creations").text)
    faux.perdre_a_partir_de = 2
    r = c.post("/creations", data={"choix": choix, "confirmation": "3"})
    assert len(faux.ecritures) == 2 and "Lot arrêté" in r.text                  # la troisième création n'est jamais partie
    assert [e["etat"] for e in cx.execute("SELECT etat FROM envois ORDER BY id")] == ["créé", "incertain"]
    assert r.text.count("Non envoyé") == 1


def test_montant_relu_different_du_montant_envoye(base, monkeypatch):
    c, cx, faux = _reel(base, monkeypatch)
    faux.montant_cree = "2146.77"                            # Pennylane aurait repris le prix du produit, pas le nôtre
    choix = [x for x in _choix(c.get("/creations").text) if x.startswith("1:abonnement")]
    c.post("/creations", data={"choix": choix, "confirmation": "1"})
    e = cx.execute("SELECT * FROM envois").fetchone()
    assert e["etat"] == "créé" and e["verification"].startswith("ÉCART") and "ÉCART" in c.get(f"/envois/{e['id']}").text
    assert ecriture.verifier_reponse("facture", {"invoice_lines": [{"raw_currency_unit_price": "10", "quantity": 3}]}, {}).startswith("Montant non relu")

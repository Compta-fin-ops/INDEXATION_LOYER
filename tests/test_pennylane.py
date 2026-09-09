import json
from datetime import date

import pytest

from indexation_loyer import demo, pennylane

AUJ = date(2026, 9, 9)
OBLIGATOIRES = {"start", "mode", "payment_conditions", "payment_method", "recurring_rule", "customer_id", "customer_invoice_data"}


def test_corps_conforme_au_schema_openapi(tmp_path):
    mapping = pennylane.charger_mapping()
    obs = demo.indices_fictifs(AUJ)
    baux = demo.baux_fictifs()
    abo = pennylane.construire_abonnement(baux[0], obs, mapping, aujourdhui=AUJ, decisions=demo.saisies_fictives().decision)
    c = abo.corps
    assert OBLIGATOIRES <= set(c)
    assert c["customer_id"] == 100001 and isinstance(c["customer_id"], int)
    assert c["start"] == "2026-10-01"
    assert c["mode"] == {"type": "awaiting_validation"}
    assert c["payment_conditions"] in mapping["enums"]["payment_conditions"]
    assert c["payment_method"] in mapping["enums"]["payment_method"]
    assert c["recurring_rule"] == {"type": "monthly", "interval": 1, "day_of_month": 1}
    lignes = c["customer_invoice_data"]["invoice_lines"]
    assert {"label", "quantity", "unit", "raw_currency_unit_price", "vat_rate"} <= set(lignes[0])
    assert isinstance(lignes[0]["raw_currency_unit_price"], str) and lignes[0]["raw_currency_unit_price"] == "2081.52"  # 24 978,24 / 12 (gel 2025 sans rattrapage)
    assert lignes[0]["unit"] == "mois" and lignes[0]["vat_rate"] == "FR_200"
    assert "product_id" not in lignes[0]
    assert lignes[1]["label"] == "Provision sur charges" and lignes[1]["raw_currency_unit_price"] == "200.00"
    assert "révision du 01/04/2026" in c["customer_invoice_data"]["special_mention"]
    assert not abo.avertissements and abo.envoyable

    trimestriel = pennylane.construire_abonnement(baux[1], obs, mapping, aujourdhui=AUJ)
    assert trimestriel.corps["recurring_rule"] == {"type": "monthly", "interval": 3, "day_of_month": 1}
    assert trimestriel.corps["customer_invoice_data"]["invoice_lines"][0]["unit"] == "trimestre"

    sans_client = pennylane.construire_abonnement(baux[3], obs, mapping, aujourdhui=AUJ)
    assert not sans_client.envoyable

    chemins = pennylane.ecrire_dry_run([abo, sans_client], tmp_path, "SCI TEST")
    assert json.loads(chemins[0].read_text(encoding="utf-8"))["body"]["customer_id"] == 100001


def test_reglages_hors_enum_signales():
    mapping = pennylane.charger_mapping()
    r = pennylane.ReglagesPennylane(mode="email", payment_conditions="90_days")
    abo = pennylane.construire_abonnement(demo.baux_fictifs()[0], demo.indices_fictifs(AUJ), mapping, r, aujourdhui=AUJ)
    assert any("payment_conditions" in a for a in abo.avertissements) and any("email" in a for a in abo.avertissements)


def test_envoi_ignore_abonnement_existant(monkeypatch):
    mapping = pennylane.charger_mapping()
    b = demo.baux_fictifs()[0]
    b.pennylane_subscription_id = "777"
    abo = pennylane.construire_abonnement(b, demo.indices_fictifs(AUJ), mapping, aujourdhui=AUJ)
    appels = []
    monkeypatch.setattr(pennylane.urllib.request, "urlopen", lambda *a, **k: appels.append(a))
    assert pennylane.envoyer([abo], mapping, token="x") == [] and appels == []


def test_premier_du_mois_suivant():
    assert pennylane.premier_du_mois_suivant(date(2026, 12, 15)) == date(2027, 1, 1)
    assert pennylane.premier_du_mois_suivant(date(2026, 9, 9)) == date(2026, 10, 1)

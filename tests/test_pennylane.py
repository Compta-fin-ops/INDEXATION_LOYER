import json
from datetime import date

import pytest

from indexation_loyer import demo, pennylane


def test_construire_abonnement_dry_run(tmp_path):
    mapping = pennylane.charger_mapping()
    obs = demo.indices_fictifs(date(2026, 9, 9))
    baux = demo.baux_fictifs()
    abo = pennylane.construire_abonnement(baux[0], obs, mapping, aujourdhui=date(2026, 9, 9))
    assert abo.corps["customer_id"] == 100001
    assert abo.corps["start"] == "2026-10-01"
    assert abo.corps["recurrence"] == {"type": "monthly", "day_of_month": 1}
    assert [l["label"] for l in abo.corps["invoice_lines"]] == ["Loyer Boutique 12 rue de la Paix – échéance mensuelle", "Provision sur charges"]
    assert "product_id" not in abo.corps["invoice_lines"][0]     # champ vide supprimé
    assert abo.corps["invoice_lines"][0]["raw_currency_unit_price"] == 2123.5   # 25 482 / 12 (indices fictifs)
    assert not abo.avertissements

    sans_client = pennylane.construire_abonnement(baux[3], obs, mapping, aujourdhui=date(2026, 9, 9))
    assert any("customer_id" in a for a in sans_client.avertissements)

    chemins = pennylane.ecrire_dry_run([abo, sans_client], tmp_path, "SCI TEST")
    assert len(chemins) == 2
    assert json.loads(chemins[0].read_text(encoding="utf-8"))["body"]["customer_id"] == 100001


def test_envoi_refuse_tant_que_schema_non_valide():
    mapping = pennylane.charger_mapping()
    assert mapping["schema_valide"] is False
    with pytest.raises(RuntimeError, match="schema_valide"):
        pennylane.envoyer([], mapping, token="x")


def test_premier_du_mois_suivant():
    assert pennylane.premier_du_mois_suivant(date(2026, 12, 15)) == date(2027, 1, 1)
    assert pennylane.premier_du_mois_suivant(date(2026, 9, 9)) == date(2026, 10, 1)

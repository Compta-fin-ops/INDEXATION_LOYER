from datetime import date
from pathlib import Path

import pytest

from indexation_loyer.periodes import Trimestre, periode_sdmx_vers_fr, dernier_trimestre_publiable
from indexation_loyer import insee
from indexation_loyer.baux import Bail

FIXTURES = Path(__file__).parent / "fixtures"


def test_trimestre_parse_et_conversion():
    t = Trimestre.parse("2024-Q2")
    assert t.fr == "2024-T2" and t.sdmx == "2024-Q2"
    assert Trimestre.parse("2024-t2") == t
    assert t.plus_annees(1).fr == "2025-T2"
    assert t.plus_trimestres(-2).fr == "2023-T4"
    assert Trimestre.de_date(date(2026, 9, 9)).fr == "2026-T3"
    assert periode_sdmx_vers_fr("2010-Q4") == "2010-T4"
    assert dernier_trimestre_publiable(date(2026, 9, 9)).fr == "2026-T1"
    with pytest.raises(ValueError):
        Trimestre.parse("2024-T5")


def test_parser_sdmx_fixture():
    obs = insee.parser_sdmx((FIXTURES / "sdmx_sample.xml").read_bytes(), date_extraction=date(2026, 9, 9))
    assert len(obs) == 7
    ilc = [o for o in obs if o.serie == "ILC"]
    assert [o.periode for o in ilc] == ["2025-T1", "2025-T2", "2025-T3", "2025-T4", "2026-T1"]
    assert ilc[-1].valeur == 122.0 and ilc[-1].statut_obs == "P"
    assert ilc[0].date_maj_insee == "2026-06-24" and ilc[0].source == "INSEE_SDMX"
    icc = [o for o in obs if o.serie == "ICC"]
    assert icc[0].valeur == 2000.0


def test_parser_sdmx_garde_fou_titre():
    xml = (FIXTURES / "sdmx_sample.xml").read_text(encoding="utf-8")
    xml = xml.replace("Indice des loyers commerciaux (ILC)", "Indice bidon")
    with pytest.raises(ValueError, match="Garde-fou"):
        insee.parser_sdmx(xml.encode("utf-8"))


def test_cache_fusion(tmp_path):
    chemin = tmp_path / "cache.csv"
    obs = insee.parser_sdmx((FIXTURES / "sdmx_sample.xml").read_bytes())
    manuel = insee.Observation("ILC", "001532540", "2026-T2", 999.0, "", "", "MANUEL", "2026-09-09")
    manuel_ecrase = insee.Observation("ILC", "001532540", "2026-T1", 1.0, "", "", "MANUEL", "2026-09-09")
    insee.ecrire_cache(insee.fusionner([manuel, manuel_ecrase], obs), chemin)
    relu = insee.lire_cache(chemin)
    par_cle = {o.cle: o for o in relu}
    assert par_cle[("ILC", "2026-T2")].valeur == 999.0            # saisie manuelle conservée
    assert par_cle[("ILC", "2026-T1")].valeur == 122.0            # l'INSEE prime sur le manuel
    assert insee.dernieres_valeurs(relu)["ILC"].periode == "2026-T2"


def test_parser_csv_insee_secours():
    contenu = ("Libellé;Indice des loyers commerciaux (ILC) - Base 100 au 1er trimestre 2008\n"
               "idBank;001532540\nDernière mise à jour;24/06/2026 08:45\nPériode;Valeur;Codes\n"
               "2025-Q4;121.5;A\n2026-Q1;122,00;P\n").encode("utf-8")
    obs = insee.parser_csv_insee(contenu)
    assert [(o.periode, o.valeur) for o in obs] == [("2025-T4", 121.5), ("2026-T1", 122.0)]
    assert obs[0].source == "INSEE_CSV" and obs[0].serie == "ILC"


def test_calendrier_bail_chaine_et_base_fixe():
    b = Bail(id="B1", local="Boutique", locataire="X", date_effet=date(2023, 4, 1),
             loyer_initial_annuel_ht=12000, trimestre_base="2022-T4")
    ech = list(b.calendrier(horizon=date(2026, 9, 9)))
    assert [e.date_revision for e in ech] == [date(2024, 4, 1), date(2025, 4, 1), date(2026, 4, 1)]
    assert [e.trimestre_reference for e in ech] == ["2023-T4", "2024-T4", "2025-T4"]
    assert [e.trimestre_precedent for e in ech] == ["2022-T4", "2023-T4", "2024-T4"]

    b2 = Bail(id="B2", local="Bureaux", locataire="Y", date_effet=date(2020, 7, 1), duree_ans=9,
              loyer_initial_annuel_ht=30000, indice="ILAT", periodicite_revision_ans=3, methode="Base fixe")
    assert b2.trimestre_base == "2020-T1"          # T-2 par défaut
    ech2 = list(b2.calendrier(horizon=date(2030, 1, 1)))
    assert [e.date_revision for e in ech2] == [date(2023, 7, 1), date(2026, 7, 1), date(2029, 7, 1)]
    assert [e.trimestre_precedent for e in ech2] == ["2020-T1"] * 3
    assert not b2.erreurs


def test_bail_erreurs():
    b = Bail(id="B3", local="", locataire="", date_effet=date(2024, 1, 1), loyer_initial_annuel_ht=1,
             indice="IPC", methode="Autre", periodicite_facturation="Hebdo")
    assert len(b.erreurs) == 3


def test_parser_xlsx_insee_export_reel():
    obs = insee.parser_xlsx_insee(FIXTURES / "insee_export_serie_001532540_10092026.xlsx", date_extraction=date(2026, 9, 10))
    assert len(obs) == 85 and {o.serie for o in obs} == {"ILC"}
    par = {o.periode: o for o in obs}
    assert par["2026-T1"].valeur == 135.26 and par["2026-T1"].statut_obs == "JO 28/06/2026"
    assert par["2005-T1"].valeur == 92.15 and par["2025-T4"].valeur == 134.62
    assert obs[0].source == "INSEE_XLSX" and obs[0].date_maj_insee == "24/06/2026 12:00"

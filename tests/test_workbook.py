"""Classeur autonome : structure, lecture, mise à jour des indices, et parité des formules
recalculées par LibreOffice avec le moteur Python (ignoré si soffice indisponible)."""
import json
import os
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook

from indexation_loyer import calcul, demo, pennylane
from indexation_loyer.insee import Observation
from indexation_loyer.workbook import (COLS_BAUX, R, A, P, B_LOYER, cellule_indice, ligne_revision,
                                       lire_classeur, lire_indices_classeur, mettre_a_jour_indices)

AUJOURDHUI = date(2026, 9, 9)


@pytest.fixture(scope="module")
def classeur_demo(tmp_path_factory) -> Path:
    return demo.generer_demo(tmp_path_factory.mktemp("demo"), aujourdhui=AUJOURDHUI)


def test_structure(classeur_demo):
    wb = load_workbook(classeur_demo)
    assert wb.sheetnames == ["Lisez-moi", "Société", "Baux", "Indices", "Révisions", "Alertes", "Pennylane", "Indices_long"]
    assert wb["Indices_long"].sheet_state == "hidden"
    ws = wb["Baux"]
    assert [ws.cell(row=1, column=i + 1).value for i in range(len(COLS_BAUX))] == [n for n, _ in COLS_BAUX]
    ws_r = wb["Révisions"]
    assert ws_r.max_row == 1 + 40 * 12                       # grille pré-câblée
    assert ws_r["A2"].value.startswith("=IF(Baux!$A$2")
    assert ws_r[f"{R['N°']}{ligne_revision(2, 3)}"].value == 3
    assert "_xlfn.MAXIFS" in wb["Alertes"][f"{A['N° dernière révision effective']}2"].value
    assert wb["Indices"][cellule_indice("ILC", 2025, 4)].value == 123.8   # valeur fictive écrite dans la grille


def test_lecture_et_mise_a_jour_indices_preservent_les_saisies(classeur_demo, tmp_path):
    copie = tmp_path / "copie.xlsx"
    shutil.copy(classeur_demo, copie)
    wb = load_workbook(copie)
    wb["Révisions"][f"{R['Commentaire']}2"] = "Facturé le 05/04/2022"
    wb["Baux"][f"{B_LOYER}2"] = 25000
    wb.save(copie)

    societe, baux, saisies = lire_classeur(copie)
    assert societe.demo and societe.max_baux == 40 and len(baux) == 4
    assert baux[0].loyer_initial_annuel_ht == 25000 and baux[0].ligne_baux == 2
    assert saisies.decision[("B01", 4)] == "Geler – sans rattrapage"
    assert saisies.commentaire[("B01", 1)] == "Facturé le 05/04/2022"
    assert saisies.consigne[("B03", 6)].startswith("Gel d'un an")

    n = mettre_a_jour_indices(copie, [Observation("ILC", "001532540", "2026-T2", 130.0, "A", "", "INSEE_SDMX", "")])
    assert n == 1
    wb2 = load_workbook(copie)
    assert wb2["Indices"][cellule_indice("ILC", 2026, 2)].value == 130.0
    assert wb2["Révisions"][f"{R['Commentaire']}2"].value == "Facturé le 05/04/2022"
    assert wb2["Baux"][f"{B_LOYER}2"].value == 25000
    assert list(tmp_path.glob("copie.*.bak.xlsx"))
    assert ("ILC", "2026-T2") in {o.cle for o in lire_indices_classeur(copie)}


def test_lire_classeur_refuse_colonnes_modifiees(classeur_demo, tmp_path):
    copie = tmp_path / "casse.xlsx"
    shutil.copy(classeur_demo, copie)
    wb = load_workbook(copie)
    wb["Baux"].insert_cols(3)
    wb.save(copie)
    with pytest.raises(ValueError, match="feuille Baux"):
        lire_classeur(copie)


def _recalculer(chemin: Path, tmp: Path) -> Path | None:
    soffice = shutil.which("soffice")
    if not soffice:
        return None
    env = dict(os.environ, HOME=str(tmp / "home"))
    (tmp / "home").mkdir(exist_ok=True)
    subprocess.run([soffice, "--headless", "--norestore", "--convert-to", "xlsx", "--outdir", str(tmp / "out"), str(chemin)],
                   capture_output=True, text=True, timeout=600, env=env)
    sortie = tmp / "out" / chemin.name
    return sortie if sortie.exists() else None


def test_parite_libreoffice_moteur_python(classeur_demo, tmp_path):
    recalc = _recalculer(classeur_demo, tmp_path)
    if recalc is None:
        pytest.skip("LibreOffice Calc indisponible : parité non vérifiée")
    wb = load_workbook(recalc, data_only=True)

    # 1. aucune erreur de formule
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                assert not (isinstance(v, str) and v.startswith(("#", "Err:"))), f"{ws.title}: {v}"

    # 2. Révisions actives : identiques au moteur Python ; lignes hors bail vides
    indices = calcul.table_indices(demo.indices_fictifs(AUJOURDHUI))
    decisions = demo.saisies_fictives().decision
    baux = demo.baux_fictifs()
    attendu = {}
    for b in baux:
        for l in calcul.calculer(b, indices, horizon=b.date_fin, aujourdhui=AUJOURDHUI, decisions=decisions):
            attendu[(b.id, l.echeance.numero)] = l
    ws = wb["Révisions"]
    actives = 0
    for r in range(2, ws.max_row + 1):
        if ws[f"{R['Actif']}{r}"].value != 1:
            assert ws[f"{R['Statut']}{r}"].value in (None, "") and ws[f"{R['Loyer retenu (annuel HT)']}{r}"].value in (None, "")
            continue
        cle = (ws[f"A{r}"].value, ws[f"{R['N°']}{r}"].value)
        l = attendu.pop(cle)
        retenu = ws[f"{R['Loyer retenu (annuel HT)']}{r}"].value
        assert (retenu in (None, "")) == (l.loyer_retenu is None), cle
        if l.loyer_retenu is not None:
            assert abs(retenu - l.loyer_retenu) < 0.005, cle
        assert ws[f"{R['Statut']}{r}"].value == l.statut, cle
        assert ws[f"{R['Trim. précédent']}{r}"].value == l.trimestre_precedent_effectif, cle
        assert ws[f"{R['Date de révision']}{r}"].value.date() == l.echeance.date_revision
        actives += 1
    assert actives == 30 and not attendu

    # 3. Alertes
    ws_a = wb["Alertes"]
    for r in range(2, 6):
        b = next(b for b in baux if b.id == ws_a[f"A{r}"].value)
        lignes = calcul.calculer(b, indices, horizon=b.date_fin, aujourdhui=AUJOURDHUI, decisions=decisions)
        assert abs(ws_a[f"{A['Loyer actuel (annuel HT)']}{r}"].value - calcul.loyer_actuel(b, lignes, AUJOURDHUI)) < 0.005
    assert ws_a[f"{A['Révisions gelées']}2"].value == 1
    assert ws_a[f"{A['Indice publié ?']}2"].value == "Non" and ws_a[f"{A['Trimestre attendu']}2"].value == "2026-T4"
    assert ws_a[f"{A['Action']}2"].value.startswith("⚠ Facturer")
    assert ws_a["A6"].value in (None, "") and ws_a[f"{A['Action']}6"].value in (None, "")   # ligne sans bail

    # 4. Lisez-moi : derniers indices saisis
    ws_l = wb["Lisez-moi"]
    derniers = {ws_l[f"B{r}"].value.split(" – ")[0]: (ws_l[f"C{r}"].value, ws_l[f"D{r}"].value) for r in range(8, 12)}
    assert derniers["ILC"] == ("2026-T1", 124.4) and derniers["ICC"][0] == "2026-T1"

    # 5. Pennylane : JSON par formules == corps du client Python
    mapping = pennylane.charger_mapping()
    societe, _, _ = lire_classeur(classeur_demo)
    reglages = pennylane.ReglagesPennylane(societe.pl_mode, societe.pl_payment_conditions, societe.pl_payment_method)
    ws_p = wb["Pennylane"]
    for r in range(2, 6):
        excel = json.loads(ws_p[f"{P['Corps JSON – POST /api/external/v2/billing_subscriptions']}{r}"].value.replace("À RENSEIGNER", "0"))
        b = next(b for b in baux if b.id == ws_p[f"A{r}"].value)
        python = pennylane.construire_abonnement(b, demo.indices_fictifs(AUJOURDHUI), mapping, reglages,
                                                 aujourdhui=date.today(), decisions=decisions).corps   # TODAY() côté LibreOffice
        python["customer_invoice_data"].pop("special_mention", None)
        python.setdefault("customer_id", 0)
        assert excel == python, b.id
    assert ws_p[f"{P['Corps JSON – POST /api/external/v2/billing_subscriptions']}6"].value in (None, "")

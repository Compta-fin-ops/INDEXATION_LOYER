"""Classeur « un onglet par bail » : structure, lecture, mise à jour des indices, et parité des
formules recalculées par LibreOffice avec le moteur Python (ignoré si soffice indisponible)."""
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
from indexation_loyer.workbook import (PC, R, A, P, SC, colonne_zone, ligne_revision, nom_onglet,
                                       lire_classeur, lire_indices_classeur, mettre_a_jour_indices)

AUJOURDHUI = date(2026, 9, 9)


@pytest.fixture(scope="module")
def classeur_demo(tmp_path_factory) -> Path:
    return demo.generer_demo(tmp_path_factory.mktemp("demo"), aujourdhui=AUJOURDHUI)


def test_structure(classeur_demo):
    wb = load_workbook(classeur_demo)
    assert wb.sheetnames == ["Lisez-moi", "Société", "Récapitulatif", "Indices", "Grille_rang", "Grille indices", "Modèle", "B01", "B02", "B03", "B04", "Pennylane"]
    assert wb["Grille_rang"].sheet_state == "hidden"
    fiche = wb["B03"]
    assert fiche["B3"].value == "ID bail (= nom de l'onglet)" and fiche[PC["id"]].value == "B03"
    assert fiche[f"{R['N°']}{ligne_revision(1)}"].value == 1 and fiche[f"{R['N°']}{ligne_revision(12)}"].value == 12
    assert fiche[f"{R['Décision']}{ligne_revision(6)}"].value == "Geler – rattrapage possible"
    assert "_xlfn.MAXIFS" in fiche[SC["n_eff"]].value
    assert wb["Récapitulatif"]["A2"].value == "B01" and "INDIRECT" in wb["Récapitulatif"]["B2"].value
    assert wb["Modèle"][PC["id"]].value is None and wb["Modèle"][PC["indice"]].value == "ILC"
    # zone de collage ILC : du plus récent au plus ancien
    zi = wb["Indices"]
    c0 = colonne_zone("ILC")
    assert zi.cell(row=1, column=c0).value == "ILC" and zi.cell(row=4, column=c0).value == "2026-T1"
    assert zi.cell(row=4, column=c0 + 1).value == 124.4
    assert nom_onglet("SCI/DUPONT [2]") == "SCI-DUPONT -2-"


def test_lecture_et_mise_a_jour_indices_preservent_les_saisies(classeur_demo, tmp_path):
    copie = tmp_path / "copie.xlsx"
    shutil.copy(classeur_demo, copie)
    wb = load_workbook(copie)
    wb["B01"][f"{R['Commentaire']}{ligne_revision(1)}"] = "Facturé le 05/04/2022"
    wb["B01"][PC["loyer_initial_annuel_ht"]] = 25000
    wb.save(copie)

    societe, baux, saisies = lire_classeur(copie)
    assert societe.demo and len(baux) == 4 and [b.onglet for b in baux] == ["B01", "B02", "B03", "B04"]
    assert baux[0].loyer_initial_annuel_ht == 25000
    assert saisies.decision[("B01", 4)] == "Geler – sans rattrapage"
    assert saisies.commentaire[("B01", 1)] == "Facturé le 05/04/2022"
    assert saisies.consigne[("B03", 6)].startswith("Gel d'un an")

    # refresh : seule la zone ILC est réécrite ; les autres zones et les fiches ne bougent pas
    n = mettre_a_jour_indices(copie, [Observation("ILC", "001532540", "2026-T2", 130.0, "JO 24/09/2026", "", "INSEE_XLSX", "")])
    assert n == 1
    wb2 = load_workbook(copie)
    zi = wb2["Indices"]
    c0 = colonne_zone("ILC")
    assert (zi.cell(row=4, column=c0).value, zi.cell(row=4, column=c0 + 1).value, zi.cell(row=4, column=c0 + 2).value) == ("2026-T2", 130.0, "24/09/2026")
    assert zi.cell(row=5, column=c0).value is None                                  # zone ILC remplacée
    assert zi.cell(row=4, column=colonne_zone("ILAT")).value == "2026-T1"          # zone ILAT intacte
    assert wb2["B01"][f"{R['Commentaire']}{ligne_revision(1)}"].value == "Facturé le 05/04/2022"
    assert wb2["B01"][PC["loyer_initial_annuel_ht"]].value == 25000
    assert list(tmp_path.glob("copie.*.bak.xlsx"))
    obs = {o.cle: o.valeur for o in lire_indices_classeur(copie)}
    assert obs[("ILC", "2026-T2")] == 130.0 and ("ILAT", "2026-T1") in obs


def test_onglet_duplique_a_la_main_est_lu(classeur_demo, tmp_path):
    """Simule l'usage réel : copie du Modèle, renommage, saisie des paramètres."""
    copie = tmp_path / "dup.xlsx"
    shutil.copy(classeur_demo, copie)
    wb = load_workbook(copie)
    ws = wb.copy_worksheet(wb["Modèle"])
    ws.title = "DURAND"
    ws[PC["id"]], ws[PC["local"]], ws[PC["locataire"]] = "DURAND", "Cave 3", "Durand & Fils"
    ws[PC["date_effet"]], ws[PC["loyer_initial_annuel_ht"]], ws[PC["trimestre_base"]] = date(2024, 2, 1), 6000, "2023-T3"
    wb.save(copie)
    _, baux, _ = lire_classeur(copie)
    d = next(b for b in baux if b.id == "DURAND")
    assert d.onglet == "DURAND" and d.loyer_initial_annuel_ht == 6000 and d.indice == "ILC" and d.duree_ans == 9


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

    # 2. fiches : révisions actives identiques au moteur Python, lignes hors durée vides
    indices = calcul.table_indices(demo.indices_fictifs(AUJOURDHUI))
    decisions = demo.saisies_fictives().decision
    baux = demo.baux_fictifs()
    actives = 0
    for b in baux:
        ws = wb[nom_onglet(b.id)]
        attendu = {l.echeance.numero: l for l in calcul.calculer(b, indices, horizon=b.date_fin, aujourdhui=AUJOURDHUI, decisions=decisions)}
        for k in range(1, 13):
            r = ligne_revision(k)
            if ws[f"{R['Actif']}{r}"].value != 1:
                assert k not in attendu and ws[f"{R['Statut']}{r}"].value in (None, "")
                continue
            l = attendu.pop(k)
            retenu = ws[f"{R['Loyer retenu (annuel HT)']}{r}"].value
            assert (retenu in (None, "")) == (l.loyer_retenu is None), (b.id, k)
            if l.loyer_retenu is not None:
                assert abs(retenu - l.loyer_retenu) < 0.005, (b.id, k)
            assert ws[f"{R['Statut']}{r}"].value == l.statut, (b.id, k)
            assert ws[f"{R['Trim. précédent']}{r}"].value == l.trimestre_precedent_effectif, (b.id, k)
            assert ws[f"{R['Date de révision']}{r}"].value.date() == l.echeance.date_revision
            actives += 1
        assert not attendu
        # situation
        lignes = calcul.calculer(b, indices, horizon=b.date_fin, aujourdhui=AUJOURDHUI, decisions=decisions)
        assert abs(ws[SC["loyer_actuel"]].value - calcul.loyer_actuel(b, lignes, AUJOURDHUI)) < 0.005
        assert ws[PC["controles"]].value == "OK"
    assert actives == 30
    b01 = wb["B01"]
    assert b01[SC["gelees"]].value == 1 and b01[SC["indice_publie"]].value == "Non" and b01[SC["trim_attendu"]].value == "2026-T4"
    assert str(b01[SC["action"]].value).startswith("⚠ Facturer")

    # 3. récapitulatif (INDIRECT) = fiches ; total
    ws_r = wb["Récapitulatif"]
    for r in range(2, 6):
        ong = ws_r[f"A{r}"].value
        assert ws_r[f"{A['ID bail']}{r}"].value == ong and ws_r[f"{A['Contrôles']}{r}"].value == "OK"
        assert abs(ws_r[f"{A['Loyer actuel (annuel HT)']}{r}"].value - wb[ong][SC["loyer_actuel"]].value) < 0.005
    assert ws_r["A6"].value in (None, "") and ws_r[f"{A['Action']}6"].value in (None, "")
    total = sum(wb[nom_onglet(b.id)][SC["loyer_actuel"]].value for b in baux)
    assert abs(ws_r[f"{A['Loyer actuel (annuel HT)']}43"].value - total) < 0.01

    # 4. Lisez-moi : derniers indices collés
    ws_l = wb["Lisez-moi"]
    derniers = {ws_l[f"B{r}"].value.split(" – ")[0]: (ws_l[f"C{r}"].value, ws_l[f"D{r}"].value) for r in range(9, 13)}
    assert derniers["ILC"] == ("2026-T1", 124.4) and derniers["ICC"] == ("2026-T1", 1892)

    # 5. Grille indices = zones collées
    assert wb["Grille indices"]["B29"].value == 124.4          # ILC 2026-T1 (ligne 3 + 26)

    # 6. Pennylane : JSON par formules == corps du client Python
    mapping = pennylane.charger_mapping()
    societe, _, _ = lire_classeur(classeur_demo)
    reglages = pennylane.ReglagesPennylane(societe.pl_mode, societe.pl_payment_conditions, societe.pl_payment_method)
    ws_p = wb["Pennylane"]
    for r in range(2, 6):
        excel = json.loads(ws_p[f"{P['Corps JSON – POST /api/external/v2/billing_subscriptions']}{r}"].value.replace("À RENSEIGNER", "0"))
        b = next(b for b in baux if nom_onglet(b.id) == ws_p[f"A{r}"].value)
        python = pennylane.construire_abonnement(b, demo.indices_fictifs(AUJOURDHUI), mapping, reglages,
                                                 aujourdhui=date.today(), decisions=decisions).corps   # TODAY() côté LibreOffice
        python["customer_invoice_data"].pop("special_mention", None)
        python.setdefault("customer_id", 0)
        assert excel == python, b.id
    assert ws_p[f"{P['Corps JSON – POST /api/external/v2/billing_subscriptions']}6"].value in (None, "")

from datetime import date
from pathlib import Path

from indexation_loyer import calcul, courriers, demo
from indexation_loyer.workbook import lire_classeur

AUJ = date(2026, 9, 9)


def test_selection_et_generation_pdf(tmp_path):
    classeur = demo.generer_demo(tmp_path, aujourdhui=AUJ)
    societe, baux, saisies = lire_classeur(classeur)
    obs = demo.indices_fictifs(AUJ)
    cibles = courriers.selectionner(baux, obs, saisies, horizon_jours=120, aujourdhui=AUJ)
    cles = {(b.id, l.echeance.numero): l.statut for b, l in cibles}
    # B01 n°1..3 appliquées (exclues), n°4 gelée mais courrier déjà envoyé (exclue), n°5 calculable -> à notifier
    assert ("B01", 5) in cles and ("B01", 4) not in cles and ("B01", 3) not in cles
    # B03 n°6 gelée sans courrier -> notifiée ; n°7 rattrapage -> notifiée
    assert cles[("B03", 6)] == "❄ Gelée" and cles[("B03", 7)] == "✔ Calculable"
    chemins = courriers.generer(societe, cibles, tmp_path / "pdf", aujourdhui=AUJ)
    assert len(chemins) == len(cibles) and all(p.exists() and p.stat().st_size > 1500 for p in chemins)
    assert any("_gel_" in p.name for p in chemins) and any("_revision_" in p.name for p in chemins)

    # --tous inclut les échéances déjà notifiées / appliquées
    assert len(courriers.selectionner(baux, obs, saisies, tous=True, aujourdhui=AUJ)) > len(cibles)

    # marquage de la date d'envoi puis relecture : plus rien à notifier
    n = courriers.marquer_envoyes(classeur, cibles, aujourdhui=AUJ)
    assert n == len(cibles)
    _, _, saisies2 = lire_classeur(classeur)
    assert courriers.selectionner(baux, obs, saisies2, horizon_jours=120, aujourdhui=AUJ) == []


def test_valeurs_courrier_gel_et_application():
    societe, baux, saisies = demo.generer_demo.__globals__["Societe"](nom="S"), demo.baux_fictifs(), demo.saisies_fictives()
    indices = calcul.table_indices(demo.indices_fictifs(AUJ))
    b01 = baux[0]
    lignes = calcul.calculer(b01, indices, b01.date_fin, AUJ, saisies.decision)
    gel = courriers.valeurs_courrier(societe, b01, lignes[3])
    assert gel["loyer_nouveau_annuel"] == gel["loyer_precedent_annuel"]
    appli = courriers.valeurs_courrier(societe, b01, lignes[4])
    assert appli["loyer_nouveau_annuel"] != appli["loyer_precedent_annuel"] and appli["variation_pct"].endswith("%")


def test_montants_par_echeance_et_virement(tmp_path):
    """Courrier lisible : loyer annuel, par échéance HT, charges, TVA et montant TTC à régler ; virement à ajuster."""
    from datetime import date
    from indexation_loyer import calcul, courriers
    from indexation_loyer.baux import Bail
    b = Bail(id="B01", local="Local", locataire="Pharmacie", date_effet=date(2025, 12, 1), loyer_initial_annuel_ht=1440.0,
             charges_annuelles_ht=240.0, indice="ILC", trimestre_base="2025-T1", periodicite_revision_ans=1, methode="Chaînée",
             periodicite_facturation="Mensuelle", tva_pct=8.5)
    l = calcul.calculer(b, {("ILC", "2025-T1"): 135.87, ("ILC", "2026-T1"): 135.26}, horizon=date(2027, 1, 1), aujourdhui=date(2026, 11, 25))[0]
    assert l.loyer_retenu == 1433.53
    assert courriers.montants_echeance(b, 1440.0) == {"loyer": 120.0, "charges": 20.0, "tva": 11.9, "ttc": 151.9}
    assert courriers.montants_echeance(b, l.loyer_retenu) == {"loyer": 119.46, "charges": 20.0, "tva": 11.85, "ttc": 151.31}
    v = courriers.valeurs_courrier(courriers.Societe(nom="SCI"), b, l)
    assert (v["unite"], v["mois_revision"]) == ("mois", "décembre 2026")
    assert v["total_ttc_nouveau"].startswith("151,31") and v["total_ttc_precedent"].startswith("151,90")
    modele = courriers.charger_modele()
    texte = (modele["virement"] + " " + modele["rappel_baisse"]).format(**v)
    assert "porter le montant à 151,31" in texte and "versée en trop" in texte
    pdf = courriers.generer_un(courriers.Societe(nom="SCI"), b, l, tmp_path / "c.pdf", aujourdhui=date(2026, 11, 25))
    assert pdf.read_bytes().startswith(b"%PDF")

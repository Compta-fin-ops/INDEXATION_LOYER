"""Éditions de la console : état annuel des révisions d'une société, situation des loyers remise au gérant, en PDF."""
from __future__ import annotations

import io
from datetime import date, timedelta

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from indexation_loyer.baux import est_gel


def _eur(x) -> str:
    return "—" if x is None else f"{x:,.2f}".replace(",", " ").replace(".", ",") + " €"


def _nb(x, decimales: int = 2) -> str:
    return "—" if x is None else f"{x:.{decimales}f}".replace(".", ",").rstrip("0").rstrip(",") if decimales > 2 else f"{x:g}".replace(".", ",")


def lignes_etat(baux: list[dict], annee: int, aujourdhui: date | None = None) -> list[dict]:
    """Une ligne par bail : la révision tombée dans l'année, s'il y en a une, et le loyer en fin d'année.

    `baux` : [{bail (ligne de la table), revisions (LigneRevision), decisions {numero: ligne}}]."""
    fin = date(annee, 12, 31)
    sortie = []
    for x in baux:
        b, revs = x["bail"], x["revisions"]
        dans_l_annee = [l for l in revs if l.echeance.date_revision.year == annee]
        avant_fin = [l for l in revs if l.echeance.date_revision <= fin and l.loyer_retenu is not None]
        depart = b.get("reprise_loyer_annuel") if b.get("reprise_date") else b.get("loyer_initial_annuel")
        loyer_fin = avant_fin[-1].loyer_retenu if avant_fin else depart
        l = dans_l_annee[-1] if dans_l_annee else None
        dec = x["decisions"].get(l.echeance.numero, {}) if l else {}
        if l is None:
            nature = "Pas de révision dans l'année"
        elif est_gel(l.decision):
            nature = "Gelée" + (", rattrapage possible" if l.decision.endswith("rattrapage possible") else ", sans rattrapage")
        elif l.loyer_retenu is None:
            nature = "Indice non publié"
        elif aujourdhui and l.echeance.date_revision > aujourdhui:
            nature = "À venir, indice publié"
        elif l.loyer_brut is not None and l.loyer_retenu < l.loyer_brut:
            nature = "Appliquée, plafonnée"
        else:
            nature = "Appliquée"
        # le commentaire n'est repris que pour un gel, dont il est le motif : une note de travail n'a pas sa place ici
        sortie.append({"bail": b, "l": l, "nature": nature,
                       "commentaire": (dec.get("commentaire") or "") if l and est_gel(l.decision) else "",
                       "courrier_le": dec.get("courrier_le"), "loyer_fin": loyer_fin,
                       "loyer_debut": (l.loyer_precedent if l else loyer_fin)})
    return sortie


def etat_annuel(societe: dict, baux: list[dict], annee: int, aujourdhui: date, fictif: bool = False) -> bytes:
    """PDF paysage : les révisions de loyer d'une société sur une année civile."""
    lignes = lignes_etat(baux, annee, aujourdhui)
    ss = getSampleStyleSheet()
    base = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=8, leading=10)
    petit = ParagraphStyle("p", parent=base, fontSize=7, leading=9, textColor=colors.grey)
    droite = ParagraphStyle("d", parent=base, alignment=2)
    gras_droite = ParagraphStyle("gd", parent=droite, fontName="Helvetica-Bold")
    titre = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=18)
    tampon = io.BytesIO()
    doc = SimpleDocTemplate(tampon, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Révisions de loyer {annee} – {societe['nom']}", author=societe["nom"])
    el = []
    if fictif:
        el.append(Paragraph("DOCUMENT DE DÉMONSTRATION – VALEURS FICTIVES",
                            ParagraphStyle("f", parent=base, textColor=colors.red, fontName="Helvetica-Bold", alignment=1)))
    el += [Paragraph(f"Révisions de loyer de l'année {annee}", titre),
           Paragraph(f"<b>{societe['nom']}</b>" + (f" · SIREN {societe['siren']}" if societe.get("siren") else "")
                     + f" · état établi le {aujourdhui:%d/%m/%Y}", base), Spacer(1, 5 * mm)]
    entete = ["Bail", "Locataire et local", "Révision", "Indice de référence", "Indice précédent", "Coefficient",
              "Loyer annuel HT avant", "Loyer annuel HT après", "Variation", "Suite donnée"]
    corps = [[Paragraph(f"<b>{t}</b>", base) for t in entete]]
    total_debut = total_fin = 0.0
    for x in lignes:
        b, l = x["bail"], x["l"]
        suite = x["nature"] + (f"<br/><font color='grey'>{x['commentaire']}</font>" if x["commentaire"] else "") + (
            f"<br/><font color='grey'>courrier du {date.fromisoformat(x['courrier_le']):%d/%m/%Y}</font>" if x["courrier_le"] else "")
        apres = None if l is None else (l.loyer_precedent if est_gel(l.decision) else l.loyer_retenu)
        corps.append([
            Paragraph(f"<b>{b['code']}</b>", base),
            Paragraph(f"{b.get('locataire') or ''}<br/><font color='grey'>{b.get('local') or ''}</font>", base),
            Paragraph(f"{l.echeance.date_revision:%d/%m/%Y}" if l else "—", base),
            Paragraph(f"{b.get('indice')} {l.echeance.trimestre_reference}<br/>{_nb(l.indice_reference)}" if l else "—", base),
            Paragraph(f"{l.trimestre_precedent_effectif}<br/>{_nb(l.indice_precedent)}" if l else "—", base),
            Paragraph(f"{l.coefficient:.5f}".replace(".", ",") if l and l.coefficient else "—", droite),
            Paragraph(_eur(x["loyer_debut"]), droite),
            Paragraph(_eur(apres if l else x["loyer_fin"]), gras_droite),
            Paragraph(f"{l.variation_pct * 100:+.2f} %".replace(".", ",") if l and l.variation_pct is not None and not est_gel(l.decision) else "—", droite),
            Paragraph(suite, base)])
        total_debut += x["loyer_debut"] or 0
        total_fin += (x["loyer_fin"] or 0)
    corps.append([Paragraph("<b>Total</b>", base), Paragraph(f"{len(lignes)} bail/baux", base), "", "", "", "",
                  Paragraph(_eur(total_debut), gras_droite), Paragraph(_eur(total_fin), gras_droite),
                  Paragraph(f"{(total_fin / total_debut - 1) * 100:+.2f} %".replace(".", ",") if total_debut else "—", droite), ""])
    tableau = Table(corps, colWidths=[14 * mm, 62 * mm, 20 * mm, 27 * mm, 22 * mm, 19 * mm, 27 * mm, 27 * mm, 17 * mm, 34 * mm], repeatRows=1)
    tableau.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9E1F2")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#BFBFBF")), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    el += [tableau, Spacer(1, 4 * mm),
           Paragraph("Loyers annuels hors taxes et hors charges. « Avant » : loyer en vigueur avant la révision de l'année, ou loyer de l'année "
                     "en l'absence de révision. Le total « après » additionne les loyers en vigueur au 31 décembre. Indices publiés par l'INSEE. "
                     "Document d'information ; seules les stipulations de chaque bail font foi.", petit)]

    def pied(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(14 * mm, 9 * mm, f"{societe['nom']} – révisions de loyer {annee}")
        canvas.drawRightString(landscape(A4)[0] - 14 * mm, 9 * mm, f"Page {d.page}")
        canvas.restoreState()
    doc.build(el, onFirstPage=pied, onLaterPages=pied)
    return tampon.getvalue()


# ---------------------------------------------------------------------------
# Situation des loyers à date, pour décision du bailleur
# ---------------------------------------------------------------------------
HORIZON_SITUATION_JOURS = 365      # révisions passées et à venir retenues dans la situation remise au gérant


def lignes_situation(baux: list[dict], aujourdhui: date, horizon_jours: int = HORIZON_SITUATION_JOURS) -> dict[str, list[dict]]:
    """Quatre groupes pour la situation remise au gérant, chacun daté par la date de réindexation :
    « revises » (révisions des douze derniers mois, ancien et nouveau loyer), « connus » (à venir, indice publié),
    « attente » (indice pas encore publié, échéance passée ou à venir), « autres » (aucune révision dans la fenêtre).

    `baux` : [{bail (ligne avec ses champs calculés), revisions (LigneRevision, vide si le bail n'est pas calculé)}]."""
    debut, fin = aujourdhui - timedelta(days=horizon_jours), aujourdhui + timedelta(days=horizon_jours)
    groupes: dict[str, list[dict]] = {"revises": [], "connus": [], "attente": [], "autres": []}

    def ligne(b: dict, l) -> dict:
        return {"bail": b, "date": l.echeance.date_revision, "indice": b.get("indice") or "", "trimestre": l.echeance.trimestre_reference,
                "valeur": l.indice_reference, "precedent": l.trimestre_precedent_effectif, "valeur_precedente": l.indice_precedent,
                "avant": l.loyer_precedent, "apres": l.loyer_retenu, "gel": est_gel(l.decision),
                "plafonne": l.loyer_brut is not None and l.loyer_retenu is not None and l.loyer_retenu < l.loyer_brut and not est_gel(l.decision),
                "retard": l.echeance.date_revision <= aujourdhui}

    for x in baux:
        b, revs = x["bail"], x["revisions"]
        avant = len(groupes["revises"]) + len(groupes["connus"]) + len(groupes["attente"])
        passees = [l for l in revs if debut < l.echeance.date_revision <= aujourdhui]
        faites = [l for l in passees if l.loyer_retenu is not None]
        if faites:
            groupes["revises"].append(ligne(b, faites[-1]))
        for l in passees:                                # révision échue dont l'indice n'est pas encore publié
            if l.loyer_retenu is None and not est_gel(l.decision):
                groupes["attente"].append(ligne(b, l))
        prochaine = next((l for l in revs if aujourdhui < l.echeance.date_revision <= fin), None)
        if prochaine is not None:
            groupes["connus" if prochaine.loyer_retenu is not None else "attente"].append(ligne(b, prochaine))
        if len(groupes["revises"]) + len(groupes["connus"]) + len(groupes["attente"]) == avant:
            groupes["autres"].append({"bail": b, "loyer": b.get("loyer_attendu_annuel"),
                                      "prochaine": b.get("date_prochaine_revision")})
    for g in ("revises", "connus", "attente"):
        groupes[g].sort(key=lambda l: (l["date"], l["bail"].get("code") or ""))
    return groupes


def situation_loyers(societe: dict, groupes: dict[str, list[dict]], aujourdhui: date, fictif: bool = False) -> bytes:
    """PDF paysage remis au gérant, pour information : loyers révisés, révisions à venir avec ou sans indice publié."""
    ss = getSampleStyleSheet()
    base = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=8, leading=10)
    petit = ParagraphStyle("p", parent=base, fontSize=7, leading=9, textColor=colors.grey)
    droite = ParagraphStyle("d", parent=base, alignment=2)
    gras_droite = ParagraphStyle("gd", parent=droite, fontName="Helvetica-Bold")
    titre = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=18)
    section = ParagraphStyle("s", parent=base, fontName="Helvetica-Bold", fontSize=10, leading=13, spaceBefore=8, spaceAfter=3)

    largeur = landscape(A4)[0]
    tampon = io.BytesIO()
    doc = SimpleDocTemplate(tampon, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Situation des loyers – {societe['nom']}", author=societe["nom"])
    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9E1F2")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#BFBFBF")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)])
    en = lambda titres: [Paragraph(f"<b>{t}</b>", base) for t in titres]
    bail = lambda b: [Paragraph(f"<b>{b['code']}</b>", base),
                      Paragraph(f"{b.get('locataire') or ''}<br/><font color='grey'>{b.get('local') or ''}</font>", base)]
    pct = lambda v: "—" if v is None else f"{v * 100:+.2f} %".replace(".", ",")
    tri = lambda t: (t or "").replace("-", " ")
    indice = lambda l: (f"{l['indice']} {tri(l['trimestre'])} : {_nb(l['valeur'])}<br/>"
                        f"<font color='grey'>contre {tri(l['precedent'])} : {_nb(l['valeur_precedente'])}</font>")

    def tableau_variations(lignes: list[dict], total: str) -> Table:
        corps = [en(["Bail", "Locataire et local", "Réindexation au", "Indice", "Ancien loyer", "Nouveau loyer", "Variation",
                     "Écart annuel", "Observation"])]
        total_avant = total_apres = 0.0
        for l in lignes:
            variation = l["apres"] / l["avant"] - 1 if l["apres"] is not None and l["avant"] else None
            observation = "Gel décidé : loyer maintenu" if l["gel"] else "Hausse plafonnée par le bail" if l["plafonne"] else (
                "Baisse de l'indice" if variation is not None and variation < 0 else "")
            corps.append([*bail(l["bail"]), Paragraph(f"<b>{l['date']:%d/%m/%Y}</b>", base), Paragraph(indice(l), base),
                          Paragraph(_eur(l["avant"]), droite), Paragraph(_eur(l["apres"]), gras_droite), Paragraph(pct(variation), droite),
                          Paragraph(_eur(l["apres"] - l["avant"]) if l["apres"] is not None and l["avant"] is not None else "—", droite),
                          Paragraph(observation, petit)])
            total_avant += l["avant"] or 0
            total_apres += l["apres"] or 0
        corps.append([Paragraph("<b>Total</b>", base), "", "", "", Paragraph(_eur(total_avant), gras_droite),
                      Paragraph(_eur(total_apres), gras_droite), Paragraph(pct(total_apres / total_avant - 1) if total_avant else "—", droite),
                      Paragraph(_eur(total_apres - total_avant), gras_droite), Paragraph(total, petit)])
        t = Table(corps, colWidths=[13 * mm, 62 * mm, 22 * mm, 42 * mm, 26 * mm, 26 * mm, 17 * mm, 24 * mm, 37 * mm], repeatRows=1)
        t.setStyle(style)
        t.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]))
        return t

    el = []
    if fictif:
        el.append(Paragraph("DOCUMENT DE DÉMONSTRATION – VALEURS FICTIVES",
                            ParagraphStyle("f", parent=base, textColor=colors.red, fontName="Helvetica-Bold", alignment=1)))
    el += [Paragraph(f"Situation des loyers au {aujourdhui:%d/%m/%Y}", titre),
           Paragraph(f"<b>{societe['nom']}</b>" + (f" · SIREN {societe['siren']}" if societe.get("siren") else "")
                     + " · loyers annuels hors taxes et hors charges · document d'information", base)]

    el.append(Paragraph(f"1. Loyers réindexés au cours des douze derniers mois : {len(groupes['revises'])}", section))
    el.append(tableau_variations(groupes["revises"], "loyers en vigueur") if groupes["revises"] else Paragraph("Aucun.", base))

    el.append(Paragraph(f"2. Réindexations à venir, indice publié : {len(groupes['connus'])}", section))
    el.append(tableau_variations(groupes["connus"], "après ces réindexations") if groupes["connus"] else Paragraph("Aucune.", base))

    el.append(Paragraph(f"3. Réindexations dont l'indice n'est pas encore publié : {len(groupes['attente'])}", section))
    if groupes["attente"]:
        corps = [en(["Bail", "Locataire et local", "Réindexation au", "Indice attendu", "Loyer en cours", "Situation"])]
        for l in groupes["attente"]:
            situation = ("<b>Date dépassée.</b> Le loyer en cours reste facturé à titre provisoire ; un rappel sera dû à la publication."
                         if l["retard"] else "Nouveau loyer calculé dès la publication de l'indice.")
            corps.append([*bail(l["bail"]), Paragraph(f"<b>{l['date']:%d/%m/%Y}</b>", base),
                          Paragraph(f"{l['indice']} {tri(l['trimestre'])}", base), Paragraph(_eur(l["avant"]), droite), Paragraph(situation, base)])
        t = Table(corps, colWidths=[13 * mm, 70 * mm, 24 * mm, 32 * mm, 28 * mm, 102 * mm], repeatRows=1)
        t.setStyle(style)
        el.append(t)
    else:
        el.append(Paragraph("Aucune.", base))

    if groupes["autres"]:
        el.append(Paragraph(f"4. Autres baux : {len(groupes['autres'])}", section))
        corps = [en(["Bail", "Locataire et local", "Loyer en cours", "Prochaine réindexation"])]
        for x in groupes["autres"]:
            prochaine = date.fromisoformat(str(x["prochaine"])[:10]).strftime("%d/%m/%Y") if x["prochaine"] else "non déterminée"
            corps.append([*bail(x["bail"]), Paragraph(_eur(x["loyer"]), droite), Paragraph(prochaine, base)])
        t = Table(corps, colWidths=[13 * mm, 100 * mm, 30 * mm, 126 * mm], repeatRows=1)
        t.setStyle(style)
        el.append(t)

    el += [Spacer(1, 5 * mm),
           Paragraph("Indices publiés par l'INSEE. Document d'information établi d'après les paramètres de chaque bail ; "
                     "seules les stipulations du bail font foi.", petit)]

    def pied(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(14 * mm, 9 * mm, f"{societe['nom']} – situation des loyers au {aujourdhui:%d/%m/%Y}")
        canvas.drawRightString(largeur - 14 * mm, 9 * mm, f"Page {d.page}")
        canvas.restoreState()
    doc.build(el, onFirstPage=pied, onLaterPages=pied)
    return tampon.getvalue()


# ---------------------------------------------------------------------------
# Relevé des factures d'un bail, et courrier de demande de régularisation
# ---------------------------------------------------------------------------
STATUTS_RELEVE = {"paid": "Payée", "late": "En retard", "upcoming": "À échoir", "partially_paid": "Partiellement payée",
                  "unpaid": "Non payée"}
HORS_RELEVE = ("draft", "cancelled", "credit_note", "proforma", "shipping_order", "purchasing_order",
               "estimate_pending", "estimate_accepted", "estimate_invoiced", "estimate_denied")


def lignes_releve(factures: list[dict], depuis: date) -> list[dict]:
    """Factures émises depuis `depuis`, de la plus ancienne à la plus récente. Brouillons, avoirs, devis et factures
    annulées sont écartés : le signe des montants d'un avoir n'a pas été vérifié."""
    sortie = []
    for f in factures:
        d = date.fromisoformat(str(f["date"])[:10]) if f.get("date") else None
        if d is None or d < depuis or f.get("statut") in HORS_RELEVE:
            continue
        ttc, reste = f.get("montant_ttc") or 0.0, f.get("restant_du") or 0.0
        sortie.append({"date": d, "numero": f.get("numero") or "", "origine": f.get("origine") or "",
                       "echeance": date.fromisoformat(str(f["echeance"])[:10]) if f.get("echeance") else None,
                       "ttc": ttc, "regle": round(ttc - reste, 2), "reste": reste,
                       "statut": STATUTS_RELEVE.get(f.get("statut"), f.get("statut") or "")})
    return sorted(sortie, key=lambda x: (x["date"], x["numero"]))


def releve(societe: dict, bail: dict, lignes: list[dict], depuis: date, aujourdhui: date, courrier: bool = False,
           attendu_ttc: float | None = None, unite: str = "mois", fictif: bool = False) -> bytes:
    """PDF : relevé des factures du bail ; précédé, si `courrier`, d'une demande cordiale de régularisation."""
    ss = getSampleStyleSheet()
    base = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=10, leading=13.5, spaceAfter=6)
    petit = ParagraphStyle("p", parent=base, fontSize=8, leading=10, textColor=colors.grey, spaceAfter=0)
    cell = ParagraphStyle("c", parent=base, fontSize=8.5, leading=10.5, spaceAfter=0)
    droite = ParagraphStyle("d", parent=base, alignment=2)
    titre = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=12, leading=15, spaceBefore=4, spaceAfter=8)
    total_du = round(sum(l["reste"] for l in lignes), 2)
    nl = lambda t: (t or "").replace("\n", "<br/>")
    tampon = io.BytesIO()
    doc = SimpleDocTemplate(tampon, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                            title=f"Relevé – {bail.get('locataire', '')}", author=societe["nom"])
    el = []
    if fictif:
        el.append(Paragraph("DOCUMENT DE DÉMONSTRATION – VALEURS FICTIVES",
                            ParagraphStyle("f", parent=base, textColor=colors.red, fontName="Helvetica-Bold", alignment=1)))
    el += [Paragraph(f"<b>{societe['nom']}</b>" + (f"<br/>{nl(societe.get('adresse'))}" if societe.get("adresse") else ""), base),
           Spacer(1, 6 * mm),
           Paragraph(f"<b>{bail.get('locataire', '')}</b>" + (f"<br/>{nl(bail.get('adresse_locataire'))}" if bail.get("adresse_locataire") else ""), droite),
           Spacer(1, 4 * mm),
           Paragraph(f"{societe.get('ville_signature') + ', ' if societe.get('ville_signature') else ''}le {aujourdhui:%d/%m/%Y}", droite)]
    if courrier:
        el += [Paragraph(f"<b>Objet : relevé de compte – {bail.get('local', '')}</b>", base), Spacer(1, 2 * mm),
               Paragraph("Madame, Monsieur,", base),
               Paragraph(f"Vous trouverez ci-dessous le relevé des factures émises depuis le {depuis:%d/%m/%Y} au titre du bail des locaux "
                         f"désignés ci-dessus. Sauf erreur ou omission de notre part, il fait apparaître au {aujourdhui:%d/%m/%Y} "
                         f"un solde restant dû de <b>{_eur(total_du)}</b> toutes taxes comprises.", base)]
        if attendu_ttc is not None:
            el.append(Paragraph(f"Ce solde peut provenir d'un virement permanent dont le montant n'a pas suivi la dernière révision du loyer. "
                                f"Le montant de chaque échéance est aujourd'hui de <b>{_eur(attendu_ttc)}</b> toutes taxes comprises par {unite}.", base))
        el += [Paragraph("Nous vous remercions de bien vouloir procéder à la régularisation de ce solde et, le cas échéant, d'ajuster le montant "
                         "de votre virement. Si un règlement est intervenu entre-temps, nous vous prions de ne pas tenir compte de ce courrier.", base),
               Paragraph("Nous restons à votre disposition pour tout renseignement et vous prions d'agréer, Madame, Monsieur, "
                         "l'expression de nos salutations distinguées.", base), Spacer(1, 6 * mm),
               Paragraph(f"{societe.get('signataire') or societe['nom']}<br/>{societe.get('qualite_signataire') or ''}, {societe['nom']}", droite),
               Spacer(1, 6 * mm)]
    el.append(Paragraph(f"Relevé des factures du {depuis:%d/%m/%Y} au {aujourdhui:%d/%m/%Y}", titre))
    el.append(Paragraph(f"Bail {bail.get('code', '')} · {bail.get('local', '')}", petit))
    el.append(Spacer(1, 3 * mm))
    corps = [[Paragraph(f"<b>{t}</b>", cell) for t in ("Date", "Facture", "Échéance", "Montant TTC", "Réglé", "Reste dû", "Statut")]]
    for l in lignes:
        corps.append([Paragraph(f"{l['date']:%d/%m/%Y}", cell), Paragraph(l["numero"] or "—", cell),
                      Paragraph(f"{l['echeance']:%d/%m/%Y}" if l["echeance"] else "—", cell),
                      Paragraph(_eur(l["ttc"]), ParagraphStyle("cd", parent=cell, alignment=2)),
                      Paragraph(_eur(l["regle"]), ParagraphStyle("cd", parent=cell, alignment=2)),
                      Paragraph(f"<b>{_eur(l['reste'])}</b>" if l["reste"] > 0.005 else _eur(l["reste"]), ParagraphStyle("cd", parent=cell, alignment=2)),
                      Paragraph(l["statut"], cell)])
    corps.append([Paragraph("<b>Total</b>", cell), "", "", Paragraph(f"<b>{_eur(round(sum(l['ttc'] for l in lignes), 2))}</b>", ParagraphStyle("cd", parent=cell, alignment=2)),
                  Paragraph(f"<b>{_eur(round(sum(l['regle'] for l in lignes), 2))}</b>", ParagraphStyle("cd", parent=cell, alignment=2)),
                  Paragraph(f"<b>{_eur(total_du)}</b>", ParagraphStyle("cd", parent=cell, alignment=2)), ""])
    t = Table(corps, colWidths=[20 * mm, 30 * mm, 20 * mm, 26 * mm, 24 * mm, 24 * mm, 26 * mm], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9E1F2")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#BFBFBF")),
                           ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]))
    el += [t, Spacer(1, 4 * mm),
           Paragraph("Montants toutes taxes comprises. Règlements tels qu'enregistrés en comptabilité à la date du relevé.", petit)]
    doc.build(el)
    return tampon.getvalue()

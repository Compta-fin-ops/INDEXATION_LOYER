"""Éditions de la console : état annuel des révisions d'une société, en PDF."""
from __future__ import annotations

import io
from datetime import date

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
def repartir(lignes: list[dict]) -> dict[str, list[dict]]:
    """Trois groupes : nouveaux loyers connus, révisions en attente d'indice, révisions déjà traitées."""
    groupes: dict[str, list[dict]] = {"connus": [], "attente": [], "traites": []}
    for l in sorted(lignes, key=lambda x: x["date"]):
        if l["etat"] in ("retard", "attente"):
            groupes["attente"].append(l)
        elif l["etat"] == "faite":
            groupes["traites"].append(l)
        else:
            groupes["connus"].append(l)
    return groupes


def situation_loyers(societe: dict, lignes: list[dict], autres: list[dict], aujourdhui: date, fictif: bool = False) -> bytes:
    """PDF paysage remis au bailleur : où en est chaque loyer, et ce qu'il lui revient de décider.

    `lignes` : révisions suivies (une par bail, plus une révision échue sans indice le cas échéant) ;
    `autres` : baux sans révision à venir connue."""
    g = repartir(lignes)
    ss = getSampleStyleSheet()
    base = ParagraphStyle("b", parent=ss["Normal"], fontName="Helvetica", fontSize=8, leading=10)
    petit = ParagraphStyle("p", parent=base, fontSize=7, leading=9, textColor=colors.grey)
    droite = ParagraphStyle("d", parent=base, alignment=2)
    gras_droite = ParagraphStyle("gd", parent=droite, fontName="Helvetica-Bold")
    titre = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=18)
    section = ParagraphStyle("s", parent=base, fontName="Helvetica-Bold", fontSize=10, leading=13, spaceBefore=8, spaceAfter=3)

    def cases() -> Table:
        """Deux cases vides à cocher, dessinées : aucune police standard ne porte le carré vide."""
        t = Table([["", Paragraph("Appliquer", base), "", Paragraph("Geler", base)]], colWidths=[3 * mm, 15 * mm, 3 * mm, 12 * mm],
                  rowHeights=[3 * mm])
        t.setStyle(TableStyle([("BOX", (0, 0), (0, 0), 0.6, colors.black), ("BOX", (2, 0), (2, 0), 0.6, colors.black),
                               ("LEFTPADDING", (0, 0), (-1, -1), 1.5), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        t.hAlign = "LEFT"
        return t

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
    el = []
    if fictif:
        el.append(Paragraph("DOCUMENT DE DÉMONSTRATION – VALEURS FICTIVES",
                            ParagraphStyle("f", parent=base, textColor=colors.red, fontName="Helvetica-Bold", alignment=1)))
    el += [Paragraph(f"Situation des loyers au {aujourdhui:%d/%m/%Y}", titre),
           Paragraph(f"<b>{societe['nom']}</b>" + (f" · SIREN {societe['siren']}" if societe.get("siren") else "")
                     + " · loyers annuels hors taxes et hors charges", base)]

    # 1. nouveaux loyers connus : la décision appartient au bailleur
    el.append(Paragraph(f"1. Nouveaux loyers connus : {len(g['connus'])} révision(s) à décider ou à notifier", section))
    if g["connus"]:
        corps = [en(["Bail", "Locataire et local", "Révision au", "Indice", "Loyer en cours", "Nouveau loyer", "Variation",
                     "Écart annuel", "Décision du bailleur"])]
        total_avant = total_apres = 0.0
        for l in g["connus"]:
            if l["gel"]:
                decision = "<b>Gel décidé</b>" + (f"<br/><font color='grey'>{l['commentaire']}</font>" if l["commentaire"] else "")
            elif l["decision_saisie"]:
                decision = "<b>Application décidée</b>"
            else:
                decision = None                          # à décider : deux cases à cocher
            if decision is not None and l["courrier_le"]:
                decision += f"<br/><font color='grey'>courrier du {date.fromisoformat(l['courrier_le']):%d/%m/%Y}</font>"
            variation = l["variation"] if l["variation"] is not None else (
                l["apres"] / l["avant"] - 1 if l["apres"] is not None and l["avant"] else None)
            indice = l["indice"] + (f" : {_nb(l['valeur'])}<br/><font color='grey'>contre {l['precedent']} : {_nb(l['valeur_precedente'])}</font>"
                                   if l.get("valeur") is not None else "")
            ecart = None if l["apres"] is None or l["avant"] is None else l["apres"] - l["avant"]
            corps.append([*bail(l["bail"]), Paragraph(f"{l['date']:%d/%m/%Y}", base), Paragraph(indice, base),
                          Paragraph(_eur(l["avant"]), droite), Paragraph(_eur(l["apres"]), gras_droite), Paragraph(pct(variation), droite),
                          Paragraph(_eur(ecart) if ecart is not None else "—", droite),
                          Paragraph(decision, base) if decision is not None else cases()])
            total_avant += l["avant"] or 0
            total_apres += (l["apres"] if l["apres"] is not None else l["avant"]) or 0
        corps.append([Paragraph("<b>Total</b>", base), "", "", "", Paragraph(_eur(total_avant), gras_droite),
                      Paragraph(_eur(total_apres), gras_droite), Paragraph(pct(total_apres / total_avant - 1) if total_avant else "—", droite),
                      Paragraph(_eur(total_apres - total_avant), gras_droite), Paragraph("si toutes les révisions sont appliquées", petit)])
        t = Table(corps, colWidths=[13 * mm, 62 * mm, 20 * mm, 40 * mm, 26 * mm, 26 * mm, 17 * mm, 24 * mm, 41 * mm], repeatRows=1)
        t.setStyle(style)
        t.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black)]))
        el.append(t)
    else:
        el.append(Paragraph("Aucune révision à décider à ce jour.", base))

    # 2. en attente de l'indice
    el.append(Paragraph(f"2. Révisions en attente de la publication de l'indice : {len(g['attente'])}", section))
    if g["attente"]:
        corps = [en(["Bail", "Locataire et local", "Révision au", "Indice attendu", "Loyer en cours", "Situation"])]
        for l in g["attente"]:
            situation = ("<b>Date dépassée.</b> Le loyer en cours reste facturé à titre provisoire ; un rappel sera dû à la publication."
                         if l["etat"] == "retard" else "Nouveau loyer calculé dès la publication de l'indice.")
            corps.append([*bail(l["bail"]), Paragraph(f"{l['date']:%d/%m/%Y}", base), Paragraph(l["indice"], base),
                          Paragraph(_eur(l["avant"]), droite), Paragraph(situation, base)])
        t = Table(corps, colWidths=[13 * mm, 70 * mm, 22 * mm, 34 * mm, 28 * mm, 102 * mm], repeatRows=1)
        t.setStyle(style)
        el.append(t)
    else:
        el.append(Paragraph("Aucune.", base))

    # 3. le reste : révisions déjà traitées, baux sans révision à venir
    reste = [(l["bail"], f"Révision du {l['date']:%d/%m/%Y} prête : " + ("gel notifié" if l["gel"] else f"nouveau loyer {_eur(l['apres'])}")
              + (f", courrier du {date.fromisoformat(l['courrier_le']):%d/%m/%Y}" if l["courrier_le"] else ""), l["avant"]) for l in g["traites"]]
    reste += [(b, "Aucune révision à venir connue", b.get("loyer_attendu_annuel")) for b in autres]
    if reste:
        el.append(Paragraph(f"3. Autres baux : {len(reste)}", section))
        corps = [en(["Bail", "Locataire et local", "Loyer en cours", "Situation"])]
        for b, texte, loyer in reste:
            corps.append([*bail(b), Paragraph(_eur(loyer), droite), Paragraph(texte, base)])
        t = Table(corps, colWidths=[13 * mm, 80 * mm, 30 * mm, 146 * mm], repeatRows=1)
        t.setStyle(style)
        el.append(t)

    el += [Spacer(1, 5 * mm),
           Paragraph("Indices publiés par l'INSEE. Document d'information établi d'après les paramètres de chaque bail ; "
                     "seules les stipulations du bail font foi.", petit)]
    if any(not l["gel"] and not l["decision_saisie"] for l in g["connus"]):
        el += [Spacer(1, 5 * mm), Paragraph("Décisions du bailleur ci-dessus arrêtées le ____ / ____ / ________"
                                            "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Signature :", base)]

    def pied(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(14 * mm, 9 * mm, f"{societe['nom']} – situation des loyers au {aujourdhui:%d/%m/%Y}")
        canvas.drawRightString(largeur - 14 * mm, 9 * mm, f"Page {d.page}")
        canvas.restoreState()
    doc.build(el, onFirstPage=pied, onLaterPages=pied)
    return tampon.getvalue()

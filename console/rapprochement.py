"""Rapprochement entre le loyer attendu (baux de la console) et la facturation Pennylane.

Fonctions pures sur des dictionnaires : aucune dépendance au web ni au réseau, donc testables.
Chaque écart porte une gravité : « bloquant » (de l'argent n'est pas facturé ou l'est à tort),
« a_traiter » (à régulariser), « info » (à connaître).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

ECHEANCES_PAR_AN = {"Mensuelle": 12, "Trimestrielle": 4, "Semestrielle": 2, "Annuelle": 1}
INTERVALLE_MOIS = {"Mensuelle": 1, "Trimestrielle": 3, "Semestrielle": 6, "Annuelle": 12}
UNITE = {"Mensuelle": "mois", "Trimestrielle": "trimestre", "Semestrielle": "semestre", "Annuelle": "an"}
ADJECTIF = {"Mensuelle": "mensuel", "Trimestrielle": "trimestriel", "Semestrielle": "semestriel", "Annuelle": "annuel"}
STATUTS_ACTIFS = ("in_progress", "not_started", "pending")
GRAVITES = ("bloquant", "a_traiter", "info")
LIBELLE_GRAVITE = {"bloquant": "Bloquant", "a_traiter": "À traiter", "info": "Information"}
TOLERANCE_ANNUELLE = 1.0          # euro : absorbe les arrondis d'échéance
DELAI_RENOUVELLEMENT = 45         # jours avant le terme d'un abonnement
DELAI_BROUILLON = 7               # jours au-delà desquels une facture en brouillon est signalée
# la liste des factures clients de Pennylane contient aussi ces documents, qui ne sont pas des factures dues
# taux de TVA admis sur un bail et code Pennylane correspondant (énumération vat_rate de l'API) ;
# 8,5 % : taux normal des départements d'outre-mer
CODES_TVA = {20.0: "FR_200", 10.0: "FR_100", 8.5: "FR_85", 5.5: "FR_55", 2.1: "FR_21", 0.0: "exempt"}
STATUTS_HORS_FACTURE = ("cancelled", "credit_note", "proforma", "shipping_order", "purchasing_order",
                        "estimate_pending", "estimate_accepted", "estimate_invoiced", "estimate_denied")


@dataclass
class Ecart:
    type: str
    gravite: str
    dossier: str
    titre: str
    detail: str = ""
    action: str = ""
    montant: float = 0.0            # enjeu en euros (annualisé pour un écart de loyer)
    bail_id: int | None = None
    bail_code: str = ""
    locataire: str = ""
    customer_id: int | None = None  # client Pennylane en cause, pour un abonnement sans bail
    abonnement_id: int | None = None

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class SituationBail:
    bail: dict
    courant: dict | None = None             # abonnement en cours
    suivant: dict | None = None             # abonnement déjà créé pour la période suivante
    dernier_termine: dict | None = None
    attendu_annuel: float = 0.0             # loyer + charges, HT
    facture_annuel: float = 0.0             # abonnement en cours, annualisé
    factures: list[dict] = field(default_factory=list)
    ecarts: list[Ecart] = field(default_factory=list)
    proposition: dict | None = None
    rappel: dict | None = None              # loyer dû et non facturé depuis la dernière révision

    @property
    def gravite(self) -> str:
        for g in GRAVITES:
            if any(e.gravite == g for e in self.ecarts):
                return g
        return "ok"


def _d(v) -> date | None:
    if v in (None, ""):
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _fr(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def _eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def occurrences_par_an(abo: dict) -> float:
    intervalle = max(int(abo.get("intervalle") or 1), 1)
    return (1 if abo.get("type_regle") == "yearly" else 52 if abo.get("type_regle") == "weekly" else 12) / intervalle


def ajouter_mois(d: date, n: int) -> date:
    m = d.month - 1 + n
    annee, mois = d.year + m // 12, m % 12 + 1
    jour = min(d.day, [31, 29 if annee % 4 == 0 and (annee % 100 != 0 or annee % 400 == 0) else 28,
                       31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mois - 1])
    return date(annee, mois, jour)


# ---------------------------------------------------------------------------
# Rattachement des abonnements aux baux
# ---------------------------------------------------------------------------
def rattacher(baux: list[dict], abonnements: list[dict]) -> tuple[dict[int, list[dict]], list[dict]]:
    """Retourne ({id du bail: abonnements}, abonnements orphelins) pour UN dossier.

    Ordre : identifiant d'abonnement inscrit sur le bail, puis client Pennylane ; si un même
    client porte plusieurs baux, l'abonnement va au bail dont le loyer par échéance est le plus proche."""
    par_bail: dict[int, list[dict]] = {b["id"]: [] for b in baux}
    restants = []
    explicites = {b["subscription_id"]: b["id"] for b in baux if b.get("subscription_id")}
    for a in abonnements:
        if a["id"] in explicites:
            par_bail[explicites[a["id"]]].append(a)
        else:
            restants.append(a)
    par_client: dict[int, list[dict]] = {}
    for b in baux:
        if b.get("customer_id") is not None:
            par_client.setdefault(b["customer_id"], []).append(b)
    orphelins = []
    for a in restants:
        candidats = par_client.get(a.get("customer_id"), [])
        if not candidats:
            orphelins.append(a)
            continue
        cible = min(candidats, key=lambda b: abs(_attendu_echeance(b) - (a.get("montant_ht") or 0)))
        par_bail[cible["id"]].append(a)
    return par_bail, orphelins


def _attendu_annuel(b: dict) -> float:
    return round((b.get("loyer_attendu_annuel") or 0) + (b.get("charges_annuelles") or 0), 2)


def _attendu_echeance(b: dict) -> float:
    return round(_attendu_annuel(b) / ECHEANCES_PAR_AN.get(b.get("periodicite"), 12), 2)


# ---------------------------------------------------------------------------
# Analyse d'un bail
# ---------------------------------------------------------------------------
def analyser_bail(bail: dict, abonnements: list[dict], factures: list[dict], aujourdhui: date,
                  reglages: dict | None = None) -> SituationBail:
    s = SituationBail(bail=bail, attendu_annuel=_attendu_annuel(bail), factures=factures)
    e = lambda type_, gravite, titre, **kw: s.ecarts.append(Ecart(
        type=type_, gravite=gravite, dossier=bail["dossier"], titre=titre, bail_id=bail["id"],
        bail_code=bail["code"], locataire=bail.get("locataire", ""), **kw))

    actifs = sorted((a for a in abonnements if a.get("statut") in STATUTS_ACTIFS), key=lambda a: a.get("debut") or "")
    en_cours = [a for a in actifs if (_d(a.get("debut")) or aujourdhui) <= aujourdhui]
    a_venir = [a for a in actifs if (_d(a.get("debut")) or aujourdhui) > aujourdhui]
    termines = sorted((a for a in abonnements if a.get("statut") in ("finished", "stopped")), key=lambda a: a.get("fin") or "")
    brouillons = [a for a in abonnements if a.get("statut") == "draft"]
    s.courant = en_cours[-1] if en_cours else None
    s.suivant = a_venir[0] if a_venir else None
    s.dernier_termine = termines[-1] if termines else None
    prochaine = _d(bail.get("date_prochaine_revision"))
    derniere = _d(bail.get("date_derniere_revision"))
    n = ECHEANCES_PAR_AN.get(bail.get("periodicite"), 12)

    if len(en_cours) > 1:
        e("DOUBLON", "bloquant", "Plusieurs abonnements en cours pour ce bail",
          detail="Abonnements " + ", ".join(f"n° {a['id']} ({_eur(a['montant_ht'])})" for a in en_cours),
          action="Arrêter l'abonnement en trop dans Pennylane",
          montant=sum(a["montant_ht"] * occurrences_par_an(a) for a in en_cours[:-1]))

    if s.courant is None:
        if s.suivant is not None:
            e("DEMARRAGE", "info", f"Abonnement n° {s.suivant['id']} programmé au {_fr(_d(s.suivant.get('debut')))}")
        elif s.dernier_termine is not None:
            e("TERME_ECHU", "bloquant",
              f"Abonnement arrivé à terme le {_fr(_d(s.dernier_termine.get('fin')))}, aucun abonnement en cours",
              detail="Le loyer n'est plus facturé depuis cette date.",
              action="Créer l'abonnement de la nouvelle période au loyer révisé", montant=s.attendu_annuel)
        elif brouillons:
            e("ABONNEMENT_BROUILLON", "a_traiter", f"Abonnement n° {brouillons[0]['id']} resté en brouillon",
              action="Activer l'abonnement dans Pennylane", montant=s.attendu_annuel)
        else:
            e("SANS_ABONNEMENT", "bloquant", "Aucun abonnement Pennylane pour ce bail",
              action="Créer l'abonnement", montant=s.attendu_annuel)
    else:
        c = s.courant
        debut, fin = _d(c.get("debut")), _d(c.get("fin"))
        s.facture_annuel = round(c["montant_ht"] * occurrences_par_an(c), 2)
        ecart = round(s.attendu_annuel - s.facture_annuel, 2)
        if abs(occurrences_par_an(c) - n) > 0.01:
            e("PERIODICITE", "a_traiter", "Périodicité de l'abonnement différente de celle du bail",
              detail=f"Bail : {bail.get('periodicite', '').lower()} ; abonnement : {occurrences_par_an(c):g} échéance(s) par an.")
        if abs(ecart) > TOLERANCE_ANNUELLE:
            sens = "sous-facturation" if ecart > 0 else "sur-facturation"
            ancien = bail.get("loyer_precedent_annuel")     # l'abonnement facture-t-il encore le loyer d'avant révision ?
            a_l_ancien_loyer = ancien is not None and abs(
                ancien + (bail.get("charges_annuelles") or 0) - s.facture_annuel) <= TOLERANCE_ANNUELLE
            if derniere and debut and derniere > debut or (bail.get("revisions_non_appliquees") or 0) > 0 or (
                    derniere and a_l_ancien_loyer):
                s.rappel = calculer_rappel(bail, c, derniere, aujourdhui)
                e("REVISION_NON_REPERCUTEE", "bloquant",
                  f"Révision du {_fr(derniere)} non répercutée dans l'abonnement",
                  detail=f"Attendu {_eur(_attendu_echeance(bail))} par échéance, facturé {_eur(c['montant_ht'])} : "
                         f"{sens} de {_eur(abs(ecart))} par an."
                         + (f" Rappel à facturer : {_eur(s.rappel['montant'])} HT pour {s.rappel['echeances']} échéance(s)."
                            if s.rappel and s.rappel["echeances"] else ""),
                  action="Clore l'abonnement en cours et créer celui de la période au loyer révisé", montant=ecart)
            else:
                e("MONTANT", "a_traiter", "Montant de l'abonnement différent du loyer attendu",
                  detail=f"Attendu {_eur(_attendu_echeance(bail))} par échéance, facturé {_eur(c['montant_ht'])} : "
                         f"{sens} de {_eur(abs(ecart))} par an.",
                  action="Vérifier les paramètres du bail ou corriger l'abonnement", montant=ecart)
        deja = any(x.type == "REVISION_NON_REPERCUTEE" for x in s.ecarts)     # l'action couvre déjà le bornage
        if prochaine and (fin is None or fin >= prochaine) and not deja:
            e("NON_BORNE", "a_traiter", "Abonnement non borné à la date anniversaire",
              detail=f"Prochaine révision le {_fr(prochaine)} ; l'abonnement "
                     + ("n'a pas de terme" if fin is None else f"court jusqu'au {_fr(fin)}")
                     + " : l'ancien loyer continuerait d'être facturé après la révision.",
              action="Borner l'abonnement, ou le remplacer à la date anniversaire")
        elif fin and s.suivant is None and (
                ajouter_mois(fin, _pas_mois(c)) - aujourdhui <= timedelta(days=DELAI_RENOUVELLEMENT)):
            e("RENOUVELLEMENT", "a_traiter",
              f"Dernière occurrence le {_fr(fin)} : préparer la période du {_fr(ajouter_mois(fin, _pas_mois(c)))}",
              detail=("Loyer révisé connu : " + _eur(bail["loyer_prochain_annuel"]) + " par an."
                      if bail.get("loyer_prochain_annuel") is not None else
                      "Loyer révisé non encore connu : l'indice de la révision n'est pas publié."),
              action="Créer l'abonnement suivant au loyer révisé", montant=s.attendu_annuel)
        precedente = _d(c.get("occurrence_precedente"))
        if precedente and not any((_d(f.get("date")) or date.min) >= precedente and f.get("statut") not in STATUTS_HORS_FACTURE
                                  for f in factures):
            e("ECHEANCE_MANQUANTE", "bloquant", f"Échéance du {_fr(precedente)} non facturée",
              detail="L'abonnement a une occurrence passée sans facture correspondante.",
              action="Contrôler la génération de la facture dans Pennylane", montant=c["montant_ht"])

    statut = bail.get("statut_revision") or ""
    if statut.startswith("⚠"):
        e("INDICE_ATTENDU", "info", "Révision échue, indice non encore publié",
          detail="Le loyer en vigueur reste provisoire ; un rappel sera dû à la publication.")
    elif statut.startswith("❄"):
        e("GEL", "info", "Dernière révision gelée par décision du bailleur")
    fin_bail = _d(bail.get("date_fin"))
    if fin_bail and fin_bail <= aujourdhui + timedelta(days=180):
        e("FIN_BAIL", "info", f"Terme contractuel du bail le {_fr(fin_bail)}",
          detail="Renouvellement, congé ou tacite prolongation à instruire.")

    for f in factures:
        d_f, ech = _d(f.get("date")), _d(f.get("echeance"))
        if f.get("statut") == "draft" and d_f and aujourdhui - d_f > timedelta(days=DELAI_BROUILLON):
            e("FACTURE_BROUILLON", "a_traiter", f"Facture du {_fr(d_f)} restée en brouillon",
              detail=f"{_eur(f['montant_ht'])} HT, en attente de validation depuis {(aujourdhui - d_f).days} jours.",
              action="Valider la facture dans Pennylane", montant=f["montant_ht"])
        elif (f.get("restant_du") or 0) > 0.005 and ech and ech < aujourdhui and f.get("statut") not in ("draft", *STATUTS_HORS_FACTURE):
            retard = (aujourdhui - ech).days
            e("IMPAYE", "bloquant" if retard > 60 else "a_traiter",
              f"Facture {f.get('numero') or f['id']} impayée depuis {retard} jours",
              detail=f"Échéance du {_fr(ech)}, reste dû {_eur(f['restant_du'])} TTC.",
              action="Relancer le locataire", montant=f["restant_du"])

    if any(x.type in ("TERME_ECHU", "SANS_ABONNEMENT", "RENOUVELLEMENT", "REVISION_NON_REPERCUTEE") for x in s.ecarts):
        s.proposition = proposer_abonnement(bail, s.courant, aujourdhui, reglages)
    return s


def _pas_mois(abo: dict) -> int:
    intervalle = max(int(abo.get("intervalle") or 1), 1)
    return 12 * intervalle if abo.get("type_regle") == "yearly" else intervalle


def calculer_rappel(bail: dict, courant: dict, derniere: date | None, aujourdhui: date) -> dict | None:
    """Loyer dû depuis la dernière révision et facturé à l'ancien prix : nombre d'occurrences
    de l'abonnement entre la date de révision et aujourd'hui, fois l'écart par échéance."""
    debut = _d(courant.get("debut"))
    if not derniere or not debut:
        return None
    pas, d, echeances = _pas_mois(courant), debut, 0
    while d <= aujourdhui:
        if d >= derniere:
            echeances += 1
        d = ajouter_mois(d, pas)
    ecart = round(_attendu_echeance(bail) - (courant.get("montant_ht") or 0), 2)
    return {"echeances": echeances, "ecart_par_echeance": ecart, "montant": round(echeances * ecart, 2),
            "depuis": derniere.isoformat()}


# ---------------------------------------------------------------------------
# Proposition d'abonnement : une période d'indexation, bornée par count
# ---------------------------------------------------------------------------
def proposer_abonnement(bail: dict, courant: dict | None, aujourdhui: date, reglages: dict | None = None) -> dict:
    """Corps de POST /billing_subscriptions pour la période allant jusqu'à la prochaine révision."""
    reglages = reglages or {}
    periodicite = bail.get("periodicite") or "Mensuelle"
    pas = INTERVALLE_MOIS.get(periodicite, 1)
    fin_courant = _d(courant.get("fin")) if courant else None
    if fin_courant and fin_courant >= aujourdhui:
        debut = ajouter_mois(fin_courant, pas)
    else:
        debut = ajouter_mois(date(aujourdhui.year, aujourdhui.month, 1), 1)
        effet = _d(bail.get("date_effet"))
        while pas > 1 and effet and (debut.year * 12 + debut.month - effet.year * 12 - effet.month) % pas:
            debut = ajouter_mois(debut, 1)               # calé sur les termes du bail : pas d'échéance à cheval sur la révision
    prochaine = _d(bail.get("date_prochaine_revision"))
    loyer, revise_le, loyer_connu, periode_revisee = bail.get("loyer_attendu_annuel") or 0, _d(bail.get("date_derniere_revision")), True, False
    count = None
    if prochaine and prochaine <= debut:
        # la période proposée commence à la date anniversaire ou après : c'est le loyer RÉVISÉ qui se facture.
        # Il n'est connu que si l'indice de cette révision est publié.
        periode_revisee, revise_le = True, prochaine
        loyer_connu = bail.get("loyer_prochain_annuel") is not None
        loyer = bail.get("loyer_prochain_annuel") if loyer_connu else loyer
        prochaine = _d(bail.get("date_revision_suivante")) or ajouter_mois(prochaine, 12)
        if prochaine <= debut:                       # référentiel en retard de plus d'une période
            loyer_connu, prochaine = False, ajouter_mois(debut, 12)
    if prochaine:
        count, d = 0, debut
        while d < prochaine:
            count, d = count + 1, ajouter_mois(d, pas)
    n = ECHEANCES_PAR_AN.get(periodicite, 12)
    taux = bail.get("tva_pct")
    tva = CODES_TVA.get(float(taux) if taux is not None else 20.0)     # None : taux inconnu, jamais « exonéré » par défaut
    unite = UNITE.get(periodicite, "mois")
    regle: dict = ({"type": "yearly", "interval": 1} if periodicite == "Annuelle"
                   else {"type": "monthly", "interval": pas, "day_of_month": debut.day})
    if count:
        regle["count"] = count
    lignes = [{"label": f"Loyer {ADJECTIF.get(periodicite, '')} – {bail.get('local', '')}", "quantity": 1, "unit": unite,
               "raw_currency_unit_price": f"{loyer / n:.2f}", "vat_rate": tva}]
    if (bail.get("charges_annuelles") or 0) > 0:
        lignes.append({"label": "Provision sur charges", "quantity": 1, "unit": unite,
                       "raw_currency_unit_price": f"{bail['charges_annuelles'] / n:.2f}", "vat_rate": tva})
    corps = {
        "customer_id": bail.get("customer_id"),
        "label": f"Loyer {bail.get('local', '')}",
        "start": debut.isoformat(),
        "mode": {"type": reglages.get("mode", "awaiting_validation")},
        "payment_conditions": reglages.get("payment_conditions", "upon_receipt"),
        "payment_method": reglages.get("payment_method", "offline"),
        "recurring_rule": regle,
        "customer_invoice_data": {
            "currency": "EUR", "language": "fr_FR", "pdf_invoice_subject": f"Loyer {bail.get('local', '')}",
            "special_mention": (f"Loyer indexé sur l'{bail.get('indice', '')}"
                                + (f", révisé le {_fr(revise_le)}" if revise_le and loyer_connu else "") + "."),
            "invoice_lines": lignes,
        },
    }
    dernier = ajouter_mois(debut, pas * (count - 1)) if count else None
    return {"corps": corps, "debut": debut.isoformat(), "occurrences": count,
            "derniere_occurrence": dernier.isoformat() if dernier else None,
            "envoyable": bail.get("customer_id") is not None and tva is not None, "tva_connue": tva is not None,
            "loyer_connu": loyer_connu, "periode_revisee": periode_revisee, "loyer_annuel": loyer,
            "note": ("Loyer révisé inconnu : l'indice de la révision du " + _fr(revise_le) + " n'est pas publié. "
                     "Le montant ci-dessous est l'ancien loyer") if not loyer_connu else
                    ("Période au loyer révisé le " + _fr(revise_le) + ", bornée à la révision suivante du " + _fr(prochaine))
                    if periode_revisee else
                    ("Borné à la prochaine révision du " + _fr(prochaine)) if count else
                    "Prochaine révision inconnue : abonnement non borné, à surveiller"}


# ---------------------------------------------------------------------------
# Facture de rappel : le loyer dû depuis la révision et facturé à l'ancien prix
# ---------------------------------------------------------------------------
def proposer_facture_rappel(bail: dict, courant: dict | None, aujourdhui: date) -> dict | None:
    """Corps de POST /customer_invoices, en brouillon, pour le rappel de loyer d'une révision non répercutée.

    Le rappel couvre toutes les échéances de l'abonnement en cours depuis la date de révision jusqu'à son
    terme : il n'est complet que si cet abonnement est borné. None s'il n'y a ni abonnement ni révision."""
    derniere, debut = _d(bail.get("date_derniere_revision")), _d(courant.get("debut")) if courant else None
    if courant is None or derniere is None or debut is None:
        return None
    fin = _d(courant.get("fin"))
    pas, d, dates = _pas_mois(courant), debut, []
    while d <= (fin or aujourdhui):
        if d >= derniere:
            dates.append(d)
        d = ajouter_mois(d, pas)
    ecart = round(_attendu_echeance(bail) - (courant.get("montant_ht") or 0), 2)
    periodicite = bail.get("periodicite") or "Mensuelle"
    taux = bail.get("tva_pct")
    tva = CODES_TVA.get(float(taux) if taux is not None else 20.0)
    reference = f"console-{bail['dossier']}-{bail['code']}-rappel-{derniere.isoformat()}"
    periode = (f"échéances du {_fr(dates[0])} au {_fr(dates[-1])}" if len(dates) > 1 else f"échéance du {_fr(dates[0])}") if dates else ""
    corps = {
        "date": aujourdhui.isoformat(), "deadline": aujourdhui.isoformat(), "customer_id": bail.get("customer_id"),
        "draft": True, "currency": "EUR", "language": "fr_FR", "external_reference": reference,
        "pdf_invoice_subject": f"Rappel de loyer – {bail.get('local', '')}",
        "special_mention": (f"Rappel de loyer consécutif à la révision du {_fr(derniere)}, indexée sur l'{bail.get('indice', '')} : "
                            f"{periode}, soit {len(dates)} échéance(s) à {_eur(ecart)} hors taxes."),
        "invoice_lines": [{"label": f"Rappel de loyer, révision du {_fr(derniere)} – {periode}", "quantity": len(dates),
                           "unit": UNITE.get(periodicite, "mois"), "raw_currency_unit_price": f"{ecart:.2f}", "vat_rate": tva}],
    }
    return {"corps": corps, "reference": reference, "date_revision": derniere.isoformat(), "echeances": len(dates),
            "du": dates[0].isoformat() if dates else None, "au": dates[-1].isoformat() if dates else None,
            "ecart_par_echeance": ecart, "montant_ht": round(len(dates) * ecart, 2), "borne": fin is not None,
            "tva_connue": tva is not None, "debut": f"rappel-{derniere.isoformat()}"}


# ---------------------------------------------------------------------------
# Analyse d'un dossier et synthèses
# ---------------------------------------------------------------------------
def analyser_dossier(dossier: dict, baux: list[dict], abonnements: list[dict], factures: list[dict],
                     aujourdhui: date, reglages: dict | None = None) -> dict:
    par_bail, orphelins = rattacher(baux, abonnements)
    fact_client: dict = {}
    for f in factures:
        fact_client.setdefault(f.get("customer_id"), []).append(f)
    situations = []
    for b in baux:
        ids = {a["id"] for a in par_bail[b["id"]]}
        clients_partages = sum(1 for x in baux if x.get("customer_id") == b.get("customer_id")) > 1
        fs = [f for f in fact_client.get(b.get("customer_id"), [])
              if not clients_partages or f.get("subscription_id") in ids or f.get("subscription_id") is None]
        situations.append(analyser_bail(b, par_bail[b["id"]], sorted(fs, key=lambda f: f.get("date") or "", reverse=True),
                                        aujourdhui, reglages))
    ecarts = [e for s in situations for e in s.ecarts]
    for a in orphelins:
        if a.get("statut") in STATUTS_ACTIFS:
            ecarts.append(Ecart(type="ORPHELIN", gravite="a_traiter", dossier=dossier["code"],
                                titre=f"Abonnement n° {a['id']} sans bail dans la console",
                                detail=f"« {a.get('label', '')} », {_eur(a['montant_ht'])} par occurrence"
                                       + (f", client Pennylane n° {a['customer_id']}." if a.get("customer_id") else "."),
                                customer_id=a.get("customer_id"), abonnement_id=a["id"],
                                action="Créer le bail dans la console, ou arrêter l'abonnement",
                                montant=round(a["montant_ht"] * occurrences_par_an(a), 2)))
    attendu = round(sum(s.attendu_annuel for s in situations), 2)
    facture = round(sum(s.facture_annuel for s in situations), 2)
    ordre = {g: i for i, g in enumerate(GRAVITES)}
    ecarts.sort(key=lambda x: (ordre[x.gravite], -abs(x.montant)))
    return {
        "dossier": dossier, "situations": situations, "ecarts": ecarts, "orphelins": orphelins,
        "attendu_annuel": attendu, "facture_annuel": facture, "ecart_annuel": round(attendu - facture, 2),
        "nb_baux": len(baux), "nb_ok": sum(1 for s in situations if s.gravite in ("ok", "info")),
        "compte": {g: sum(1 for e in ecarts if e.gravite == g) for g in GRAVITES},
        "impayes": round(sum(e.montant for e in ecarts if e.type == "IMPAYE"), 2),
        "brouillons": sum(1 for e in ecarts if e.type == "FACTURE_BROUILLON"),
    }


def echeancier(analyses: list[dict], aujourdhui: date, horizon_jours: int = 120) -> list[dict]:
    """Révisions et termes d'abonnement à venir, triés par date."""
    limite = aujourdhui + timedelta(days=horizon_jours)
    lignes = []
    for a in analyses:
        for s in a["situations"]:
            prochaine = _d(s.bail.get("date_prochaine_revision"))
            fin = _d(s.courant.get("fin")) if s.courant else None
            if prochaine and aujourdhui <= prochaine <= limite:
                lignes.append({"date": prochaine, "nature": "Révision du loyer", "dossier": a["dossier"]["code"],
                               "bail": s.bail, "jours": (prochaine - aujourdhui).days,
                               "pret": s.suivant is not None,
                               "detail": f"Indice {s.bail.get('indice', '')} ; " +
                                         ("abonnement suivant déjà créé" if s.suivant else
                                          ("abonnement en cours borné au " + _fr(fin) if fin and fin < prochaine
                                           else "abonnement en cours non borné"))})
            if fin and aujourdhui <= fin <= limite and not (prochaine and fin < prochaine <= limite):
                lignes.append({"date": fin, "nature": "Terme de l'abonnement", "dossier": a["dossier"]["code"],
                               "bail": s.bail, "jours": (fin - aujourdhui).days, "pret": s.suivant is not None,
                               "detail": "Abonnement suivant " + ("déjà créé" if s.suivant else "à créer")})
    return sorted(lignes, key=lambda l: l["date"])

"""Application web de la console (FastAPI, rendu serveur, aucun outillage JavaScript).

    python -m console demo      charge le portefeuille fictif
    python -m console serve     http://127.0.0.1:8000

Les baux sont saisis et calculés ici. Côté Pennylane, la console lit ; elle ne crée un abonnement que
sur confirmation explicite, verrous levés (console/ecriture.py), et ne modifie ni ne supprime jamais rien.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import urllib.parse
import zipfile
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from indexation_loyer import calcul
from indexation_loyer.baux import DECISIONS, INDICES, METHODES, PERIODICITES_FACTURATION, TYPES_BAIL, est_gel

from . import (__version__, comptes, db, depot, ecriture, editions, import_tableur, jetons, pieces, rapprochement as rp,
               referentiel, simulation)

ICI = Path(__file__).resolve().parent
STATUTS_ABONNEMENT = {"in_progress": "en cours", "not_started": "programmé", "finished": "terminé", "stopped": "arrêté",
                      "draft": "brouillon", "pending": "en attente"}
MODES = {"awaiting_validation": "brouillon à valider", "finalized": "finalisée", "email": "finalisée et envoyée"}
TYPES = {
    "REVISION_NON_REPERCUTEE": "Révision non répercutée", "TERME_ECHU": "Abonnement à terme échu",
    "SANS_ABONNEMENT": "Bail sans abonnement", "ECHEANCE_MANQUANTE": "Échéance non facturée",
    "DOUBLON": "Abonnements en doublon", "IMPAYE": "Impayé", "MONTANT": "Montant à vérifier",
    "NON_BORNE": "Abonnement non borné", "RENOUVELLEMENT": "Période suivante à préparer",
    "FACTURE_BROUILLON": "Facture en brouillon", "ABONNEMENT_BROUILLON": "Abonnement en brouillon",
    "PERIODICITE": "Périodicité différente", "ORPHELIN": "Abonnement sans bail",
    "INDICE_ATTENDU": "Indice attendu", "GEL": "Révision gelée", "FIN_BAIL": "Terme du bail", "DEMARRAGE": "Abonnement programmé",
}


def _eur(x, decimales: int = 0) -> str:
    if x is None or x == "":
        return "—"
    x = round(x, decimales) + 0.0          # + 0.0 : pas de « -0 »
    return f"{x:,.{decimales}f}".replace(",", chr(0x202f)).replace(".", ",") + chr(0xa0) + "€"


def _date_fr(v) -> str:
    d = rp._d(v)
    return d.strftime("%d/%m/%Y") if d else "—"


FICTIF = "portefeuille fictif"
TEMOIN_BASE = "console_session"
LIBRES = ("/connexion",)                     # seules pages servies sans session
RE_VARIABLE_JETON = re.compile(r"^PENNYLANE_TOKEN_[A-Z0-9_]+$")


def creer_app(chemin_db: Path | str = db.CHEMIN_DEFAUT, aujourdhui: date | None = None,
              chemin_indices: Path | None = None, stockage: Path | None = None) -> FastAPI:
    stockage = Path(stockage) if stockage else (
        db.RACINE / "data" / "pieces" if str(chemin_db) == ":memory:" else Path(chemin_db).parent / "pieces")
    app = FastAPI(title="Console des loyers", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=ICI / "static"), name="static")
    gabarits = Jinja2Templates(directory=ICI / "templates")
    gabarits.env.filters.update(eur=_eur, eur2=lambda x: _eur(x, 2), date_fr=_date_fr,
                                pct=lambda x: f"{x * 100:.0f} %".replace(".", ","))
    cx = db.connecter(chemin_db)
    jour = lambda: aujourdhui or date.today()
    ENV = db.environnement(cx)                           # production | test, inscrit dans la base
    TEMOIN = TEMOIN_BASE + ("_test" if ENV == "test" else "")    # deux consoles sur le même poste : deux sessions distinctes

    verrous: dict = {}

    def role_requis(methode: str, chemin: str) -> str:
        if chemin.startswith("/utilisateurs") or (chemin.endswith("/jeton") and methode == "POST"):
            return "administrateur"                      # comptes, et dépôt ou retrait d'un jeton Pennylane
        if methode in ("GET", "HEAD", "OPTIONS") or chemin in ("/compte", "/deconnexion"):
            return "lecture"
        if chemin.endswith(("/abonnement", "/rappel")) or chemin == "/creations":     # création dans Pennylane
            return "validation"
        return "gestion"

    def meme_origine(request: Request) -> bool:
        """Un formulaire doit venir de la console elle-même : en-tête Origin, à défaut Referer, obligatoire."""
        source = request.headers.get("origin") or request.headers.get("referer")
        return bool(source) and urllib.parse.urlsplit(source).netloc == request.headers.get("host")

    @app.middleware("http")
    async def garde(request: Request, suite):
        chemin, lecture = request.url.path, request.method in ("GET", "HEAD", "OPTIONS")
        if chemin.startswith("/static/"):
            return await suite(request)
        if not lecture and not meme_origine(request):
            return PlainTextResponse("Requête refusée : elle ne vient pas de la console.", status_code=403)
        u = comptes.session(cx, request.cookies.get(TEMOIN))
        request.state.utilisateur = u
        if chemin not in LIBRES:
            if u is None:
                if lecture and not chemin.startswith("/api/"):
                    cible = chemin + ("?" + request.url.query if request.url.query else "")
                    return RedirectResponse("/connexion?" + urllib.parse.urlencode({"suite": cible}), status_code=303)
                return PlainTextResponse("Session absente ou expirée : se reconnecter.", status_code=401)
            if u["doit_changer"] and chemin not in ("/compte", "/deconnexion"):
                return RedirectResponse("/compte", status_code=303)
            requis = role_requis(request.method, chemin)
            if not comptes.peut(u["role"], requis):
                return gabarits.TemplateResponse(request, "refus.html", contexte(request, page="", requis=requis), status_code=403)
        jeton_auteur = referentiel.auteur_courant.set(u["identifiant"] if u else "")
        verrou = verrous.setdefault(asyncio.get_running_loop(), asyncio.Lock())
        try:
            async with verrou:                           # une requête à la fois sur la base : pas d'écritures entremêlées
                reponse = await suite(request)
        finally:
            referentiel.auteur_courant.reset(jeton_auteur)
        reponse.headers.setdefault("Cache-Control", "no-store")
        reponse.headers.setdefault("X-Frame-Options", "DENY")
        reponse.headers.setdefault("Referrer-Policy", "same-origin")
        return reponse

    def analyses(code: str | None = None) -> list[dict]:
        return [rp.analyser_dossier(x["dossier"], x["baux"], x["abonnements"], x["factures"], jour(), liens=x["liens"])
                for x in depot.charger(cx, code, jour(), chemin_indices)]

    def table_indices() -> dict:
        return calcul.table_indices(referentiel.lire_indices(chemin_indices))

    def est_simule() -> bool:
        """Vrai tant que la base ne porte que le portefeuille fictif (ou rien) : aucun appel à Pennylane."""
        r = cx.execute("SELECT COUNT(*) AS n, SUM(source_referentiel<>?) AS reels FROM dossiers", (FICTIF,)).fetchone()
        return r["n"] > 0 and not r["reels"]

    def est_fictif(code: str) -> bool:
        r = cx.execute("SELECT source_referentiel FROM dossiers WHERE code=?", (code,)).fetchone()
        return r is not None and r["source_referentiel"] == FICTIF

    def sans_appel(code: str) -> bool:
        """Création d'abonnement simulée, sans aucun appel : dossier fictif, ou base de test quelle que soit la société."""
        return ENV == "test" or est_fictif(code)

    async def formulaire(request: Request) -> dict[str, str]:
        return {k: v for k, v in (await request.form()).items() if isinstance(v, str)}

    def contexte(request: Request, **kw) -> dict:
        derniere = cx.execute("SELECT MAX(horodatage) AS h, MAX(source) AS s FROM synchros").fetchone()
        simule = est_simule()
        return {"request": request, "version": __version__, "aujourdhui": jour(), "TYPES": TYPES,
                "utilisateur": getattr(request.state, "utilisateur", None), "ROLES": comptes.LIBELLES_ROLE,
                "STATUTS": STATUTS_ABONNEMENT, "MODES": MODES, "ecriture_activee": ecriture.ecriture_activee(),
                "GRAVITE": rp.LIBELLE_GRAVITE, "derniere_synchro": derniere["h"], "simule": simule,
                "CONDITIONS": rp.LIBELLE_CONDITIONS, "PAIEMENTS": rp.LIBELLE_PAIEMENT,
                "environnement": ENV, "collectes": collectes(), **kw}

    def collectes() -> dict[str, dict]:
        """Dernière collecte Pennylane de chaque dossier : date, volumes lus, ou message d'échec."""
        raccordes = {r["code"] for r in cx.execute("SELECT code, variable_jeton FROM dossiers") if jetons.source(r["variable_jeton"])}
        return {r["dossier"]: dict(r) for r in cx.execute(
            """SELECT s.* FROM synchros s JOIN (SELECT dossier, MAX(id) AS id FROM synchros GROUP BY dossier) d ON d.id = s.id
               WHERE s.source='pennylane'""") if r["dossier"] in raccordes}        # un dossier sans jeton n'a pas d'échec à montrer

    def synthese(an: list[dict]) -> dict:
        ecarts = [e for a in an for e in a["ecarts"]]
        horizon = rp.echeancier(an, jour(), 60)
        return {
            "attendu": round(sum(a["attendu_annuel"] for a in an), 2),
            "facture": round(sum(a["facture_annuel"] for a in an), 2),
            "ecart": round(sum(a["ecart_annuel"] for a in an), 2),
            "nb_baux": sum(a["nb_baux"] for a in an), "nb_dossiers": len(an),
            "nb_ok": sum(a["nb_ok"] for a in an),
            "compte": {g: sum(a["compte"][g] for a in an) for g in rp.GRAVITES},
            "impayes": round(sum(a["impayes"] for a in an), 2),
            "nb_impayes": sum(1 for e in ecarts if e.type == "IMPAYE"),
            "revisions_60j": sum(1 for l in horizon if l["nature"] == "Révision du loyer"),
            "sous_facturation": round(sum(e.montant for e in ecarts
                                          if e.type in ("REVISION_NON_REPERCUTEE", "TERME_ECHU", "SANS_ABONNEMENT")), 2),
        }

    # -------------------------------------------------------------------- comptes
    def destination(suite: str) -> str:
        return suite if suite.startswith("/") and not suite.startswith("//") and "\\" not in suite else "/"

    def page_connexion(request: Request, suite: str = "/", erreur: str = "", statut: int = 200):
        aucun = cx.execute("SELECT COUNT(*) AS n FROM utilisateurs").fetchone()["n"] == 0
        return gabarits.TemplateResponse(request, "connexion.html", contexte(
            request, page="", suite=destination(suite), erreur=erreur, aucun_compte=aucun), status_code=statut)

    @app.get("/connexion", response_class=HTMLResponse)
    def connexion(request: Request, suite: str = "/"):
        if request.state.utilisateur:
            return RedirectResponse(destination(suite), status_code=303)
        return page_connexion(request, suite)

    @app.post("/connexion")
    async def se_connecter(request: Request):
        champs = await formulaire(request)
        u, erreur = comptes.authentifier(cx, champs.get("identifiant", ""), champs.get("mot_de_passe", ""))
        if u is None:
            return page_connexion(request, champs.get("suite", "/"), erreur, 401)
        reponse = RedirectResponse(destination(champs.get("suite", "/")), status_code=303)
        reponse.set_cookie(TEMOIN, comptes.ouvrir_session(cx, u["id"]), httponly=True, samesite="lax",
                           secure=request.url.scheme == "https", max_age=int(comptes.DUREE_SESSION.total_seconds()), path="/")
        return reponse

    @app.post("/deconnexion")
    def se_deconnecter(request: Request):
        comptes.fermer_session(cx, request.cookies.get(TEMOIN))
        reponse = RedirectResponse("/connexion", status_code=303)
        reponse.delete_cookie(TEMOIN, path="/")
        return reponse

    @app.get("/compte", response_class=HTMLResponse)
    def compte(request: Request, erreur: str = ""):
        return gabarits.TemplateResponse(request, "compte.html", contexte(request, page="compte", erreur=erreur, message=""))

    @app.post("/compte")
    async def compte_mot_de_passe(request: Request):
        u, champs = request.state.utilisateur, await formulaire(request)
        erreur = ""
        if comptes.authentifier(cx, u["identifiant"], champs.get("actuel", ""))[0] is None:
            erreur = "Mot de passe actuel incorrect."
        elif champs.get("nouveau", "") != champs.get("confirmation", ""):
            erreur = "Les deux saisies du nouveau mot de passe diffèrent."
        elif champs.get("nouveau", "") == champs.get("actuel", ""):
            erreur = "Le nouveau mot de passe doit différer de l'actuel."
        else:
            try:
                comptes.changer_mot_de_passe(cx, u["id"], champs.get("nouveau", ""))
            except ValueError as exc:
                erreur = str(exc)
        if erreur:
            return gabarits.TemplateResponse(request, "compte.html", contexte(request, page="compte", erreur=erreur, message=""),
                                             status_code=400)
        referentiel.tracer(cx, "Mot de passe changé", u["identifiant"])
        cx.commit()
        reponse = RedirectResponse("/", status_code=303)             # les autres sessions sont tombées : on en rouvre une
        reponse.set_cookie(TEMOIN, comptes.ouvrir_session(cx, u["id"]), httponly=True, samesite="lax",
                           secure=request.url.scheme == "https", max_age=int(comptes.DUREE_SESSION.total_seconds()), path="/")
        return reponse

    def page_utilisateurs(request: Request, erreur: str = "", message: str = "", statut: int = 200):
        return gabarits.TemplateResponse(request, "utilisateurs.html", contexte(
            request, page="utilisateurs", erreur=erreur, message=message, LONGUEUR_MIN=comptes.LONGUEUR_MIN,
            comptes=[dict(r) for r in cx.execute("SELECT * FROM utilisateurs ORDER BY actif DESC, nom")],
            journal=[dict(r) for r in cx.execute(
                "SELECT * FROM historique WHERE action LIKE 'Compte%' OR action LIKE 'Mot de passe%' ORDER BY id DESC LIMIT 12")]),
            status_code=statut)

    @app.get("/utilisateurs", response_class=HTMLResponse)
    def utilisateurs(request: Request):
        return page_utilisateurs(request)

    @app.post("/utilisateurs")
    async def utilisateur_creer(request: Request):
        c = await formulaire(request)
        try:
            comptes.creer(cx, c.get("identifiant", ""), c.get("nom", ""), c.get("role", "lecture"), c.get("mot_de_passe", ""),
                          doit_changer=True)
        except ValueError as exc:
            return page_utilisateurs(request, erreur=str(exc), statut=400)
        referentiel.tracer(cx, "Compte créé", f"{comptes.normaliser(c.get('identifiant', ''))} · rôle {c.get('role', 'lecture')}")
        cx.commit()
        return page_utilisateurs(request, message="Compte créé. Le mot de passe provisoire devra être changé à la première connexion.")

    @app.post("/utilisateurs/{utilisateur_id}")
    async def utilisateur_modifier(request: Request, utilisateur_id: int):
        c = await formulaire(request)
        cible = cx.execute("SELECT * FROM utilisateurs WHERE id=?", (utilisateur_id,)).fetchone()
        if cible is None:
            raise HTTPException(404, "Compte inconnu")
        role, actif = c.get("role", cible["role"]), 1 if c.get("actif", "1") == "1" else 0
        if role not in comptes.ROLES:
            return page_utilisateurs(request, erreur="Rôle inconnu.", statut=400)
        perd_admin = cible["role"] == "administrateur" and cible["actif"] and (role != "administrateur" or not actif)
        if perd_admin and comptes.nb_administrateurs(cx, sauf=utilisateur_id) == 0:
            return page_utilisateurs(request, erreur="Refusé : il doit rester au moins un administrateur actif.", statut=400)
        faits = []
        if c.get("mot_de_passe"):
            try:
                comptes.changer_mot_de_passe(cx, utilisateur_id, c["mot_de_passe"], doit_changer=True)
            except ValueError as exc:
                return page_utilisateurs(request, erreur=str(exc), statut=400)
            faits.append("mot de passe provisoire réinitialisé")
        if role != cible["role"]:
            faits.append(f"rôle {cible['role']} → {role}")
        if actif != cible["actif"]:
            faits.append("réactivé" if actif else "désactivé")
        cx.execute("UPDATE utilisateurs SET role=?, actif=?, nom=? WHERE id=?",
                   (role, actif, c.get("nom", "").strip() or cible["nom"], utilisateur_id))
        if not actif or role != cible["role"]:
            cx.execute("DELETE FROM sessions WHERE utilisateur_id=?", (utilisateur_id,))    # effet immédiat
        if faits:
            referentiel.tracer(cx, "Compte modifié", f"{cible['identifiant']} · " + ", ".join(faits))
        cx.commit()
        return page_utilisateurs(request, message=f"{cible['identifiant']} : " + (", ".join(faits) or "aucun changement") + ".")

    def pilotage(an: list[dict], horizon: int) -> list[dict]:
        """Réindexations à piloter : révisions échues sans indice, et prochaine révision de chaque bail dans l'horizon.

        Chaque ligne dit où en est la préparation : indice publié ou non, décision de gel, courrier envoyé,
        abonnement de la période suivante déjà créé dans Pennylane."""
        table = table_indices()
        lignes = []
        for a in an:
            for sit in a["situations"]:
                b = sit.bail
                candidates = []
                if b.get("calcule") and b.get("date_effet"):
                    dec = referentiel.decisions_du_bail(cx, b["id"])
                    _, revs = referentiel.reviser(b, table, {n: d["decision"] for n, d in dec.items()}, jour())
                    en_retard = [l for l in revs if l.statut == calcul.STATUT_ATTENDU]
                    prochaine = next((l for l in revs if l.echeance.date_revision > jour()), None)
                    for l in [*en_retard[-1:], *([prochaine] if prochaine else [])]:
                        d = dec.get(l.echeance.numero, {})
                        candidates.append({"date": l.echeance.date_revision, "numero": l.echeance.numero,
                                           "indice": f"{b['indice']} {l.echeance.trimestre_reference}",
                                           "publie": l.indice_reference is not None, "gel": est_gel(l.decision),
                                           "avant": l.loyer_precedent, "apres": l.loyer_retenu, "variation": l.variation_pct,
                                           "courrier_le": d.get("courrier_le"), "commentaire": d.get("commentaire") or "",
                                           "valeur": l.indice_reference, "precedent": l.trimestre_precedent_effectif,
                                           "valeur_precedente": l.indice_precedent, "decision_saisie": bool(d)})
                elif b.get("date_prochaine_revision"):            # bail aux valeurs importées : pas de détail de calcul
                    candidates.append({"date": rp._d(b["date_prochaine_revision"]), "numero": None, "indice": b.get("indice") or "",
                                       "publie": b.get("loyer_prochain_annuel") is not None, "gel": False,
                                       "avant": b.get("loyer_attendu_annuel"), "apres": b.get("loyer_prochain_annuel"),
                                       "variation": None, "courrier_le": None, "commentaire": "", "valeur": None,
                                       "precedent": "", "valeur_precedente": None, "decision_saisie": False})
                for c in candidates:
                    jours = (c["date"] - jour()).days
                    if jours > horizon:
                        continue
                    debut_suivant = rp._d(sit.suivant.get("debut")) if sit.suivant else None
                    abonnement_pret = c["date"] > jour() and debut_suivant is not None and debut_suivant >= c["date"]
                    if not c["publie"] and not c["gel"]:
                        etat = "retard" if jours < 0 else "attente"
                    elif c["date"] <= jour():
                        continue                          # révision échue et chiffrée : c'est l'affaire des écarts
                    elif c["gel"]:
                        etat = "faite" if c["courrier_le"] else "a_faire"
                    else:
                        etat = "faite" if c["courrier_le"] and abonnement_pret else "a_faire"
                    lignes.append({**c, "jours": jours, "bail": b, "dossier": a["dossier"], "abonnement_pret": abonnement_pret, "etat": etat})
        ordre = {"retard": 0, "a_faire": 1, "attente": 2, "faite": 3}
        return sorted(lignes, key=lambda l: (ordre[l["etat"]], l["date"]))

    @app.get("/", response_class=HTMLResponse)
    def tableau_de_bord(request: Request, horizon: int = 90):
        horizon = min(max(horizon, 30), 730)
        an = analyses()
        ordre = {g: i for i, g in enumerate(rp.GRAVITES)}
        pilote = pilotage(an, horizon)
        pilotes = {l["bail"]["id"] for l in pilote}     # leur abonnement suivant se traite dans le tableau des réindexations
        a_corriger = sorted((e for a in an for e in a["ecarts"] if e.gravite != "info" and e.type != "IMPAYE"
                             and not (e.type == "RENOUVELLEMENT" and e.bail_id in pilotes)),
                            key=lambda e: (ordre[e.gravite], -abs(e.montant)))
        impayes = [e for a in an for e in a["ecarts"] if e.type == "IMPAYE"]
        prochaine = {}
        for l in pilote:
            code = l["dossier"]["code"]
            if l["date"] > jour() and (code not in prochaine or l["date"] < prochaine[code]):
                prochaine[code] = l["date"]
        noms = {a["dossier"]["code"]: a["dossier"]["nom"] for a in an}
        return gabarits.TemplateResponse(request, "tableau_de_bord.html", contexte(
            request, page="bord", analyses=an, noms=noms, horizon=horizon, pilote=pilote, prochaine=prochaine,
            a_corriger=a_corriger[:15], nb_corriger=len(a_corriger), impayes=impayes,
            nb={"bloquant": sum(1 for e in a_corriger if e.gravite == "bloquant"),
                "a_traiter": sum(1 for e in a_corriger if e.gravite == "a_traiter"),
                "a_faire": sum(1 for l in pilote if l["etat"] == "a_faire"),
                "attente": sum(1 for l in pilote if l["etat"] == "attente"),
                "retard": sum(1 for l in pilote if l["etat"] == "retard"),
                "baux": sum(a["nb_baux"] for a in an)}))

    FILTRES_BAUX = {"corriger": "À corriger", "regulariser": "À régulariser", "preparer": "Réindexations à préparer",
                    "attente": "En attente d'indice"}

    @app.get("/baux", response_class=HTMLResponse)
    def page_baux(request: Request, filtre: str = "", q: str = "", horizon: int = 90):
        """Tous les baux, regroupés par société. Les tuiles du haut filtrent ; la recherche porte sur la société,
        le code du bail, le locataire et le local."""
        horizon = min(max(horizon, 30), 730)
        filtre = filtre if filtre in FILTRES_BAUX else ""
        an = analyses()
        pilote = pilotage(an, horizon)
        pilotes = {l["bail"]["id"] for l in pilote}
        etats: dict[int, set] = {}
        for a in an:
            for e in a["ecarts"]:
                if e.bail_id is None or e.gravite == "info" or e.type == "IMPAYE" or (e.type == "RENOUVELLEMENT" and e.bail_id in pilotes):
                    continue
                etats.setdefault(e.bail_id, set()).add("corriger" if e.gravite == "bloquant" else "regulariser")
        reindexation = {}
        for l in pilote:
            if l["etat"] == "a_faire":
                etats.setdefault(l["bail"]["id"], set()).add("preparer")
            elif l["etat"] in ("attente", "retard"):
                etats.setdefault(l["bail"]["id"], set()).add("attente")
            reindexation.setdefault(l["bail"]["id"], l)          # la plus urgente : le pilotage est trié
        mot = q.strip().lower()
        societes = []
        for a in an:
            d = a["dossier"]
            lignes = []
            for sit in a["situations"]:
                b = sit.bail
                if filtre and filtre not in etats.get(b["id"], set()):
                    continue
                if mot and not any(mot in str(x or "").lower() for x in (d["nom"], d["code"], b["code"], b.get("locataire"), b.get("local"))):
                    continue
                lignes.append({"sit": sit, "etats": etats.get(b["id"], set()), "reindexation": reindexation.get(b["id"]),
                               "n": rp.ECHEANCES_PAR_AN.get(b.get("periodicite"), 12)})
            if lignes or (not filtre and not mot):
                societes.append({"a": a, "lignes": lignes,
                                 "compte": {f: sum(1 for x in a["situations"] if f in etats.get(x.bail["id"], set())) for f in FILTRES_BAUX}})
        compte = {f: sum(1 for v in etats.values() if f in v) for f in FILTRES_BAUX}
        return gabarits.TemplateResponse(request, "baux.html", contexte(
            request, page="baux", societes=societes, filtre=filtre, q=q, horizon=horizon, compte=compte, FILTRES=FILTRES_BAUX,
            ouvert=bool(filtre or mot), nb_baux=sum(len(x["lignes"]) for x in societes)))

    @app.get("/ecarts", response_class=HTMLResponse)
    def ecarts(request: Request, gravite: str = "", type: str = "", dossier: str = ""):
        an = analyses()
        ordre = {g: i for i, g in enumerate(rp.GRAVITES)}
        tous = sorted((e for a in an for e in a["ecarts"]), key=lambda e: (ordre[e.gravite], -abs(e.montant)))
        filtres = [e for e in tous if (not gravite or e.gravite == gravite) and (not type or e.type == type)
                   and (not dossier or e.dossier == dossier)]
        return gabarits.TemplateResponse(request, "ecarts.html", contexte(
            request, page="ecarts", ecarts=filtres, total=len(tous), f={"gravite": gravite, "type": type, "dossier": dossier},
            types_presents=sorted({e.type for e in tous}, key=lambda t: TYPES.get(t, t)),
            noms={a["dossier"]["code"]: a["dossier"]["nom"] for a in an}))

    @app.get("/echeancier", response_class=HTMLResponse)
    def page_echeancier(request: Request, horizon: int = 120):
        an = analyses()
        return gabarits.TemplateResponse(request, "echeancier.html", contexte(
            request, page="echeancier", lignes=rp.echeancier(an, jour(), horizon), horizon=horizon,
            noms={a["dossier"]["code"]: a["dossier"]["nom"] for a in an}))

    @app.get("/dossiers/{code}", response_class=HTMLResponse)
    def dossier(request: Request, code: str):
        an = analyses(code)
        if not an:
            raise HTTPException(404, "Dossier inconnu")
        return gabarits.TemplateResponse(request, "dossier.html", contexte(request, page="bord", a=an[0], s=synthese(an)))

    # ------------------------------------------------------------------ dossiers
    def page_dossier_form(request: Request, d: dict, erreurs: list[str], creation: bool, statut: int = 200):
        return gabarits.TemplateResponse(request, "dossier_form.html", contexte(
            request, page="bord", d=d, erreurs=erreurs, creation=creation), status_code=statut)

    def valider_dossier(champs: dict[str, str], creation: bool) -> tuple[dict, list[str]]:
        t = lambda nom: (champs.get(nom) or "").strip()
        d = {c: t(c) for c in ("code", "nom", "siren", "forme", "adresse", "signataire", "qualite_signataire",
                               "ville_signature", "variable_jeton")}
        d["code"], d["variable_jeton"] = d["code"].upper(), d["variable_jeton"].upper()
        d["siren"] = d["siren"].replace(" ", "")
        erreurs = []
        if creation and not re.fullmatch(r"[A-Z0-9_]{2,12}", d["code"]):
            erreurs.append("Code du dossier : 2 à 12 lettres majuscules, chiffres ou soulignés.")
        if not d["nom"]:
            erreurs.append("Raison sociale : champ obligatoire.")
        if d["siren"] and not re.fullmatch(r"\d{9}", d["siren"]):
            erreurs.append("SIREN : 9 chiffres.")
        if d["variable_jeton"] and not RE_VARIABLE_JETON.fullmatch(d["variable_jeton"]):
            erreurs.append("Jeton Pennylane : saisir le NOM de la variable d'environnement, de la forme "
                           "PENNYLANE_TOKEN_CODE. Le jeton lui-même ne se saisit jamais dans la console.")
            d["variable_jeton"] = ""                      # ne jamais réafficher ce qui pourrait être un jeton
        if creation and d["code"] and cx.execute("SELECT 1 FROM dossiers WHERE code=?", (d["code"],)).fetchone():
            erreurs.append(f"Code du dossier : {d['code']} existe déjà.")
        return d, erreurs

    @app.get("/nouveau-dossier", response_class=HTMLResponse)
    def dossier_nouveau(request: Request):
        return page_dossier_form(request, {"qualite_signataire": "Gérant"}, [], True)

    @app.post("/dossiers")
    async def dossier_creer(request: Request):
        d, erreurs = valider_dossier(await formulaire(request), True)
        if erreurs:
            return page_dossier_form(request, d, erreurs, True, 400)
        cx.execute("""INSERT INTO dossiers (code, nom, siren, variable_jeton, source_referentiel, forme, adresse, signataire,
                      qualite_signataire, ville_signature) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                   (d["code"], d["nom"], d["siren"], d["variable_jeton"], "console", d["forme"], d["adresse"],
                    d["signataire"], d["qualite_signataire"] or "Gérant", d["ville_signature"]))
        referentiel.tracer(cx, "Dossier créé", d["nom"], d["code"])
        cx.commit()
        return RedirectResponse(f"/dossiers/{d['code']}", status_code=303)

    @app.get("/dossiers/{code}/modifier", response_class=HTMLResponse)
    def dossier_modifier(request: Request, code: str):
        d = cx.execute("SELECT * FROM dossiers WHERE code=?", (code,)).fetchone()
        if d is None:
            raise HTTPException(404, "Dossier inconnu")
        return page_dossier_form(request, dict(d), [], False)

    @app.post("/dossiers/{code}")
    async def dossier_enregistrer(request: Request, code: str):
        avant = cx.execute("SELECT * FROM dossiers WHERE code=?", (code,)).fetchone()
        if avant is None:
            raise HTTPException(404, "Dossier inconnu")
        d, erreurs = valider_dossier({**await formulaire(request), "code": code}, False)
        if erreurs:
            return page_dossier_form(request, {**d, "code": code}, erreurs, False, 400)
        colonnes = ("nom", "siren", "forme", "adresse", "signataire", "qualite_signataire", "ville_signature", "variable_jeton")
        changes = [c for c in colonnes if (avant[c] or "") != d[c]]
        cx.execute(f"UPDATE dossiers SET {', '.join(f'{c}=?' for c in colonnes)} WHERE code=?", [*(d[c] for c in colonnes), code])
        if changes:
            referentiel.tracer(cx, "Dossier modifié", "Champs : " + ", ".join(changes), code)
        cx.commit()
        return RedirectResponse(f"/dossiers/{code}", status_code=303)

    def page_pennylane(request: Request, d: dict, bilan: dict | None = None, erreur: str = "", message: str = "", statut: int = 200,
                       clients: list[dict] | None = None, tronquee: bool = False):
        rattaches: dict = {}
        for r in cx.execute("SELECT id, code, locataire, customer_id FROM baux WHERE dossier=? AND customer_id IS NOT NULL", (d["code"],)):
            rattaches.setdefault(r["customer_id"], []).append(dict(r))
        return gabarits.TemplateResponse(request, "pennylane.html", contexte(
            request, page="bord", d=d, b=bilan, erreur=erreur, message=message, clients=clients, tronquee=tronquee,
            rattaches=rattaches,
            source=jetons.source(d["variable_jeton"]), coffre=jetons.disponible(), nom_coffre=jetons.nom_du_coffre(),
            brut=json.dumps(bilan["identite"], ensure_ascii=False, indent=2) if bilan and bilan["identite"] is not None else None),
            status_code=statut)

    def dossier_reel_ou_409(code: str) -> dict:
        d = dossier_ou_404(code)
        if d["source_referentiel"] == FICTIF:
            raise HTTPException(409, "Dossier fictif : aucune connexion à Pennylane.")
        return d

    def tester(code: str, d: dict) -> dict:
        """Lecture seule : vérifie le jeton du dossier et montre à quelle société Pennylane il donne accès."""
        from .pennylane_api import ClientReel
        bilan = ClientReel({code: d["variable_jeton"]}).tester_connexion(code)
        issue = ("jeton absent" if not bilan["jeton_present"] else
                 f"identité HTTP {bilan['me']['code']}, abonnements HTTP {bilan['abonnements']['code']}"
                 + (", environnement de test reconnu" if bilan["sandbox"] else ""))
        referentiel.tracer(cx, "Connexion Pennylane testée", issue, code)
        cx.commit()
        return bilan

    @app.get("/dossiers/{code}/pennylane", response_class=HTMLResponse)
    def dossier_pennylane(request: Request, code: str):
        return page_pennylane(request, dossier_reel_ou_409(code))

    @app.post("/dossiers/{code}/connexion-pennylane", response_class=HTMLResponse)
    def dossier_tester_connexion(request: Request, code: str):
        d = dossier_reel_ou_409(code)
        return page_pennylane(request, d, tester(code, d))

    @app.post("/dossiers/{code}/clients-pennylane", response_class=HTMLResponse)
    def dossier_clients_pennylane(request: Request, code: str):
        """Lecture seule : les clients du dossier dans Pennylane, pour retrouver l'identifiant à porter sur un bail."""
        from .pennylane_api import ClientReel
        d = dossier_reel_ou_409(code)
        try:
            clients, tronquee = ClientReel({code: d["variable_jeton"]}).clients(code)
        except RuntimeError as exc:
            return page_pennylane(request, d, erreur=f"{exc}. Vérifier que le jeton porte le droit de lecture des clients.", statut=502)
        return page_pennylane(request, d, clients=clients, tronquee=tronquee)

    @app.post("/dossiers/{code}/jeton", response_class=HTMLResponse)
    async def dossier_jeton(request: Request, code: str):
        """Dépose le jeton de lecture dans le coffre du système, ou l'en retire. Il n'entre pas dans la base."""
        d = dossier_reel_ou_409(code)
        champs = await formulaire(request)
        variable = d["variable_jeton"] or f"PENNYLANE_TOKEN_{code}"
        if champs.get("action") == "retirer":
            retire = jetons.retirer(variable)
            if retire:
                referentiel.tracer(cx, "Jeton Pennylane retiré", f"jeton de lecture · {variable}", code)
                cx.commit()
            return page_pennylane(request, d, message="Jeton retiré du coffre." if retire else "Aucun jeton à retirer dans le coffre.")
        try:
            jetons.enregistrer(variable, (champs.get("jeton") or "").strip())
        except (ValueError, jetons.CoffreIndisponible) as exc:
            return page_pennylane(request, d, erreur=str(exc), statut=400)
        if not d["variable_jeton"]:
            cx.execute("UPDATE dossiers SET variable_jeton=? WHERE code=?", (variable, code))
            d = {**d, "variable_jeton": variable}
        referentiel.tracer(cx, "Jeton Pennylane enregistré", f"jeton de lecture · {variable} · {jetons.nom_du_coffre()}", code)
        cx.commit()
        return page_pennylane(request, d, tester(code, d), message="Jeton enregistré. Résultat du test de connexion ci-dessous.")

    # ---------------------------------------------------------------------- baux
    def ligne_bail(bail_id: int) -> dict:
        ligne = cx.execute("SELECT * FROM baux WHERE id=?", (bail_id,)).fetchone()
        if ligne is None:
            raise HTTPException(404, "Bail inconnu")
        return dict(ligne)

    def revisions(b: dict) -> tuple:
        """(bail du moteur, lignes de révision, décisions par numéro) d'un bail calculé par la console."""
        dec = referentiel.decisions_du_bail(cx, b["id"])
        bail_m, lignes = referentiel.reviser(b, table_indices(), {n: d["decision"] for n, d in dec.items()}, jour())
        return bail_m, lignes, dec

    def page_bail_form(request: Request, dossier: dict, b: dict, erreurs: list[str], bail_id: int | None, statut: int = 200,
                       avertissement: str | None = None):
        return gabarits.TemplateResponse(request, "bail_form.html", contexte(
            request, page="bord", dossier=dossier, b=b, erreurs=erreurs, bail_id=bail_id, avertissement=avertissement,
            choix={"type_bail": TYPES_BAIL, "periodicite": tuple(PERIODICITES_FACTURATION), "indice": INDICES,
                   "methode": METHODES}), status_code=statut)

    def dossier_ou_404(code: str) -> dict:
        d = cx.execute("SELECT * FROM dossiers WHERE code=?", (code,)).fetchone()
        if d is None:
            raise HTTPException(404, "Dossier inconnu")
        return dict(d)

    @app.get("/dossiers/{code}/baux/nouveau", response_class=HTMLResponse)
    def bail_nouveau(request: Request, code: str, customer_id: int | None = None, abonnement: int | None = None):
        d = dossier_ou_404(code)
        pris = {r["code"] for r in cx.execute("SELECT code FROM baux WHERE dossier=?", (code,))}
        suivant = next(f"B{i:02d}" for i in range(1, 1000) if f"B{i:02d}" not in pris)
        depart = {"code": suivant, "tva_pct": 20, "duree_ans": 9, "periodicite_revision_ans": 1, "periodicite": "Mensuelle",
                  "indice": "ILC", "methode": "Chaînée", "type_bail": "Commercial", "customer_id": customer_id}
        note = None
        abo = cx.execute("SELECT * FROM abonnements WHERE dossier=? AND id=?", (code, abonnement)).fetchone() if abonnement else None
        if abo is not None:                              # bail créé à partir d'un abonnement lu dans Pennylane
            par_an = rp.occurrences_par_an(dict(abo))
            depart["periodicite"] = {12: "Mensuelle", 4: "Trimestrielle", 2: "Semestrielle", 1: "Annuelle"}.get(round(par_an), "Mensuelle")
            depart["customer_id"] = customer_id or abo["customer_id"]
            note = (f"Bail créé à partir de l'abonnement Pennylane n° {abo['id']}, « {abo['label']} » : "
                    f"{_eur(abo['montant_ht'], 2)} HT par occurrence, soit {_eur(abo['montant_ht'] * par_an, 2)} par an. "
                    "Le client et la périodicité sont repris. Le loyer initial, la date d'effet et l'indice restent à saisir d'après le bail.")
        return page_bail_form(request, d, depart, [], None, avertissement=note)

    @app.post("/dossiers/{code}/baux")
    async def bail_creer(request: Request, code: str):
        d = dossier_ou_404(code)
        champs = await formulaire(request)
        valeurs, erreurs = referentiel.valider(champs)
        if valeurs["code"] and cx.execute("SELECT 1 FROM baux WHERE dossier=? AND code=?", (code, valeurs["code"])).fetchone():
            erreurs.append(f"Code du bail : {valeurs['code']} existe déjà dans ce dossier.")
        if erreurs:
            return page_bail_form(request, d, champs, erreurs, None, 400)
        return RedirectResponse(f"/baux/{referentiel.enregistrer_bail(cx, code, valeurs)}", status_code=303)

    @app.get("/baux/{bail_id}/modifier", response_class=HTMLResponse)
    def bail_modifier(request: Request, bail_id: int):
        b = ligne_bail(bail_id)
        return page_bail_form(request, dossier_ou_404(b["dossier"]), b, [], bail_id,
                              avertissement=referentiel.controler_indice_de_depart(b, table_indices()) if b["calcule"] else None)

    @app.post("/baux/{bail_id}")
    async def bail_enregistrer(request: Request, bail_id: int):
        b = ligne_bail(bail_id)
        champs = await formulaire(request)
        valeurs, erreurs = referentiel.valider(champs)
        if valeurs["code"] and cx.execute("SELECT 1 FROM baux WHERE dossier=? AND code=? AND id<>?",
                                          (b["dossier"], valeurs["code"], bail_id)).fetchone():
            erreurs.append(f"Code du bail : {valeurs['code']} existe déjà dans ce dossier.")
        if not erreurs and b["calcule"] and referentiel.calendrier_modifie(b, valeurs) and referentiel.gels_enregistres(cx, bail_id):
            erreurs.append("Des révisions gelées sont enregistrées sur ce bail, et cette modification changerait les dates ou la "
                           "numérotation des révisions. Remettre d'abord ces révisions sur « Appliquer », modifier, puis ressaisir les gels.")
        if erreurs:
            return page_bail_form(request, dossier_ou_404(b["dossier"]), champs, erreurs, bail_id, 400)
        referentiel.enregistrer_bail(cx, b["dossier"], valeurs, bail_id)
        return RedirectResponse(f"/baux/{bail_id}", status_code=303)

    @app.get("/baux/{bail_id}", response_class=HTMLResponse)
    def bail(request: Request, bail_id: int, message: str = "", erreur: str = ""):
        b = ligne_bail(bail_id)
        a = analyses(b["dossier"])[0]
        s = next(x for x in a["situations"] if x.bail["id"] == bail_id)
        corps = json.dumps(s.proposition["corps"], ensure_ascii=False, indent=2) if s.proposition else None
        lignes, dec, alerte_indice = [], {}, None
        if b["calcule"]:
            _, lignes, dec = revisions(b)
            alerte_indice = referentiel.controler_indice_de_depart(b, table_indices())
        return gabarits.TemplateResponse(request, "bail.html", contexte(
            request, page="bord", a=a, sit=s, corps=corps, lignes=lignes, dec=dec, DECISIONS=DECISIONS,
            alerte_indice=alerte_indice, message=message, erreur=erreur, mention=rp.mention_speciale(s.bail, jour()),
            pieces=pieces.du_bail(cx, bail_id), NATURES=referentiel.NATURES_PIECE,
            historique=[dict(r) for r in cx.execute(
                "SELECT * FROM historique WHERE bail_id=? ORDER BY id DESC LIMIT 15", (bail_id,))],
            n=rp.ECHEANCES_PAR_AN.get(s.bail.get("periodicite"), 12)))

    def retour_bail(bail_id: int, **kw) -> RedirectResponse:
        return RedirectResponse(f"/baux/{bail_id}?{urllib.parse.urlencode(kw)}", status_code=303)

    @app.post("/baux/{bail_id}/revisions/{numero}")
    async def revision_decider(request: Request, bail_id: int, numero: int):
        b = ligne_bail(bail_id)
        champs = await formulaire(request)
        erreurs: list[str] = []
        courrier_le = referentiel._date(champs.get("courrier_le", ""), "date_effet", erreurs)
        decision = champs.get("decision", DECISIONS[0])
        if not b["calcule"] or decision not in DECISIONS or erreurs:
            return retour_bail(bail_id, erreur="Décision refusée : valeur ou date illisible.")
        referentiel.enregistrer_decision(cx, b, numero, decision, champs.get("commentaire", "").strip(), courrier_le)
        return retour_bail(bail_id, message=f"Révision n° {numero} : décision enregistrée.")

    def pdf_courrier(b: dict, numero: int) -> tuple[str, bytes, object]:
        """(nom du fichier, contenu, ligne de révision) du courrier d'une révision. Lève HTTPException s'il n'y a rien à notifier."""
        from indexation_loyer import courriers
        from indexation_loyer.workbook import Societe
        if not b["calcule"]:
            raise HTTPException(409, "Bail non calculé par la console : compléter ses paramètres.")
        _, lignes, _ = revisions(b)
        bail_m = referentiel.vers_bail(b, affichage=True)        # le courrier cite la vraie date d'effet du bail
        l = next((x for x in lignes if x.echeance.numero == numero), None)
        if l is None:
            raise HTTPException(404, "Révision inconnue")
        if l.loyer_precedent is None or l.statut == calcul.STATUT_A_VENIR:
            raise HTTPException(409, "Rien à notifier : ni indice publié ni décision pour cette révision.")
        d = dossier_ou_404(b["dossier"])
        societe = Societe(nom=d["nom"], siren=d["siren"] or "", forme=d["forme"] or "", adresse=d["adresse"] or "",
                          signataire=d["signataire"] or "", qualite_signataire=d["qualite_signataire"] or "Gérant",
                          ville_signature=d["ville_signature"] or "", demo=d["source_referentiel"] == FICTIF)
        with tempfile.TemporaryDirectory() as tmp:
            contenu = courriers.generer_un(societe, bail_m, l, Path(tmp) / "courrier.pdf", aujourdhui=jour()).read_bytes()
        nature = "gel" if est_gel(l.decision) else "attente" if l.loyer_retenu is None else "revision"
        locataire = "".join(c if c.isalnum() else "_" for c in b["locataire"]).strip("_")[:30]
        return f"{b['dossier']}_{b['code']}_{locataire}_{l.echeance.date_revision:%Y-%m-%d}_{nature}.pdf", contenu, l

    @app.get("/baux/{bail_id}/revisions/{numero}/courrier.pdf")
    def revision_courrier(bail_id: int, numero: int):
        b = ligne_bail(bail_id)
        nom, contenu, l = pdf_courrier(b, numero)
        referentiel.tracer(cx, "Courrier généré", f"révision n° {numero} du {l.echeance.date_revision:%d/%m/%Y}", b["dossier"], bail_id)
        cx.commit()
        return Response(contenu, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{nom}"'})

    # --------------------------------------------------------- courriers en lot
    def courriers_a_editer(horizon: int, dossier: str = "") -> list[dict]:
        """Révisions « à préparer » dont le courrier n'est pas encore daté comme envoyé."""
        return [l for l in pilotage(analyses(dossier or None), horizon)
                if l["etat"] == "a_faire" and not l["courrier_le"] and l["numero"] is not None]

    @app.get("/courriers", response_class=HTMLResponse)
    def courriers_lot(request: Request, horizon: int = 90, dossier: str = "", message: str = ""):
        horizon = min(max(horizon, 30), 730)
        return gabarits.TemplateResponse(request, "courriers.html", contexte(
            request, page="bord", lignes=courriers_a_editer(horizon, dossier), horizon=horizon, dossier=dossier, message=message,
            noms={r["code"]: r["nom"] for r in cx.execute("SELECT code, nom FROM dossiers ORDER BY nom")}))

    @app.get("/courriers/lot.zip")
    def courriers_lot_zip(horizon: int = 90, dossier: str = ""):
        lignes = courriers_a_editer(min(max(horizon, 30), 730), dossier)
        if not lignes:
            raise HTTPException(404, "Aucun courrier à éditer.")
        tampon = io.BytesIO()
        with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as archive:
            for l in lignes:
                nom, contenu, _ = pdf_courrier(ligne_bail(l["bail"]["id"]), l["numero"])
                archive.writestr(nom, contenu)
        referentiel.tracer(cx, "Courriers édités en lot", f"{len(lignes)} courrier(s)" + (f" · dossier {dossier}" if dossier else ""), dossier)
        cx.commit()
        return Response(tampon.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="courriers_reindexation_{jour():%Y-%m-%d}.zip"'})

    @app.post("/courriers")
    async def courriers_dater(request: Request):
        """Date comme envoyés les courriers cochés : la décision et le commentaire de chaque révision sont conservés."""
        form = await request.form()
        erreurs: list[str] = []
        envoye_le = referentiel._date(str(form.get("envoye_le") or ""), "date_effet", erreurs)
        retour = "/courriers?" + urllib.parse.urlencode({"horizon": form.get("horizon") or 90, "dossier": form.get("dossier") or ""})
        if erreurs or not envoye_le:
            return RedirectResponse(retour + "&" + urllib.parse.urlencode({"message": "Date d'envoi illisible : rien n'a été modifié."}), status_code=303)
        n = 0
        for cle in form.getlist("revision"):
            try:
                bail_id, numero = (int(x) for x in str(cle).split(":"))
            except ValueError:
                continue
            b = cx.execute("SELECT * FROM baux WHERE id=?", (bail_id,)).fetchone()
            if b is None or not b["calcule"]:
                continue
            d = referentiel.decisions_du_bail(cx, bail_id).get(numero, {})
            referentiel.enregistrer_decision(cx, dict(b), numero, d.get("decision") or DECISIONS[0], d.get("commentaire") or "", envoye_le)
            n += 1
        return RedirectResponse(retour + "&" + urllib.parse.urlencode({"message": f"{n} courrier(s) daté(s) comme envoyé(s)."}), status_code=303)

    # ------------------------------------------- situation des loyers, pour le bailleur
    @app.get("/dossiers/{code}/situation-loyers.pdf")
    def situation_loyers(code: str):
        """À date, pour le gérant : loyers réindexés sur douze mois, réindexations à venir avec ou sans indice publié."""
        d = dossier_ou_404(code)
        baux = []
        for b in depot.charger(cx, code, jour(), chemin_indices)[0]["baux"]:
            if b.get("date_sortie") and str(b["date_sortie"])[:10] <= jour().isoformat():
                continue                                 # locataire sorti
            baux.append({"bail": b, "revisions": revisions(b)[1] if b.get("calcule") and b.get("date_effet") else []})
        if not baux:
            raise HTTPException(404, "Aucun bail dans ce dossier.")
        groupes = editions.lignes_situation(baux, jour())
        contenu = editions.situation_loyers(d, groupes, jour(), fictif=d["source_referentiel"] == FICTIF)
        referentiel.tracer(cx, "Situation des loyers éditée", f"{len(baux)} bail/baux", code)
        cx.commit()
        return Response(contenu, media_type="application/pdf", headers={
            "Content-Disposition": f'inline; filename="{code}_situation_loyers_{jour():%Y-%m-%d}.pdf"'})

    # ---------------------------------------------------- état annuel des révisions
    @app.get("/dossiers/{code}/etat-revisions.pdf")
    def etat_revisions(code: str, annee: int | None = None):
        d = dossier_ou_404(code)
        annee = annee or jour().year
        if not 2000 <= annee <= jour().year + 1:
            raise HTTPException(400, "Année hors limites.")
        table, baux = table_indices(), []
        for b in map(dict, cx.execute("SELECT * FROM baux WHERE dossier=? AND calcule=1 AND date_effet IS NOT NULL ORDER BY code", (code,)).fetchall()):
            if (b.get("date_sortie") and str(b["date_sortie"])[:4] < str(annee)) or str(b["reprise_date"] or b["date_effet"])[:4] > str(annee):
                continue                                 # sorti avant l'année, ou pas encore en vigueur
            dec = referentiel.decisions_du_bail(cx, b["id"])
            _, revs = referentiel.reviser(b, table, {n: x["decision"] for n, x in dec.items()}, max(jour(), date(annee, 12, 31)))
            baux.append({"bail": b, "revisions": revs, "decisions": dec})
        if not baux:
            raise HTTPException(404, "Aucun bail calculé par la console pour cette année.")
        contenu = editions.etat_annuel(d, baux, annee, jour(), fictif=d["source_referentiel"] == FICTIF)
        referentiel.tracer(cx, "État annuel des révisions édité", f"année {annee} · {len(baux)} bail/baux", code)
        cx.commit()
        return Response(contenu, media_type="application/pdf", headers={
            "Content-Disposition": f'inline; filename="{code}_revisions_{annee}.pdf"'})

    @app.get("/baux/{bail_id}/releve.pdf")
    def releve_pdf(bail_id: int, courrier: int = 0, depuis: str = ""):
        """Relevé des factures du bail depuis une date (douze mois par défaut), seul ou précédé du courrier de régularisation."""
        b = ligne_bail(bail_id)
        a = analyses(b["dossier"])[0]
        sit = next(x for x in a["situations"] if x.bail["id"] == bail_id)
        try:
            debut = date.fromisoformat(depuis) if depuis else date(jour().year - 1, jour().month, 1)
        except ValueError:
            raise HTTPException(400, "Date de début illisible.")
        lignes = editions.lignes_releve(sit.factures, debut)
        if not lignes:
            raise HTTPException(404, "Aucune facture émise sur la période.")
        n = rp.ECHEANCES_PAR_AN.get(sit.bail.get("periodicite"), 12)
        ht = round(sit.attendu_annuel / n, 2)
        attendu = round(ht + ht * (sit.bail.get("tva_pct") or 0) / 100, 2) if sit.bail.get("calcule") else None
        d = dossier_ou_404(b["dossier"])
        contenu = editions.releve(d, sit.bail, lignes, debut, jour(), courrier=bool(courrier), attendu_ttc=attendu,
                                  unite=rp.UNITE.get(sit.bail.get("periodicite"), "échéance"), fictif=d["source_referentiel"] == FICTIF)
        referentiel.tracer(cx, "Courrier de régularisation édité" if courrier else "Relevé des factures édité",
                           f"depuis le {debut:%d/%m/%Y}, solde {sum(l['reste'] for l in lignes):.2f} € TTC", b["dossier"], bail_id)
        cx.commit()
        return Response(contenu, media_type="application/pdf", headers={
            "Content-Disposition": f'inline; filename="{b["dossier"]}_{b["code"]}_{"regularisation" if courrier else "releve"}_{jour():%Y-%m-%d}.pdf"'})

    @app.post("/baux/{bail_id}/pieces")
    async def piece_deposer(request: Request, bail_id: int):
        b = ligne_bail(bail_id)
        form = await request.form()
        fichier = form.get("fichier")
        if fichier is None or isinstance(fichier, str) or not fichier.filename:
            return retour_bail(bail_id, erreur="Aucun fichier choisi.")
        contenu = await fichier.read(pieces.TAILLE_MAX + 1)
        try:
            pieces.deposer(cx, stockage, b, fichier.filename, contenu, str(form.get("nature") or "Bail"))
        except pieces.PieceRefusee as exc:
            return retour_bail(bail_id, erreur=str(exc))
        return retour_bail(bail_id, message=f"Pièce jointe : {Path(fichier.filename).name}.")

    @app.get("/pieces/{piece_id}")
    def piece_telecharger(piece_id: int):
        p = cx.execute("SELECT * FROM pieces WHERE id=?", (piece_id,)).fetchone()
        chemin = pieces.chemin_fichier(stockage, p["sha256"]) if p else None
        if p is None or not chemin.exists():
            raise HTTPException(404, "Pièce introuvable")
        nom = urllib.parse.quote(p["nom"])
        return Response(chemin.read_bytes(), media_type=p["type_mime"], headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{nom}", "X-Content-Type-Options": "nosniff"})

    # -------------------------------------------------------------- import en lot
    def page_import(request: Request, erreur: str = "", statut: int = 200, imp: dict | None = None, analyse: dict | None = None):
        recents = [dict(r) for r in cx.execute("SELECT id, horodatage, auteur, nom_fichier, etat FROM imports ORDER BY id DESC LIMIT 8")]
        return gabarits.TemplateResponse(request, "import.html", contexte(
            request, page="bord", erreur=erreur, imp=imp, analyse=analyse, recents=recents,
            COLONNES=import_tableur.COLONNES), status_code=statut)

    @app.get("/import", response_class=HTMLResponse)
    def import_accueil(request: Request):
        return page_import(request)

    @app.get("/import/modele.xlsx")
    def import_modele():
        return Response(import_tableur.modele_xlsx(), headers={"Content-Disposition": 'attachment; filename="modele_import_baux.xlsx"'},
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.post("/import")
    async def import_deposer(request: Request):
        fichier = (await request.form()).get("fichier")
        if fichier is None or isinstance(fichier, str) or not fichier.filename:
            return page_import(request, "Aucun fichier choisi.", 400)
        contenu = await fichier.read(import_tableur.TAILLE_MAX + 1)
        nom = Path(fichier.filename).name
        try:
            lignes = import_tableur.lire(nom, contenu)
        except import_tableur.ImportRefuse as exc:
            return page_import(request, str(exc), 400)
        cur = cx.execute("INSERT INTO imports (horodatage, auteur, nom_fichier, empreinte, lignes) VALUES (?,?,?,?,?)",
                         (datetime.now().isoformat(timespec="seconds"), referentiel.utilisateur(), nom,
                          hashlib.sha256(contenu).hexdigest(), json.dumps(lignes, ensure_ascii=False)))
        cx.commit()
        return RedirectResponse(f"/import/{cur.lastrowid}", status_code=303)

    def import_ou_404(import_id: int) -> tuple[dict, dict]:
        imp = cx.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
        if imp is None:
            raise HTTPException(404, "Import inconnu")
        return dict(imp), import_tableur.analyser(cx, json.loads(imp["lignes"]), table_indices(), jour())

    @app.get("/import/{import_id}", response_class=HTMLResponse)
    def import_apercu(request: Request, import_id: int):
        imp, analyse = import_ou_404(import_id)
        return page_import(request, imp=imp, analyse=analyse)

    @app.post("/import/{import_id}")
    async def import_confirmer(request: Request, import_id: int):
        imp, analyse = import_ou_404(import_id)
        champs = await formulaire(request)
        if imp["etat"] != "analysé":
            return page_import(request, "Ce fichier a déjà été importé.", 409, imp, analyse)
        if champs.get("empreinte") != imp["empreinte"] or champs.get("nb") != str(analyse["nb_creer"]):
            return page_import(request, "La situation a changé depuis l'aperçu : le relire avant de confirmer.", 409, imp, analyse)
        try:
            import_tableur.executer(cx, analyse, imp["nom_fichier"])
        except import_tableur.ImportRefuse as exc:
            return page_import(request, str(exc), 409, imp, analyse)
        cx.execute("UPDATE imports SET etat='importé' WHERE id=?", (import_id,))
        cx.commit()
        return RedirectResponse(f"/import/{import_id}", status_code=303)

    # ------------------------------------------------------------------- indices
    @app.get("/indices", response_class=HTMLResponse)
    def page_indices(request: Request):
        obs = referentiel.lire_indices(chemin_indices)
        series = []
        for code in INDICES:
            valeurs = sorted((o for o in obs if o.serie == code), key=lambda o: o.periode)
            if valeurs:
                series.append({"code": code, "dernieres": valeurs[-5:][::-1], "releve": max(o.date_extraction for o in valeurs)})
        table, suivi = calcul.table_indices(obs), []
        noms = {r["code"]: r["nom"] for r in cx.execute("SELECT code, nom FROM dossiers")}
        for b in map(dict, cx.execute("SELECT * FROM baux WHERE calcule=1 AND date_effet IS NOT NULL ORDER BY dossier, code").fetchall()):
            dec = {n: d["decision"] for n, d in referentiel.decisions_du_bail(cx, b["id"]).items()}
            _, lignes = referentiel.reviser(b, table, dec, jour())
            echues_sans_indice = [l for l in lignes if l.statut == calcul.STATUT_ATTENDU]
            prochaine = next((l for l in lignes if l.echeance.date_revision > jour()), None)
            for l in [*echues_sans_indice[-1:], *([prochaine] if prochaine else [])]:
                suivi.append({"bail": b, "nom": noms.get(b["dossier"], b["dossier"]), "l": l,
                              "jours": (l.echeance.date_revision - jour()).days})
        suivi.sort(key=lambda x: x["l"].echeance.date_revision)
        return gabarits.TemplateResponse(request, "indices.html", contexte(request, page="indices", series=series, suivi=suivi))

    def collecter(code: str | None = None) -> list[dict]:
        from .pennylane_api import ClientReel
        variables = {r["code"]: r["variable_jeton"] for r in cx.execute(
            "SELECT code, variable_jeton FROM dossiers WHERE source_referentiel<>?", (FICTIF,))}   # jamais le fictif
        if code and code not in variables:
            return [{"dossier": code, "ok": False, "message": "dossier fictif : aucune collecte"}]
        # « Actualiser » ne lit que les dossiers raccordés : un dossier sans jeton n'est pas un échec
        codes = [code] if code else sorted(c for c, v in variables.items() if jetons.source(v))
        if not codes:
            return []
        return depot.synchroniser(cx, ClientReel(variables), codes, source="pennylane")

    def preparer(bail_id: int):
        """(analyse du dossier, situation du bail, proposition au mode brouillon imposé, contrôles)."""
        a = analyses(ligne_bail(bail_id)["dossier"])[0]
        return preparer_situation(a, next(x for x in a["situations"] if x.bail["id"] == bail_id))

    def preparer_situation(a: dict, sit):
        prop = rp.proposer_abonnement(sit.bail, sit.courant, jour(), None, sit.courant or sit.dernier_termine)
        prop["corps"] = ecriture.imposer_brouillon(prop["corps"])
        controles = ecriture.controler(sit, prop, a["dossier"], ecriture.journal(cx, a["dossier"]["code"]), jour(),
                                      sans_appel(a["dossier"]["code"]))
        return a, sit, prop, controles

    def preparer_rappel(bail_id: int, interroger: bool = True):
        """(analyse du dossier, situation du bail, proposition de facture, contrôles, simulation ?).

        `interroger` : demander à Pennylane si une facture porte déjà la référence du rappel. La liste des créations
        en lot s'en passe à l'affichage (elle s'en tient aux factures déjà collectées) ; la création, jamais."""
        a = analyses(ligne_bail(bail_id)["dossier"])[0]
        return preparer_rappel_situation(a, next(x for x in a["situations"] if x.bail["id"] == bail_id), interroger)

    def preparer_rappel_situation(a: dict, sit, interroger: bool = True):
        from .pennylane_api import ClientReel
        code = a["dossier"]["code"]
        simule = sans_appel(code)
        prop = rp.proposer_facture_rappel(sit.bail, sit.courant, jour()) if sit.rappel else None
        existe = None
        if prop and not simule and interroger:           # lecture : cette référence est-elle déjà dans Pennylane ?
            try:
                existe = ClientReel({code: a["dossier"]["variable_jeton"]}).facture_existe(code, prop["reference"])
            except RuntimeError:
                existe = None
        elif prop and not simule:
            existe = sit.rappel_facture is not None
        controles = ecriture.controler_rappel(sit, prop, a["dossier"], ecriture.journal(cx, code), simule, existe)
        return a, sit, prop, controles, simule

    def executer(bail_id: int, nature: str, empreinte: str, confirmation: str | None = None, relire: bool = True,
                 lot: str = "") -> dict:
        """Crée UN abonnement ou UNE facture de rappel, après tous les contrôles. Seul chemin vers l'écriture,
        emprunté par la création unitaire comme par chaque ligne d'un lot.

        Retourne {envoi, etat, erreur, statut}. `envoi` vaut None si rien n'a été tenté : l'erreur dit pourquoi."""
        refus = lambda erreur, statut: {"envoi": None, "etat": "", "erreur": erreur, "statut": statut}
        code_dossier = ligne_bail(bail_id)["dossier"]
        if relire and not sans_appel(code_dossier):      # relecture de Pennylane juste avant d'écrire
            if not all(x["ok"] for x in collecter(code_dossier)):
                return refus("Relecture de Pennylane impossible : aucun envoi sans état à jour.", 409)
        if nature == "facture":
            a, sit, prop, controles, simule = preparer_rappel(bail_id)
        else:
            a, sit, prop, controles = preparer(bail_id)
            simule = sans_appel(code_dossier)
        if prop is None or empreinte != ecriture.empreinte(prop["corps"]):
            return refus("La situation a changé depuis l'affichage : relire la proposition avant de confirmer.", 409)
        if confirmation is not None and confirmation.strip().upper() != sit.bail["code"].upper():
            return refus(f"Confirmation incorrecte : saisir le code du bail, {sit.bail['code']}.", 400)
        if not ecriture.envoi_possible(controles):
            echec = next(c for c in controles if c.bloquant and not c.ok)
            return refus(f"Envoi refusé, contrôle en échec : {echec.libelle}.", 409)
        corps = prop["corps"]
        envoi_id = ecriture.ouvrir_envoi(cx, sit.bail, prop, corps, "simulation" if simule else "pennylane", nature=nature, lot=lot)
        if simule and nature == "facture":
            ttc = round(prop["montant_ht"] * (1 + (sit.bail.get("tva_pct") or 0) / 100), 2)
            cx.execute("""INSERT INTO factures (id, dossier, customer_id, subscription_id, numero, libelle, date, echeance,
                          montant_ht, montant_ttc, restant_du, statut, reference) VALUES (?,?,?,NULL,'',?,?,?,?,?,?,'draft',?)""",
                       (900000 + envoi_id, sit.bail["dossier"], corps["customer_id"], corps["pdf_invoice_subject"], corps["date"],
                        corps["deadline"], prop["montant_ht"], ttc, ttc, prop["reference"]))
            ecriture.clore_envoi(cx, envoi_id, "créé", 201, None, "draft", "Envoi simulé : aucun appel à Pennylane.",
                                 facture_id=900000 + envoi_id, verification="Simulation : rien à relire.")
            return {"envoi": envoi_id, "etat": "créé", "erreur": "", "statut": 201}
        if simule:
            donnees, regle = corps["customer_invoice_data"], corps["recurring_rule"]
            montant = sum(float(l["raw_currency_unit_price"]) * float(l.get("quantity", 1)) for l in donnees["invoice_lines"])
            cx.execute("""INSERT INTO abonnements (id, dossier, customer_id, label, statut, mode, debut, fin, prochaine_occurrence,
                          type_regle, intervalle, montant_ht, parametres) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (800000 + envoi_id, sit.bail["dossier"], corps["customer_id"], corps["label"], "not_started",
                        ecriture.MODE_IMPOSE, prop["debut"], prop["derniere_occurrence"], prop["debut"],
                        regle["type"], regle.get("interval", 1), round(montant, 2),
                        json.dumps(ecriture.parametres_du_corps(corps), ensure_ascii=False)))
            ecriture.clore_envoi(cx, envoi_id, "créé", 201, 800000 + envoi_id, "not_started", "Envoi simulé : aucun appel à Pennylane.",
                                 verification="Simulation : rien à relire.")
            return {"envoi": envoi_id, "etat": "créé", "erreur": "", "statut": 201}
        try:
            etat, http, reponse, brut = (ecriture.creer_facture_dans_pennylane if nature == "facture"
                                         else ecriture.creer_dans_pennylane)(a["dossier"], corps)
        except PermissionError as exc:
            ecriture.clore_envoi(cx, envoi_id, "refusé", None, None, "", str(exc))
            return {"envoi": envoi_id, "etat": "refusé", "erreur": str(exc), "statut": 403}
        identifiant = (reponse or {}).get("id")
        ecriture.clore_envoi(cx, envoi_id, etat, http, None if nature == "facture" else identifiant, (reponse or {}).get("status", ""), brut,
                             facture_id=identifiant if nature == "facture" else None,
                             verification=ecriture.verifier_reponse(nature, corps, reponse) if etat == "créé" else "")
        if etat == "créé" and relire:
            collecter(sit.bail["dossier"])
        return {"envoi": envoi_id, "etat": etat, "erreur": "", "statut": http or 0}

    def page_creation(request: Request, bail_id: int, erreur: str = "", statut: int = 200):
        a, sit, prop, controles = preparer(bail_id)
        return gabarits.TemplateResponse(request, "creation.html", contexte(
            request, page="bord", a=a, sit=sit, prop=prop, controles=controles, erreur=erreur,
            possible=ecriture.envoi_possible(controles), empreinte=ecriture.empreinte(prop["corps"]),
            sans_appel=sans_appel(a["dossier"]["code"]),
            corps=json.dumps(prop["corps"], ensure_ascii=False, indent=2)), status_code=statut)

    @app.get("/baux/{bail_id}/abonnement", response_class=HTMLResponse)
    def creation(request: Request, bail_id: int):
        return page_creation(request, bail_id)

    @app.post("/baux/{bail_id}/abonnement")
    async def creer(request: Request, bail_id: int):
        champs = await formulaire(request)
        r = executer(bail_id, "abonnement", champs.get("empreinte", ""), champs.get("confirmation", ""))
        if r["envoi"] is None or r["statut"] == 403:
            return page_creation(request, bail_id, r["erreur"], r["statut"])
        return RedirectResponse(f"/envois/{r['envoi']}", status_code=303)

    # ------------------------------------------------ facture de rappel, en brouillon
    def page_rappel(request: Request, bail_id: int, erreur: str = "", statut: int = 200):
        a, sit, prop, controles, simule = preparer_rappel(bail_id)
        return gabarits.TemplateResponse(request, "rappel.html", contexte(
            request, page="bord", a=a, sit=sit, prop=prop, controles=controles, erreur=erreur, sans_appel=simule,
            possible=prop is not None and ecriture.envoi_possible(controles),
            empreinte=ecriture.empreinte(prop["corps"]) if prop else "",
            corps=json.dumps(prop["corps"], ensure_ascii=False, indent=2) if prop else ""), status_code=statut)

    @app.get("/baux/{bail_id}/rappel", response_class=HTMLResponse)
    def rappel(request: Request, bail_id: int):
        return page_rappel(request, bail_id)

    @app.post("/baux/{bail_id}/rappel")
    async def rappel_creer(request: Request, bail_id: int):
        champs = await formulaire(request)
        r = executer(bail_id, "facture", champs.get("empreinte", ""), champs.get("confirmation", ""))
        if r["envoi"] is None or r["statut"] == 403:
            return page_rappel(request, bail_id, r["erreur"], r["statut"])
        return RedirectResponse(f"/envois/{r['envoi']}", status_code=303)

    # ------------------------------------------------ créations en lot
    def candidats() -> list[dict]:
        """Tout ce que la console propose de créer, tous dossiers confondus : abonnements de la période suivante
        et factures de rappel. Chaque ligne porte ses contrôles ; seules les lignes sans contrôle bloquant sont cochables."""
        lignes = []
        for a in analyses():
            code = a["dossier"]["code"]
            for sit in a["situations"]:
                propositions = []
                if sit.proposition and sit.suivant is None:      # période suivante pas encore créée
                    _, _, prop, controles = preparer_situation(a, sit)
                    lignes_f = prop["corps"]["customer_invoice_data"]["invoice_lines"]
                    propositions.append(("abonnement", prop, controles, round(sum(
                        float(l["raw_currency_unit_price"]) * float(l.get("quantity", 1)) for l in lignes_f), 2),
                        f"du {_date_fr(prop['debut'])}" + (f" au {_date_fr(prop['derniere_occurrence'])}, {prop['occurrences']} occurrence(s)"
                                                           if prop["occurrences"] else ", sans terme")))
                if sit.rappel and sit.rappel["echeances"] and sit.rappel_facture is None:
                    _, _, prop, controles, _ = preparer_rappel_situation(a, sit, interroger=False)
                    if prop:
                        propositions.append(("facture", prop, controles, prop["montant_ht"],
                                             f"révision du {_date_fr(prop['date_revision'])}, {prop['echeances']} échéance(s)"))
                for nature, prop, controles, montant, periode in propositions:
                    bloquants = [c for c in controles if c.bloquant and not c.ok]
                    lignes.append({"cle": f"{sit.bail['id']}:{nature}:{ecriture.empreinte(prop['corps'])}", "nature": nature,
                                   "dossier": a["dossier"], "bail": sit.bail, "montant": montant, "periode": periode,
                                   "pret": not bloquants, "bloquants": bloquants, "simule": sans_appel(code),
                                   "avertissements": [c for c in controles if not c.bloquant and not c.ok]})
        return lignes

    def page_creations(request: Request, erreur: str = "", statut: int = 200, resultats: list[dict] | None = None, lot: str = ""):
        lignes = candidats()
        return gabarits.TemplateResponse(request, "creations.html", contexte(
            request, page="creations", lignes=lignes, erreur=erreur, resultats=resultats, lot=lot,
            nb_prets=sum(1 for l in lignes if l["pret"])), status_code=statut)

    @app.get("/creations", response_class=HTMLResponse)
    def creations(request: Request):
        return page_creations(request)

    @app.post("/creations")
    async def creations_lancer(request: Request):
        champs = urllib.parse.parse_qs((await request.body()).decode("utf-8"))
        choix = champs.get("choix", [])
        if not choix:
            return page_creations(request, "Aucune création cochée.", 400)
        if (champs.get("confirmation") or [""])[0].strip() != str(len(choix)):
            return page_creations(request, f"Confirmation incorrecte : {len(choix)} création(s) cochée(s), saisir ce nombre.", 400)
        demandes = []
        for c in choix:
            morceaux = c.split(":")
            if len(morceaux) != 3 or not morceaux[0].isdigit() or morceaux[1] not in ("abonnement", "facture"):
                return page_creations(request, "Demande illisible : recharger la page.", 400)
            demandes.append((int(morceaux[0]), morceaux[1], morceaux[2]))
        lot = datetime.now().strftime("%Y%m%d-%H%M%S")
        # une seule relecture de Pennylane par dossier réel, avant la première écriture du lot
        relus: dict[str, bool] = {}
        for bail_id, _, _ in demandes:
            code = ligne_bail(bail_id)["dossier"]
            if code not in relus and not sans_appel(code):
                relus[code] = all(x["ok"] for x in collecter(code))
        resultats, arret = [], ""
        for bail_id, nature, empreinte in demandes:
            b = ligne_bail(bail_id)
            ligne = {"bail": b, "nature": nature, "envoi": None, "etat": "non envoyé", "erreur": ""}
            resultats.append(ligne)
            if arret:
                ligne["erreur"] = arret
                continue
            if relus.get(b["dossier"]) is False:
                ligne["erreur"] = "Relecture de Pennylane impossible pour ce dossier : rien n'a été envoyé."
                continue
            r = executer(bail_id, nature, empreinte, relire=False, lot=lot)
            ligne.update(envoi=r["envoi"], etat=r["etat"] or "non envoyé", erreur=r["erreur"])
            if r["envoi"] is not None and r["etat"] != "créé":     # Pennylane n'a pas confirmé : on n'insiste pas
                arret = f"Lot arrêté : l'envoi n° {r['envoi']} n'a pas été confirmé par Pennylane. Rien d'autre n'a été envoyé."
        for code in relus:                               # l'état local reflète ce qui vient d'être créé
            collecter(code)
        referentiel.tracer(cx, "Créations en lot", f"lot {lot} : {sum(1 for x in resultats if x['etat'] == 'créé')} création(s) "
                                                   f"sur {len(resultats)} demandée(s)")
        cx.commit()
        return page_creations(request, arret, 200, resultats, lot)

    @app.get("/envois", response_class=HTMLResponse)
    def envois(request: Request, lot: str = ""):
        noms = {r["code"]: r["nom"] for r in cx.execute("SELECT code, nom FROM dossiers")}
        return gabarits.TemplateResponse(request, "envois.html", contexte(
            request, page="envois", envois=ecriture.journal(cx, lot=lot), noms=noms, lot=lot))

    @app.get("/envois/{envoi_id}", response_class=HTMLResponse)
    def envoi(request: Request, envoi_id: int):
        e = cx.execute("SELECT * FROM envois WHERE id=?", (envoi_id,)).fetchone()
        if e is None:
            raise HTTPException(404, "Envoi inconnu")
        nom = cx.execute("SELECT nom FROM dossiers WHERE code=?", (e["dossier"],)).fetchone()
        return gabarits.TemplateResponse(request, "envoi.html", contexte(request, page="envois", e=dict(e), nom=nom["nom"] if nom else e["dossier"]))

    @app.post("/actions/synchroniser")
    def synchroniser(request: Request):
        collecter()                                      # dossiers réels seulement ; le fictif n'est jamais relu
        retour = urllib.parse.urlsplit(request.headers.get("referer") or "").path     # on revient d'où l'on vient
        return RedirectResponse(retour if retour.startswith("/") and not retour.startswith("//") else "/", status_code=303)

    @app.get("/api/synthese")
    def api_synthese():
        an = analyses()
        return JSONResponse({"date": jour().isoformat(), **synthese(an),
                             "dossiers": [{"code": a["dossier"]["code"], "nom": a["dossier"]["nom"],
                                           "baux": a["nb_baux"], "attendu_annuel": a["attendu_annuel"],
                                           "facture_annuel": a["facture_annuel"], "ecarts": a["compte"]} for a in an]})

    @app.get("/api/ecarts")
    def api_ecarts(gravite: str = ""):
        return JSONResponse([e.dict() for a in analyses() for e in a["ecarts"] if not gravite or e.gravite == gravite])

    return app

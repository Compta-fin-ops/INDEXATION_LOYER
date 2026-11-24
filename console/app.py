"""Application web de la console (FastAPI, rendu serveur, aucun outillage JavaScript).

    python -m console demo      charge le portefeuille fictif
    python -m console serve     http://127.0.0.1:8000

Les baux sont saisis et calculés ici. Côté Pennylane, la console lit ; elle ne crée un abonnement que
sur confirmation explicite, verrous levés (console/ecriture.py), et ne modifie ni ne supprime jamais rien.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import tempfile
import urllib.parse
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from indexation_loyer import calcul
from indexation_loyer.baux import DECISIONS, INDICES, METHODES, PERIODICITES_FACTURATION, TYPES_BAIL, est_gel

from . import (__version__, comptes, db, depot, ecriture, import_tableur, jetons, pieces, rapprochement as rp,
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
        if chemin.endswith("/abonnement"):               # création d'un abonnement dans Pennylane
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
        return [rp.analyser_dossier(x["dossier"], x["baux"], x["abonnements"], x["factures"], jour())
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
                "environnement": ENV, **kw}

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

    @app.get("/", response_class=HTMLResponse)
    def tableau_de_bord(request: Request):
        an = analyses()
        ordre = {g: i for i, g in enumerate(rp.GRAVITES)}
        priorites = sorted((e for a in an for e in a["ecarts"] if e.gravite != "info"),
                           key=lambda e: (ordre[e.gravite], -abs(e.montant)))[:12]
        noms = {a["dossier"]["code"]: a["dossier"]["nom"] for a in an}
        return gabarits.TemplateResponse(request, "tableau_de_bord.html", contexte(
            request, page="bord", analyses=an, s=synthese(an), priorites=priorites, noms=noms,
            prochaines=rp.echeancier(an, jour(), 60)[:8]))

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
    def bail_nouveau(request: Request, code: str):
        d = dossier_ou_404(code)
        pris = {r["code"] for r in cx.execute("SELECT code FROM baux WHERE dossier=?", (code,))}
        suivant = next(f"B{i:02d}" for i in range(1, 1000) if f"B{i:02d}" not in pris)
        return page_bail_form(request, d, {"code": suivant, "tva_pct": 20, "duree_ans": 9, "periodicite_revision_ans": 1,
                                           "periodicite": "Mensuelle", "indice": "ILC", "methode": "Chaînée",
                                           "type_bail": "Commercial"}, [], None)

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
            alerte_indice=alerte_indice, message=message, erreur=erreur,
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

    @app.get("/baux/{bail_id}/revisions/{numero}/courrier.pdf")
    def revision_courrier(bail_id: int, numero: int):
        from indexation_loyer import courriers
        from indexation_loyer.workbook import Societe
        b = ligne_bail(bail_id)
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
        nom = f"{b['dossier']}_{b['code']}_{l.echeance.date_revision:%Y-%m-%d}_{nature}.pdf"
        referentiel.tracer(cx, "Courrier généré", f"révision n° {numero} du {l.echeance.date_revision:%d/%m/%Y} ({nature})",
                           b["dossier"], bail_id)
        cx.commit()
        return Response(contenu, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{nom}"'})

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
        if not variables:
            return []
        return depot.synchroniser(cx, ClientReel(variables), [code] if code else sorted(variables), source="pennylane")

    def preparer(bail_id: int):
        """(analyse du dossier, situation du bail, proposition au mode brouillon imposé, contrôles)."""
        a = analyses(ligne_bail(bail_id)["dossier"])[0]
        sit = next(x for x in a["situations"] if x.bail["id"] == bail_id)
        prop = rp.proposer_abonnement(sit.bail, sit.courant, jour())
        prop["corps"] = ecriture.imposer_brouillon(prop["corps"])
        controles = ecriture.controler(sit, prop, a["dossier"], ecriture.journal(cx, a["dossier"]["code"]), jour(),
                                      sans_appel(a["dossier"]["code"]))
        return a, sit, prop, controles

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
        champs = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode("utf-8")).items()}
        code_dossier = ligne_bail(bail_id)["dossier"]
        simule = sans_appel(code_dossier)                # dossier fictif ou base de test : jamais d'appel
        if not simule:                                   # relecture de Pennylane juste avant d'écrire
            if not all(x["ok"] for x in collecter(code_dossier)):
                return page_creation(request, bail_id, "Relecture de Pennylane impossible : aucun envoi sans état à jour.", 409)
        a, sit, prop, controles = preparer(bail_id)
        if champs.get("empreinte") != ecriture.empreinte(prop["corps"]):
            return page_creation(request, bail_id, "La situation a changé depuis l'affichage : relire la proposition ci-dessous avant de confirmer.", 409)
        if champs.get("confirmation", "").strip().upper() != sit.bail["code"].upper():
            return page_creation(request, bail_id, f"Confirmation incorrecte : saisir le code du bail, {sit.bail['code']}.", 400)
        if not ecriture.envoi_possible(controles):
            return page_creation(request, bail_id, "Envoi refusé : au moins un contrôle bloquant est en échec.", 409)
        corps = prop["corps"]
        envoi_id = ecriture.ouvrir_envoi(cx, sit.bail, prop, corps, "simulation" if simule else "pennylane")
        if simule:
            montant = sum(float(l["raw_currency_unit_price"]) for l in corps["customer_invoice_data"]["invoice_lines"])
            regle = corps["recurring_rule"]
            cx.execute("""INSERT INTO abonnements (id, dossier, customer_id, label, statut, mode, debut, fin, prochaine_occurrence,
                          type_regle, intervalle, montant_ht) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (800000 + envoi_id, sit.bail["dossier"], corps["customer_id"], corps["label"], "not_started",
                        ecriture.MODE_IMPOSE, prop["debut"], prop["derniere_occurrence"], prop["debut"],
                        regle["type"], regle.get("interval", 1), round(montant, 2)))
            ecriture.clore_envoi(cx, envoi_id, "créé", 201, 800000 + envoi_id, "not_started", "Envoi simulé : aucun appel à Pennylane.")
        else:
            try:
                etat, http, reponse, brut = ecriture.creer_dans_pennylane(a["dossier"], corps)
            except PermissionError as exc:
                ecriture.clore_envoi(cx, envoi_id, "refusé", None, None, "", str(exc))
                return page_creation(request, bail_id, str(exc), 403)
            ecriture.clore_envoi(cx, envoi_id, etat, http, (reponse or {}).get("id"), (reponse or {}).get("status", ""), brut)
            if etat == "créé":
                collecter(sit.bail["dossier"])
        return RedirectResponse(f"/envois/{envoi_id}", status_code=303)

    @app.get("/envois", response_class=HTMLResponse)
    def envois(request: Request):
        noms = {r["code"]: r["nom"] for r in cx.execute("SELECT code, nom FROM dossiers")}
        return gabarits.TemplateResponse(request, "envois.html", contexte(request, page="envois", envois=ecriture.journal(cx), noms=noms))

    @app.get("/envois/{envoi_id}", response_class=HTMLResponse)
    def envoi(request: Request, envoi_id: int):
        e = cx.execute("SELECT * FROM envois WHERE id=?", (envoi_id,)).fetchone()
        if e is None:
            raise HTTPException(404, "Envoi inconnu")
        nom = cx.execute("SELECT nom FROM dossiers WHERE code=?", (e["dossier"],)).fetchone()
        return gabarits.TemplateResponse(request, "envoi.html", contexte(request, page="envois", e=dict(e), nom=nom["nom"] if nom else e["dossier"]))

    @app.post("/actions/synchroniser")
    def synchroniser():
        collecter()                                      # dossiers réels seulement ; le fictif n'est jamais relu
        return RedirectResponse("/", status_code=303)

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

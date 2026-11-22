"""Application web de la console (FastAPI, rendu serveur, aucun outillage JavaScript).

    python -m console demo      charge le portefeuille fictif
    python -m console serve     http://127.0.0.1:8000

La console ne fait que LIRE Pennylane. Les propositions d'abonnement sont affichées, jamais envoyées.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, db, depot, rapprochement as rp, simulation

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


def creer_app(chemin_db: Path | str = db.CHEMIN_DEFAUT, aujourdhui: date | None = None) -> FastAPI:
    app = FastAPI(title="Console des loyers", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=ICI / "static"), name="static")
    gabarits = Jinja2Templates(directory=ICI / "templates")
    gabarits.env.filters.update(eur=_eur, eur2=lambda x: _eur(x, 2), date_fr=_date_fr,
                                pct=lambda x: f"{x * 100:.0f} %".replace(".", ","))
    cx = db.connecter(chemin_db)
    jour = lambda: aujourdhui or date.today()

    def analyses(code: str | None = None) -> list[dict]:
        return [rp.analyser_dossier(x["dossier"], x["baux"], x["abonnements"], x["factures"], jour())
                for x in depot.charger(cx, code)]

    def contexte(request: Request, **kw) -> dict:
        derniere = cx.execute("SELECT MAX(horodatage) AS h, MAX(source) AS s FROM synchros").fetchone()
        simule = cx.execute("SELECT COUNT(*) AS n FROM dossiers WHERE source_referentiel<>'portefeuille fictif'").fetchone()["n"] == 0
        return {"request": request, "version": __version__, "aujourdhui": jour(), "TYPES": TYPES,
                "STATUTS": STATUTS_ABONNEMENT, "MODES": MODES,
                "GRAVITE": rp.LIBELLE_GRAVITE, "derniere_synchro": derniere["h"], "simule": simule, **kw}

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

    @app.get("/baux/{bail_id}", response_class=HTMLResponse)
    def bail(request: Request, bail_id: int):
        ligne = cx.execute("SELECT dossier FROM baux WHERE id=?", (bail_id,)).fetchone()
        if ligne is None:
            raise HTTPException(404, "Bail inconnu")
        a = analyses(ligne["dossier"])[0]
        s = next(x for x in a["situations"] if x.bail["id"] == bail_id)
        corps = json.dumps(s.proposition["corps"], ensure_ascii=False, indent=2) if s.proposition else None
        return gabarits.TemplateResponse(request, "bail.html", contexte(
            request, page="bord", a=a, sit=s, corps=corps,
            n=rp.ECHEANCES_PAR_AN.get(s.bail.get("periodicite"), 12)))

    @app.post("/actions/synchroniser")
    def synchroniser():
        simule = cx.execute("SELECT COUNT(*) AS n FROM dossiers WHERE source_referentiel<>'portefeuille fictif'").fetchone()["n"] == 0
        if simule:
            simulation.charger_demo(cx, jour())
        else:
            from .pennylane_api import ClientReel
            variables = {r["code"]: r["variable_jeton"] for r in cx.execute("SELECT code, variable_jeton FROM dossiers")}
            depot.synchroniser(cx, ClientReel(variables), source="pennylane")
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

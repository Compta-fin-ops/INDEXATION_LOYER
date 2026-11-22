"""Ligne de commande de la console.

    python -m console demo                       charge le portefeuille fictif (10 dossiers)
    python -m console serve [--port 8000]        lance la console web
    python -m console importer CLASSEUR.xlsx --dossier CODE [--variable-jeton PENNYLANE_TOKEN_CODE]
    python -m console synchroniser               collecte Pennylane en lecture seule
    python -m console ecarts [--gravite bloquant]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import db, depot, rapprochement as rp, simulation


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="console", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=str(db.CHEMIN_DEFAUT), help="Fichier SQLite de la console")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("demo", help="Charge le portefeuille fictif")
    s = sp.add_parser("serve", help="Lance la console web")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--hote", default="127.0.0.1", help="Laisser 127.0.0.1 tant que la console n'a pas d'authentification")
    i = sp.add_parser("importer", help="Importe le référentiel des baux d'un classeur d'indexation")
    i.add_argument("classeur")
    i.add_argument("--dossier", help="Code du dossier (défaut : dérivé de la raison sociale)")
    i.add_argument("--variable-jeton", default="", help="Nom de la variable d'environnement portant le jeton du dossier")
    sp.add_parser("synchroniser", help="Collecte les abonnements et factures dans Pennylane (lecture seule)")
    e = sp.add_parser("ecarts", help="Liste les écarts dans le terminal")
    e.add_argument("--gravite", choices=rp.GRAVITES)
    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        from .app import creer_app
        uvicorn.run(creer_app(args.base), host=args.hote, port=args.port)
        return 0
    cx = db.connecter(args.base)
    if args.cmd == "demo":
        print("Portefeuille fictif chargé :", simulation.charger_demo(cx, date.today()))
    elif args.cmd == "importer":
        n = depot.importer_classeur(cx, Path(args.classeur), args.dossier, args.variable_jeton)
        print(f"{n} bail/baux importé(s) depuis {args.classeur}")
    elif args.cmd == "synchroniser":
        from .pennylane_api import ClientReel
        variables = {r["code"]: r["variable_jeton"] for r in cx.execute("SELECT code, variable_jeton FROM dossiers")}
        for b in depot.synchroniser(cx, ClientReel(variables), source="pennylane"):
            print(f"  {b['dossier']:<12}", f"{b['abonnements']} abonnement(s), {b['factures']} facture(s)" if b["ok"]
                  else f"ÉCHEC : {b['message']}")
    elif args.cmd == "ecarts":
        for x in depot.charger(cx):
            a = rp.analyser_dossier(x["dossier"], x["baux"], x["abonnements"], x["factures"], date.today())
            for ec in a["ecarts"]:
                if not args.gravite or ec.gravite == args.gravite:
                    print(f"[{ec.gravite:<9}] {ec.dossier:<10} {ec.bail_code:<4} {ec.locataire[:22]:<22} {ec.titre}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

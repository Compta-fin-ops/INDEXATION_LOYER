"""Ligne de commande de la console.

    python -m console demo                       charge le portefeuille fictif (10 dossiers)
    python -m console serve [--port 8000]        lance la console web
    python -m console importer CLASSEUR.xlsx --dossier CODE [--variable-jeton PENNYLANE_TOKEN_CODE]
                                                 reprise d'un ancien classeur Excel, une fois par société
    python -m console synchroniser               collecte Pennylane en lecture seule
    python -m console sauvegarder                copie horodatée de la base dans data/sauvegardes
    python -m console utilisateur creer          crée un compte (le premier : un administrateur)
    python -m console utilisateur lister
    python -m console utilisateur mot-de-passe IDENTIFIANT     attribue un nouveau mot de passe
    python -m console ecarts [--gravite bloquant]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import comptes, db, depot, rapprochement as rp, simulation


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="console", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=str(db.CHEMIN_DEFAUT), help="Fichier SQLite de la console")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("demo", help="Charge le portefeuille fictif")
    s = sp.add_parser("serve", help="Lance la console web")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--hote", default="127.0.0.1", help="Laisser 127.0.0.1 tant que la console n'a pas d'authentification")
    i = sp.add_parser("importer", help="Reprend les baux d'un ancien classeur d'indexation")
    i.add_argument("classeur")
    i.add_argument("--dossier", help="Code du dossier (défaut : dérivé de la raison sociale)")
    i.add_argument("--variable-jeton", default="", help="Nom de la variable d'environnement portant le jeton du dossier")
    sp.add_parser("synchroniser", help="Collecte les abonnements et factures dans Pennylane (lecture seule)")
    sv = sp.add_parser("sauvegarder", help="Copie horodatée de la base")
    sv.add_argument("--vers", default=str(db.RACINE / "data" / "sauvegardes"))
    u = sp.add_parser("utilisateur", help="Comptes de la console")
    u.add_argument("action", choices=("creer", "lister", "mot-de-passe"))
    u.add_argument("identifiant", nargs="?")
    u.add_argument("--nom")
    u.add_argument("--role", choices=comptes.ROLES)
    e = sp.add_parser("ecarts", help="Liste les écarts dans le terminal")
    e.add_argument("--gravite", choices=rp.GRAVITES)
    args = p.parse_args(argv)

    for flux in (sys.stdout, sys.stderr):                # console Windows : pas d'erreur sur un caractère non affichable
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(errors="replace")
    if args.cmd == "serve":
        if args.hote not in ("127.0.0.1", "localhost"):
            print("ATTENTION : la console sera joignable depuis d'autres postes, et sans HTTPS les mots de passe "
                  "circulent en clair sur le réseau. À réserver à un serveur placé derrière un accès chiffré.", file=sys.stderr)
        if db.connecter(args.base).execute("SELECT COUNT(*) AS n FROM utilisateurs").fetchone()["n"] == 0:
            print("Aucun compte : créer le premier avec  python -m console utilisateur creer", file=sys.stderr)
        import uvicorn
        from .app import creer_app
        uvicorn.run(creer_app(args.base), host=args.hote, port=args.port)
        return 0
    cx = db.connecter(args.base)
    if args.cmd == "demo":
        try:
            print("Portefeuille fictif chargé :", simulation.charger_demo(cx, date.today()))
        except RuntimeError as exc:                      # base réelle : on ne l'écrase jamais
            print(f"Refusé. {exc}", file=sys.stderr)
            return 2
    elif args.cmd == "utilisateur":
        return _utilisateur(cx, args)
    elif args.cmd == "sauvegarder":
        print("Sauvegarde :", db.sauvegarder(cx, Path(args.vers)))
        print("Les pièces jointes sont à part, dans le dossier « pieces » à côté de la base : à sauvegarder aussi.")
    elif args.cmd == "importer":
        n = depot.importer_classeur(cx, Path(args.classeur), args.dossier, args.variable_jeton)
        print(f"{n} bail/baux importé(s) depuis {args.classeur}")
    elif args.cmd == "synchroniser":
        from .pennylane_api import ClientReel
        variables = {r["code"]: r["variable_jeton"] for r in cx.execute(
            "SELECT code, variable_jeton FROM dossiers WHERE source_referentiel<>'portefeuille fictif'")}
        for b in (depot.synchroniser(cx, ClientReel(variables), sorted(variables), source="pennylane") if variables else []):
            print(f"  {b['dossier']:<12}", f"{b['abonnements']} abonnement(s), {b['factures']} facture(s)" if b["ok"]
                  else f"ÉCHEC : {b['message']}")
    elif args.cmd == "ecarts":
        for x in depot.charger(cx):
            a = rp.analyser_dossier(x["dossier"], x["baux"], x["abonnements"], x["factures"], date.today())
            for ec in a["ecarts"]:
                if not args.gravite or ec.gravite == args.gravite:
                    print(f"[{ec.gravite:<9}] {ec.dossier:<10} {ec.bail_code:<4} {ec.locataire[:22]:<22} {ec.titre}")
    return 0


def _mot_de_passe(identifiant: str) -> str | None:
    """Saisie masquée, deux fois. Le mot de passe ne passe jamais par un argument de commande."""
    import getpass
    for _ in range(3):
        premier = getpass.getpass(f"Mot de passe ({comptes.LONGUEUR_MIN} caractères au moins, la saisie ne s'affiche pas) : ")
        erreurs = comptes.controler_mot_de_passe(premier, identifiant)
        if erreurs:
            print("  " + " ".join(erreurs))
        elif getpass.getpass("Le même, une seconde fois : ") != premier:
            print("  Les deux saisies diffèrent.")
        else:
            return premier
    return None


def _utilisateur(cx, args) -> int:
    if args.action == "lister":
        for r in cx.execute("SELECT * FROM utilisateurs ORDER BY nom"):
            print(f"  {r['identifiant']:<28} {r['role']:<15} {'actif' if r['actif'] else 'désactivé':<10} {r['nom']}")
        return 0
    identifiant = comptes.normaliser(args.identifiant or input("Identifiant (sans espace, par exemple prenom.nom) : "))
    existe = cx.execute("SELECT id FROM utilisateurs WHERE identifiant=?", (identifiant,)).fetchone()
    if args.action == "mot-de-passe":
        if existe is None:
            print(f"Compte inconnu : {identifiant}", file=sys.stderr)
            return 2
        mdp = _mot_de_passe(identifiant)
        if mdp is None:
            return 2
        comptes.changer_mot_de_passe(cx, existe["id"], mdp)
        print(f"Mot de passe de {identifiant} changé ; ses sessions ouvertes sont fermées.")
        return 0
    premier = cx.execute("SELECT COUNT(*) AS n FROM utilisateurs").fetchone()["n"] == 0
    nom = args.nom or input("Nom affiché : ").strip()
    role = args.role or ("administrateur" if premier else
                         (input(f"Rôle ({', '.join(comptes.ROLES)}) [lecture] : ").strip() or "lecture"))
    mdp = _mot_de_passe(identifiant)
    if mdp is None:
        return 2
    try:
        comptes.creer(cx, identifiant, nom, role, mdp)
    except ValueError as exc:
        print(f"Refusé. {exc}", file=sys.stderr)
        return 2
    print(f"Compte créé : {identifiant}, rôle {role}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

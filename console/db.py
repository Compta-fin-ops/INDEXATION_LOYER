"""Base SQLite de la console : dossiers, baux, décisions de révision, pièces jointes, historique,
abonnements et factures collectés. Les baux vivent ici : la base est la source de vérité, à sauvegarder."""
from __future__ import annotations

import sqlite3
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
CHEMIN_DEFAUT = RACINE / "data" / "console.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dossiers (
    code TEXT PRIMARY KEY,
    nom TEXT NOT NULL,
    siren TEXT DEFAULT '',
    variable_jeton TEXT DEFAULT '',          -- nom de la variable d'environnement, jamais le jeton
    derniere_synchro TEXT,
    source_referentiel TEXT DEFAULT '',      -- console | nom du classeur importé | portefeuille fictif
    forme TEXT DEFAULT '',
    adresse TEXT DEFAULT '',                 -- en-tête des courriers
    signataire TEXT DEFAULT '',
    qualite_signataire TEXT DEFAULT 'Gérant',
    ville_signature TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS baux (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dossier TEXT NOT NULL REFERENCES dossiers(code) ON DELETE CASCADE,
    code TEXT NOT NULL,
    local TEXT DEFAULT '',
    locataire TEXT DEFAULT '',
    customer_id INTEGER,                     -- client Pennylane
    subscription_id INTEGER,                 -- abonnement rattaché explicitement, si connu
    indice TEXT DEFAULT 'ILC',
    periodicite TEXT DEFAULT 'Mensuelle',    -- Mensuelle, Trimestrielle, Semestrielle, Annuelle
    tva_pct REAL DEFAULT 20,
    date_effet TEXT,
    date_fin TEXT,
    loyer_initial_annuel REAL DEFAULT 0,
    loyer_attendu_annuel REAL DEFAULT 0,     -- loyer en vigueur ; recalculé à la lecture si calcule = 1
    charges_annuelles REAL DEFAULT 0,
    date_derniere_revision TEXT,
    date_prochaine_revision TEXT,
    loyer_prochain_annuel REAL,              -- loyer après la prochaine révision, si l'indice est publié
    date_revision_suivante TEXT,             -- révision d'après : terme de la période suivante
    statut_revision TEXT DEFAULT '',         -- ✔ Calculable, ⚠ Indice attendu, ❄ Gelée, À venir…
    revisions_non_appliquees INTEGER DEFAULT 0,
    -- paramètres du bail : quand calcule = 1, la console recalcule le loyer et les dates à chaque lecture
    calcule INTEGER DEFAULT 0,
    type_bail TEXT DEFAULT 'Commercial',
    adresse_locataire TEXT DEFAULT '',
    trimestre_base TEXT DEFAULT '',          -- indice de départ, AAAA-Tn
    periodicite_revision_ans INTEGER DEFAULT 1,
    methode TEXT DEFAULT 'Chaînée',
    plafond_annuel_pct REAL,
    duree_ans INTEGER,
    date_sortie TEXT,                        -- départ du locataire : arrête les révisions
    notes TEXT DEFAULT '',
    -- point de reprise d'un bail en cours : la console ne recalcule rien avant cette date
    reprise_date TEXT,                       -- date de la dernière révision appliquée
    reprise_loyer_annuel REAL,               -- loyer annuel HT en vigueur à cette date
    reprise_trimestre TEXT,                  -- trimestre de l'indice appliqué à cette révision
    UNIQUE (dossier, code)
);
CREATE TABLE IF NOT EXISTS decisions (             -- décision du bailleur sur une échéance de révision
    bail_id INTEGER NOT NULL REFERENCES baux(id) ON DELETE CASCADE,
    numero INTEGER NOT NULL,
    decision TEXT NOT NULL DEFAULT 'Appliquer',
    commentaire TEXT DEFAULT '',
    courrier_le TEXT,
    auteur TEXT DEFAULT '',
    horodatage TEXT,
    PRIMARY KEY (bail_id, numero)
);
CREATE TABLE IF NOT EXISTS pieces (                -- bail signé, avenants, courriers ; le fichier est sur disque
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bail_id INTEGER NOT NULL REFERENCES baux(id) ON DELETE RESTRICT,
    nature TEXT NOT NULL DEFAULT 'Bail',
    nom TEXT NOT NULL,
    type_mime TEXT NOT NULL,
    taille INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    horodatage TEXT NOT NULL,
    auteur TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS historique (            -- qui a changé quoi, jamais purgé
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage TEXT NOT NULL,
    auteur TEXT DEFAULT '',
    dossier TEXT DEFAULT '',
    bail_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS abonnements (
    id INTEGER NOT NULL,
    dossier TEXT NOT NULL REFERENCES dossiers(code) ON DELETE CASCADE,
    customer_id INTEGER,
    label TEXT DEFAULT '',
    statut TEXT DEFAULT '',                  -- draft, not_started, in_progress, finished, stopped, pending
    mode TEXT DEFAULT '',
    debut TEXT,
    fin TEXT,                                -- date de la dernière occurrence (finish)
    prochaine_occurrence TEXT,
    occurrence_precedente TEXT,
    type_regle TEXT DEFAULT 'monthly',
    intervalle INTEGER DEFAULT 1,
    montant_ht REAL DEFAULT 0,               -- par occurrence
    PRIMARY KEY (dossier, id)
);
CREATE TABLE IF NOT EXISTS factures (
    id INTEGER NOT NULL,
    dossier TEXT NOT NULL REFERENCES dossiers(code) ON DELETE CASCADE,
    customer_id INTEGER,
    subscription_id INTEGER,
    numero TEXT DEFAULT '',
    libelle TEXT DEFAULT '',
    date TEXT,
    echeance TEXT,
    montant_ht REAL DEFAULT 0,
    montant_ttc REAL DEFAULT 0,
    restant_du REAL DEFAULT 0,
    statut TEXT DEFAULT '',                  -- draft, upcoming, late, paid, partially_paid, cancelled…
    PRIMARY KEY (dossier, id)
);
CREATE TABLE IF NOT EXISTS synchros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage TEXT NOT NULL,
    dossier TEXT,
    source TEXT,
    abonnements INTEGER DEFAULT 0,
    factures INTEGER DEFAULT 0,
    message TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS envois (                 -- journal des créations d'abonnement, jamais purgé
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage TEXT NOT NULL,
    dossier TEXT NOT NULL,
    bail_id INTEGER,
    bail_code TEXT DEFAULT '',
    locataire TEXT DEFAULT '',
    cle TEXT NOT NULL,                               -- dossier|bail|date de début
    empreinte TEXT NOT NULL,                         -- SHA-256 du corps envoyé
    corps TEXT NOT NULL,
    mode TEXT NOT NULL,                              -- simulation | pennylane
    etat TEXT NOT NULL,                              -- en cours | créé | refusé | incertain
    statut_http INTEGER,
    subscription_id INTEGER,
    statut_abonnement TEXT DEFAULT '',
    reponse TEXT DEFAULT '',
    auteur TEXT DEFAULT '',
    nature TEXT DEFAULT 'abonnement',                -- abonnement | facture
    facture_id INTEGER
);
CREATE TABLE IF NOT EXISTS reglages (              -- réglages portés par la base elle-même
    cle TEXT PRIMARY KEY,
    valeur TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (               -- fichier analysé, en attente de confirmation
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage TEXT NOT NULL,
    auteur TEXT DEFAULT '',
    nom_fichier TEXT NOT NULL,
    empreinte TEXT NOT NULL,                         -- SHA-256 du fichier déposé
    lignes TEXT NOT NULL,                            -- lignes lues, en JSON
    etat TEXT NOT NULL DEFAULT 'analysé'             -- analysé | importé
);
CREATE TABLE IF NOT EXISTS utilisateurs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifiant TEXT NOT NULL UNIQUE,
    nom TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'lecture',            -- lecture | gestion | validation | administrateur
    mot_de_passe TEXT NOT NULL,                      -- empreinte scrypt salée, jamais le mot de passe
    doit_changer INTEGER DEFAULT 0,                  -- mot de passe provisoire, à changer à la connexion
    actif INTEGER DEFAULT 1,
    echecs INTEGER DEFAULT 0,
    bloque_jusqu TEXT,
    cree_le TEXT,
    derniere_connexion TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    empreinte TEXT PRIMARY KEY,                      -- SHA-256 du jeton de session, jamais le jeton
    utilisateur_id INTEGER NOT NULL REFERENCES utilisateurs(id) ON DELETE CASCADE,
    cree_le TEXT NOT NULL,
    expire_le TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_envois_cle ON envois(cle);
CREATE INDEX IF NOT EXISTS ix_pieces_bail ON pieces(bail_id);
CREATE INDEX IF NOT EXISTS ix_historique_bail ON historique(bail_id);
CREATE INDEX IF NOT EXISTS ix_baux_dossier ON baux(dossier);
CREATE INDEX IF NOT EXISTS ix_abo_client ON abonnements(dossier, customer_id);
CREATE INDEX IF NOT EXISTS ix_fac_client ON factures(dossier, customer_id);
"""


MIGRATIONS = {
    "dossiers": (("forme", "TEXT DEFAULT ''"), ("adresse", "TEXT DEFAULT ''"), ("signataire", "TEXT DEFAULT ''"),
                 ("qualite_signataire", "TEXT DEFAULT 'Gérant'"), ("ville_signature", "TEXT DEFAULT ''")),
    "baux": (("loyer_prochain_annuel", "REAL"), ("date_revision_suivante", "TEXT"), ("calcule", "INTEGER DEFAULT 0"),
             ("type_bail", "TEXT DEFAULT 'Commercial'"), ("adresse_locataire", "TEXT DEFAULT ''"),
             ("trimestre_base", "TEXT DEFAULT ''"), ("periodicite_revision_ans", "INTEGER DEFAULT 1"),
             ("methode", "TEXT DEFAULT 'Chaînée'"), ("plafond_annuel_pct", "REAL"), ("duree_ans", "INTEGER"),
             ("date_sortie", "TEXT"), ("notes", "TEXT DEFAULT ''"),
             ("reprise_date", "TEXT"), ("reprise_loyer_annuel", "REAL"), ("reprise_trimestre", "TEXT")),
    "envois": (("auteur", "TEXT DEFAULT ''"), ("nature", "TEXT DEFAULT 'abonnement'"), ("facture_id", "INTEGER")),
}


def connecter(chemin: Path | str = CHEMIN_DEFAUT) -> sqlite3.Connection:
    if str(chemin) != ":memory:":
        Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(chemin), check_same_thread=False)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    cx.executescript(SCHEMA)
    for table, ajouts in MIGRATIONS.items():         # base créée par une version antérieure
        colonnes = {r["name"] for r in cx.execute(f"PRAGMA table_info({table})")}
        for nom, type_ in ajouts:
            if nom not in colonnes:
                cx.execute(f"ALTER TABLE {table} ADD COLUMN {nom} {type_}")
    cx.commit()
    return cx


def vider(cx: sqlite3.Connection) -> None:
    """Vide le portefeuille. Refuse dès qu'un dossier réel est présent : les baux n'existent plus qu'ici."""
    reels = cx.execute("SELECT COUNT(*) AS n FROM dossiers WHERE source_referentiel<>'portefeuille fictif'").fetchone()["n"]
    if reels:
        raise RuntimeError(f"La base contient {reels} dossier(s) réel(s) : elle ne peut pas être vidée. "
                           "Charger la démonstration dans une autre base, avec l'option --base.")
    for table in ("factures", "abonnements", "decisions", "pieces", "baux", "synchros", "dossiers"):
        cx.execute(f"DELETE FROM {table}")
    cx.commit()


def sauvegarder(cx: sqlite3.Connection, dossier: Path) -> Path:
    """Copie cohérente de la base, horodatée, par l'API de sauvegarde de SQLite."""
    from datetime import datetime
    dossier.mkdir(parents=True, exist_ok=True)
    cible = dossier / f"console-{datetime.now():%Y%m%d-%H%M%S}.sqlite"
    copie = sqlite3.connect(str(cible))
    try:
        cx.backup(copie)
    finally:
        copie.close()
    return cible


ENVIRONNEMENTS = ("production", "test")


def environnement(cx: sqlite3.Connection) -> str:
    """Nature de la base, inscrite dans la base elle-même : elle la suit où qu'on la copie. Production par défaut."""
    r = cx.execute("SELECT valeur FROM reglages WHERE cle='environnement'").fetchone()
    return r["valeur"] if r and r["valeur"] in ENVIRONNEMENTS else "production"


def definir_environnement(cx: sqlite3.Connection, valeur: str) -> None:
    if valeur not in ENVIRONNEMENTS:
        raise ValueError(f"Environnement inconnu : {valeur}")
    cx.execute("INSERT INTO reglages (cle, valeur) VALUES ('environnement', ?) ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
               (valeur,))
    cx.commit()


def copier_vers_test(source: Path, cible: Path, remplacer: bool = False) -> dict:
    """Copie une base de production vers une base de test, pièces jointes comprises.

    La copie est marquée « test » et ses sessions sont fermées. Une base de production n'est jamais
    écrasée : la cible doit être absente, ou être une base de test avec `remplacer`."""
    import os
    import shutil
    source, cible = Path(source), Path(cible)
    if not source.exists():
        raise RuntimeError(f"Base source introuvable : {source}")
    if source.resolve() == cible.resolve():
        raise RuntimeError("La base de test ne peut pas être la base de production elle-même.")
    cx = connecter(source)
    if environnement(cx) != "production":
        raise RuntimeError("La base source est déjà une base de test.")
    if cible.exists():
        existante = connecter(cible)
        nature = environnement(existante)
        existante.close()
        if nature != "test":
            raise RuntimeError(f"Refusé : {cible} est une base de production, elle ne sera jamais écrasée.")
        if not remplacer:
            raise RuntimeError(f"La base de test {cible} existe déjà. Pour la remplacer par une copie fraîche : option --remplacer.")
    cible.parent.mkdir(parents=True, exist_ok=True)
    provisoire = cible.with_name(cible.name + ".nouvelle")
    if provisoire.exists():
        provisoire.unlink()
    copie = sqlite3.connect(str(provisoire))
    try:
        cx.backup(copie)
        copie.execute("INSERT INTO reglages (cle, valeur) VALUES ('environnement', 'test') "
                      "ON CONFLICT(cle) DO UPDATE SET valeur='test'")
        copie.execute("DELETE FROM sessions")
        copie.execute("INSERT INTO historique (horodatage, auteur, action, detail) VALUES (datetime('now', 'localtime'), 'poste local', "
                      "'Base de test créée', ?)", (f"copie de {source.name}",))
        copie.commit()
        bilan = {t: copie.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("dossiers", "baux", "pieces", "utilisateurs")}
    finally:
        copie.close()
        cx.close()
    try:
        os.replace(provisoire, cible)
    except PermissionError as exc:                       # Windows : fichier ouvert par une console de test en marche
        provisoire.unlink(missing_ok=True)
        raise RuntimeError("La base de test est en cours d'utilisation : fermer la console de test, puis recommencer.") from exc
    pieces_source, pieces_cible = source.parent / "pieces", cible.parent / "pieces"
    if pieces_source.is_dir():                           # fichiers nommés par leur empreinte : on ajoute, on n'écrase ni ne retire
        shutil.copytree(pieces_source, pieces_cible, dirs_exist_ok=True)
    return {"cible": cible, **bilan}

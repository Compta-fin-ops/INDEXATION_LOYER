"""Base SQLite de la console : dossiers, baux attendus, abonnements et factures collectés."""
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
    source_referentiel TEXT DEFAULT ''
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
    loyer_attendu_annuel REAL DEFAULT 0,     -- loyer en vigueur selon le classeur d'indexation
    charges_annuelles REAL DEFAULT 0,
    date_derniere_revision TEXT,
    date_prochaine_revision TEXT,
    loyer_prochain_annuel REAL,              -- loyer après la prochaine révision, si l'indice est publié
    date_revision_suivante TEXT,             -- révision d'après : terme de la période suivante
    statut_revision TEXT DEFAULT '',         -- ✔ Calculable, ⚠ Indice attendu, ❄ Gelée, À venir…
    revisions_non_appliquees INTEGER DEFAULT 0,
    UNIQUE (dossier, code)
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
    reponse TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_envois_cle ON envois(cle);
CREATE INDEX IF NOT EXISTS ix_baux_dossier ON baux(dossier);
CREATE INDEX IF NOT EXISTS ix_abo_client ON abonnements(dossier, customer_id);
CREATE INDEX IF NOT EXISTS ix_fac_client ON factures(dossier, customer_id);
"""


def connecter(chemin: Path | str = CHEMIN_DEFAUT) -> sqlite3.Connection:
    if str(chemin) != ":memory:":
        Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(chemin), check_same_thread=False)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    cx.executescript(SCHEMA)
    colonnes = {r["name"] for r in cx.execute("PRAGMA table_info(baux)")}
    for nom, type_ in (("loyer_prochain_annuel", "REAL"), ("date_revision_suivante", "TEXT")):
        if nom not in colonnes:                      # base créée par une version antérieure
            cx.execute(f"ALTER TABLE baux ADD COLUMN {nom} {type_}")
    cx.commit()
    return cx


def vider(cx: sqlite3.Connection) -> None:
    for table in ("factures", "abonnements", "baux", "synchros", "dossiers"):
        cx.execute(f"DELETE FROM {table}")
    cx.commit()

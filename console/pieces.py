"""Pièces jointes d'un bail : bail signé, avenants, courriers.

Le fichier est rangé sur disque sous son empreinte SHA-256, jamais sous le nom fourni par
l'utilisateur ; la base ne garde que la description. Rien n'est supprimé par la console.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

from . import referentiel

TAILLE_MAX = 25 * 1024 * 1024
# extension -> (type MIME servi, signatures possibles en tête de fichier)
TYPES = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
}


class PieceRefusee(ValueError):
    pass


def chemin_fichier(stockage: Path, sha256: str) -> Path:
    return Path(stockage) / sha256[:2] / sha256


def deposer(cx: sqlite3.Connection, stockage: Path, bail: dict, nom: str, contenu: bytes, nature: str = "Bail") -> int:
    nom = Path(nom or "").name.strip()
    extension = Path(nom).suffix.lower()
    if nature not in referentiel.NATURES_PIECE:
        raise PieceRefusee(f"Nature de pièce inconnue : {nature}")
    if extension not in TYPES:
        raise PieceRefusee("Format refusé : déposer un PDF, une image PNG ou JPEG, ou un document Word (.docx).")
    if not contenu:
        raise PieceRefusee("Fichier vide.")
    if len(contenu) > TAILLE_MAX:
        raise PieceRefusee(f"Fichier trop lourd : {len(contenu) / 1e6:.1f} Mo, pour {TAILLE_MAX // (1024 * 1024)} Mo au plus.")
    type_mime, signatures = TYPES[extension]
    if not any(contenu.startswith(s) for s in signatures):
        raise PieceRefusee(f"Le contenu du fichier ne correspond pas à son extension {extension}.")
    sha = hashlib.sha256(contenu).hexdigest()
    deja = cx.execute("SELECT id FROM pieces WHERE bail_id=? AND sha256=?", (bail["id"], sha)).fetchone()
    if deja:
        raise PieceRefusee("Cette pièce est déjà jointe à ce bail.")
    cible = chemin_fichier(stockage, sha)
    if not cible.exists():
        cible.parent.mkdir(parents=True, exist_ok=True)
        provisoire = cible.with_suffix(".part")
        provisoire.write_bytes(contenu)
        provisoire.replace(cible)
    cur = cx.execute("""INSERT INTO pieces (bail_id, nature, nom, type_mime, taille, sha256, horodatage, auteur)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (bail["id"], nature, nom, type_mime, len(contenu), sha,
                      datetime.now().isoformat(timespec="seconds"), referentiel.utilisateur()))
    referentiel.tracer(cx, "Pièce jointe", f"{nature} : {nom} ({len(contenu) / 1024:.0f} Ko)", bail["dossier"], bail["id"])
    cx.commit()
    return cur.lastrowid


def du_bail(cx: sqlite3.Connection, bail_id: int) -> list[dict]:
    return [dict(r) for r in cx.execute("SELECT * FROM pieces WHERE bail_id=? ORDER BY horodatage DESC, id DESC", (bail_id,))]

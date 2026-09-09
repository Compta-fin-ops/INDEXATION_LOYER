"""Récupération des indices INSEE (BDM, service web SDMX) et cache local CSV.

Source primaire : ``https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/<idbank>[+<idbank>...]``
  - sans authentification, réponse SDMX-ML « StructureSpecificData »
  - paramètres utiles : ``startPeriod=2008-Q1``, ``lastNObservations=8``
  - documentation : https://www.insee.fr/fr/information/2862759

Source de secours : export CSV d'insee.fr (zip) ou fichier CSV déposé à la main
(``--fichier``), pour les postes sans accès direct à l'API.

Le cache ``data/indices/indices_insee.csv`` est en format long :
    serie;idbank;periode;valeur;statut_obs;date_maj_insee;source;date_extraction
Les lignes de source ``MANUEL`` ne sont jamais écrasées par une lecture INSEE,
sauf si l'INSEE fournit la même clé (serie, periode) : la valeur officielle prime.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import unicodedata
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from .periodes import periode_sdmx_vers_fr

log = logging.getLogger(__name__)

RACINE = Path(__file__).resolve().parent.parent
CHEMIN_CONFIG = RACINE / "config" / "series_insee.json"
CHEMIN_CACHE = RACINE / "data" / "indices" / "indices_insee.csv"

COLONNES_CACHE = [
    "serie", "idbank", "periode", "valeur", "statut_obs",
    "date_maj_insee", "source", "date_extraction",
]


@dataclass(frozen=True)
class Observation:
    serie: str            # ILC, ILAT, ICC, IRL
    idbank: str
    periode: str          # 2024-T2
    valeur: float
    statut_obs: str       # A (définitif), P (provisoire), ... tel que fourni par l'INSEE
    date_maj_insee: str   # LAST_UPDATE de la série
    source: str           # INSEE_SDMX | INSEE_CSV | MANUEL | FICTIF_DEMO
    date_extraction: str  # ISO date de l'extraction

    @property
    def cle(self) -> tuple[str, str]:
        return (self.serie, self.periode)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def charger_config(chemin: Path = CHEMIN_CONFIG) -> dict:
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def _sans_accents(texte: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn"
    ).lower()


# ---------------------------------------------------------------------------
# Appel du service SDMX
# ---------------------------------------------------------------------------
def url_sdmx(idbanks: Iterable[str], start_period: str | None = None,
             last_n: int | None = None, base_url: str | None = None) -> str:
    base_url = base_url or charger_config()["api"]["sdmx_base_url"]
    url = base_url + "+".join(idbanks)
    params = []
    if start_period:
        params.append(f"startPeriod={start_period}")
    if last_n:
        params.append(f"lastNObservations={last_n}")
    if params:
        url += "?" + "&".join(params)
    return url


def telecharger(url: str, timeout: int = 60, accept: str = "application/xml") -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "indexation-loyer/0.1 (+cabinet expertise comptable; contact via dépôt)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # respecte HTTPS_PROXY
        return resp.read()


def parser_sdmx(xml_bytes: bytes, config: dict | None = None,
                date_extraction: date | None = None) -> list[Observation]:
    """Parse une réponse SDMX-ML StructureSpecificData de la BDM.

    Chaque ``<Series>`` porte des attributs (IDBANK, TITLE_FR, LAST_UPDATE, ...)
    et des enfants ``<Obs TIME_PERIOD="2024-Q2" OBS_VALUE="132.63" OBS_STATUS="A"/>``.
    Les espaces de noms XML varient selon la version : on compare le nom local.
    """
    config = config or charger_config()
    par_idbank = {v["idbank"]: (code, v) for code, v in config["series"].items()}
    date_extraction = (date_extraction or date.today()).isoformat()

    racine = ET.fromstring(xml_bytes)
    observations: list[Observation] = []
    for series in racine.iter():
        if _nom_local(series.tag) != "Series":
            continue
        idbank = series.get("IDBANK", "")
        if idbank not in par_idbank:
            log.warning("Série %s ignorée : idbank absent de la configuration", idbank)
            continue
        code, meta = par_idbank[idbank]
        titre = series.get("TITLE_FR", "")
        if meta.get("mot_cle_titre") and _sans_accents(meta["mot_cle_titre"]) not in _sans_accents(titre):
            raise ValueError(
                f"Garde-fou : le titre INSEE de l'idbank {idbank} est {titre!r}, "
                f"il ne contient pas {meta['mot_cle_titre']!r}. Vérifier config/series_insee.json."
            )
        last_update = series.get("LAST_UPDATE", "")
        for obs in series:
            if _nom_local(obs.tag) != "Obs":
                continue
            brut = obs.get("OBS_VALUE")
            if brut in (None, "", "NaN"):
                continue
            observations.append(Observation(
                serie=code,
                idbank=idbank,
                periode=periode_sdmx_vers_fr(obs.get("TIME_PERIOD", "")),
                valeur=float(brut.replace(",", ".")),
                statut_obs=obs.get("OBS_STATUS", ""),
                date_maj_insee=last_update,
                source="INSEE_SDMX",
                date_extraction=date_extraction,
            ))
    return observations


def _nom_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def recuperer_series(codes: Iterable[str] | None = None, start_period: str | None = "2000-Q1",
                     config: dict | None = None) -> list[Observation]:
    """Interroge la BDM pour les séries demandées (toutes par défaut)."""
    config = config or charger_config()
    codes = list(codes) if codes else list(config["series"])
    idbanks = [config["series"][c]["idbank"] for c in codes]
    url = url_sdmx(idbanks, start_period=start_period, base_url=config["api"]["sdmx_base_url"])
    log.info("Appel INSEE : %s", url)
    try:
        contenu = telecharger(url)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"INSEE a répondu {e.code} pour {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Impossible de joindre l'INSEE ({e.reason}). "
                           "Réessayer, ou utiliser --fichier avec un export CSV insee.fr.") from e
    return parser_sdmx(contenu, config=config)


# ---------------------------------------------------------------------------
# Secours : export CSV d'insee.fr
# ---------------------------------------------------------------------------
_RE_PERIODE_LIGNE = re.compile(r"^(\d{4}-[QT][1-4])\s*[;,]\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:[;,]\s*([A-Z]*))?")


def parser_csv_insee(contenu: bytes, config: dict | None = None,
                     serie_forcee: str | None = None,
                     date_extraction: date | None = None) -> list[Observation]:
    """Parse l'export « télécharger (CSV) » d'une série insee.fr (zip ou csv).

    Format observé (à contrôler sur un export récent, l'INSEE peut le faire évoluer) :
    quelques lignes d'en-tête (``Libellé;...``, ``idBank;001532540``, ``Dernière mise à jour;...``),
    puis une ligne par période ``2024-Q2;132.63;A``.
    Le parseur ne s'appuie que sur la ligne ``idBank`` et sur le motif ``AAAA-Qn;valeur``.
    """
    config = config or charger_config()
    par_idbank = {v["idbank"]: code for code, v in config["series"].items()}
    date_extraction = (date_extraction or date.today()).isoformat()

    if contenu[:2] == b"PK":  # zip
        with zipfile.ZipFile(io.BytesIO(contenu)) as z:
            noms = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not noms:
                raise ValueError("Archive INSEE sans fichier CSV")
            contenu = z.read(noms[0])
    texte = contenu.decode("utf-8-sig", errors="replace")

    idbank, code, maj = "", serie_forcee, ""
    obs: list[Observation] = []
    for ligne in texte.splitlines():
        l = ligne.strip()
        if l.lower().startswith("idbank"):
            idbank = re.sub(r"\D", "", l.split(";", 1)[-1])
            code = code or par_idbank.get(idbank)
            continue
        if _sans_accents(l).startswith("derniere mise a jour"):
            maj = l.split(";", 1)[-1].strip()
            continue
        m = _RE_PERIODE_LIGNE.match(l)
        if m and code:
            obs.append(Observation(
                serie=code, idbank=idbank or config["series"][code]["idbank"],
                periode=periode_sdmx_vers_fr(m.group(1).replace("T", "Q")),
                valeur=float(m.group(2).replace(",", ".")),
                statut_obs=m.group(3) or "", date_maj_insee=maj,
                source="INSEE_CSV", date_extraction=date_extraction,
            ))
    if not obs:
        raise ValueError("Aucune observation reconnue dans le fichier CSV INSEE "
                         "(préciser --serie ILC|ILAT|ICC|IRL si la ligne idBank manque).")
    return obs


# ---------------------------------------------------------------------------
# Cache CSV
# ---------------------------------------------------------------------------
def lire_cache(chemin: Path = CHEMIN_CACHE) -> list[Observation]:
    if not chemin.exists():
        return []
    with open(chemin, encoding="utf-8", newline="") as f:
        lecteur = csv.DictReader(f, delimiter=";")
        return [Observation(
            serie=r["serie"], idbank=r["idbank"], periode=r["periode"],
            valeur=float(r["valeur"]), statut_obs=r.get("statut_obs", ""),
            date_maj_insee=r.get("date_maj_insee", ""), source=r.get("source", ""),
            date_extraction=r.get("date_extraction", ""),
        ) for r in lecteur]


def ecrire_cache(observations: Iterable[Observation], chemin: Path = CHEMIN_CACHE) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tri = sorted(observations, key=lambda o: (o.serie, o.periode))
    with open(chemin, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLONNES_CACHE, delimiter=";")
        w.writeheader()
        for o in tri:
            d = asdict(o)
            d["valeur"] = repr(o.valeur) if o.valeur != int(o.valeur) else str(int(o.valeur))
            w.writerow(d)


def fusionner(existant: Iterable[Observation], nouveau: Iterable[Observation]) -> list[Observation]:
    """Fusion clé (serie, periode) : la donnée INSEE écrase tout ; une saisie
    MANUEL n'écrase qu'une absence ou une autre saisie MANUEL/FICTIF."""
    resultat: dict[tuple[str, str], Observation] = {o.cle: o for o in existant}
    for o in nouveau:
        courant = resultat.get(o.cle)
        if courant is None or o.source.startswith("INSEE") or not courant.source.startswith("INSEE"):
            resultat[o.cle] = o
    return list(resultat.values())


def mettre_a_jour_cache(codes: Iterable[str] | None = None, chemin: Path = CHEMIN_CACHE,
                        fichier_csv: Path | None = None, serie_forcee: str | None = None,
                        start_period: str | None = "2000-Q1") -> tuple[list[Observation], list[Observation]]:
    """Point d'entrée de la commande ``fetch-indices``.

    Retourne (observations fusionnées, observations nouvelles ou modifiées).
    """
    existant = lire_cache(chemin)
    if fichier_csv:
        nouveau = parser_csv_insee(Path(fichier_csv).read_bytes(), serie_forcee=serie_forcee)
    else:
        nouveau = recuperer_series(codes, start_period=start_period)
    avant = {o.cle: o.valeur for o in existant}
    delta = [o for o in nouveau if avant.get(o.cle) != o.valeur]
    fusion = fusionner(existant, nouveau)
    ecrire_cache(fusion, chemin)
    return fusion, delta


def dernieres_valeurs(observations: Iterable[Observation]) -> dict[str, Observation]:
    """Dernière période disponible par série."""
    derniers: dict[str, Observation] = {}
    for o in observations:
        if o.serie not in derniers or o.periode > derniers[o.serie].periode:
            derniers[o.serie] = o
    return derniers

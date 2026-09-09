# INDEXATION_LOYER – suivi des réindexations de loyers par société

**Thèse.** Un classeur Excel par société bailleresse, alimenté automatiquement par les indices INSEE
(ILC, ILAT, ICC, IRL), qui calcule par formules vivantes le calendrier des révisions de chaque local,
laisse le client décider échéance par échéance (appliquer ou geler, avec une consigne libre), signale
ce qui doit être facturé, produit les courriers PDF d'information des locataires et prépare le corps
des abonnements de facturation Pennylane. Le cabinet saisit les baux une fois ; le reste se met à jour
d'une commande.

```
   INSEE – BDM (service SDMX, sans clé)                 GitHub Actions (hebdo)
   bdm.insee.fr/series/sdmx/data/SERIES_BDM/<idbank>   ──►  fetch-indices  ──►  data/indices/indices_insee.csv (cache, versionné)
                                                                                         │
                                                                                         ▼ refresh --tous
   ┌──────────────────────────────── suivi/<SOCIETE>_indexation_loyers.xlsx ────────────────────────────────┐
   │ Baux (saisie)  ─►  Révisions (formules : indice N / indice N-1 × loyer)  ─►  Alertes (loyer actuel,    │
   │ Indices (copie du cache)                                                       prochaine échéance,     │
   │                                                                                action)                 │
   │      Décision / Consigne (saisie par échéance)               └─►  Pennylane (aperçu JSON par bail)     │
   └────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                        │                                │
                                  courriers <société>   ▼                                ▼ pennylane <société> [--push]
                        out/courriers/<société>/*.pdf (un PDF par échéance)   out/*.json (dry-run) ──► POST /billing_subscriptions
```

## Ce que fait la version 0.1

| Brique | État | Détail |
|---|---|---|
| Récupération INSEE | ✔ codée, **non testée en réel** depuis l'environnement de génération (accès réseau à insee.fr bloqué) | Service SDMX de la BDM, sans authentification. Garde-fou : le titre renvoyé par l'INSEE doit contenir le mot-clé attendu, sinon rejet. Secours : import d'un export CSV d'insee.fr (`--fichier`). |
| Cache d'indices | ✔ | `data/indices/indices_insee.csv`, format long, fusion INSEE > saisie manuelle. |
| Classeur par société | ✔ | 7 feuilles, formules Excel natives (SUMIFS, INDEX/MATCH, EDATE, MAXIFS/MINIFS), validations de données, mise en forme conditionnelle. |
| Calendrier des révisions | ✔ | Méthode chaînée ou base fixe, périodicité 1/2/3 ans, plafond annuel optionnel, baisse appliquée si l'indice recule. |
| Décision par échéance | ✔ | Colonne « Décision » : Appliquer, Geler sans rattrapage, Geler avec rattrapage. Colonne « Consigne (à faire) » libre, reprise dans Alertes. |
| Courriers locataires | ✔ | Un PDF par échéance à notifier (révision appliquée, gel, ou attente d'indice), texte paramétrable dans `config/courrier_modele.json`, marquage de la date d'envoi dans le classeur. |
| Rafraîchissement | ✔ | Relit la feuille Baux (saisies du cabinet), régénère les feuilles calculées, conserve toutes les colonnes jaunes (Décision, Consigne, Appliqué, dates, Commentaire), crée un `.bak`. |
| Contre-épreuve | ✔ | Un moteur Python indépendant recalcule tout ; un test recalcule le classeur avec LibreOffice et compare cellule à cellule (30 échéances de démo dont 2 gels, 0 écart). |
| Pennylane | ◐ dry-run seulement | Corps JSON par bail. **Schéma à valider** contre la documentation (non consultable depuis ici) ; l'envoi est verrouillé tant que `schema_valide` est `false`. |

## Installation et premier usage

```bash
pip install -r requirements.txt
python -m indexation_loyer fetch-indices                      # interroge l'INSEE, remplit le cache
python -m indexation_loyer init "SCI DES HALLES" --siren 123456789 --baux config/baux_modele.csv
#   -> suivi/SCI_DES_HALLES_indexation_loyers.xlsx : compléter la feuille Baux dans Excel
python -m indexation_loyer refresh --tous                     # après chaque fetch-indices ou modification des baux
python -m indexation_loyer courriers "SCI DES HALLES"         # PDF pour chaque échéance à notifier (--marquer inscrit la date d'envoi)
python -m indexation_loyer pennylane "SCI DES HALLES"         # écrit out/*.json, n'envoie rien
python -m indexation_loyer demo                               # classeur de démonstration à valeurs FICTIVES
```

Sans accès direct à l'API depuis le poste : télécharger la série sur
`https://www.insee.fr/fr/statistiques/serie/001532540` (bouton CSV) puis
`python -m indexation_loyer fetch-indices --fichier <export.zip>`.

## Le classeur

| Feuille | Rôle | Qui écrit |
|---|---|---|
| Lisez-moi | Mode d'emploi, derniers indices connus, points de vigilance | script |
| Société | Raison sociale, SIREN, régime TVA, identifiant Pennylane | cabinet |
| Baux | Un local / bail par ligne. Colonne **Contrôles** : « OK » ou liste des anomalies (indice de base absent, doublon d'ID…) | cabinet (cellules jaunes) |
| Indices | Copie du cache : série, trimestre `AAAA-Tn`, valeur, statut A/P, variation annuelle | script |
| Révisions | Une ligne par bail × échéance : trimestres de référence, indices, coefficient, **Décision**, loyer brut, plafond, loyer retenu, mensuel, par échéance, TVA, TTC, **statut**, **Consigne (à faire)** | script ; cabinet pour Décision, Consigne, Appliqué ?, dates, Commentaire |
| Alertes | Par bail : loyer actuel, prochaine révision, trimestre attendu, indice publié ?, révisions calculables non facturées, révisions gelées, **action**, consigne de la prochaine révision | script |
| Pennylane | Par bail : customer_id, libellé, prix unitaire HT à l'échéance, code TVA, JSON d'aperçu | script |

Statuts de la feuille Révisions :

| Statut | Signification | Action |
|---|---|---|
| ✔ Calculable | Date de révision passée et indice publié | Facturer le nouveau loyer, cocher « Appliqué ? » |
| ❄ Gelée | Le bailleur a décidé de ne pas appliquer la révision (colonne Décision) | Loyer inchangé ; courrier d'information au locataire |
| ⚠ Indice attendu | Date de révision passée, indice pas encore publié | Attendre la publication INSEE (fin mars / juin / septembre / décembre) |
| Indice connu – à venir | Indice publié, date de révision future | Anticiper l'avenant de facturation |
| À venir | Ni l'un ni l'autre | Rien |

### Conventions de calcul (à caler sur chaque bail)

- **Trimestre de base** : par défaut T-2 par rapport à la prise d'effet (« dernier indice publié »). À relever sur l'acte.
- **Trimestre de référence** : même trimestre que la base, décalé de la périodicité (clause « indice du même trimestre »).
- **Chaînée** : loyer N = loyer N-1 × I(N) / I(N-1). **Base fixe** : loyer N = loyer initial × I(N) / I(base).
- **Plafond annuel (%)** : optionnel, borne le loyer retenu à base × (1 + p)^années couvertes. Sert au plafonnement ILC 3,5 % des PME (loi n° 2022-1158 du 16 août 2022, art. 14, et sa prolongation : période et champ d'application à vérifier bail par bail) ou à un plafond contractuel.
- Arrondi au centime sur le loyer annuel ; la baisse est appliquée si l'indice recule.

### Geler une révision : deux sémantiques

| Décision | Loyer de l'échéance gelée | Révision suivante (méthode chaînée) | Usage |
|---|---|---|---|
| Geler – sans rattrapage | inchangé | repart de l'indice de référence de l'échéance gelée : la variation de l'année est **abandonnée** | geste commercial définitif |
| Geler – rattrapage possible | inchangé | repart de l'indice de la **dernière révision appliquée** : le coefficient couvre deux périodes, le plafond annuel éventuel aussi | report négocié, avenant |

En méthode « Base fixe » le rattrapage est mécanique (loyer initial × indice N / indice de base), les deux
gels produisent le même résultat. Le renoncement du bailleur à une indexation acquise est un acte de
gestion à documenter (consigne, courrier) ; sa portée juridique vis-à-vis de la clause n'est pas tranchée par l'outil.

### Courriers aux locataires

`courriers <société>` sélectionne les échéances échues ou à venir dans 120 jours (`--horizon-jours`) dont
le sort est connu et qui n'ont pas encore été notifiées (« Courrier envoyé le » vide, « Appliqué ? » ≠ Oui ;
`--tous` pour tout régénérer). Trois textes : révision appliquée (avec tableau indice / coefficient / loyers),
gel (loyer maintenu, mention du rattrapage éventuel), attente d'indice (facturation provisoire).
En-tête et signataire viennent de la feuille Société, l'adresse du destinataire de la colonne
« Adresse du locataire (courrier) » (défaut : le local). `--marquer` inscrit la date du jour dans le classeur.

Hors périmètre v0.1 : révision légale triennale (art. L145-38 C. com.), déplafonnement, lissage de 10 %,
seuil de 25 % de l'art. L145-39. Ces points sont signalés dans le classeur pour instruction par le juriste.

## Séries INSEE

| Code | idbank | Base | Usage |
|---|---|---|---|
| ILC | 001532540 | 100 au T1 2008 | Baux commerciaux |
| ILAT | 001617112 | 100 au T1 2010 | Bureaux, professions libérales, logistique |
| ICC | 000008630 | 100 au T4 1953 | Clauses anciennes |
| IRL | 001515333 | 100 au T4 1998 | Habitation – **idbank à confirmer au premier appel** (le garde-fou sur le titre bloquera un mauvais identifiant) |

Les trois premiers idbanks ont été recoupés avec les pages `insee.fr/fr/statistiques/serie/<idbank>`.
L'INSEE ouvre par ailleurs un portail à clé (`portail-api.insee.fr`, « Api BDM ») ; si le service SDMX
historique devait fermer, seul `insee.recuperer_series` est à adapter.

## Pennylane – ce qui reste à faire avant un envoi réel

1. Ouvrir <https://pennylane.readme.io/reference/postbillingsubscriptions> et comparer chaque champ de
   `config/pennylane_mapping.json` (`recurrence`, `invoice_lines`, `raw_currency_unit_price`, `vat_rate`,
   `mode`, `start`). Les noms sont issus de la connaissance générale de l'API v2, pas de la page elle-même.
2. Renseigner `Pennylane customer_id` (et `product_id` si les produits « Loyer » existent) dans la feuille Baux.
3. Basculer `schema_valide` à `true`, exporter `PENNYLANE_API_TOKEN`, puis `pennylane <société> --push`.

Cible suivante : à chaque révision passée en « Appliqué », clôturer l'abonnement courant et en créer un
nouveau au loyer révisé (ou mettre à jour l'abonnement si l'API le permet), et rapprocher les factures
émises avec le loyer théorique du classeur.

## Structure du dépôt

```
indexation_loyer/   insee.py (API + cache) · periodes.py · baux.py · calcul.py (moteur) · workbook.py (Excel) · courriers.py (PDF) · pennylane.py · cli.py · demo.py
config/             series_insee.json · pennylane_mapping.json · courrier_modele.json · baux_modele.csv
data/indices/       indices_insee.csv (cache, commité par le workflow hebdomadaire)
suivi/              classeurs clients (ignorés par git : données nominatives)
demo/               DEMO_SCI_EXEMPLE_valeurs_fictives.xlsx
tests/              21 tests, dont la parité LibreOffice ↔ moteur Python et la génération des PDF
.github/workflows/  indices.yml : fetch-indices chaque lundi + commit du cache
```

Tests : `python -m pytest -q tests` (le test de parité est ignoré si LibreOffice Calc est absent).

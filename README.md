# INDEXATION_LOYER – suivi des réindexations de loyers par société

**Thèse.** Un classeur Excel par société bailleresse, **autonome** : le cabinet saisit les baux et les
indices INSEE, tout le reste est calculé par formules (calendrier des révisions, décision d'appliquer ou de
geler, alertes de facturation, préparation des abonnements Pennylane). Le script Python ne fait que créer le
classeur, puis, dans un second temps, l'alimenter en indices et créer les abonnements Pennylane.

**Phasage**

| Phase | Contenu | État |
|---|---|---|
| 1 – Classeur | Modèle Excel testable seul : Baux, Indices (grille de saisie), Révisions pré-câblées, Décision de gel + consigne, Alertes, courriers PDF | ✔ à tester en cabinet |
| 2 – Automatisation | Récupération des indices INSEE dans la grille, création des abonnements Pennylane depuis le classeur | code prêt, à tester en réel une fois la phase 1 validée |

```
                       PHASE 1 (autonome)                                         PHASE 2 (script)
   Cabinet ──saisit──►  Baux  ─────────────┐                          INSEE BDM ──fetch-indices──► cache CSV
   Cabinet ──saisit──►  Indices (grille)   ├──formules──►  Révisions ──► Alertes            │ refresh : écrit dans la grille Indices
                                            │              (Décision, Consigne)               ▼
                                            └────────────────────────────────►  Pennylane ──pennylane --push──► POST /billing_subscriptions
                              courriers <société>  ──►  out/courriers/*.pdf
```

## Le classeur (phase 1)

Créer un classeur : `python -m indexation_loyer init "SCI DES HALLES"` (ou `demo` pour l'exemple à valeurs
fictives). Ensuite tout se passe dans Excel :

| Feuille | Rôle | Qui écrit |
|---|---|---|
| Lisez-moi | Mode d'emploi, derniers indices saisis (formules), points de vigilance | modèle |
| Société | Raison sociale, signataire des courriers, réglages Pennylane (listes déroulantes) | cabinet |
| Baux | 40 lignes prêtes (capacité réglable à la création). Un local / bail par ligne, dans l'ordre d'arrivée. Colonne Contrôles : « OK » ou anomalies | cabinet (cellules jaunes) |
| Indices | Grille de saisie : une ligne par année (2000–2035), quatre colonnes T1..T4 par série (ILC, ILAT, ICC, IRL). Une cellule vide = indice non publié | cabinet, ou `refresh` en phase 2 |
| Révisions | 40 × 12 échéances pré-câblées ; celles d'un bail s'activent dès sa saisie. Indices, coefficient, **Décision**, loyer retenu, TVA, TTC, **statut**, **Consigne (à faire)**, Appliqué ?, dates | formules ; cabinet pour les colonnes jaunes |
| Alertes | Une ligne par bail : loyer actuel, prochaine révision, indice publié ?, révisions à facturer, gelées, action, consigne | formules |
| Pennylane | Une ligne par bail : corps JSON complet de l'abonnement (phase 2) | formules |

Règles d'usage : ne pas trier ni insérer de lignes dans Baux (les décisions de Révisions sont attachées à la
position) ; filtrer Révisions sur `Actif = 1` pour masquer les échéances hors bail ; les lignes sans bail restent
vides en bas de feuille. Taille du fichier : ~200 Ko, recalcul instantané.

## Installation et premier usage

```bash
pip install -r requirements.txt
python -m indexation_loyer init "SCI DES HALLES" --siren 123456789 [--baux config/baux_modele.csv] [--max-baux 40]
#   -> suivi/SCI_DES_HALLES_indexation_loyers.xlsx : saisir Baux et Indices dans Excel, c'est tout
# phase 2 :
python -m indexation_loyer fetch-indices                      # interroge l'INSEE, remplit le cache
python -m indexation_loyer refresh --tous                     # écrit les indices du cache dans la grille de chaque classeur
python -m indexation_loyer courriers "SCI DES HALLES"         # PDF pour chaque échéance à notifier (--marquer inscrit la date d'envoi)
python -m indexation_loyer pennylane "SCI DES HALLES"         # écrit out/*.json, n'envoie rien
python -m indexation_loyer demo                               # classeur de démonstration à valeurs FICTIVES
```

Sans accès direct à l'API depuis le poste : télécharger la série sur
`https://www.insee.fr/fr/statistiques/serie/001532540` (bouton CSV) puis
`python -m indexation_loyer fetch-indices --fichier <export.zip>`.

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
Point d'accès alternatif : `fetch-indices --api-insee` interroge `https://api.insee.fr/series/BDM/V1/…` avec le
jeton de la variable `INSEE_API_TOKEN`. Le nom exact de l'en-tête d'authentification attendu par l'INSEE
(historiquement `Authorization: Bearer`, `X-INSEE-Api-Key-Integration` sur le nouveau portail) est à
confirmer au premier appel ; il se règle dans `config/series_insee.json`.

## Pennylane

Corps envoyé (un abonnement par bail, au loyer actuel) :

| Champ | Source |
|---|---|
| `customer_id`, `product_id` | feuille Baux |
| `start` | 1er jour du mois suivant |
| `mode`, `payment_conditions`, `payment_method` | feuille Société (listes déroulantes ; défauts `awaiting_validation` = factures en brouillon, `upon_receipt`, `offline`) |
| `recurring_rule` | périodicité de facturation : Mensuelle → monthly/1, Trimestrielle → monthly/3, Semestrielle → monthly/6, Annuelle → yearly/1 ; `day_of_month` 1 |
| `customer_invoice_data.invoice_lines` | ligne loyer (`unit` mois/trimestre…, `raw_currency_unit_price` en chaîne à 2 décimales, `vat_rate` FR_200…) + ligne « Provision sur charges » si charges |
| `customer_invoice_data.special_mention` | « Loyer indexé sur l'ILC – révision du … (indice 2025-T4 : …) » |

Procédure :

1. Renseigner `Pennylane customer_id` (et `product_id` si les produits « Loyer » existent) dans la feuille Baux ;
   vérifier les trois réglages Pennylane de la feuille Société.
2. `pennylane <société>` : écrit `out/*.json`, n'envoie rien. Relire un corps.
3. `export PENNYLANE_API_TOKEN=…` (jeton OAuth avec le scope `billing_subscriptions:all`) puis
   `pennylane <société> --push`. Chaque 201 inscrit l'identifiant renvoyé dans la colonne
   `Pennylane subscription_id` de Baux ; un bail qui en a déjà un est ignoré (sauf `--remplacer`).

Le mode `email` (envoi automatique des factures) exige des destinataires et un modèle d'e-mail
Pennylane : non géré, le client le signale.

Cible suivante : à chaque révision passée en « Appliqué », arrêter l'abonnement courant (endpoint à
identifier dans la doc : `llms.txt`) et en créer un nouveau au loyer révisé, puis rapprocher les
factures émises avec le loyer théorique du classeur.

## Structure du dépôt

```
indexation_loyer/   insee.py (API + cache) · periodes.py · baux.py · calcul.py (moteur) · workbook.py (Excel) · courriers.py (PDF) · pennylane.py · cli.py · demo.py
config/             series_insee.json · pennylane_mapping.json · courrier_modele.json · baux_modele.csv
data/indices/       indices_insee.csv (cache, commité par le workflow hebdomadaire)
suivi/              classeurs clients (ignorés par git : données nominatives)
demo/               DEMO_SCI_EXEMPLE_valeurs_fictives.xlsx
tests/              22 tests, dont la parité LibreOffice ↔ moteur Python et la génération des PDF
.github/workflows/  indices.yml : fetch-indices chaque lundi + commit du cache
```

Tests : `python -m pytest -q tests` (le test de parité est ignoré si LibreOffice Calc est absent).

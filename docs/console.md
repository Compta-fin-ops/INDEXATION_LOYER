# Console de pilotage des loyers facturés

**Thèse.** Le classeur d'indexation dit ce que le loyer doit être, Pennylane dit ce qui est facturé. La console
compare les deux sur tous les dossiers et ne montre que ce qui demande une décision : révision non répercutée,
abonnement arrivé à terme, bail sans abonnement, échéance non facturée, impayé, facture restée en brouillon.

```
  Classeurs d'indexation ──importer──►  référentiel des baux : loyer en vigueur, dates de révision
  Pennylane (API v2)  ──synchroniser, lecture seule──►  abonnements et factures
                                   │
                              base SQLite locale
                                   │
              rapprochement : écarts gradués, échéancier, abonnement proposé
                                   │
                    console web : tableau de bord, écarts, échéancier, dossier, bail
```

## Le principe de facturation : un abonnement par période d'indexation

La question « comment changer le loyer à la date anniversaire » est réglée par la structure, pas par une
intervention : chaque abonnement est créé avec un nombre d'occurrences (`recurring_rule.count`) qui s'arrête
juste avant la prochaine révision. Il se termine seul. La console voit le terme approcher, calcule le début et
le nombre d'occurrences de la période suivante, et affiche le corps de l'abonnement à créer au loyer révisé.

| Situation | Ce que la console affiche |
|---|---|
| Abonnement en cours au bon loyer, borné à la révision | Conforme |
| Abonnement en cours sans terme, ou qui dépasse la révision | À traiter : l'ancien loyer continuerait d'être facturé |
| Dernière occurrence à moins de 45 jours du début de la période suivante | À traiter : préparer la période suivante |
| Révision passée, abonnement resté à l'ancien loyer | Bloquant, avec le rappel de loyer à facturer |
| Abonnement terminé et rien derrière, ou bail sans abonnement | Bloquant : le loyer n'est plus facturé |
| Occurrence passée sans facture | Bloquant |
| Facture échue non réglée | À traiter, bloquant au-delà de 60 jours |
| Facture en brouillon depuis plus de 7 jours | À traiter |
| Abonnement actif sans bail au référentiel | À traiter |

## Démarrer

```bash
pip install -r requirements.txt
python -m console demo        # portefeuille fictif : 10 sociétés, 78 baux, anomalies réalistes
python -m console serve       # http://127.0.0.1:8000
```

## Passer aux dossiers réels

```bash
# 1. référentiel : un import par classeur d'indexation, à refaire après chaque révision
python -m console importer suivi/SCI_DES_HALLES_indexation_loyers.xlsx --dossier HALLES --variable-jeton PENNYLANE_TOKEN_HALLES

# 2. jetons : un jeton API Entreprise v2 par dossier, en lecture seule, dans l'environnement
export PENNYLANE_TOKEN_HALLES=…

# 3. collecte, puis console
python -m console synchroniser
python -m console serve
```

Le rapprochement d'un bail avec son abonnement se fait par l'identifiant d'abonnement inscrit dans la fiche du
bail, à défaut par le client Pennylane ; si un client porte plusieurs baux, l'abonnement va au bail dont le loyer
par échéance est le plus proche.

## Ce qui est vérifié, ce qui ne l'est pas

| Brique | État |
|---|---|
| Règles de rapprochement, rappel de loyer, proposition d'abonnement | Testées |
| Verrous de la création d'abonnement, réponse perdue, refus de Pennylane | Testés contre un faux Pennylane, 9 tests |
| Import du référentiel depuis un classeur d'indexation | Testé sur le classeur de démonstration |
| Pages web et API JSON | Testées |
| Forme d'un abonnement lu dans Pennylane | Alignée sur l'OpenAPI « Create a billing subscription » fournie par le cabinet |
| Lecture des abonnements : pagination par curseur, champs, valeurs nulles | Conforme à l'OpenAPI « List billing subscriptions » du 2026-05-27, fournie par le cabinet |
| Champs d'une facture client | **Non vérifiés** : il manque l'OpenAPI « List customer invoices » |
| Jeton cabinet | La doc indique qu'un jeton cabinet est accepté sur l'API v2 avec l'en-tête `X-Company-Id`. Non exploité : la console utilise un jeton par dossier |
| Appel réel à Pennylane | **Jamais exécuté**, faute de jeton |

Les deux points non vérifiés sont isolés dans `console/pennylane_api.py`, fonctions `normaliser_abonnement`,
`normaliser_facture` et `_pages`. C'est le seul fichier à ajuster au premier essai réel.

## Créer un abonnement depuis la console

Règle de construction : **ne rien faire qui puisse mettre des données en danger.** Elle se traduit ainsi.

| Verrou | Effet |
|---|---|
| Aucun code de modification, d'arrêt ni de suppression | La console ne sait que créer un abonnement nouveau. Les données existantes de Pennylane ne sont jamais touchées. Un test vérifie qu'aucun verbe PUT, PATCH ou DELETE n'existe dans le code. |
| Écriture désactivée par défaut | Il faut `CONSOLE_ECRITURE=oui` sur le poste, sinon la console reste en lecture seule. |
| Jeton d'écriture distinct | Variable `<variable du dossier>_ECRITURE`. Le jeton de lecture ne sert jamais à écrire. |
| Factures toujours en brouillon | Le mode `awaiting_validation` est imposé dans le code, réglable nulle part. Un corps portant un autre mode est refusé. |
| Relecture de Pennylane juste avant l'envoi | Les contrôles portent sur l'état du moment, pas sur celui de la veille. |
| Contrôles bloquants | Client identifié, début dans le futur, abonnement borné à une période, loyer de la période connu, aucun chevauchement, aucun envoi antérieur, montant cohérent à 15 % près avec l'abonnement précédent. |
| Ce qui est affiché est ce qui est envoyé | Empreinte du corps exact. Si la situation change entre l'affichage et la confirmation, l'envoi est refusé. |
| Confirmation saisie à la main | Il faut taper le code du bail. Un bail à la fois, aucun envoi en lot. |
| Journal écrit avant l'appel | Chaque tentative laisse une trace avec le corps exact, même si la réponse se perd. |
| Réponse perdue | L'envoi est marqué « incertain » et tout nouvel essai pour ce bail est bloqué jusqu'à relecture de Pennylane. |

Deux refus méritent une explication, parce qu'ils sont voulus :

- **Abonnement en cours sans terme.** La console ne l'arrêtera pas à ta place. Il faut le clore dans Pennylane, puis actualiser.
- **Loyer révisé inconnu.** Si la période proposée commence à la date anniversaire et que l'indice n'est pas publié, la console ne crée rien : elle ne facture pas un loyer qu'elle ne connaît pas.

### Ce qui reste à constater sur un dossier d'essai

- L'appel de création **n'a jamais été exécuté pour de vrai**. Le parcours est testé contre un faux Pennylane.
- Le schéma permet d'imposer des **factures** en brouillon. Il ne contient aucun champ demandant que
  l'**abonnement** lui-même naisse en brouillon. Son statut de création est lu dans la réponse et affiché
  dans le journal : c'est le premier point à regarder.
- Risque résiduel, à connaître : un abonnement créé à tort reste un objet à arrêter à la main dans Pennylane.
  Ses factures seraient des brouillons, et aucune ne naît avant la date de début, toujours future.

### Mise en service prudente

```bash
python -m console demo && python -m console serve     # s'exercer sur le portefeuille fictif : bouton « Simuler la création »
# puis, sur UN dossier d'essai seulement :
export PENNYLANE_TOKEN_ESSAI=…                        # jeton de lecture
export PENNYLANE_TOKEN_ESSAI_ECRITURE=…               # jeton limité à billing_subscriptions
export CONSOLE_ECRITURE=oui
```

## Sécurité

- Les jetons ne sont ni dans la base ni dans le dépôt : la base ne retient que le nom des variables d'environnement.
- La console n'a pas d'authentification. Elle écoute sur `127.0.0.1` et ne doit pas être exposée en l'état :
  quiconque atteint la page peut déclencher une création si l'écriture est activée.
- La base `data/console.sqlite` contient des données de dossiers : elle est exclue du dépôt.

## Suite envisagée

1. Essai réel en lecture sur un dossier, pour caler la pagination et les champs de facture.
2. Première création réelle sur un dossier d'essai, pour constater le statut de l'abonnement créé.
3. Période provisoire à l'ancien loyer quand l'indice tarde, avec rappel à la publication.
4. Authentification et hébergement, pour un accès de l'équipe hors du poste.
5. Collecte planifiée chaque nuit et courriel des écarts bloquants.

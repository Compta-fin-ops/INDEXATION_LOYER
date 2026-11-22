# Console de pilotage des loyers facturés

**Thèse.** Les baux vivent dans la console : elle calcule ce que le loyer doit être, Pennylane dit ce qui est
facturé. La console compare les deux sur tous les dossiers et ne montre que ce qui demande une décision : révision
non répercutée, abonnement arrivé à terme, bail sans abonnement, échéance non facturée, impayé, facture restée en
brouillon. Il n'y a plus de classeur Excel dans le circuit.

```
  Baux saisis dans la console ─┐
  Indices INSEE relevés chaque ├─calcul─►  loyer en vigueur, révisions, loyer de la période suivante
  semaine (ILC, ILAT, ICC, IRL)┘
  Pennylane (API v2)  ──collecte, lecture seule──►  abonnements et factures
                                   │
                              base SQLite
                                   │
              rapprochement : écarts gradués, échéancier, abonnement proposé
                                   │
     console web : tableau de bord, écarts, échéancier, indices, dossier, bail, pièces, courriers
```

## Les baux dans la console

| Besoin | Où |
|---|---|
| Créer une société bailleresse | Tableau de bord, « Nouveau dossier » |
| Saisir un bail : loyer initial, date d'effet, indice, trimestre de départ, périodicité, méthode, plafond | Page du dossier, « Nouveau bail » |
| Voir les révisions calculées, de la première aux deux prochaines | Page du bail, tableau « Révisions » |
| Geler une révision, avec ou sans rattrapage, et noter pourquoi | Même tableau : décision, commentaire, date d'envoi du courrier |
| Éditer le courrier d'information au locataire | Même tableau, lien « PDF » : application, gel ou attente d'indice |
| Joindre le bail signé, un avenant, un courrier | Page du bail, « Pièces jointes » |
| Savoir qui a changé quoi | Page du bail, « Historique » |
| Voir les indices relevés et les révisions qui en dépendent | Page « Indices » |

Trois règles de gestion à connaître.

- **Le loyer n'est pas figé en base.** Il est recalculé à chaque affichage à partir des paramètres du bail, des
  indices relevés et des décisions de gel. Corriger un paramètre corrige toute la chaîne.
- **Les révisions continuent après le terme contractuel**, tant qu'aucune date de sortie du locataire n'est saisie.
  Le terme reste signalé. Un renouvellement qui refixe le loyer se traite par un nouveau bail. C'est un choix de
  gestion de l'outil, pas une lecture juridique du bail.
- **Rien ne se supprime.** Un bail se clôt par sa date de sortie ; une pièce jointe ne se retire pas.

Le moteur de calcul est celui du paquet `indexation_loyer`, déjà contrôlé cellule à cellule contre les formules
de l'ancien classeur. Un test vérifie que la console retrouve, bail par bail, les loyers que le classeur calculait.

### Reprendre un ancien classeur

```bash
python -m console importer suivi/SCI_DES_HALLES_indexation_loyers.xlsx --dossier HALLES --variable-jeton PENNYLANE_TOKEN_HALLES
```

Opération de migration, une fois par société : les baux, leurs paramètres, les gels et leurs commentaires passent
dans la console. Le classeur n'est plus relu ensuite.

### Sauvegarder

La base est désormais la source de vérité. `python -m console sauvegarder` en fait une copie horodatée dans
`data/sauvegardes`. Les pièces jointes sont des fichiers à part, dans le dossier `pieces` à côté de la base :
les deux sont à sauvegarder, et aucun n'est versionné.

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
# 1. créer le dossier et ses baux dans la console (ou reprendre un ancien classeur, voir plus haut)
python -m console --base data/cabinet.sqlite serve

# 2. jetons : un jeton API Entreprise v2 par dossier, en lecture seule, dans l'environnement
export PENNYLANE_TOKEN_HALLES=…

# 3. collecte
python -m console --base data/cabinet.sqlite synchroniser
```

La démonstration et les dossiers réels ne se mélangent pas : `python -m console demo` refuse de s'exécuter sur
une base qui contient un dossier réel, et un dossier fictif ne déclenche jamais d'appel à Pennylane.

Le rapprochement d'un bail avec son abonnement se fait par l'identifiant d'abonnement saisi sur le
bail, à défaut par le client Pennylane ; si un client porte plusieurs baux, l'abonnement va au bail dont le loyer
par échéance est le plus proche.

## Ce qui est vérifié, ce qui ne l'est pas

| Brique | État |
|---|---|
| Règles de rapprochement, rappel de loyer, proposition d'abonnement | Testées |
| Verrous de la création d'abonnement, réponse perdue, refus de Pennylane | Testés contre un faux Pennylane, 9 tests |
| Saisie d'un bail, calcul, gel, courrier PDF, pièces jointes, historique, sauvegarde | Testés, 11 tests |
| Reprise d'un ancien classeur | Testée : la console retrouve les loyers du classeur de démonstration |
| Pages web et API JSON | Testées |
| Forme d'un abonnement lu dans Pennylane | Alignée sur l'OpenAPI « Create a billing subscription » fournie par le cabinet |
| Lecture des abonnements : pagination par curseur, champs, valeurs nulles | Conforme à l'OpenAPI « List billing subscriptions » du 2026-05-27, fournie par le cabinet |
| Lecture des factures : pagination, champs, valeurs nulles, filtre sur la date | Conforme à l'OpenAPI « List customer invoices » du 2026-05-27, fournie par le cabinet. Les factures de plus de 36 mois ne sont pas collectées |
| Avoirs, proformas et devis présents dans la liste des factures | Collectés avec leur statut, écartés du rapprochement. **Signe des montants d'un avoir : inconnu** |
| Jeton cabinet | La doc indique qu'un jeton cabinet est accepté sur l'API v2 avec l'en-tête `X-Company-Id`. Non exploité : la console utilise un jeton par dossier |
| Appel réel à Pennylane | **Jamais exécuté**, faute de jeton |

Tout ce qui parle à Pennylane en lecture est isolé dans `console/pennylane_api.py`, fonctions
`normaliser_abonnement`, `normaliser_facture` et `_pages`. C'est le seul fichier à ajuster au premier essai réel.

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

- Les jetons ne sont ni dans la base ni dans le dépôt : la base ne retient que le nom des variables
  d'environnement. Le formulaire de dossier refuse toute valeur qui n'a pas la forme `PENNYLANE_TOKEN_CODE`,
  pour qu'un jeton collé par erreur ne soit ni enregistré ni réaffiché.
- **La console n'a pas encore de comptes utilisateurs.** Elle écoute sur `127.0.0.1` et ne doit pas être exposée
  en l'état : quiconque atteint la page peut modifier un bail, et déclencher une création si l'écriture est
  activée. L'historique porte « poste local », ou la valeur de `CONSOLE_UTILISATEUR`, faute d'identité.
- Un formulaire posté depuis un autre site est refusé (contrôle de l'en-tête `Origin`).
- Une pièce jointe est acceptée si son extension et son contenu concordent (PDF, PNG, JPEG, Word), 25 Mo au
  plus. Elle est rangée sous son empreinte, jamais sous le nom fourni, et servie en téléchargement.
- La base et les pièces contiennent des données de dossiers : elles sont exclues du dépôt.

## Limites connues

- **Révision en cours de mois.** Un bail dont la date anniversaire n'est pas le premier du mois est facturé au
  loyer révisé à partir de l'échéance qui suit ; le prorata du mois de révision n'est pas calculé.
- **Indice publié après la date anniversaire.** La console signale la révision échue sans indice et refuse de
  créer un abonnement à un loyer inconnu. La période provisoire à l'ancien loyer, puis le rappel, restent manuels.
- **Envoi du courrier.** Le PDF est produit par la console ; il n'est ni envoyé ni déposé dans Pennylane.
  L'API d'envoi par courriel de Pennylane porte sur les factures et les documents commerciaux, avec un modèle
  de courriel choisi dans Pennylane : elle n'envoie pas une lettre libre. Une facture expose une liste
  d'annexes en lecture ; la route d'ajout d'une annexe n'a pas été vérifiée.

## Suite envisagée

1. Comptes utilisateurs, rôles et session, préalable à tout accès de l'équipe hors du poste.
2. Essai réel en lecture sur un dossier.
3. File « à valider » : dès que l'indice d'une révision est publié, abonnement de la période et courrier préparés.
4. Première création réelle sur un dossier d'essai, pour constater le statut de l'abonnement créé.
5. Période provisoire à l'ancien loyer quand l'indice tarde, avec rappel à la publication.
6. Hébergement, base serveur, collecte planifiée chaque nuit et courriel des écarts bloquants.
7. Mandats de prélèvement : à instruire sur la documentation de Pennylane, rien n'est vérifié à ce jour.

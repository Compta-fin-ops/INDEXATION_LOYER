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
| Créer plusieurs sociétés et baux d'un coup, depuis Excel ou CSV | Tableau de bord, « Importer un fichier Excel ou CSV » |
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

### Éditions

| Édition | Où | Contenu |
|---|---|---|
| Courrier d'une révision | Page du bail, tableau « Révisions » | Un PDF : application, gel ou attente d'indice |
| Courriers en lot | Tableau de bord, « Éditer les courriers en lot » | Un ZIP d'un PDF par locataire, pour toutes les révisions à préparer ; puis la date d'envoi, posée d'un geste sur les courriers cochés |
| Situation des loyers à ce jour | Page du dossier, bloc « Éditions » | Un PDF pour le bailleur : les nouveaux loyers connus avec l'écart annuel et deux cases « Appliquer » ou « Geler » à cocher, les révisions en attente d'indice, les autres baux. Une décision déjà saisie dans la console y figure à la place des cases |
| État annuel des révisions | Page du dossier, bloc « Éditions » | Un PDF par société et par année : pour chaque bail la révision de l'année, les indices, le coefficient, le loyer avant et après, la suite donnée, et le total |

La console édite ; elle n'envoie rien. La date d'envoi d'un courrier est celle que vous posez.

### Reprendre un bail en cours, avec son historique

| Situation | Comment la saisir |
|---|---|
| Toutes les révisions ont été appliquées selon la clause | Loyer initial, date d'effet, trimestre de départ. La console recalcule toute la chaîne ; le loyer en vigueur doit retomber sur le loyer facturé |
| Une révision passée a été gelée, le reste est conforme | Même saisie, puis sur la page du bail, tableau « Révisions » : décision « Geler » sur l'année concernée, avec le commentaire |
| L'historique ne se recalcule pas : loyer renégocié, avenant, révision oubliée, gel mal documenté | Point de reprise : date de la dernière révision appliquée, loyer en vigueur à cette date, trimestre de l'indice appliqué. La console ne recalcule rien avant |

Le point de reprise se saisit dans le formulaire du bail, ou par les trois colonnes « Reprise » du fichier
d'import. Trois règles :

- le trimestre de reprise est celui appliqué à la révision de reprise ; la révision suivante prend le même
  trimestre, une période plus tard ;
- un gel postérieur à la reprise se saisit dans le tableau des révisions, pas dans la reprise ;
- les dates anniversaires des révisions deviennent celles de la date de reprise. C'est aussi le moyen de
  traiter un bail révisé à une date qui n'est pas l'anniversaire de sa prise d'effet.

La date d'effet et le loyer d'origine restent sur la fiche et dans les courriers. Une fois un gel saisi, la
console refuse de modifier la date d'effet, la périodicité de révision ou la date de reprise : les gels ne
viseraient plus la bonne année.

### Importer en lot depuis Excel ou CSV

1. Page « Importer » : télécharger le modèle Excel. Il contient la feuille « Baux » à remplir, un exemple et la
   notice des colonnes. Une ligne par bail ; les colonnes de la société se répètent sur chacun de ses baux.
2. Déposer le fichier : la console l'analyse **sans rien créer** et affiche chaque ligne avec son sort (à créer,
   déjà présent, en erreur) et le loyer en vigueur qu'elle calcule.
3. Confirmer : les sociétés et les baux sont créés d'un bloc.

| Règle | Effet |
|---|---|
| Création seulement | Une société ou un bail déjà présent n'est jamais modifié : la ligne est ignorée |
| Tout ou rien | Une seule ligne en erreur et rien n'entre : corriger le fichier et le redéposer |
| Aperçu obligatoire | Si la situation change entre l'aperçu et la confirmation, la confirmation est refusée |
| CSV | Mêmes titres de colonnes ; point-virgule et encodage d'Excel sous Windows acceptés |

Le loyer en vigueur calculé dans l'aperçu est le contrôle à faire avant de confirmer : s'il diffère du loyer
facturé aujourd'hui, le trimestre de départ ou la méthode du fichier est à revoir.

### Reprendre un ancien classeur

```bash
python -m console importer suivi/SCI_DES_HALLES_indexation_loyers.xlsx --dossier HALLES --variable-jeton PENNYLANE_TOKEN_HALLES
```

Opération de migration, une fois par société : les baux, leurs paramètres, les gels et leurs commentaires passent
dans la console. Le classeur n'est plus relu ensuite.

### Production et test

Une base porte sa nature, production ou test. La base de test se crée par copie de la production
(`python -m console copier-vers-test`, ou `lancer_console_test.bat` sur Windows), s'affiche avec un ruban sur
toutes les pages, a ses propres sessions, et simule toute création d'abonnement : aucune écriture ne part vers
Pennylane depuis une base de test, même écriture activée et jeton présent. Détail dans
[installation.md](installation.md).

### Sauvegarder

La base est désormais la source de vérité. `python -m console sauvegarder` en fait une copie horodatée dans
`data/sauvegardes`. Les pièces jointes sont des fichiers à part, dans le dossier `pieces` à côté de la base :
les deux sont à sauvegarder, et aucun n'est versionné.

## Le tableau de bord

Il sert à deux choses et ne montre rien d'autre : pas de total de loyers ni de chiffre d'affaires.

| Bloc | Ce qu'il montre |
|---|---|
| Facturation à corriger | Les irrégularités, bloquantes d'abord : bail sans abonnement, abonnement arrivé à terme, révision non répercutée, échéance non facturée, abonnement à borner. Les impayés sont comptés à part, par un lien |
| Réindexations à piloter | Les révisions échues sans indice, puis la prochaine révision de chaque bail dans l'horizon choisi. Pour chacune : l'indice attendu et s'il est publié, le loyer avant et après, le courrier, l'abonnement de la période suivante, et ce qu'il reste à faire |
| Par dossier | Le nombre d'irrégularités, la prochaine réindexation et l'état de la lecture de Pennylane |

Une réindexation passe par quatre états : « attendre l'indice », « indice attendu » si la date est dépassée,
« à préparer » dès que l'indice est publié ou qu'un gel est décidé, « prête » quand le courrier est daté comme
envoyé et que l'abonnement suivant existe dans Pennylane.

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
python -m console demo                 # portefeuille fictif : 10 sociétés, 78 baux, anomalies réalistes
python -m console utilisateur creer    # premier compte, administrateur
python -m console serve                # http://127.0.0.1:8000
```

Sur Windows, sans ligne de commande : double-clic sur `lancer_console.bat`, voir [installation.md](installation.md).

## Raccorder un dossier à Pennylane

Page du dossier, bouton « Raccordement Pennylane ».

1. **Déposer le jeton de lecture** : un administrateur le colle une fois dans la page. Il est rangé dans le coffre
   du système (sous Windows, le Gestionnaire d'identification), protégé par la session de l'utilisateur. Il
   n'entre ni dans la base ni dans les sauvegardes, et la console ne le réaffiche jamais. Aucune variable
   d'environnement à créer sur le poste.
2. **Tester la connexion** : deux lectures, aucune écriture. L'identité du jeton (`GET /me`), puis une page
   d'abonnements. La page affiche le code de chaque réponse, la réponse brute de l'identité, et si elle porte la
   marque d'un environnement de test Pennylane.

Sur un serveur, où il n'y a pas de coffre, le jeton se fournit par une variable d'environnement du nom
`PENNYLANE_TOKEN_CODE`. Quand les deux existent, la variable passe devant.

Pour s'exercer sans enjeu : créer son environnement de test dans Pennylane (« Mon compte », « Environnements de
test »), y générer un jeton (« Paramètres entreprise », « Connectivité », onglet « Développeurs »), puis créer
dans la **base de test** de la console un dossier `SANDBOX` et y déposer ce jeton.

## Passer aux dossiers réels

```bash
# 1. créer le dossier et ses baux dans la console (ou reprendre un ancien classeur, voir plus haut)
python -m console --base data/cabinet.sqlite serve

# 2. jetons : un jeton API Entreprise v2 par dossier, en lecture seule, déposé depuis la page
#    « Raccordement Pennylane » du dossier (ou, sur un serveur : export PENNYLANE_TOKEN_HALLES=…)

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
| Import en lot : modèle, aperçu, confirmation, tout ou rien, fichiers refusés | Testé, et parcours vérifié dans un navigateur |
| Base de test : copie, marquage, sessions distinctes, aucune écriture vers Pennylane | Testé, 4 tests |
| Comptes, rôles, session, blocage après échecs, mot de passe provisoire | Testés, 7 tests, et parcours vérifié dans un navigateur |
| Lanceur `lancer_console.bat` et notice Windows | **Non essayés sur Windows** |
| Reprise d'un ancien classeur | Testée : la console retrouve les loyers du classeur de démonstration |
| Pages web et API JSON | Testées |
| Forme d'un abonnement lu dans Pennylane | Alignée sur l'OpenAPI « Create a billing subscription » fournie par le cabinet |
| Lecture des abonnements : pagination par curseur, champs, valeurs nulles | Conforme à l'OpenAPI « List billing subscriptions » du 2026-05-27, fournie par le cabinet |
| Lecture des factures : pagination, champs, valeurs nulles, filtre sur la date | Conforme à l'OpenAPI « List customer invoices » du 2026-05-27, fournie par le cabinet. Les factures de plus de 36 mois ne sont pas collectées |
| Avoirs, proformas et devis présents dans la liste des factures | Collectés avec leur statut, écartés du rapprochement. **Signe des montants d'un avoir : inconnu** |
| Environnement de test Pennylane (Sandbox) | Existe selon le centre d'aide : un dossier de démonstration par utilisateur, nommé « Sandbox - votre email », avec son propre jeton d'API. La console sait tester la connexion d'un dossier en lecture seule et reconnaître cette marque. **Schéma de la route d'identité non vérifié** ; aucune création n'y a encore été faite |
| Jeton cabinet | La doc indique qu'un jeton cabinet est accepté sur l'API v2 avec l'en-tête `X-Company-Id`. Non exploité : la console utilise un jeton par dossier |
| Appel réel à Pennylane, en lecture | Exécuté le 24/11/2026 sur un environnement de test Pennylane : la liste des abonnements a été lue et rapprochée. La liste des factures a été appelée sans erreur ; son contenu n'a pas encore été observé |
| Facture de rappel en brouillon | Corps conforme à l'OpenAPI « Create a customer invoice » fournie par le cabinet ; parcours testé contre un faux Pennylane, 5 tests |
| Appel réel à Pennylane, en écriture | **Jamais exécuté** |

Tout ce qui parle à Pennylane en lecture est isolé dans `console/pennylane_api.py`, fonctions
`normaliser_abonnement`, `normaliser_facture` et `_pages`. C'est le seul fichier à ajuster au premier essai réel.

## Ce que la console écrit dans Pennylane

Deux créations, et rien d'autre. Dans les deux cas, ce qui est créé reste à valider dans Pennylane.

| Création | Quand | Ce qui naît dans Pennylane |
|---|---|---|
| Abonnement de la période suivante | Terme de l'abonnement en cours, ou révision à répercuter | Un abonnement dont les factures naissent en brouillon |
| Facture de rappel | Révision non répercutée : l'abonnement est resté à l'ancien loyer | Une facture en brouillon, du montant dû depuis la date de révision |

La facture de rappel couvre toutes les échéances de l'abonnement resté à l'ancien loyer, de la date de révision
à son terme. Elle exige donc que cet abonnement soit borné : sinon il continuerait à l'ancien loyer et le rappel
serait incomplet. Un seul rappel par bail et par révision. Elle porte une référence externe,
`console-DOSSIER-BAIL-rappel-DATE`, que la console recherche dans Pennylane avant de créer : une facture déjà
présente sous cette référence bloque l'envoi. Un rappel négatif, quand le loyer baisse, est refusé : c'est un
avoir, à établir à la main.

Les verrous sont les mêmes pour les deux créations, décrits ci-dessous.

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

## Comptes et connexion

Chaque personne a un compte : identifiant et mot de passe. Rien n'est servi sans session, hormis la page de
connexion. Installation pas à pas sur un poste Windows : [installation.md](installation.md).

| Rôle | Ce qu'il permet |
|---|---|
| Lecture | Consulter tout, ne rien modifier |
| Gestion | Saisir les baux, geler une révision, joindre des pièces, éditer les courriers, relancer la collecte |
| Validation | Gestion, plus la création d'abonnements dans Pennylane |
| Administrateur | Validation, plus la gestion des comptes |

```bash
python -m console utilisateur creer                      # le premier compte est administrateur
python -m console utilisateur lister
python -m console utilisateur mot-de-passe IDENTIFIANT   # secours si le seul administrateur a oublié le sien
```

Les autres comptes se créent dans la console, page « Comptes », avec un mot de passe provisoire à changer à la
première connexion. Un compte ne se supprime pas, il se désactive : l'historique garde son nom.

## Sécurité

| Sujet | Ce qui est en place |
|---|---|
| Mots de passe | 12 caractères au moins. La base ne garde qu'une empreinte scrypt salée, jamais le mot de passe |
| Essais répétés | Compte bloqué 15 minutes après 5 échecs. Le message ne dit pas si c'est l'identifiant ou le mot de passe qui est faux |
| Session | 12 heures. Témoin inaccessible aux scripts de la page ; la base ne garde que l'empreinte du jeton |
| Changement de rôle, désactivation, nouveau mot de passe | Les sessions du compte sont fermées à l'instant |
| Formulaire posté depuis un autre site | Refusé : l'en-tête `Origin`, à défaut `Referer`, doit désigner la console |
| Traçabilité | L'historique des baux et le journal des envois portent l'identifiant de l'auteur |
| Écritures simultanées | Une requête à la fois sur la base |
| Jetons Pennylane | Ni dans la base ni dans le dépôt : ils vivent dans le coffre du système ou dans une variable d'environnement, et la base ne retient que leur nom technique. Dépôt réservé à un administrateur ; un jeton n'est jamais réaffiché. Le jeton d'écriture reste, à ce stade, une variable d'environnement |
| Pièces jointes | Extension et contenu concordants (PDF, PNG, JPEG, Word), 25 Mo au plus, rangées sous leur empreinte, servies en téléchargement |
| Données | La base et les pièces sont exclues du dépôt |

**Ce qui manque encore pour ouvrir la console à d'autres postes.** Elle écoute sur `127.0.0.1`. Sans connexion
chiffrée (HTTPS), un mot de passe circulerait en clair sur le réseau : l'ouverture au réseau du cabinet ou à
Internet demande un serveur placé derrière un accès chiffré. Il n'y a pas non plus de double authentification.

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

1. Essai réel en lecture sur un dossier.
2. File « à valider » : dès que l'indice d'une révision est publié, abonnement de la période et courrier préparés.
3. Première création réelle sur un dossier d'essai, pour constater le statut de l'abonnement créé.
4. Hébergement avec accès chiffré, pour un usage depuis les autres postes ; connexion par le compte Microsoft
   du cabinet en complément du mot de passe.
5. Période provisoire à l'ancien loyer quand l'indice tarde, avec rappel à la publication.
6. Collecte planifiée chaque nuit et courriel des écarts bloquants.
7. Mandats de prélèvement : à instruire sur la documentation de Pennylane, rien n'est vérifié à ce jour.

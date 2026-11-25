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
     console web : baux par société, indices, dossier, bail, pièces, courriers
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
| Courrier d'une révision | Page du bail, tableau « Révisions » | Un PDF : application, gel ou attente d'indice. Le calcul de l'indice, puis un tableau avant / après : loyer annuel HT, loyer, provision sur charges et TVA par échéance, montant TTC à régler. Si le montant change, un paragraphe demande d'ajuster le virement permanent |
| Courriers en lot | Tableau de bord, « Éditer les courriers en lot » | Un ZIP d'un PDF par locataire, pour toutes les révisions à préparer ; puis la date d'envoi, posée d'un geste sur les courriers cochés |
| Situation des loyers à ce jour | Page du dossier, bloc « Éditions », ou page « Baux » | Un PDF d'information pour le gérant, chaque ligne datée par sa date de réindexation : loyers réindexés sur douze mois (ancien et nouveau loyer, écart annuel), réindexations à venir avec indice publié, réindexations dont l'indice n'est pas publié, autres baux |
| Relevé des factures d'un bail | Page du bail, bloc « Dernières factures » | Un PDF : les factures émises sur douze mois, montant TTC, réglé, reste dû, total. Brouillons, avoirs et devis écartés |
| Courrier de demande de régularisation | Page du bail, quand un solde reste dû | Le relevé, précédé d'une lettre cordiale : solde dû, montant TTC actuel de l'échéance pour ajuster un virement permanent. Le reste dû est celui de Pennylane : lettrer avant d'envoyer |
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

## La page d'accueil : les baux par société

La console s'ouvre sur la liste de tous les baux, regroupés par société. Chaque société se déplie d'un clic,
ou toutes à la fois. Aucun total de loyers ni de chiffre d'affaires : seulement ce qui est à corriger et les
réindexations à piloter.

| Filtre | Effet |
|---|---|
| Les quatre tuiles | À corriger, à régulariser, réindexations à préparer (indice publié, sous 90 jours), en attente d'indice. Un clic filtre, un second retire le filtre |
| Réindexation | Mois en cours, mois prochain, trimestre à venir (trois prochains mois), année civile en cours ou suivante, période personnalisée. Ne garde que les baux qui ont une réindexation dans la période, triés par date |
| Indice | Publié, ou pas encore publié, pour la réindexation affichée |
| Recherche | Société, code du bail, locataire, local |

Les filtres se combinent, et les tuiles conservent les autres. Pour chaque bail, la colonne de réindexation
donne la date, l'indice attendu, s'il est publié, le nouveau loyer et la variation, le courrier (à éditer ou
envoyé le) et l'abonnement suivant (créé, ou à créer). Chaque société porte ses liens (fiche, modifier la
société, nouveau bail, situation PDF), chaque bail les siens (voir, modifier, abonnement, rappel).

Une réindexation passe par quatre états : « attendre l'indice », « indice attendu » si la date est dépassée,
« à préparer » dès que l'indice est publié ou qu'un gel est décidé, « prête » quand le courrier est daté comme
envoyé et que l'abonnement suivant existe dans Pennylane.

### Créer un bail à partir d'un abonnement Pennylane

Un abonnement sans bail apparaît sur l'accueil, dans sa société (filtre « À régulariser »), avec le lien
« Créer le bail à partir de cet abonnement ». Le formulaire reprend le client, la périodicité, le lien vers
l'abonnement, et ventile ses lignes : celles qui parlent de charges ou de provision forment la provision sur
charges, celles qui parlent de loyer forment le loyer, la TVA de la ligne de loyer donne le taux. Le loyer
pré-rempli est celui facturé aujourd'hui : si le bail a déjà été révisé, saisir le loyer d'origine ou la reprise.
La date d'effet et l'indice restent à saisir d'après le bail.

### Comptes clients : le grand livre

La page « Grand livre » d'une société (lien sur l'accueil et sur la fiche société) reçoit l'export « Grand livre »
de Pennylane, au format xlsx, déposé à la main : aucun appel à Pennylane. Seules les lignes des comptes 411 sont
conservées, le reste du fichier n'est pas stocké ; un nouveau dépôt remplace le précédent.

Chaque compte 411 se rattache à son bail ; la console suggère le bail dont le locataire porte le même nom, à
confirmer. Sur la fiche du bail : solde, débit et crédit non lettrés, extrait du compte en PDF, et, si le
locataire doit une somme, le courrier de demande de régularisation avec l'extrait en annexe. Sur l'accueil, un
solde dû apparaît dans la colonne d'état du bail.

À la différence du relevé des factures, l'extrait du grand livre montre les règlements tels qu'ils sont arrivés,
lettrés ou non : un virement d'un montant différent de l'échéance y figure, au lieu de faire paraître la facture
impayée. Le solde est celui de la période exportée : il n'y a pas de ligne de report dans l'export constaté.
Exporter depuis l'ouverture de l'exercice, ou depuis une date où les comptes étaient soldés.

Format constaté sur un export réel (dossier de test) : une feuille, colonnes « N° de compte, Libellé de compte,
Date, Journal, Libellé de pièce, N° de facture, Let., Débit, Crédit, Solde ». Un fichier dont les colonnes
diffèrent est refusé. Le signe « ↔ » qui suit parfois une lettre de lettrage n'est pas documenté.

## Le principe de facturation : un abonnement par période d'indexation

La question « comment changer le loyer à la date anniversaire » est réglée par la structure, pas par une
intervention : chaque abonnement est créé avec un nombre d'occurrences (`recurring_rule.count`) qui s'arrête
juste avant la prochaine révision. Il se termine seul. La console voit le terme approcher, calcule le début et
le nombre d'occurrences de la période suivante, et affiche le corps de l'abonnement à créer au loyer révisé.

| Situation | Ce que la console affiche |
|---|---|
| Abonnement en cours au bon loyer, borné à la révision | Conforme |
| Abonnement en cours sans terme, ou qui dépasse la révision | À traiter : le nombre d'échéances facturées à l'ancien loyer après la réindexation, et la date où l'abonnement aurait dû s'arrêter. La création de l'abonnement suivant est bloquée tant qu'il n'est pas raccourci dans Pennylane |
| Dernière occurrence à moins de 45 jours du début de la période suivante | À traiter : préparer la période suivante |
| Révision passée, abonnement resté à l'ancien loyer | Bloquant, avec le rappel de loyer à facturer |
| Abonnement terminé et rien derrière, ou bail sans abonnement | Bloquant : le loyer n'est plus facturé |
| Occurrence passée sans facture | Bloquant |
| Facture échue non réglée | À traiter, bloquant au-delà de 60 jours |
| Facture en brouillon depuis plus de 7 jours | À traiter |
| Abonnement actif sans bail au référentiel | À traiter |
| Abonnement suivant qui chevauche l'abonnement en cours, ou échéance couverte par aucun des deux | Bloquant |
| Abonnement suivant à un autre montant que le loyer révisé | Bloquant |
| Abonnement facturé à un autre client que celui du bail | Bloquant |
| Abonnement actif après la sortie du locataire | Bloquant |
| TVA des lignes de l'abonnement différente du bail | À traiter |
| Factures de l'abonnement émises sans brouillon | Information |

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
| Créations en lot, reprise des paramètres de l'abonnement existant, mention de suivi, chaînage des factures | Testés, 10 tests, dont trois contre un faux Pennylane ; parcours vérifié dans un navigateur sur une base de test |
| Lecture des lignes et des sections d'un abonnement | Conforme aux OpenAPI « List invoice lines for a billing subscription » et « List the invoice line sections of a billing subscription » du 2026-05-27, fournies par le cabinet. **Jamais exécutée pour de vrai** |
| Référence externe relue dans la liste des factures | Champ `external_reference` supposé présent dans la réponse de « List customer invoices » : le filtre sur ce champ est documenté, sa présence dans la réponse **reste à constater** |
| Création d'une ligne portant à la fois un produit et un prix | Le schéma l'admet. **À constater sur un dossier d'essai** que Pennylane garde le prix envoyé : la console compare le montant de la réponse au montant envoyé et signale tout écart au journal |
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

### Ce qui est repris de l'abonnement existant

La création ne réinvente rien de ce que l'abonnement en place porte déjà. À chaque collecte, la console lit
les paramètres de l'abonnement en cours (à défaut du dernier terminé) et ses lignes de facture.

| Élément | Nouvel abonnement | Facture de rappel |
|---|---|---|
| Conditions et mode de paiement | Repris | Conditions reprises : elles fixent l'échéance |
| Modèle de facture, langue, devise, texte libre | Repris | Repris |
| Libellé, objet, description | Repris | Propres au rappel |
| Lignes | Recopiées à l'identique, produit compris ; seul le prix de la ligne de loyer change | Une ligne, avec l'unité, la TVA et le produit de la ligne de loyer |
| Mention spéciale | Remplacée par le suivi des révisions | Phrase du rappel, puis suivi des révisions |
| Mode des factures | Toujours brouillon, quoi que porte l'abonnement existant | Toujours brouillon |

La ligne de loyer est reconnue ainsi : la ligne dont le montant est la provision sur charges du bail est la
ligne de charges ; la plus forte des autres est le loyer ; toute autre ligne est recopiée telle quelle et
signalée. Quand la console ne peut pas désigner la ligne de loyer avec certitude (lignes non lues, aucune
ligne égale à la provision du bail, remise, quantité différente de 1), elle ne recopie pas les lignes : elle
construit les siennes d'après le bail et l'écrit dans les contrôles. Une remise n'est jamais reconduite.

### La mention de suivi des révisions

Pour un bail calculé par la console, la mention portée sur les factures est produite par elle :

```
ILC 2019 T1 : 114,64
Loyer de base : 1 160,00 € HT par mois
Premier loyer : décembre 2019
Révision au 01/12/2022 : ILC 2022 T1 : 120,61 contre 2019 T1 : 114,64, soit +5,21 % ; loyer porté à 1 220,41 € HT par mois.
Prochaine ré-indexation triennale du loyer au 01/12/2025.
```

Les montants sont ceux d'une échéance. Une révision plafonnée, gelée ou en attente d'indice est dite comme
telle. Au-delà de quatre révisions, les plus anciennes sont résumées par le loyer qu'elles ont donné. Pour un
bail repris en cours, la console ne connaît pas les révisions d'avant la reprise : elle recopie le texte saisi
dans le champ « Historique à reporter sur les factures » du bail, à défaut le loyer constaté à la reprise. La
mention figure sur la page du bail et sur la page de confirmation, avant tout envoi. Comme un abonnement ne
couvre qu'une période d'indexation, sa mention reste exacte pendant toute sa durée.

### Le lien entre une facture Pennylane et un bail

| Facture | Comment la console sait à quel bail elle appartient |
|---|---|
| Générée par un abonnement | Pennylane inscrit l'abonnement sur la facture ; l'abonnement est rattaché au bail |
| Rappel créé par la console | Référence externe `console-DOSSIER-BAIL-rappel-DATE` inscrite sur la facture, et journal des envois |
| Autre facture du client | Rattachée au client du bail, marquée « Hors abonnement » |

Un abonnement se rattache à un bail dans cet ordre : créé par la console pour ce bail (journal des envois),
identifiant inscrit sur le bail, client Pennylane du bail. Créer l'abonnement de la période suivante ne
demande donc aucune modification du bail. La page du bail affiche l'origine de chaque facture.

### Créer en lot

La page « Créations en lot » liste tout ce que la console propose de créer, tous dossiers confondus, avec
l'état de ses contrôles. Une ligne prête est cochée d'office, sauf si elle porte un avertissement. La
confirmation consiste à saisir le nombre de lignes cochées. Chaque création passe ensuite par le même chemin
qu'une création unitaire : contrôles rejoués, empreinte du corps, journal. Pennylane est relu une fois par
dossier avant le lot et une fois après. Une ligne dont la situation a changé depuis l'affichage est écartée
sans arrêter le lot ; le lot s'arrête à la première création que Pennylane ne confirme pas.

## Créer un abonnement depuis la console

Règle de construction : **ne rien faire qui puisse mettre des données en danger.** Elle se traduit ainsi.

| Verrou | Effet |
|---|---|
| Aucun code de modification, d'arrêt ni de suppression | La console ne sait que créer un abonnement nouveau. Les données existantes de Pennylane ne sont jamais touchées. Un test vérifie qu'aucun verbe PUT, PATCH ou DELETE n'existe dans le code. |
| Écriture désactivée par défaut | Il faut `CONSOLE_ECRITURE=oui` sur le poste, sinon la console reste en lecture seule. |
| Jeton d'écriture distinct | Variable `<variable du dossier>_ECRITURE`. Le jeton de lecture ne sert jamais à écrire. |
| Factures toujours en brouillon | Le mode `awaiting_validation` est imposé dans le code, réglable nulle part. Un corps portant un autre mode est refusé. |
| Relecture de Pennylane juste avant l'envoi | Les contrôles portent sur l'état du moment, pas sur celui de la veille. |
| Contrôles bloquants | Client identifié, début dans le futur, abonnement borné à une période d'indexation du bail (un, deux ou trois ans), loyer de la période connu, aucun chevauchement, aucun envoi antérieur, montant cohérent à 15 % près avec l'abonnement précédent. |
| Ce qui est affiché est ce qui est envoyé | Empreinte du corps exact. Si la situation change entre l'affichage et la confirmation, l'envoi est refusé. |
| Confirmation saisie à la main | Création unitaire : taper le code du bail. Création en lot : taper le nombre de créations cochées. |
| Montant relu | Le montant hors taxes de la réponse de Pennylane est comparé au montant envoyé ; un écart est écrit au journal. |
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
- **Plafond de hausse.** Le plafond saisi sur un bail s'applique à chaque révision, quelle que soit sa périodicité :
  le loyer retenu ne dépasse pas le loyer antérieur augmenté de ce pourcentage. Une révision qui rattrape un gel
  n'est plafonnée qu'une fois. Un plafond composé année par année n'est pas modélisé.
- **Plafonnement légal temporaire de l'ILC.** Non modélisé. Une révision concernée se saisit par le point de reprise.
- **Bornage d'un abonnement sans terme.** La console ne modifie pas un abonnement existant. Un abonnement repris
  sans terme se borne une fois, à la main, dans Pennylane ; les suivants, créés par la console, sont bornés.
- **Envoi d'une facture.** La console ne finalise ni n'envoie aucune facture : tout se valide dans Pennylane.
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

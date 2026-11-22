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
| Règles de rapprochement, rappel de loyer, proposition d'abonnement | Testées, 14 tests |
| Import du référentiel depuis un classeur d'indexation | Testé sur le classeur de démonstration |
| Pages web et API JSON | Testées |
| Forme d'un abonnement lu dans Pennylane | Alignée sur l'OpenAPI « Create a billing subscription » fournie par le cabinet |
| Pagination des listes et champs d'une facture client | **Non vérifiés** : documentation inaccessible depuis l'environnement de développement |
| Appel réel à Pennylane | **Jamais exécuté**, faute de jeton |

Les deux points non vérifiés sont isolés dans `console/pennylane_api.py`, fonctions `normaliser_abonnement`,
`normaliser_facture` et `_pages`. C'est le seul fichier à ajuster au premier essai réel.

## Sécurité

- La console **lit** Pennylane et n'écrit rien. Les abonnements proposés sont affichés, jamais envoyés.
- Les jetons ne sont ni dans la base ni dans le dépôt : la base ne retient que le nom de la variable d'environnement.
- La console n'a pas d'authentification. Elle écoute sur `127.0.0.1` et ne doit pas être exposée en l'état.
- La base `data/console.sqlite` contient des données de dossiers : elle est exclue du dépôt.

## Suite envisagée

1. Essai réel en lecture sur un dossier, pour caler la pagination et les champs de facture.
2. Bouton « créer l'abonnement » avec confirmation, une fois la lecture validée : c'est le seul écran qui écrirait.
3. Authentification et hébergement, pour un accès de l'équipe hors du poste.
4. Collecte planifiée chaque nuit et courriel des écarts bloquants.

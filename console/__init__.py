"""Console de pilotage des loyers facturés sur Pennylane, tous dossiers.

    Baux saisis dans la console + indices INSEE relevés  ──calcul──►  loyer attendu, révisions
    Pennylane (abonnements, factures)  ──collecte en lecture──►  base SQLite
                                                           │
                                         rapprochement : écarts, échéancier, propositions
                                                           │
                                                     console web (FastAPI)

Principe de facturation retenu : UN ABONNEMENT PAR PÉRIODE D'INDEXATION. Chaque abonnement
est borné par un nombre d'occurrences qui s'arrête à la date anniversaire ; la console
détecte les abonnements arrivant à terme et propose le suivant au loyer révisé.
"""

__version__ = "0.9.0"

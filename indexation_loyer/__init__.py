"""Suivi des réindexations de loyers (ILC / ILAT / ICC / IRL) par société.

Pipeline :
  INSEE (BDM, service SDMX)  ->  cache CSV  ->  classeur Excel par société
                                                  -> calendrier des révisions (formules vivantes)
                                                  -> préparation des abonnements Pennylane
"""

__version__ = "0.1.0"

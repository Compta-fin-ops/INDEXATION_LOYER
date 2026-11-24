# Installer et ouvrir la console sur un poste Windows

**Ce que l'on obtient.** La console tourne sur ce poste et s'ouvre dans le navigateur de ce même poste, à l'adresse
`http://127.0.0.1:8000`, avec un identifiant et un mot de passe. Elle n'est pas joignable depuis un autre
ordinateur : c'est voulu à ce stade.

> Cette notice et le fichier `lancer_console.bat` ont été écrits sans pouvoir être essayés sur Windows. Les
> commandes Python, elles, sont testées. Si une étape accroche, le message affiché suffit en général à corriger.

## 1. Vérifier si Python est installé

Ouvrir le menu Démarrer, taper `PowerShell`, valider, puis saisir :

```
py --version
```

- Une réponse du type `Python 3.11.x` ou plus récent : passer à l'étape 2.
- Un message d'erreur : installer Python depuis <https://www.python.org/downloads/>. Dans la première fenêtre de
  l'installateur, cocher **« Add python.exe to PATH »**, puis « Install Now ». Fermer et rouvrir PowerShell,
  et refaire la vérification.

## 2. Récupérer le code

Sur la page GitHub du dépôt `INDEXATION_LOYER` :

1. choisir la branche `claude/suivi-reindexations-loyers-ag5rbc` dans le sélecteur de branche (la console n'est
   pas encore dans la branche principale) ;
2. bouton vert « Code », puis « Download ZIP » ;
3. extraire le ZIP dans un dossier stable, par exemple `Documents\console-loyers`. Éviter un dossier
   synchronisé (OneDrive, SharePoint) : la base y serait copiée hors du poste.

Avec Git installé, l'équivalent est `git clone` puis `git checkout claude/suivi-reindexations-loyers-ag5rbc`.

## 3. Lancer

Double-cliquer sur **`lancer_console.bat`**, dans le dossier extrait.

- La première fois, il prépare l'environnement (quelques minutes, il faut Internet), puis demande de créer le
  premier compte : identifiant, nom affiché, mot de passe saisi deux fois. **La saisie du mot de passe ne
  s'affiche pas à l'écran**, c'est normal. Ce premier compte est administrateur.
- Ensuite il ouvre le navigateur sur la page de connexion.
- La fenêtre noire doit rester ouverte : la fermer arrête la console.

Windows peut afficher un avertissement à l'ouverture d'un fichier `.bat` téléchargé : « Informations
complémentaires », puis « Exécuter quand même ».

### Sans le fichier .bat

Dans PowerShell, depuis le dossier extrait :

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m console --base data\cabinet.sqlite utilisateur creer
.venv\Scripts\python -m console --base data\cabinet.sqlite serve
```

Puis ouvrir `http://127.0.0.1:8000` dans le navigateur.

## Installer avec Git : les mises à jour en un double-clic

Avec le ZIP, chaque mise à jour demande un téléchargement et un copier-coller. Avec Git, c'est un double-clic
sur `mettre_a_jour.bat`, qui rapporte aussi les derniers indices INSEE. Le passage se fait une fois.

1. **Installer Git** depuis <https://git-scm.com/download/win>, en laissant les options proposées.
2. **Arrêter la console** : fermer la fenêtre noire.
3. **Récupérer le code avec Git.** Ouvrir PowerShell et saisir, ligne par ligne :

   ```
   cd $HOME\Documents
   git clone -b claude/suivi-reindexations-loyers-ag5rbc https://github.com/Compta-fin-ops/INDEXATION_LOYER.git console-loyers-git
   ```

   Le dépôt étant privé, une fenêtre s'ouvre la première fois pour s'identifier sur GitHub.
4. **Reprendre vos données.** Depuis le dossier `data` de l'ancienne installation, copier vers le dossier `data`
   de la nouvelle (`Documents\console-loyers-git\data`) :
   - le fichier `cabinet.sqlite` ;
   - le dossier `pieces` ;
   - le dossier `sauvegardes`, s'il existe.

   **Ne pas copier le dossier `indices`** : celui de la nouvelle installation est plus récent, et l'écraser
   bloquerait les mises à jour.
5. **Lancer** par un double-clic sur `lancer_console.bat` dans le nouveau dossier. Il prépare de nouveau
   l'environnement, quelques minutes. Les comptes sont dans la base : même identifiant, même mot de passe.
6. Vérifier que les dossiers et les baux sont là, puis supprimer l'ancien dossier d'installation.

**Ensuite, pour chaque mise à jour** : fermer la console, double-cliquer sur `mettre_a_jour.bat`, relancer.
Le script sauvegarde la base avant de commencer et refuse d'agir en cas de doute : il ne supprime jamais rien.

> Comme le lanceur, `mettre_a_jour.bat` n'a pas pu être essayé sur Windows.

### Trois façons d'obtenir Git

| Voie | Pour qui | Mise à jour ensuite |
|---|---|---|
| Installateur de git-scm.com | Voie décrite ci-dessus | Double-clic sur `mettre_a_jour.bat` |
| Une commande dans PowerShell : `winget install --id Git.Git -e` | Évite le site et l'assistant d'installation ; demande Windows 10 ou 11 à jour | Double-clic sur `mettre_a_jour.bat` |
| GitHub Desktop, depuis <https://desktop.github.com> | Aucune ligne de commande, tout à la souris | Dans l'application : « Fetch origin », puis « Pull origin » |

Avec GitHub Desktop : se connecter avec son compte GitHub, menu « File », « Clone repository », choisir
`INDEXATION_LOYER` et un dossier dans `Documents`, puis sélectionner la branche
`claude/suivi-reindexations-loyers-ag5rbc` dans « Current Branch ». Reprendre ensuite les données comme à
l'étape 4 ci-dessus. `mettre_a_jour.bat` ne fonctionne pas avec cette voie : GitHub Desktop garde son propre Git.

Quelle que soit la voie, `lancer_console.bat` installe de lui-même les composants qu'une nouvelle version
réclamerait : après une mise à jour, il suffit de le relancer.

## 4. Se connecter, puis créer les autres comptes

1. Saisir l'identifiant et le mot de passe créés à l'étape 3.
2. Menu « Comptes » : créer un compte par personne, avec un mot de passe provisoire à transmettre de vive
   voix. Il devra être changé à la première connexion.

| Rôle | Ce qu'il permet |
|---|---|
| Lecture | Consulter tout, ne rien modifier |
| Gestion | Saisir les baux, geler une révision, joindre des pièces, éditer les courriers |
| Validation | Gestion, plus la création d'abonnements dans Pennylane |
| Administrateur | Validation, plus la gestion des comptes |

Tant que la console tourne sur un seul poste, ces comptes servent à s'exercer et à préparer la suite : seules
les personnes assises à ce poste peuvent s'y connecter.

## 5. S'exercer sans risque : le portefeuille fictif

Dans une base séparée, pour ne jamais mélanger avec les vrais dossiers :

```
.venv\Scripts\python -m console --base data\demo.sqlite demo
.venv\Scripts\python -m console --base data\demo.sqlite utilisateur creer
.venv\Scripts\python -m console --base data\demo.sqlite serve
```

## 6. Au quotidien

| Besoin | Geste |
|---|---|
| Ouvrir la console | Double-clic sur `lancer_console.bat` |
| L'arrêter | Fermer la fenêtre noire |
| Mot de passe oublié par un collaborateur | Menu « Comptes » : nouveau mot de passe provisoire |
| Mot de passe oublié par le seul administrateur | `.venv\Scripts\python -m console --base data\cabinet.sqlite utilisateur mot-de-passe IDENTIFIANT` |
| Sauvegarder | `.venv\Scripts\python -m console --base data\cabinet.sqlite sauvegarder`, et copier le dossier `data\pieces` |
| Mettre à jour la console | Installation Git : double-clic sur `mettre_a_jour.bat`. Installation ZIP : télécharger le nouveau ZIP et copier ses fichiers par-dessus ; le dossier `data` est fusionné, rien n'y est supprimé |

Le dossier `data` contient tout ce qui vous appartient : la base (`cabinet.sqlite`), les pièces jointes
(`pieces`) et les sauvegardes. Rien d'autre n'est à conserver.

## Ce que cette installation ne fait pas

- **Pas d'accès depuis un autre poste.** Il faudra un serveur et une connexion chiffrée (HTTPS) : sans elle,
  les mots de passe circuleraient en clair sur le réseau. C'est l'étape d'hébergement.
- **Pas de connexion à Pennylane** tant qu'aucun jeton n'est déclaré sur le poste. La console fonctionne sans :
  baux, révisions, courriers et pièces jointes n'en dépendent pas.

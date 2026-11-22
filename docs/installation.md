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
| Mettre à jour la console | Télécharger le nouveau ZIP et remplacer les fichiers, **en conservant le dossier `data`** |

Le dossier `data` contient tout ce qui vous appartient : la base (`cabinet.sqlite`), les pièces jointes
(`pieces`) et les sauvegardes. Rien d'autre n'est à conserver.

## Ce que cette installation ne fait pas

- **Pas d'accès depuis un autre poste.** Il faudra un serveur et une connexion chiffrée (HTTPS) : sans elle,
  les mots de passe circuleraient en clair sur le réseau. C'est l'étape d'hébergement.
- **Pas de connexion à Pennylane** tant qu'aucun jeton n'est déclaré sur le poste. La console fonctionne sans :
  baux, révisions, courriers et pièces jointes n'en dépendent pas.

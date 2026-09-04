# YT Audio DLer

Application de bureau personnelle pour télécharger l’audio de vidéos et de playlists YouTube avec [yt-dlp](https://github.com/yt-dlp/yt-dlp).

> **Utilisation responsable**
>
> Vérifiez que vous avez le droit de télécharger le contenu concerné et respectez les conditions d’utilisation de YouTube ainsi que les lois applicables. Ce projet n’est pas affilié à YouTube ou Google.

## Fonctionnalités

- Charger les informations d’une vidéo ou d’une playlist YouTube.
- Sélectionner les titres à télécharger et les organiser dans une file d’attente.
- Déplacer, supprimer ou vider les éléments en attente.
- Choisir le format `Best (original)`, `MP3 320kbps`, `MP3 256kbps`, `M4A`, `FLAC` ou `OPUS`.
- Ignorer les fichiers déjà présents ou forcer leur retéléchargement.
- Vérifier la disponibilité des titres et filtrer les éléments indisponibles.
- Mettre en pause, reprendre ou arrêter les téléchargements.
- Reprendre la file d’attente après un redémarrage grâce à la sauvegarde de session.
- Consulter l’historique des playlists récemment chargées (jusqu’à 20 entrées).
- Ouvrir les fichiers téléchargés avec le lecteur audio par défaut.
- Consulter le diagnostic intégré de `yt-dlp` et de `FFmpeg`.

## Prérequis

- Python 3.10 ou une version plus récente.
- [FFmpeg](https://ffmpeg.org/) installé et accessible dans le `PATH`. Il est requis pour les conversions audio vers MP3, M4A, FLAC et OPUS.
- Tkinter, généralement inclus avec Python. Sur certaines distributions Linux, le paquet système `python3-tk` doit être installé séparément.

`yt-dlp` est installé depuis `requirements.txt`.

## Installation

Depuis la racine du projet :

```bash
python -m venv .venv
```

Activation de l’environnement virtuel :

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

Installation des dépendances Python :

```bash
python -m pip install -r requirements.txt
```

Vérifiez ensuite que FFmpeg est disponible :

```bash
ffmpeg -version
```

## Lancement

```bash
python main.py
```

## Utilisation rapide

1. Collez l’URL d’une vidéo ou d’une playlist dans le champ **URL YouTube**.
2. Cliquez sur **Charger les infos**.
3. Sélectionnez les titres souhaités, puis cliquez sur **Ajouter à la file d’attente**.
4. Choisissez le dossier de sortie et le format audio.
5. Choisissez le mode de vitesse, puis cliquez sur **Démarrer**.

Le dossier de sortie est initialisé avec le dossier personnel de l’utilisateur. Il est créé automatiquement s’il n’existe pas.

Le bouton **Vérifier disponibilité** permet de contrôler les titres privés, supprimés, protégés par une connexion ou soumis à une restriction d’âge. Les éléments indisponibles ne sont pas ajoutés à la file.

## Modes de vitesse

- **Turbo** : aucun délai volontaire entre deux téléchargements.
- **Normal** : délai aléatoire de 20 à 45 secondes.
- **Doux** : délai d’au moins 60 secondes, ajusté à la durée du titre.
- **Très doux** : délai d’au moins 120 secondes, plus long pour les titres volumineux.
- **Personnalisé** : réglage du délai minimum, du multiplicateur de durée et des bornes aléatoires.

Ces délais ralentissent volontairement la file entre les titres ; ils ne modifient pas la vitesse de téléchargement d’un titre en cours.

## Fichiers générés

Les téléchargements utilisent le modèle suivant :

```text
Titre de la vidéo [ID_YouTube].extension
```

Exemple :

```text
CVIIXXX x CVIIXXX [dQw4w9WgXcQ].mp3
```

Les fichiers suivants sont créés automatiquement à la racine du projet :

- `session.json` : file d’attente, paramètres et progression de la session.
- `playlist_history.json` : historique des playlists chargées.

## Structure du projet

```text
YT Audio DLer/
├── main.py                    # Point d’entrée
├── gui/
│   └── main_window.py         # Interface Tkinter
├── core/
│   └── downloader.py          # Extraction et téléchargements yt-dlp
├── utils/
│   └── helpers.py             # Diagnostic et fonctions utilitaires
├── requirements.txt           # Dépendances Python
├── session.json               # Généré après sauvegarde d’une session
└── playlist_history.json      # Généré après chargement d’une playlist
```

## Dépannage

Utilisez le bouton **Environnement** dans l’application pour vérifier les versions et la disponibilité de `yt-dlp` et `FFmpeg`.

- Si `yt-dlp` est absent de l’environnement utilisé pour lancer l’application, activez `.venv` puis exécutez `python -m pip install -r requirements.txt`.
- Si une conversion échoue, vérifiez que la commande `ffmpeg` fonctionne dans le même terminal.
- Le fonctionnement dépend de `yt-dlp` et de l’évolution de YouTube ; une mise à jour peut être nécessaire avec `python -m pip install -U yt-dlp`.

## Licence

Projet personnel, fourni sans garantie.
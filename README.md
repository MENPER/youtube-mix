# Studio Mix

Application web pour assembler **des fichiers vidéo et audio que vous êtes autorisé·e à utiliser**. Le navigateur statique est hébergeable sur GitHub Pages; l’API Python utilise FFmpeg. L’import depuis YouTube n’est volontairement pas implémenté : aucun extracteur n’est inclus. Les pistes audio importées sont normalisées en AAC et conservées dans la bibliothèque; celles-ci sont tronquées à 20 minutes maximum. Les rendus temporaires sont supprimés après expiration.

> Un déploiement gratuit ne garantit pas une disponibilité 24 h/24. Hugging Face Spaces peut mettre une application en veille, redémarrer le conteneur ou fournir un stockage éphémère. Pour une bibliothèque persistante et un service toujours disponible, prévoir un stockage persistant et un hébergement adapté.

## Architecture

- `frontend/` : HTML/CSS/JavaScript sans compilation, adapté aux écrans mobiles et à Safari iOS.
- `backend/main.py` : API FastAPI, bibliothèque audio, normalisation et rendu MP4.
- `backend/Dockerfile` : image Python 3.11 avec FFmpeg, port 7860 pour un Docker Space.

L’API attend les médias en multipart upload. Elle n’effectue pas de récupération depuis des URL fournies par l’utilisateur. La vidéo finale utilise H.264, `yuv420p`, AAC et `+faststart`; l’API sert le fichier MP4 avec prise en charge des requêtes Range fournie par Starlette pour le seek et le streaming progressif.

## Utilisation locale

Prérequis : Python 3.11+, FFmpeg et FFprobe accessibles dans le `PATH`.

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data CORS_ORIGINS=http://localhost:8001 uvicorn main:app --host 127.0.0.1 --port 8000
```

Puis servir le frontend, par exemple dans un second terminal :

```bash
cd frontend
python -m http.server 8001
```

Ouvrir `http://localhost:8001`, utiliser ⚙ et définir l’API à `http://127.0.0.1:8000`. Endpoint de diagnostic : `GET /health`.

### Fonctionnement

1. Choisir un fichier vidéo sur l’appareil.
2. Réutiliser une piste de la bibliothèque, ou importer un fichier audio. L’import crée une copie AAC dans la bibliothèque et limite sa durée à 1200 secondes.
3. Ajuster le gain de 0 à 200 %, puis générer le rendu. L’audio boucle jusqu’à la fin de la vidéo.
4. Lire le MP4 dans le lecteur natif ou le télécharger.

Formats d’entrée acceptés selon les codecs disponibles dans FFmpeg. Les limites par défaut sont de 1 Gio par fichier et un travail FFmpeg concurrent. Les réglages sont modifiables via les variables d’environnement décrites ci-dessous.

## Déployer le backend sur Hugging Face Spaces

1. Créer un Space et choisir **Docker** comme SDK.
2. Copier `backend/main.py`, `backend/requirements.txt` et `backend/Dockerfile` à la racine du dépôt du Space (le Dockerfile fourni attend `main.py` et `requirements.txt` dans le même répertoire).
3. Dans les variables du Space, ajouter `CORS_ORIGINS` avec l’origine exacte du site GitHub Pages, par exemple `https://<compte>.github.io` (et non l’URL d’une page/projet).
4. Le conteneur écoute sur le port 7860. Vérifier `https://<espace>.hf.space/health`.
5. Activer un stockage persistant si la bibliothèque doit survivre aux redémarrages. `DATA_DIR` vaut `/data` par défaut; sans volume persistant, les pistes et métadonnées peuvent être perdues.

Les plans gratuits ont des quotas et peuvent mettre le Space en veille : ce n’est pas un hébergement garanti H24. Pour les médias volumineux ou un usage continu, configurez un volume persistant et une offre adaptée. N’exposez pas cette API publiquement sans contrôle d’accès/quotas si elle doit rester privée : le traitement vidéo consomme CPU, RAM et espace disque.

## Déployer le frontend sur GitHub Pages

1. Copier le contenu de `frontend/` à la racine d’un dépôt GitHub (ou configurer Pages pour publier le dossier frontend via GitHub Actions).
2. Dans GitHub, ouvrir **Settings → Pages**, sélectionner la branche et le dossier de publication.
3. Ouvrir l’URL publique, cliquer sur ⚙, renseigner l’URL HTTPS du backend et enregistrer.
4. Recharger; la bibliothèque est alors chargée depuis l’API.

CORS doit autoriser l’origine du site (schéma et hôte, sans chemin). GitHub Pages sert généralement en HTTPS : utilisez une URL HTTPS pour l’API afin d’éviter le blocage de contenu mixte.

## Configuration API

| Variable | Valeur par défaut | Description |
|---|---:|---|
| `DATA_DIR` | `/data` | Répertoire de la bibliothèque et des rendus temporaires |
| `CORS_ORIGINS` | `*` | Liste CSV des origines autorisées; en production, préciser le site Pages |
| `MAX_UPLOAD_BYTES` | 1073741824 | Taille maximale d’un upload, en octets |
| `MAX_CONCURRENT_JOBS` | `1` | Nombre maximal de rendus FFmpeg simultanés |
| `TEMP_TTL_SECONDS` | `3600` | Durée avant purge des dossiers de rendu abandonnés ou expirés |
| `FFMPEG_BIN` / `FFPROBE_BIN` | `ffmpeg` / `ffprobe` | Chemins des binaires média |

Les sorties sont supprimées par le nettoyage périodique après le TTL; les données audio de bibliothèque ne sont pas purgées automatiquement. Sauvegardez `DATA_DIR` si la bibliothèque compte. La bibliothèque est un stockage local simple, pas un service multi-utilisateur transactionnel.

## Routes

- `GET /health` — état du service et mention `youtube_import: not_configured`.
- `GET /library` — pistes audio disponibles.
- `POST /library` — multipart `file` obligatoire, `title` facultatif; ajoute une piste à la bibliothèque.
- `GET /library/{audio_id}/audio` — données audio pour pré-écoute.
- `POST /process` — multipart `video`, puis exactement l’un de `audio_id` ou `audio`, et `volume` entre `0` et `2`.
- `GET /stream/{video_id}` — MP4 généré; prend en charge les plages HTTP pour la lecture/seek.

`/process` est une requête synchrone : le navigateur affiche un état de traitement jusqu’à ce que FFmpeg ait terminé. Les rendus réussis expirent selon `TEMP_TTL_SECONDS`.

## Sécurité et limites

- Aucune URL distante ou URL YouTube n’est téléchargée ou transmise à FFmpeg. L’intégration YouTube n’est pas disponible.
- Les noms de fichiers de l’utilisateur ne servent jamais de chemins de stockage ni de paramètres shell; les commandes sont exécutées comme tableaux d’arguments, sans shell.
- Les limites d’upload sont vérifiées pendant la lecture. Ajoutez en plus des quotas, authentification et limites réseau avant toute exposition publique.
- Les formats/codecs et la taille maximale dépendent des ressources disponibles sur le serveur. La génération est gourmande en CPU.
- Les fichiers du dépôt et la bibliothèque sont distincts; le nettoyage ne cible que les dossiers de jobs.

## Vérifications manuelles

- `GET /health` et `GET /library` répondent sur l’API.
- Importer une piste de plus de 20 minutes : la durée retournée ne dépasse pas 1200 s.
- Générer un mix avec un média test autorisé; vérifier le lecteur et le téléchargement. Avec `ffprobe`, contrôler codecs H.264/AAC, pixel format `yuv420p` et durée de sortie égale à la vidéo.
- Faire une requête `Range: bytes=0-1023` sur `/stream/{id}` et vérifier une réponse partielle `206`.
- Vérifier qu’un dossier de rendu temporaire est purgé après `TEMP_TTL_SECONDS`; la piste importée reste dans la bibliothèque.

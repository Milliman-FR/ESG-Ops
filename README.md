# ESG File Downloader

Application de bureau Python (Tkinter) pour télécharger des fichiers ESG.

## Prérequis

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Tkinter disponible avec Python 3.12 (inclus dans l'installation Windows de Python gérée par uv).

## Installation et lancement

Depuis la racine du dépôt :

```powershell
uv sync
uv run python esg_file_downloader.py
```

uv utilise Python 3.12 indiqué dans `.python-version`, crée `.venv` et installe les dépendances définies dans `pyproject.toml` conformément à `uv.lock`. Renseigner l'URL du projet et le jeton Bearer dans l'interface ; ne pas enregistrer de jeton dans le dépôt.

## Exécution dans Ops

Le point d'entrée `main.py` permet de télécharger **une table par exécution**, sans interface graphique. Les champs Ops sont : `ProjectUrl` (URL complète du projet ESG), `TableId` (identifiant de la table) et `SensitivityId` (facultatif). La table doit avoir une opération exploitable ; sa version et son univers sont récupérés automatiquement. Tous les chemins de fichiers retournés par l'API sont téléchargés, sans filtrage par dossier.

Le jeton Bearer brut (sans préfixe `Bearer`) est lu depuis la variable d'environnement `TOKEN`. Pour les essais locaux seulement, le fichier `.env` à la racine peut contenir une ligne `TOKEN=<jeton>` : le nom et le signe `=` sont indispensables. Si la variable d'environnement existe déjà, elle a priorité sur ce fichier. **Ne jamais ajouter `.env` au dépôt ni au ZIP envoyé à Ops** : il est exclu par `.gitignore` et `.ops/.opsignore`. Avant le déploiement, prévoir une injection sécurisée de `TOKEN` dans l'environnement d'exécution Ops (ou un fichier `.env` monté hors de l'archive du modèle). La spécification de package fournie ne décrit pas cette configuration de secrets ; elle doit être confirmée avec l'administrateur Ops. Vérifier aussi l'accès réseau HTTPS du conteneur à l'API ESG (par exemple `https://esg-caa.milliman-mind.com/p/...`).

Ops exécute `python main.py run <input_directory> <output_directory>`. Il produit une archive `esg_download.zip` (arborescence conservée) et un bilan `download_summary.json` dans le répertoire de sortie. En cas d'erreur API, le processus échoue plutôt que de publier une archive partielle. Les requêtes HTTPS vérifient le certificat par défaut (contrairement à l'interface locale historique).
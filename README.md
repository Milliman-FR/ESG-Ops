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

Le point d'entrée `main.py` permet de télécharger **une table par exécution**, sans interface graphique. Les champs Ops sont : `ProjectUrl` (URL complète du projet ESG), `Token` (jeton Bearer brut, sans le préfixe `Bearer`), `TableId` (identifiant de la table) et `SensitivityId` (facultatif). La table doit avoir une opération exploitable ; sa version et son univers sont récupérés automatiquement. Tous les chemins de fichiers retournés par l'API sont téléchargés, sans filtrage par dossier.

Le champ `Token` est une entrée Ops de type texte ordinaire : la spécification fournie ne prévoit pas de champ secret. Sa valeur sera donc potentiellement conservée dans les entrées ou l'historique des runs Ops ; **ne l'utiliser qu'après validation de cette exposition avec l'administrateur Ops**. Le script ne place pas le jeton dans les fichiers de sortie et ne l'affiche pas dans les logs. Le fichier local `.env` n'est ni lu par le point d'entrée Ops, ni inclus dans le dépôt ou le ZIP du modèle. Vérifier également l'accès réseau HTTPS du conteneur à l'API ESG (par exemple `https://esg-caa.milliman-mind.com/p/...`).

Ops exécute `python main.py run <input_directory> <output_directory>`. Il produit une archive `esg_download.zip` (arborescence conservée) et un bilan `download_summary.json` dans le répertoire de sortie. En cas d'erreur API, le processus échoue plutôt que de publier une archive partielle. Les requêtes HTTPS vérifient le certificat par défaut (contrairement à l'interface locale historique).

Pour reproduire un run local sur Windows : `uv run --locked python main.py run inputs/input <output_directory>`. La commande utilise le magasin de certificats Windows pour valider HTTPS et garde une même session HTTP pour les appels successifs à l'API. Le répertoire `inputs/` est exclu du dépôt et du package Ops ; il doit être préparé séparément pour les essais locaux.
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

Les logs de run détaillent les appels API : recherche des tables du projet (GET), liste des fichiers de la table et de la sensibilité éventuelle (GET), puis téléchargement (POST). Ils indiquent l'URL sans en-têtes d'authentification : le jeton Bearer n'est jamais affiché. Après téléchargement, les fichiers `.fac` directement situés dans `MLM_Data_MLM_Param_311225/RN_outputs/Tables/` à la racine de l'archive sont copiés dans le sous-dossier de sortie `tables/` et exposés individuellement dans Ops comme fichiers binaires (`RNOutputTables`). Ils restent également dans le ZIP. Si le dossier est absent de l'archive, aucune table individuelle n'est produite ; le ZIP et le bilan restent disponibles.

Pour reproduire un run local sur Windows : `uv run --locked python main.py run inputs/input <output_directory>`. La commande utilise le magasin de certificats Windows pour valider HTTPS et garde une même session HTTP pour les appels successifs à l'API. Le répertoire `inputs/` est exclu du dépôt et du package Ops ; il doit être préparé séparément pour les essais locaux.
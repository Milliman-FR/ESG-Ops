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

Le point d'entrée `main.py` permet de télécharger **une table par exécution**, sans interface graphique. Les champs Ops sont : `ProjectUrl` (URL complète du projet ESG), `Token` (jeton Bearer brut, sans le préfixe `Bearer`), `TableId` (identifiant de la table) et `SensitivityId` (facultatif). Pour le projet CAA, renseigner `https://esg.milliman-mind.com/p/609add27-24e1-47e6-b4dc-7a5763a45fa1/t` : le code traduit cet hôte générique en `esg-caa.milliman-mind.com` uniquement pour les requêtes. Les URL directes `esg-*.milliman-mind.com` restent acceptées. La table doit avoir une opération exploitable ; sa version et son univers sont récupérés automatiquement. Tous les chemins de fichiers retournés par l'API sont téléchargés, sans filtrage par dossier.

Le champ `Token` est une entrée Ops de type texte ordinaire : la spécification fournie ne prévoit pas de champ secret. Sa valeur sera donc potentiellement conservée dans les entrées ou l'historique des runs Ops ; **ne l'utiliser qu'après validation de cette exposition avec l'administrateur Ops**. Le script ne place pas le jeton dans les fichiers de sortie et ne l'affiche pas dans les logs. Le fichier local `.env` n'est ni lu par le point d'entrée Ops, ni inclus dans le dépôt ou le ZIP du modèle. Vérifier également l'accès réseau HTTPS du conteneur à l'API ESG (par exemple `https://esg-caa.milliman-mind.com/p/...`).

Ops exécute `python main.py run <input_directory> <output_directory>`. Il produit une archive `esg_download.zip` (arborescence conservée) et un bilan `download_summary.json` dans le répertoire de sortie. En cas d'erreur API, le processus échoue plutôt que de publier une archive partielle. Les requêtes HTTPS vérifient le certificat par défaut (contrairement à l'interface locale historique).

Les logs de run détaillent les appels API : recherche des tables du projet (GET), liste des fichiers de la table et de la sensibilité éventuelle (GET), puis téléchargement (POST). Ils indiquent l'URL sans en-têtes d'authentification : le jeton Bearer n'est jamais affiché. Pour la lecture des logs uniquement, `esg-caa` est remplacé par `esg` ; les appels HTTP utilisent toujours l'URL réelle. Après téléchargement, les fichiers `.fac` directement situés dans un dossier `<nom de table>/RN_outputs/Tables/` à la racine de l'archive sont copiés dans le sous-dossier de sortie `tables/`. Les sorties binaires déclarées dans la catégorie Ops « RN Output » sont distinctes par type (courbes de spread, matrices de transition, tables, ZCB et leurs variantes CEV) ; les noms logiques ZCB ne dépendent plus du client ni de la date. Les noms de fichiers d'origine sont conservés et les fichiers restent également dans le ZIP. Si aucun tel dossier n'est présent dans l'archive, aucune table individuelle n'est produite ; le ZIP et le bilan restent disponibles.

Pour reproduire un run local sur Windows : `uv run --locked python main.py run inputs/input <output_directory>`. La commande utilise le magasin de certificats Windows pour valider HTTPS et garde une même session HTTP pour les appels successifs à l'API. Le répertoire `inputs/` est exclu du dépôt et du package Ops ; il doit être préparé séparément pour les essais locaux.

Le fichier statique `ESG_311225_Central_VA_sto.csv`, fourni sous `tests/`, est également copié en sortie Ops (catégorie « RN Output », type CSV, séparateur `;`), sans message dans les logs du run. Son volume dépasse 100 Mo : `.gitattributes` le destine à Git LFS. Pour un modèle installé depuis Git, vérifier que l'intégration Ops récupère les **contenus Git LFS**, pas seulement les pointeurs ; sinon, utiliser un ZIP de modèle contenant le CSV réel. Le programme refuse explicitement un pointeur LFS non matérialisé.
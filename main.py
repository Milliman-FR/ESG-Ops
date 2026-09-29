"""Headless ESG download entry point for Milliman Ops."""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit
from zipfile import BadZipFile, ZipFile

import requests

if sys.platform == "win32":
    import truststore

    truststore.inject_into_ssl()

from esg_api import extract_table_metadata, parse_project_url

TABLES_SOURCE = re.compile(r"^[A-Za-z0-9_.-]+/RN_outputs/Tables/([^/]+\.fac)$")
CONFIG = Path(__file__).resolve().parent / "tests" / "config.csv"


def log(message: str, *, file=None) -> None:
    """Mask the CAA hostname in displayed logs, not in real API requests."""
    print(message.replace("esg-caa", "esg"), file=file, flush=True)


def log_step(number: int, message: str) -> None:
    """Show API progress in Ops immediately; never include request headers."""
    log(f"[{number}/4] {message}")


def extract_tables(archive: ZipFile, output_directory: Path) -> list[str]:
    """Expose direct .fac files in a table's RN_outputs/Tables folder."""
    matches = [
        (member, match.group(1))
        for member in archive.infolist()
        if not member.is_dir() and (match := TABLES_SOURCE.fullmatch(member.filename))
    ]
    names = [name for _, name in matches]
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.fac", name) for name in names):
        raise ValueError("Nom de table non sûr dans l'archive ESG.")
    if len(names) != len(set(names)):
        raise ValueError("Noms de tables en double dans l'archive ESG.")

    tables_directory = output_directory / "tables"
    if matches:
        tables_directory.mkdir(parents=True, exist_ok=True)
        try:
            for member, name in matches:
                with archive.open(member) as source, (tables_directory / name).open("wb") as target:
                    shutil.copyfileobj(source, target)
        except (OSError, BadZipFile):
            for name in names:
                (tables_directory / name).unlink(missing_ok=True)
            raise
    return names


def read_inputs(input_directory: Path) -> dict[str, str]:
    """Read the scalar values supplied by Ops, without logging their contents."""
    with (input_directory / "inputs.json").open(encoding="utf-8") as stream:
        payload = json.load(stream)
    entries = payload["inputs"]
    if not isinstance(entries, list):
        raise TypeError("Le champ inputs doit être une liste.")

    values = {}
    for entry in entries:
        if entry.get("category") == "parameters" and entry.get("name") in {
            "ProjectUrl", "Token", "TableId", "SensitivityId"
        }:
            values[entry["name"]] = str(entry.get("value") or "").strip()
    for required in ("ProjectUrl", "Token", "TableId"):
        if not values.get(required):
            raise ValueError(f"Entrée obligatoire absente : {required}")
    return values


def download(input_directory: Path, output_directory: Path) -> dict:
    """Download the selected table as a single ZIP into the Ops output volume."""
    values = read_inputs(input_directory)
    with CONFIG.open("rb") as source:
        if source.read(64).startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError("Fichier Git LFS non matérialisé dans le package Ops.")
    base_url, project_id = parse_project_url(values["ProjectUrl"])
    parsed = urlsplit(base_url or "")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or (
            parsed.netloc.lower() != "esg.milliman-mind.com"
            and not (
                parsed.hostname.startswith("esg-")
                and parsed.hostname.endswith(".milliman-mind.com")
            )
        )
        or parsed.username
        or parsed.password
        or not project_id
    ):
        raise ValueError("ProjectUrl doit être une URL HTTPS ESG sur milliman-mind.com.")

    if parsed.netloc.lower() == "esg.milliman-mind.com":
        base_url = "https://esg-caa.milliman-mind.com"

    with requests.Session() as session:
        return _download_with_session(session, values, base_url, project_id, output_directory)


def _download_with_session(session, values, base_url, project_id, output_directory):
    """Reuse one verified TLS connection for metadata, listing and ZIP requests."""
    headers = {"Authorization": f"Bearer {values['Token']}", "Accept": "application/json"}
    project_path = f"{base_url}/api/projects/{quote(project_id, safe='')}"
    log_step(1, f"GET {project_path}/operations/tables — récupération des tables du projet")
    states_response = session.get(
        f"{project_path}/operations/tables", headers=headers, timeout=30
    )
    states_response.raise_for_status()
    tables = extract_table_metadata(states_response.json())
    log(f"Tables exploitables : {len(tables)} ; TableId demandé : {values['TableId']}")
    table = next(
        (item for item in tables if str(item["tableId"]) == values["TableId"]), None
    )
    if table is None:
        raise ValueError("TableId introuvable parmi les tables exploitables du projet.")

    files_url = (
        f"{project_path}/operations/tables/{quote(str(table['tableId']), safe='')}"
        f"/zip/files/{quote(table['universe'], safe='')}"
        f"/{quote(str(table['versionId']), safe='')}"
    )
    params = {"sensitivityId": values["SensitivityId"]} if values.get("SensitivityId") else None
    log(
        f"Table sélectionnée : {table['tableName']} ; univers : {table['universe']} ; "
        f"version : {table['versionId']} ; sensibilité : {values.get('SensitivityId') or 'aucune'}"
    )
    log_step(2, f"GET {files_url} — liste des fichiers (sensibilité : {values.get('SensitivityId') or 'aucune'})")
    listing = session.get(files_url, headers=headers, params=params, timeout=30)
    listing.raise_for_status()
    file_paths = listing.json()
    if not isinstance(file_paths, list) or not file_paths or not all(
        isinstance(path, str) and path for path in file_paths
    ):
        raise ValueError("Aucun fichier téléchargeable retourné par l'API.")
    log(f"Fichiers disponibles : {len(file_paths)}")

    output_directory.mkdir(parents=True, exist_ok=True)
    archive_path = output_directory / "esg_download.zip"
    log_step(3, f"POST {files_url} — téléchargement du ZIP ({len(file_paths)} fichiers demandés)")
    try:
        with session.post(
            files_url,
            headers={**headers, "Content-Type": "application/json", "Accept": "application/octet-stream"},
            params=params,
            json={"filePaths": file_paths},
            timeout=(30, 300),
            stream=True,
        ) as response:
            response.raise_for_status()
            with archive_path.open("wb") as destination:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        destination.write(chunk)
        with ZipFile(archive_path) as archive:
            if not archive.namelist():
                raise ValueError("L'archive retournée est vide.")
            log_step(4, "Extraction des fichiers .fac des dossiers RN_outputs/Tables vers les sorties Ops")
            table_names = extract_tables(archive, output_directory)
    except (OSError, requests.RequestException, BadZipFile, ValueError):
        archive_path.unlink(missing_ok=True)
        raise
    log(f"Tables .fac exposées : {len(table_names)}")

    summary = {
        "projectId": project_id,
        "tableId": str(table["tableId"]),
        "tableName": table["tableName"],
        "universe": table["universe"],
        "versionId": str(table["versionId"]),
        "sensitivityId": values.get("SensitivityId") or None,
        "requestedFileCount": len(file_paths),
        "archive": archive_path.name,
        "tables": [f"tables/{name}" for name in table_names],
    }
    with (output_directory / "download_summary.json").open("w", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    shutil.copyfile(CONFIG, output_directory / "ESG_Central_VA_det.csv")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Télécharger une table ESG dans Ops")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("input_directory", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    try:
        summary = download(args.input_directory.resolve(), args.output_directory.resolve())
    except requests.RequestException as exc:
        log(f"Erreur API : {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    log(f"Archive créée : {summary['archive']} ({summary['requestedFileCount']} fichiers demandés)")


if __name__ == "__main__":
    main()
"""Headless ESG download entry point for Milliman Ops."""

import argparse
import json
from pathlib import Path
from urllib.parse import quote, urlsplit
from zipfile import BadZipFile, ZipFile

import requests

from esg_api import extract_table_metadata, parse_project_url


def read_inputs(input_directory: Path) -> dict[str, str]:
    """Read the scalar values supplied by Ops, without logging their contents."""
    with (input_directory / "inputs.json").open(encoding="utf-8") as stream:
        payload = json.load(stream)
    entries = payload["inputs"]
    if not isinstance(entries, list):
        raise ValueError("Le champ inputs doit être une liste.")

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
    base_url, project_id = parse_project_url(values["ProjectUrl"])
    parsed = urlsplit(base_url or "")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.startswith("esg-")
        or not parsed.hostname.endswith(".milliman-mind.com")
        or parsed.username
        or parsed.password
        or not project_id
    ):
        raise ValueError("ProjectUrl doit être une URL HTTPS ESG sur milliman-mind.com.")

    headers = {"Authorization": f"Bearer {values['Token']}", "Accept": "application/json"}
    project_path = f"{base_url}/api/projects/{quote(project_id, safe='')}"
    states_response = requests.get(
        f"{project_path}/operations/tables", headers=headers, timeout=30
    )
    states_response.raise_for_status()
    tables = extract_table_metadata(states_response.json())
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
    listing = requests.get(files_url, headers=headers, params=params, timeout=30)
    listing.raise_for_status()
    file_paths = listing.json()
    if not isinstance(file_paths, list) or not file_paths or not all(
        isinstance(path, str) and path for path in file_paths
    ):
        raise ValueError("Aucun fichier téléchargeable retourné par l'API.")

    output_directory.mkdir(parents=True, exist_ok=True)
    archive_path = output_directory / "esg_download.zip"
    try:
        with requests.post(
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
    except (OSError, requests.RequestException, BadZipFile, ValueError):
        archive_path.unlink(missing_ok=True)
        raise

    summary = {
        "projectId": project_id,
        "tableId": str(table["tableId"]),
        "tableName": table["tableName"],
        "universe": table["universe"],
        "versionId": str(table["versionId"]),
        "sensitivityId": values.get("SensitivityId") or None,
        "requestedFileCount": len(file_paths),
        "archive": archive_path.name,
    }
    with (output_directory / "download_summary.json").open("w", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Télécharger une table ESG dans Ops")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("input_directory", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    summary = download(args.input_directory.resolve(), args.output_directory.resolve())
    print(f"Archive créée : {summary['archive']} ({summary['requestedFileCount']} fichiers demandés)")


if __name__ == "__main__":
    main()
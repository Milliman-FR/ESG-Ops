"""Helpers shared by the desktop downloader and the headless Ops runner."""

import re


def parse_project_url(value: str):
    """Extract the ESG API origin and project ID from a project URL."""
    match = re.match(r"^(https?://[^/]+)/p/([^/?#]+)", value.strip())
    if not match:
        return None, None
    return match.group(1), match.group(2)


def normalize_universe_for_files_api(raw_universe):
    """Convert API universe names to the values accepted by the ZIP endpoint."""
    if not raw_universe:
        return "RW"

    value = str(raw_universe).strip()
    return {
        "RealWorld": "RW",
        "RiskNeutral": "RN",
        "RW": "RW",
        "RN": "RN",
        "realworld": "RW",
        "riskneutral": "RN",
    }.get(value, value)


def extract_table_metadata(op_states):
    """Return completed tables, preferring rows without a sensitivity ID."""
    by_table = {}
    for state in op_states:
        table_id = state.get("tableId")
        table_name = state.get("tableName")
        version_id = state.get("versionId")
        status = state.get("lastOperationStatus")
        sensitivity_id = state.get("sensitivityId")

        if not table_id or not table_name or not version_id:
            continue
        if status is not None and status != 2:
            continue

        raw_universe = (
            state.get("universe")
            or state.get("universeType")
            or state.get("projectionUniverse")
            or "RW"
        )
        candidate = {
            "tableId": table_id,
            "tableName": table_name,
            "versionId": version_id,
            "universe": normalize_universe_for_files_api(raw_universe),
            "rawUniverse": raw_universe,
            "isSensitivityRow": sensitivity_id is not None,
            "source": state,
        }
        if table_id not in by_table or (
            by_table[table_id]["isSensitivityRow"] and sensitivity_id is None
        ):
            by_table[table_id] = candidate

    return sorted(by_table.values(), key=lambda item: item["tableName"].lower())
#!/usr/bin/env python3
"""Prepare a reviewed notebook assignment without changing its source files.

This tool applies a teacher-reviewed assignment configuration to a copy of the
original notebook. The prepared notebook is an input for later provisioning
and dynamic JupyterLab policy support; it does not change current extension
behaviour by itself.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from scan_notebook import CONFIG_SCHEMA_VERSION, notebook_fingerprint, validate_reviewed_config


def json_fingerprint(value: dict[str, Any]) -> str:
    """Return a reproducible SHA-256 fingerprint for JSON-compatible data."""
    canonical_value = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical_value.encode("utf-8")).hexdigest()


def validate_notebook_matches_config(
    notebook: dict[str, Any], config: dict[str, Any], notebook_name: str
) -> list[str]:
    """Return errors when a reviewed configuration does not match its source."""
    errors = validate_reviewed_config(config)
    configured_name = config.get("source_notebook")
    if configured_name and configured_name != notebook_name:
        errors.append(
            f"Configuration belongs to '{configured_name}', not '{notebook_name}'."
        )

    configured_fingerprint = config.get("source_notebook_sha256")
    if configured_fingerprint and configured_fingerprint != notebook_fingerprint(notebook):
        errors.append("Notebook content no longer matches the notebook reviewed in the configuration.")

    notebook_cells = notebook.get("cells")
    configured_cells = config.get("cells")
    if not isinstance(notebook_cells, list) or not isinstance(configured_cells, list):
        return errors + ["Notebook and configuration must both contain a cells list."]
    if len(notebook_cells) != len(configured_cells):
        errors.append(
            f"Notebook has {len(notebook_cells)} cells but configuration has {len(configured_cells)}."
        )
        return errors

    for position, (notebook_cell, configured_cell) in enumerate(
        zip(notebook_cells, configured_cells), start=1
    ):
        if configured_cell.get("position") != position:
            errors.append(f"Configuration position is incorrect for cell {position}.")
        if configured_cell.get("source_cell_id") != notebook_cell.get("id"):
            errors.append(f"Cell {position} does not match the reviewed source cell ID.")
        if configured_cell.get("cell_type") != notebook_cell.get("cell_type"):
            errors.append(f"Cell {position} does not match the reviewed cell type.")
    return errors


def prepare_assignment(notebook: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return a metadata-enriched copy of a validated source notebook."""
    prepared = copy.deepcopy(notebook)
    config_fingerprint = json_fingerprint(config)
    course = config.get("course", {})
    assignment = config.get("assignment", {})

    notebook_metadata = prepared.setdefault("metadata", {})
    analytics_metadata = notebook_metadata.setdefault("student_analytics", {})
    analytics_metadata.update(
        {
            "course_id": course.get("id", ""),
            "assignment_id": assignment.get("id", ""),
            "assignment_authoring_schema_version": CONFIG_SCHEMA_VERSION,
            "assignment_configuration_sha256": config_fingerprint,
        }
    )
    notebook_metadata["assignment_authoring"] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "source_notebook": config.get("source_notebook"),
        "source_notebook_sha256": config.get("source_notebook_sha256"),
        "configuration_sha256": config_fingerprint,
        "structure": copy.deepcopy(config.get("structure", {})),
    }

    for notebook_cell, configured_cell in zip(prepared["cells"], config["cells"]):
        cell_metadata = notebook_cell.setdefault("metadata", {})
        # This namespace is intentionally separate from analytics_cell_key:
        # current pilot extension code treats that older field as protected.
        cell_metadata["assignment_authoring"] = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "template_cell": True,
            "analytics_key": configured_cell["analytics_key"],
            "role": configured_cell["role"],
            "section": configured_cell.get("section", ""),
            "policy": copy.deepcopy(configured_cell["policy"]),
        }
    return prepared


def output_paths(notebook_path: Path, output: Path | None, manifest: Path | None) -> tuple[Path, Path]:
    """Choose default prepared-notebook and manifest paths."""
    prepared_path = output or notebook_path.with_name(f"{notebook_path.stem}.prepared.ipynb")
    manifest_path = manifest or prepared_path.with_suffix(".manifest.json")
    return prepared_path, manifest_path


def build_manifest(
    notebook_path: Path, config_path: Path, prepared_path: Path, notebook: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """Describe exactly which reviewed inputs produced a prepared notebook."""
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "source_notebook": notebook_path.name,
        "source_notebook_sha256": notebook_fingerprint(notebook),
        "reviewed_configuration": config_path.name,
        "reviewed_configuration_sha256": json_fingerprint(config),
        "prepared_notebook": prepared_path.name,
        "cell_count": len(notebook["cells"]),
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a student-ready copy from a notebook and reviewed configuration."
    )
    parser.add_argument("notebook", type=Path, help="Original reviewed .ipynb notebook")
    parser.add_argument("configuration", type=Path, help="Reviewed .assignment-config.json file")
    parser.add_argument("--output", type=Path, help="Prepared notebook path")
    parser.add_argument("--manifest", type=Path, help="Manifest JSON path")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing prepared notebook or manifest.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    notebook_path = args.notebook.resolve()
    config_path = args.configuration.resolve()
    if not notebook_path.is_file() or not config_path.is_file():
        raise FileNotFoundError("Both the source notebook and reviewed configuration must exist.")

    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    errors = validate_notebook_matches_config(notebook, config, notebook_path.name)
    if errors:
        raise ValueError("Preparation stopped:\n- " + "\n- ".join(errors))

    prepared_path, manifest_path = output_paths(notebook_path, args.output, args.manifest)
    for path in (prepared_path, manifest_path):
        if path.exists() and not args.overwrite:
            raise FileExistsError(f"Refusing to overwrite {path}. Use --overwrite if intended.")

    prepared = prepare_assignment(notebook, config)
    prepared_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    prepared_path.write_text(json.dumps(prepared, indent=1) + "\n", encoding="utf-8")
    manifest = build_manifest(notebook_path, config_path, prepared_path, notebook, config)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared notebook: {prepared_path}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Create a deterministic draft assignment configuration from a notebook.

This is the first building block for the future teacher authoring platform. It
does not edit the input notebook or interact with the JupyterLab extension.
Instead, it creates a reviewable JSON proposal that a later user interface and
LLM step can improve before an assignment is published.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


CONFIG_SCHEMA_VERSION = 1


def source_text(cell: dict[str, Any]) -> str:
    """Return a notebook cell's source as one string."""
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def slugify(value: str, fallback: str) -> str:
    """Create a stable, metadata-safe identifier fragment."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or fallback


def markdown_heading(source: str) -> str | None:
    """Return the first Markdown heading text, when present."""
    match = re.search(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", source, re.MULTILINE)
    return match.group(1) if match else None


def propose_role(cell: dict[str, Any], position: int) -> tuple[str, str, str]:
    """Return deterministic role, key stem, and section for one notebook cell.

    These are intentionally conservative suggestions. A teacher or a later LLM
    review may replace every proposed value before publication.
    """
    cell_type = cell.get("cell_type", "raw")
    source = source_text(cell).strip()

    if cell_type == "markdown":
        if not source:
            return "answer_markdown", "answer_markdown", "general"
        heading = markdown_heading(source)
        if position == 1:
            return "locked_text", "header", "header"
        if heading:
            return "locked_text", slugify(heading, "section"), slugify(heading, "section")
        return "locked_text", "instruction", "general"

    if cell_type == "code":
        if source:
            return "starter_code", "starter_code", "general"
        return "answer_code", "answer_code", "general"

    return "unrestricted", "raw", "general"


def policy_for_role(role: str) -> dict[str, bool]:
    """Return the proposed default permissions for a supported cell role."""
    policies = {
        "locked_text": {
            "source_editable": False,
            "executable": False,
            "deletable": False,
            "movable": False,
        },
        "starter_code": {
            "source_editable": False,
            "executable": True,
            "deletable": False,
            "movable": False,
        },
        "answer_code": {
            "source_editable": True,
            "executable": True,
            "deletable": False,
            "movable": False,
        },
        "answer_markdown": {
            "source_editable": True,
            "executable": False,
            "deletable": False,
            "movable": False,
        },
        "unrestricted": {
            "source_editable": True,
            "executable": True,
            "deletable": True,
            "movable": True,
        },
    }
    return policies[role].copy()


def build_draft_config(notebook: dict[str, Any], source_notebook: str) -> dict[str, Any]:
    """Build a reviewable, deterministic configuration without modifying input."""
    counters: defaultdict[str, int] = defaultdict(int)
    cells: list[dict[str, Any]] = []

    for position, cell in enumerate(notebook.get("cells", []), start=1):
        role, stem, section = propose_role(cell, position)
        counters[stem] += 1
        key = stem if counters[stem] == 1 else f"{stem}_{counters[stem]}"
        cells.append(
            {
                "source_cell_id": cell.get("id"),
                "position": position,
                "cell_type": cell.get("cell_type", "raw"),
                "source_preview": source_text(cell).strip().replace("\n", " ")[:160],
                "proposed_key": key,
                "proposed_role": role,
                "proposed_section": section,
                "proposed_policy": policy_for_role(role),
            }
        )

    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "status": "draft_requires_teacher_review",
        "source_notebook": source_notebook,
        "course": {"proposed_id": "", "proposed_name": ""},
        "assignment": {"proposed_id": "", "proposed_name": ""},
        "structure": {"allow_extra_cells": False},
        "cells": cells,
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a draft assignment configuration from a Jupyter notebook."
    )
    parser.add_argument("notebook", type=Path, help="Input .ipynb notebook")
    parser.add_argument(
        "--output",
        type=Path,
        help="Draft JSON output path (default: next to the notebook)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    notebook_path = args.notebook.resolve()
    if not notebook_path.is_file():
        raise FileNotFoundError(f"Notebook not found: {notebook_path}")

    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    if not isinstance(notebook.get("cells"), list):
        raise ValueError("The input file is not a valid notebook with a cells list.")

    output_path = args.output or notebook_path.with_suffix(".assignment-draft.json")
    draft = build_draft_config(notebook, notebook_path.name)
    output_path.write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()

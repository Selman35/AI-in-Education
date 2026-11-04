#!/usr/bin/env python3
"""Create a deterministic draft assignment configuration from a notebook.

This is the first building block for the future teacher authoring platform. It
does not edit the input notebook or interact with the JupyterLab extension.
Instead, it creates a reviewable JSON proposal that a later user interface and
LLM step can improve before an assignment is published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


CONFIG_SCHEMA_VERSION = 1
VALID_ROLES = {
    "locked_text",
    "starter_code",
    "answer_code",
    "answer_markdown",
    "unrestricted",
}


def source_text(cell: dict[str, Any]) -> str:
    """Return a notebook cell's source as one string."""
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def notebook_fingerprint(notebook: dict[str, Any]) -> str:
    """Return a reproducible SHA-256 fingerprint for a notebook structure."""
    canonical_notebook = json.dumps(
        notebook, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical_notebook.encode("utf-8")).hexdigest()


def slugify(value: str, fallback: str) -> str:
    """Create a stable, metadata-safe identifier fragment."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    slug = slug or fallback
    # Analytics keys are intentionally required to start with a letter. Many
    # assignments use headings such as "1. Introduction", so preserve the
    # useful number while making the generated key valid without teacher work.
    return slug if slug[0].isalpha() else f"section_{slug}"


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


def review_summary(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise a teacher's current review selections for the web interface."""
    cell_types: defaultdict[str, int] = defaultdict(int)
    roles: defaultdict[str, int] = defaultdict(int)
    sections: set[str] = set()
    for cell in cells:
        cell_types[str(cell.get("cell_type", "unknown"))] += 1
        roles[str(cell.get("role", "unassigned"))] += 1
        section = str(cell.get("section", "")).strip()
        if section:
            sections.add(section)
    return {
        "total_cells": len(cells),
        "code_cells": cell_types["code"],
        "markdown_cells": cell_types["markdown"],
        "section_count": len(sections),
        "roles": dict(sorted(roles.items())),
    }


def build_reviewed_config(
    draft: dict[str, Any],
    course_name: str,
    assignment_name: str,
    allow_extra_cells: bool,
    reviewed_cells: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a canonical reviewed configuration from teacher selections.

    This remains an export-only artifact. Publishing a template and adding
    metadata to a notebook are intentionally later steps.
    """
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "status": "teacher_reviewed_not_published",
        "source_notebook": draft["source_notebook"],
        "source_notebook_sha256": draft.get("source_notebook_sha256"),
        "course": {
            "id": slugify(course_name, "course"),
            "name": course_name.strip(),
        },
        "assignment": {
            "id": slugify(assignment_name, "assignment"),
            "name": assignment_name.strip(),
        },
        "structure": {"allow_extra_cells": bool(allow_extra_cells)},
        "cells": reviewed_cells,
    }


def validate_reviewed_config(config: dict[str, Any]) -> list[str]:
    """Return user-facing validation errors for a reviewed configuration."""
    errors: list[str] = []
    seen_keys: set[str] = set()
    for cell in config.get("cells", []):
        key = str(cell.get("analytics_key", "")).strip()
        if not key:
            errors.append(f"Cell at position {cell.get('position')} needs an analytics key.")
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            errors.append(
                f"Cell key '{key}' must start with a lowercase letter and use only lowercase letters, numbers, and underscores."
            )
        if key in seen_keys:
            errors.append(f"Cell key '{key}' is used more than once.")
        seen_keys.add(key)
        if cell.get("role") not in VALID_ROLES:
            errors.append(f"Cell key '{key}' has an unsupported role.")
    return errors


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
                "source": source_text(cell),
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
        "source_notebook_sha256": notebook_fingerprint(notebook),
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

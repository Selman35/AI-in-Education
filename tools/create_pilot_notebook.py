#!/usr/bin/env python3
"""Create one provisioned, participant-specific pilot notebook.

The generated notebook contains a fixed participant identifier in its
metadata. For a JupyterHub deployment, the optional student workspace arguments
place the notebook under the authenticated user's provisioned workspace.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "templates" / "pilot_text_analyser_template.ipynb"
DEFAULT_OUTPUT_DIRECTORY = ROOT / "pilot_notebooks"
PILOT_COURSE_ID = "pilot_study"
PILOT_ASSIGNMENT_ID = "pilot_study_1"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a locked pilot notebook for one participant."
    )
    parser.add_argument("participant_id", help="For example: participant_001")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument(
        "--student-id",
        help="JupyterHub/VUnetID for a provisioned student workspace, for example: aaa100",
    )
    parser.add_argument(
        "--student-workspaces-root",
        type=Path,
        help="Parent directory containing per-student workspaces, for example: /srv/scistor/ai_in_education/students",
    )
    return parser.parse_args()


def safe_identifier(value: str, field_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(
            f"{field_name} must contain only letters, numbers, underscores, or hyphens."
        )
    return value


def replace_source(cell: dict[str, Any], old: str, new: str) -> None:
    source = cell.get("source", [])
    if isinstance(source, list):
        cell["source"] = [line.replace(old, new) for line in source]
    elif isinstance(source, str):
        cell["source"] = source.replace(old, new)


def provision_notebook(
    template: Path, output_path: Path, participant_id: str, course_id: str, assignment_id: str
) -> None:
    notebook = json.loads(template.read_text(encoding="utf-8"))
    metadata = notebook.setdefault("metadata", {}).setdefault("student_analytics", {})
    metadata.update(
        {
            "course_id": course_id,
            "assignment_id": assignment_id,
            "participant_id": participant_id,
            "assignment_instance_id": f"{assignment_id}__{participant_id}",
            "template_version": "1.0-text-analyser-pilot",
        }
    )

    for cell in notebook["cells"]:
        cell_metadata = cell.setdefault("metadata", {})
        if cell_metadata.get("analytics_cell_key") == "course_header":
            replace_source(cell, "COURSE_ID", course_id)
        if cell_metadata.get("analytics_cell_key") in {"student_name", "participant_id"}:
            cell["cell_type"] = "markdown"
            cell["execution_count"] = None
            cell.pop("outputs", None)
            cell_metadata.update(
                {
                    "deletable": False,
                    "editable": False,
                    "analytics_cell_key": "participant_id",
                    "tags": [
                        "assignment-header",
                        "participant-information",
                        "protected-cell",
                    ],
                }
            )
            cell["source"] = [
                f"## Participant ID: `{participant_id}`\n",
                "\n",
                "This identifier is assigned by the research team and is used to keep "
                "your study data separate from other participants.\n",
            ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(notebook, indent=1) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_arguments()
    participant_id = safe_identifier(args.participant_id, "participant ID")
    course_id = PILOT_COURSE_ID
    assignment_id = PILOT_ASSIGNMENT_ID
    if bool(args.student_id) != bool(args.student_workspaces_root):
        raise ValueError(
            "Use --student-id and --student-workspaces-root together, or omit both."
        )
    template = args.template.resolve()
    if not template.is_file():
        raise FileNotFoundError(f"Template not found: {template}")

    if args.student_id:
        student_id = safe_identifier(args.student_id, "student ID")
        output_path = (
            args.student_workspaces_root.resolve()
            / student_id
            / f"{assignment_id}.ipynb"
        )
    else:
        output_path = (
            args.output_directory.resolve()
            / f"{participant_id}_{assignment_id}.ipynb"
        )
    provision_notebook(template, output_path, participant_id, course_id, assignment_id)
    print(output_path)


if __name__ == "__main__":
    main()

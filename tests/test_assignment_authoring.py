"""Tests for the isolated assignment-authoring foundation."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


AUTHORING_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "tools" / "assignment_authoring"
)
sys.path.insert(0, str(AUTHORING_DIRECTORY))

MODULE_PATH = (
    AUTHORING_DIRECTORY / "scan_notebook.py"
)
SPEC = importlib.util.spec_from_file_location("scan_notebook", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)

PREPARER_PATH = (
    AUTHORING_DIRECTORY / "prepare_assignment.py"
)
PREPARER_SPEC = importlib.util.spec_from_file_location("prepare_assignment", PREPARER_PATH)
assert PREPARER_SPEC is not None and PREPARER_SPEC.loader is not None
preparer = importlib.util.module_from_spec(PREPARER_SPEC)
PREPARER_SPEC.loader.exec_module(preparer)


class AssignmentAuthoringTests(unittest.TestCase):
    def test_arbitrary_layout_receives_unique_draft_keys(self) -> None:
        notebook = {
            "cells": [
                {"id": "a", "cell_type": "markdown", "source": "# Statistics"},
                {"id": "b", "cell_type": "code", "source": "import pandas as pd"},
                {"id": "c", "cell_type": "code", "source": ""},
                {"id": "d", "cell_type": "code", "source": ""},
                {"id": "e", "cell_type": "markdown", "source": ""},
                {"id": "f", "cell_type": "markdown", "source": "Explain your result."},
            ]
        }

        draft = scanner.build_draft_config(notebook, "statistics.ipynb")
        cells = draft["cells"]

        self.assertEqual([cell["proposed_key"] for cell in cells], [
            "header",
            "header_starter_code",
            "header_code",
            "header_code_2",
            "header_explanation",
            "header_question",
        ])
        self.assertEqual(cells[2]["proposed_role"], "answer_code")
        self.assertTrue(cells[2]["proposed_policy"]["executable"])
        self.assertFalse(cells[2]["proposed_policy"]["deletable"])

    def test_headings_create_task_sections_and_descriptive_keys(self) -> None:
        notebook = {
            "cells": [
                {"id": "a", "cell_type": "markdown", "source": "# Course"},
                {
                    "id": "b",
                    "cell_type": "markdown",
                    "source": "## Task 1 — Optimisation",
                },
                {
                    "id": "c",
                    "cell_type": "markdown",
                    "source": "Write a function that minimises the objective.",
                },
                {"id": "d", "cell_type": "code", "source": ""},
                {"id": "e", "cell_type": "markdown", "source": ""},
                {"id": "f", "cell_type": "markdown", "source": "## Task 2 — Analysis"},
                {"id": "g", "cell_type": "code", "source": "import numpy as np"},
            ]
        }

        cells = scanner.build_draft_config(notebook, "example.ipynb")["cells"]

        self.assertEqual(cells[1]["proposed_key"], "task_1_optimisation")
        self.assertEqual(cells[2]["proposed_key"], "task_1_optimisation_question")
        self.assertEqual(cells[3]["proposed_key"], "task_1_optimisation_code")
        self.assertEqual(cells[4]["proposed_key"], "task_1_optimisation_explanation")
        self.assertEqual(cells[6]["proposed_key"], "task_2_analysis_starter_code")
        self.assertEqual(cells[3]["proposed_section"], "task_1_optimisation")
        self.assertIn("Empty code", cells[3]["suggestion_reason"])

    def test_input_notebook_is_not_modified(self) -> None:
        notebook = {
            "cells": [{"id": "a", "cell_type": "code", "source": ""}],
            "metadata": {"unchanged": True},
        }

        scanner.build_draft_config(notebook, "original.ipynb")

        self.assertEqual(notebook["metadata"], {"unchanged": True})
        self.assertEqual(notebook["cells"][0], {"id": "a", "cell_type": "code", "source": ""})

    def test_reviewed_configuration_requires_unique_safe_keys(self) -> None:
        draft = {
            "source_notebook": "example.ipynb",
            "cells": [],
        }
        reviewed = scanner.build_reviewed_config(
            draft=draft,
            course_name="Course 1",
            assignment_name="Assignment 1",
            allow_extra_cells=True,
            reviewed_cells=[
                {
                    "source_cell_id": "a",
                    "position": 1,
                    "cell_type": "code",
                    "analytics_key": "task_1_code",
                    "role": "answer_code",
                    "section": "task_1",
                    "policy": scanner.policy_for_role("answer_code"),
                },
                {
                    "source_cell_id": "b",
                    "position": 2,
                    "cell_type": "markdown",
                    "analytics_key": "task_1_code",
                    "role": "answer_markdown",
                    "section": "task_1",
                    "policy": scanner.policy_for_role("answer_markdown"),
                },
            ],
        )

        errors = scanner.validate_reviewed_config(reviewed)
        self.assertTrue(any("used more than once" in error for error in errors))

    def test_numeric_heading_generates_a_valid_key(self) -> None:
        notebook = {
            "cells": [
                {
                    "id": "heading",
                    "cell_type": "markdown",
                    "source": "## 1. Understanding the objective",
                }
            ]
        }

        draft = scanner.build_draft_config(notebook, "example.ipynb")

        self.assertEqual(
            draft["cells"][0]["proposed_key"],
            "header",
        )
        self.assertEqual(draft["cells"][0]["source"], "## 1. Understanding the objective")

        later_heading = {"cells": notebook["cells"] * 2}
        later_draft = scanner.build_draft_config(later_heading, "example.ipynb")
        self.assertEqual(
            later_draft["cells"][1]["proposed_key"],
            "section_1_understanding_the_objective",
        )

    def test_review_summary_counts_types_roles_and_sections(self) -> None:
        summary = scanner.review_summary(
            [
                {"cell_type": "markdown", "role": "locked_text", "section": "intro"},
                {"cell_type": "code", "role": "starter_code", "section": "task_1"},
                {"cell_type": "code", "role": "answer_code", "section": "task_1"},
            ]
        )

        self.assertEqual(summary["total_cells"], 3)
        self.assertEqual(summary["code_cells"], 2)
        self.assertEqual(summary["markdown_cells"], 1)
        self.assertEqual(summary["section_count"], 2)
        self.assertEqual(summary["roles"]["answer_code"], 1)

    def test_preparation_writes_metadata_without_changing_inputs(self) -> None:
        notebook = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"existing": True},
            "cells": [
                {"id": "intro", "cell_type": "markdown", "metadata": {}, "source": "# Intro"},
                {"id": "answer", "cell_type": "code", "metadata": {}, "source": ""},
            ],
        }
        draft = scanner.build_draft_config(notebook, "example.ipynb")
        reviewed = scanner.build_reviewed_config(
            draft=draft,
            course_name="Example course",
            assignment_name="Example assignment",
            allow_extra_cells=False,
            reviewed_cells=[
                {
                    "source_cell_id": "intro",
                    "position": 1,
                    "cell_type": "markdown",
                    "analytics_key": "intro",
                    "role": "locked_text",
                    "section": "intro",
                    "policy": scanner.policy_for_role("locked_text"),
                },
                {
                    "source_cell_id": "answer",
                    "position": 2,
                    "cell_type": "code",
                    "analytics_key": "answer_code",
                    "role": "answer_code",
                    "section": "task_1",
                    "policy": scanner.policy_for_role("answer_code"),
                },
            ],
        )
        original = json.loads(json.dumps(notebook))

        self.assertEqual(
            preparer.validate_notebook_matches_config(notebook, reviewed, "example.ipynb"),
            [],
        )
        prepared = preparer.prepare_assignment(notebook, reviewed)

        self.assertEqual(notebook, original)
        self.assertEqual(prepared["metadata"]["student_analytics"]["course_id"], "example_course")
        self.assertEqual(prepared["metadata"]["student_analytics"]["assignment_id"], "example_assignment")
        self.assertNotIn("analytics_cell_key", prepared["cells"][1]["metadata"])
        self.assertEqual(
            prepared["cells"][1]["metadata"]["assignment_authoring"]["analytics_key"],
            "answer_code",
        )

    def test_preparation_rejects_a_changed_source_notebook(self) -> None:
        notebook = {
            "cells": [{"id": "intro", "cell_type": "markdown", "source": "# Intro"}]
        }
        draft = scanner.build_draft_config(notebook, "example.ipynb")
        reviewed = scanner.build_reviewed_config(
            draft=draft,
            course_name="Example course",
            assignment_name="Example assignment",
            allow_extra_cells=False,
            reviewed_cells=[
                {
                    "source_cell_id": "intro",
                    "position": 1,
                    "cell_type": "markdown",
                    "analytics_key": "header",
                    "role": "locked_text",
                    "section": "header",
                    "policy": scanner.policy_for_role("locked_text"),
                }
            ],
        )
        changed_notebook = json.loads(json.dumps(notebook))
        changed_notebook["cells"][0]["source"] = "# Changed"

        errors = preparer.validate_notebook_matches_config(
            changed_notebook, reviewed, "example.ipynb"
        )

        self.assertTrue(any("content no longer matches" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

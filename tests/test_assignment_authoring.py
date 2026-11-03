"""Tests for the isolated assignment-authoring foundation."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "assignment_authoring"
    / "scan_notebook.py"
)
SPEC = importlib.util.spec_from_file_location("scan_notebook", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


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
            "starter_code",
            "answer_code",
            "answer_code_2",
            "answer_markdown",
            "instruction",
        ])
        self.assertEqual(cells[2]["proposed_role"], "answer_code")
        self.assertTrue(cells[2]["proposed_policy"]["executable"])
        self.assertFalse(cells[2]["proposed_policy"]["deletable"])

    def test_input_notebook_is_not_modified(self) -> None:
        notebook = {
            "cells": [{"id": "a", "cell_type": "code", "source": ""}],
            "metadata": {"unchanged": True},
        }

        scanner.build_draft_config(notebook, "original.ipynb")

        self.assertEqual(notebook["metadata"], {"unchanged": True})
        self.assertEqual(notebook["cells"][0], {"id": "a", "cell_type": "code", "source": ""})


if __name__ == "__main__":
    unittest.main()

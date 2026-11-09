"""Headless smoke tests for the Streamlit assignment-authoring interface."""

from __future__ import annotations

import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "tools" / "assignment_authoring" / "app.py"


def streamlit_testing_is_available() -> bool:
    """Return whether this environment can import Streamlit's test API."""
    try:
        from streamlit.testing.v1 import AppTest  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        return False
    return True


@unittest.skipUnless(
    streamlit_testing_is_available(),
    "A compatible Streamlit runtime is not installed in this environment.",
)
class AssignmentAuthoringStreamlitTests(unittest.TestCase):
    def test_app_initial_screen_renders_without_an_exception(self) -> None:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(APP_PATH), default_timeout=15).run()

        self.assertFalse(app.exception)
        self.assertEqual(len(app.file_uploader), 1)


if __name__ == "__main__":
    unittest.main()

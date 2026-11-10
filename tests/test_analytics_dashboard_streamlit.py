"""Headless smoke tests for the local analytics dashboard."""

from __future__ import annotations

import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "tools" / "analytics_dashboard" / "app.py"


def streamlit_testing_is_available() -> bool:
    try:
        from streamlit.testing.v1 import AppTest  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        return False
    return True


@unittest.skipUnless(
    streamlit_testing_is_available(),
    "A compatible Streamlit runtime is not installed in this environment.",
)
class AnalyticsDashboardStreamlitTests(unittest.TestCase):
    def test_initial_screen_renders_without_an_exception(self) -> None:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.file_uploader), 2)

    def test_empty_submit_shows_validation_message(self) -> None:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.error)


if __name__ == "__main__":
    unittest.main()

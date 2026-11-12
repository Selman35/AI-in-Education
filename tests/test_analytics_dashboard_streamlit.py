"""Headless smoke tests for the local analytics dashboard."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from tools.analytics_dashboard.analytics_workspace import ProcessingResult


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

    def test_custom_bar_chart_without_a_group_does_not_crash(self) -> None:
        """A missing colour channel must not be passed to Altair as None."""
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(APP_PATH), default_timeout=20)
        app.session_state["analytics_result"] = ProcessingResult(
            reports={
                "student_analytics": pd.DataFrame(
                    {"student": ["aaa101", "aaa102"], "example_metric": [2, 4]}
                )
            },
            plots={},
            warnings=(),
            manifest={"input_type": "csv_reports"},
        )
        app.session_state["analytics_group_warnings"] = []
        app.session_state["analytics_download"] = b"zip"
        app.session_state["analytics_selected_cells"] = ["task_1_code"]
        app.run()
        app.segmented_control(key="analytics_workspace_view").set_value("Custom chart").run()
        self.assertFalse(app.exception)
        self.assertIsNotNone(app.button(key="save_chart_custom_preview"))

    def test_saved_chart_collection_can_save_review_remove_and_clear(self) -> None:
        from streamlit.testing.v1 import AppTest

        renderer = SimpleNamespace(vegalite_to_png=lambda specification: b"\x89PNG\r\n\x1a\n")
        with patch.dict("sys.modules", {"vl_convert": renderer}):
            app = AppTest.from_file(str(APP_PATH), default_timeout=20)
            app.session_state["analytics_result"] = ProcessingResult(
                reports={
                    "student_analytics": pd.DataFrame(
                        {
                            "student": ["aaa101__pilot", "bbb101__pilot"],
                            "total_working_duration": [42, 65],
                        }
                    )
                },
                plots={},
                warnings=(),
                manifest={"input_type": "csv_reports"},
            )
            app.session_state["analytics_group_warnings"] = []
            app.session_state["analytics_download"] = b"zip"
            app.run()

            app.segmented_control(key="analytics_workspace_view").set_value("Preset views").run()
            app.button(key="save_chart_preset_total_working_duration").click().run(timeout=20)
            self.assertFalse(app.exception)
            self.assertEqual(len(app.session_state["analytics_saved_charts"]), 1)

            app.segmented_control(key="analytics_workspace_view").set_value("Saved charts").run()
            self.assertFalse(app.exception)
            self.assertIsNotNone(app.download_button(key="download_selected_saved_charts"))
            chart_id = app.session_state["analytics_saved_charts"][0].chart_id
            app.button(key=f"remove_saved_chart_{chart_id}").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["analytics_saved_charts"], [])

            app.segmented_control(key="analytics_workspace_view").set_value("Preset views").run()
            app.button(key="save_chart_preset_total_working_duration").click().run(timeout=20)
            app.segmented_control(key="analytics_workspace_view").set_value("Saved charts").run()
            app.button(key="clear_saved_charts").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["analytics_saved_charts"], [])

    def test_cell_duration_presets_render_when_cell_metrics_are_available(self) -> None:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(APP_PATH), default_timeout=20)
        app.session_state["analytics_result"] = ProcessingResult(
            reports={
                "student_analytics": pd.DataFrame(
                    {
                        "student": ["aaa101__pilot"],
                        "task_1_code_total_duration": [120],
                        "task_1_code_working_duration": [90],
                    }
                )
            },
            plots={},
            warnings=(),
            manifest={"input_type": "csv_reports"},
        )
        app.session_state["analytics_group_warnings"] = []
        app.session_state["analytics_download"] = b"zip"
        app.session_state["analytics_selected_cells"] = ["task_1_code"]
        app.run()
        app.segmented_control(key="analytics_workspace_view").set_value("Preset views").run()
        self.assertFalse(app.exception)
        self.assertIsNotNone(app.button(key="save_chart_preset_cell_duration_comparison"))
        self.assertIsNotNone(app.button(key="save_chart_preset_working_time_allocation"))

    def test_all_workspace_views_render_for_a_complete_report_set(self) -> None:
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_file(str(APP_PATH), default_timeout=20)
        app.session_state["analytics_result"] = ProcessingResult(
            reports={
                "student_analytics": pd.DataFrame(
                    {
                        "student": ["aaa101__pilot", "bbb101__pilot"],
                        "group": ["ai", "non_ai"],
                        "total_working_duration": [42, 65],
                        "total_idle_time": [4, 7],
                        "execution_status_success": [3, 4],
                        "execution_status_error": [1, 0],
                    }
                ),
                "clipboard_events": pd.DataFrame(
                    {
                        "student": ["aaa101__pilot"],
                        "event": ["paste"],
                        "character_length": [12],
                        "cell": ["task_1_code"],
                    }
                ),
                "cell_execution_summary": pd.DataFrame(
                    {"student": ["aaa101__pilot"], "cell": ["task_1_code"], "count": [4]}
                ),
            },
            plots={"working_time.png": b"png"},
            warnings=(),
            manifest={"input_type": "csv_reports"},
        )
        app.session_state["analytics_group_warnings"] = []
        app.session_state["analytics_download"] = b"zip"
        app.run()
        for view in (
            "Data quality",
            "Preset views",
            "Event reports",
            "Custom chart",
            "Saved charts",
            "Download",
        ):
            app.segmented_control(key="analytics_workspace_view").set_value(view).run()
            self.assertFalse(app.exception, view)


if __name__ == "__main__":
    unittest.main()

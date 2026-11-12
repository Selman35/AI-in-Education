"""Unit tests for the local no-code analytics workspace."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import pandas as pd

from tools.analytics_dashboard.analytics_workspace import (
    DashboardInputError,
    ProcessingResult,
    aggregate_for_chart,
    apply_group_mapping,
    available_cell_labels,
    build_download_zip,
    chart_defaults,
    chart_png,
    convert_time_unit,
    data_quality_findings,
    extract_log_archive,
    filter_reports_to_cells,
    grouped_summary,
    participant_id_from_student,
    read_csv_uploads,
    run_raw_log_analytics,
    search_rows,
    validate_group_mapping,
)


def make_zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class AnalyticsWorkspaceTests(unittest.TestCase):
    def test_extract_log_archive_finds_nested_log_root(self) -> None:
        archive = make_zip({"pilot/logs/changes/aaa101__pilot.ipynb.log": "[event]"})
        with tempfile.TemporaryDirectory() as temporary:
            root = extract_log_archive(archive, Path(temporary))
        self.assertEqual(root.name, "logs")

    def test_extract_log_archive_rejects_path_traversal(self) -> None:
        archive = make_zip({"../outside.txt": "not allowed"})
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(DashboardInputError):
                extract_log_archive(archive, Path(temporary))

    def test_csv_uploads_and_group_mapping(self) -> None:
        reports = read_csv_uploads(
            {"student_analytics.csv": b"student,total_working_duration\naaa101__pilot,42\n"}
        )
        mapping = validate_group_mapping(b"participant_id,group\naaa101,non_ai\n")
        enriched, warnings = apply_group_mapping(reports, mapping)
        self.assertEqual(enriched["student_analytics"].loc[0, "participant_id"], "aaa101")
        self.assertEqual(enriched["student_analytics"].loc[0, "group"], "non_ai")
        self.assertEqual(warnings, [])

    def test_participant_identifier_uses_prefix(self) -> None:
        self.assertEqual(participant_id_from_student("BBB101__pilot_study_1"), "bbb101")
        self.assertEqual(participant_id_from_student("student_without_separator"), "student_without_separator")

    def test_aggregate_for_chart_supports_mean_and_count(self) -> None:
        frame = pd.DataFrame({"group": ["ai", "ai", "non_ai"], "value": [2, 4, 10]})
        mean = aggregate_for_chart(frame, "group", "value", None, "Mean")
        count = aggregate_for_chart(frame, "group", None, None, "Count")
        self.assertEqual(mean.loc[mean["group"] == "ai", "value"].item(), 3)
        self.assertEqual(count.loc[count["group"] == "ai", "count"].item(), 2)

    def test_chart_defaults_and_png_export_use_loaded_data(self) -> None:
        frame = pd.DataFrame(
            {
                "student": ["aaa101", "aaa102"],
                "group": ["ai", "non_ai"],
                "total_working_duration": [42, 65],
            }
        )
        defaults = chart_defaults(frame, "Grouped bar")
        self.assertEqual(defaults["x"], "student")
        self.assertEqual(defaults["y"], "total_working_duration")
        self.assertEqual(defaults["group"], "group")

        plotted = aggregate_for_chart(
            frame, "student", "total_working_duration", "group", "Sum"
        )
        png = chart_png(
            plotted,
            "Grouped bar",
            "student",
            "total_working_duration",
            "group",
            "Working time by participant",
            "Participant",
            "Seconds",
            "#1F77B4",
        )
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_data_quality_search_and_cohort_summary(self) -> None:
        reports = {
            "student_analytics": pd.DataFrame(
                {
                    "student": ["aaa101__pilot", "aaa101__pilot", "bbb101__pilot"],
                    "group": ["ai", "ai", "non_ai"],
                    "total_working_duration": [42, -1, 65],
                }
            ),
            "clipboard_events": pd.DataFrame(columns=["student", "event"]),
        }
        findings = data_quality_findings(reports)
        self.assertTrue(findings["message"].str.contains("duplicate participant").any())
        self.assertTrue(findings["message"].str.contains("negative values").any())
        self.assertTrue(findings["message"].str.contains("no rows").any())

        visible = search_rows(reports["student_analytics"], "bbb101")
        self.assertEqual(visible["student"].tolist(), ["bbb101__pilot"])
        summary = grouped_summary(reports["student_analytics"], "total_working_duration", "group")
        self.assertEqual(set(summary["group"]), {"ai", "non_ai"})

    def test_time_unit_conversion_is_display_only(self) -> None:
        reports = {
            "student_analytics": pd.DataFrame(
                {
                    "student": ["aaa101__pilot"],
                    "total_working_duration": [120.0],
                    "task_1_code_idle_time": [30.0],
                    "execution_status_success": [2],
                }
            ),
            "active_sessions": pd.DataFrame(
                {"duration_seconds": [180.0], "start_time": ["2026-01-01T10:00:00Z"]}
            ),
        }
        minutes = convert_time_unit(reports, "Minutes")
        self.assertEqual(minutes["student_analytics"].loc[0, "total_working_duration"], 2.0)
        self.assertEqual(minutes["student_analytics"].loc[0, "task_1_code_idle_time"], 0.5)
        self.assertEqual(minutes["active_sessions"].loc[0, "duration_seconds"], 3.0)
        self.assertEqual(minutes["student_analytics"].loc[0, "execution_status_success"], 2)
        self.assertEqual(reports["student_analytics"].loc[0, "total_working_duration"], 120.0)

    def test_filter_reports_to_cells_hides_only_cell_level_data(self) -> None:
        reports = {
            "student_analytics": pd.DataFrame(
                {
                    "student": ["aaa101"],
                    "total_working_duration": [42],
                    "course_header_total_duration": [2],
                    "task_1_code_total_duration": [40],
                }
            ),
            "cell_execution_summary": pd.DataFrame(
                {"student": ["aaa101", "aaa101"], "cell": ["course_header", "task_1_code"]}
            ),
            "cell_mapping": pd.DataFrame(
                {"student": ["aaa101", "aaa101"], "cell_label": ["course_header", "task_1_code"]}
            ),
        }
        self.assertEqual(available_cell_labels(reports), ["course_header", "task_1_code"])
        filtered = filter_reports_to_cells(reports, ["task_1_code"])
        self.assertIn("total_working_duration", filtered["student_analytics"].columns)
        self.assertNotIn("course_header_total_duration", filtered["student_analytics"].columns)
        self.assertIn("task_1_code_total_duration", filtered["student_analytics"].columns)
        self.assertEqual(filtered["cell_execution_summary"]["cell"].tolist(), ["task_1_code"])
        self.assertEqual(filtered["cell_mapping"]["cell_label"].tolist(), ["task_1_code"])

    def test_download_zip_excludes_raw_uploads(self) -> None:
        result = ProcessingResult(
            reports={"student_analytics": pd.DataFrame({"student": ["aaa101"], "value": [1]})},
            plots={"plot.png": b"png"},
            warnings=(),
            manifest={"input_type": "csv_reports"},
        )
        with zipfile.ZipFile(io.BytesIO(build_download_zip(result))) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["analytics/student_analytics.csv", "manifest.json", "plots/plot.png"],
            )

    def test_raw_log_package_generates_reports(self) -> None:
        log = "\n".join(
            [
                "[2026-01-01T10:00:00.000Z][focusin][cell label: task_1_code]",
                "[2026-01-01T10:00:01.000Z][edit][cell label: task_1_code]",
                "[2026-01-01T10:00:02.000Z][execute][cell label: task_1_code] status: success",
            ]
        )
        archive = make_zip({"logs/changes/aaa101__pilot_study_1.ipynb.log": log})
        with tempfile.TemporaryDirectory() as temporary:
            result = run_raw_log_analytics(archive, Path(temporary))
        self.assertEqual(
            set(result.reports),
            {
                "student_analytics",
                "cell_mapping",
                "cell_execution_summary",
                "idle_events",
                "active_sessions",
                "clipboard_events",
            },
        )
        self.assertEqual(len(result.reports["student_analytics"]), 1)
        self.assertEqual(
            set(result.plots),
            {
                "estimated_working_time_by_student.png",
                "idle_time_by_student.png",
                "execution_outcomes_by_student.png",
            },
        )


if __name__ == "__main__":
    unittest.main()

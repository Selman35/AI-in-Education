"""Regression tests for the log-to-analytics pipeline.

These tests use synthetic log files. They verify the event format produced by
the extension can be parsed and that the reported analytics remain stable when
the implementation changes. They do not replace a live JupyterLab/JupyterHub
test because browser focus, clipboard permissions, and server access control
cannot be simulated by this Python module.

Run with:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ANALYTICS_PATH = REPOSITORY_ROOT / "tools" / "analytics" / "compute_analytics.py"
SPEC = importlib.util.spec_from_file_location("compute_analytics", ANALYTICS_PATH)
assert SPEC is not None and SPEC.loader is not None
analytics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analytics)


def log_line(second: int, event: str, cell_label: str = "task_1_code", extra: str = "") -> str:
    """Create one tracker-compatible event line at a predictable timestamp."""
    timestamp = datetime(2026, 1, 1, 12, 0, 0) + timedelta(seconds=second)
    return (
        f"[{timestamp.strftime('%Y-%m-%dT%H:%M:%S')}.000Z]"
        f"[{event}][cell label: {cell_label}]{extra}\n"
    )


class AnalyticsPipelineTests(unittest.TestCase):
    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_jsonl(self, path: Path, records: list[dict]) -> None:
        self.write_text(path, "".join(json.dumps(record) + "\n" for record in records))

    def test_parser_recognises_all_logged_event_types(self) -> None:
        """Every event type emitted by the tracker has a usable parsed form."""
        lines = [
            log_line(0, "focusin"),
            log_line(1, "focusout"),
            log_line(2, "edit"),
            log_line(3, "copy", extra=" length: 12"),
            log_line(4, "cut", extra=" length: 3"),
            log_line(5, "paste", extra=" length: 15"),
            log_line(6, "execute", extra=" status: success"),
            log_line(7, "execute", extra=" status: error"),
            log_line(8, "assignment-active"),
            log_line(9, "assignment-inactive"),
            log_line(10, "initial save"),
            log_line(11, "save 2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.ipynb.log"
            self.write_text(path, "".join(reversed(lines)))
            parsed = analytics.parse_log_file(str(path))

        self.assertEqual(len(parsed), len(lines))
        self.assertEqual(parsed["event"].tolist(), [line for line in [
            "focusin", "focusout", "edit", "copy", "cut", "paste",
            "execute", "execute", "assignment-active", "assignment-inactive",
            "initial save", "save 2",
        ]])
        self.assertTrue((parsed["cell_label"] == "task_1_code").all())
        self.assertEqual(parsed.loc[parsed["event"] == "copy", "length"].iloc[0], 12)
        self.assertEqual(parsed.loc[parsed["event"] == "cut", "length"].iloc[0], 3)
        self.assertEqual(parsed.loc[parsed["event"] == "paste", "length"].iloc[0], 15)
        self.assertEqual(
            parsed.loc[parsed["event"] == "execute", "status"].tolist(),
            ["success", "error"],
        )

    def test_working_idle_and_session_metrics_use_meaningful_actions(self) -> None:
        """Focus and saves do not inflate active work, idle time, or sessions."""
        lines = [
            log_line(0, "assignment-active"),
            log_line(1, "edit", "task_1_code"),
            log_line(11, "copy", "task_1_code", " length: 10"),
            log_line(31, "paste", "task_1_code", " length: 5"),
            log_line(46, "execute", "task_1_code", " status: success"),
            log_line(200, "edit", "task_2_code"),
            log_line(600, "edit", "task_2_code"),
            log_line(610, "execute", "task_2_code", " status: error"),
            log_line(620, "assignment-inactive", "task_2_code"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.ipynb.log"
            self.write_text(path, "".join(lines))
            parsed = analytics.parse_log_file(str(path))

        labels = analytics.resolve_cell_labels(parsed)
        self.assertEqual(analytics.compute_working_duration(parsed), 55.0)

        idle_events = analytics.compute_idle_events(parsed, labels)
        self.assertEqual([event["duration_seconds"] for event in idle_events], [34.0, 280.0])
        self.assertEqual(analytics.compute_idle_summary(idle_events)["total_idle_time"], 314.0)

        sessions = analytics.compute_active_sessions(parsed, labels)
        self.assertIsNotNone(sessions)
        assert sessions is not None
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["end_reason"], "five_minute_gap")
        self.assertEqual(sessions[0]["duration_seconds"], 199.0)
        self.assertEqual(sessions[1]["end_reason"], "assignment_inactive")
        self.assertEqual(sessions[1]["duration_seconds"], 10.0)

    def test_cell_snapshots_report_modification_without_false_line_deletion(self) -> None:
        """A changed line is a modification, rather than a deletion and addition."""
        snapshots = [
            {
                "timestamp": "2026-01-01T12:00:00.000Z",
                "cells": [
                    {
                        "cell_id": "cell-a",
                        "cell_index": 1,
                        "template_cell_key": "task_1_code",
                        "source": "x = 1\n",
                    }
                ],
            },
            {
                "timestamp": "2026-01-01T12:00:30.000Z",
                "cells": [
                    {
                        "cell_id": "cell-a",
                        "cell_index": 1,
                        "template_cell_key": "task_1_code",
                        "source": "x = 2\ny = 3\n",
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "student.ipynb.jsonl"
            self.write_jsonl(path, snapshots)
            line_stats = analytics.compute_cell_line_diff_stats(str(path))
            character_stats = analytics.compute_cell_character_diff_stats(str(path))

        self.assertEqual(
            line_stats["task_1_code"],
            # The first snapshot is still counted relative to empty content.
            # The separate expected-failure test below documents that known
            # template-baseline limitation.
            {"added_lines": 2, "removed_lines": 0, "modified_lines": 1},
        )
        self.assertGreater(character_stats["task_1_code"]["char_diff_added_characters"], 0)
        self.assertGreater(character_stats["task_1_code"]["char_diff_removed_characters"], 0)

    def test_template_baseline_is_not_counted_as_student_work(self) -> None:
        """A provisioned template baseline is not student-written content."""
        snapshots = [
            {
                "timestamp": "2026-01-01T12:00:00.000Z",
                "kind": "baseline",
                "cells": [
                    {
                        "cell_id": "cell-a",
                        "cell_index": 1,
                        "template_cell_key": "task_1_code",
                        "source": "starter text\n",
                    }
                ],
            },
            {
                "timestamp": "2026-01-01T12:00:30.000Z",
                "kind": "save",
                "cells": [
                    {
                        "cell_id": "cell-a",
                        "cell_index": 1,
                        "template_cell_key": "task_1_code",
                        "source": "starter text\nstudent answer\n",
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "student.ipynb.jsonl"
            self.write_jsonl(path, snapshots)
            line_stats = analytics.compute_cell_line_diff_stats(str(path))

        self.assertEqual(line_stats["task_1_code"]["added_lines"], 1)

    def test_revision_metrics_require_and_use_template_baseline(self) -> None:
        baseline = {
            "timestamp": "2026-01-01T12:00:00.000Z",
            "kind": "baseline",
            "cells": [{"cell_id": "cell-a", "cell_index": 1, "template_cell_key": "task_1_code", "source": "abc"}],
        }
        changed = {
            "timestamp": "2026-01-01T12:00:30.000Z",
            "kind": "save",
            "cells": [{"cell_id": "cell-a", "cell_index": 1, "template_cell_key": "task_1_code", "source": "acX"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "student.ipynb.jsonl"
            self.write_jsonl(path, [baseline, changed])
            stats = analytics.compute_cell_revision_stats(str(path))
            summary = analytics.compute_revision_summary(stats)
            self.write_jsonl(path, [changed])
            unavailable = analytics.compute_cell_revision_stats(str(path))

        self.assertEqual(stats["task_1_code"]["revision_added_characters"], 1)
        self.assertEqual(stats["task_1_code"]["revision_removed_characters"], 1)
        self.assertEqual(stats["task_1_code"]["revision_changed_save_count"], 1)
        self.assertEqual(summary["revision_ratio"], 0.5)
        self.assertIsNone(unavailable)

    def test_error_recovery_uses_edits_and_preserves_brief_absence(self) -> None:
        lines = [
            log_line(0, "assignment-active"),
            log_line(1, "execute", "task_1_code", " status: error"),
            log_line(10, "edit", "task_1_code"),
            log_line(30, "execute", "task_1_code", " status: success"),
            log_line(40, "execute", "task_2_code", " status: error"),
            log_line(50, "execute", "task_2_code", " status: success"),
            log_line(60, "execute", "task_3_code", " status: error"),
            log_line(70, "assignment-inactive"),
            log_line(180, "assignment-active"),
            log_line(185, "edit", "task_3_code"),
            log_line(190, "execute", "task_3_code", " status: success"),
            log_line(200, "execute", "task_4_code", " status: error"),
            log_line(210, "assignment-inactive"),
            log_line(331, "assignment-active"),
            log_line(340, "edit", "task_5_code"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.ipynb.log"
            self.write_text(path, "".join(lines))
            parsed = analytics.parse_log_file(str(path))
        labels = analytics.resolve_cell_labels(parsed)
        events = analytics.compute_error_recovery_events(parsed, labels)
        summary = analytics.compute_error_recovery_summary(events)
        by_cell = {event["cell"]: event for event in events}

        self.assertEqual(summary["error_episode_count"], 4)
        self.assertEqual(summary["recovered_error_episode_count"], 2)
        self.assertEqual(summary["error_recovery_rate"], 0.5)
        self.assertEqual(by_cell["task_1_code"]["recovery_duration_seconds"], 29.0)
        self.assertTrue(by_cell["task_3_code"]["recovered"])
        self.assertEqual(by_cell["task_2_code"]["closure_reason"], "successful_rerun_without_edit")
        self.assertEqual(by_cell["task_4_code"]["closure_reason"], "assignment_inactive_2min")

    def test_code_work_metrics_use_only_editable_code_cells(self) -> None:
        """Planning, testing, retries, and revisits ignore locked starter code."""
        lines = [
            log_line(0, "assignment-active", "task_1_code"),
            log_line(10, "edit", "task_1_code"),
            log_line(20, "execute", "task_1_code", " status: error"),
            log_line(30, "execute", "task_1_code", " status: error"),
            log_line(40, "edit", "task_1_code"),
            log_line(50, "execute", "task_1_code", " status: success"),
            log_line(60, "edit", "task_2_code"),
            log_line(70, "execute", "task_2_code", " status: success"),
            log_line(80, "edit", "task_1_code"),
            log_line(90, "execute", "starter_code", " status: success"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "events.ipynb.log"
            snapshots = [
                {"kind": "baseline", "timestamp": "2026-01-01T12:00:00.000Z", "cells": [
                    {"cell_id": "a", "cell_index": 1, "template_cell_key": "task_1_code", "cell_type": "code", "source_editable": True, "source": ""},
                    {"cell_id": "b", "cell_index": 2, "template_cell_key": "task_2_code", "cell_type": "code", "source_editable": True, "source": ""},
                    {"cell_id": "c", "cell_index": 3, "template_cell_key": "starter_code", "cell_type": "code", "source_editable": False, "source": "x=1"},
                ]},
                {"kind": "save", "timestamp": "2026-01-01T12:01:30.000Z", "cells": [
                    {"cell_id": "a", "cell_index": 1, "template_cell_key": "task_1_code", "cell_type": "code", "source_editable": True, "source": "answer"},
                    {"cell_id": "b", "cell_index": 2, "template_cell_key": "task_2_code", "cell_type": "code", "source_editable": True, "source": "next"},
                    {"cell_id": "c", "cell_index": 3, "template_cell_key": "starter_code", "cell_type": "code", "source_editable": False, "source": "x=1"},
                ]},
            ]
            self.write_text(log_path, "".join(lines))
            snapshot_path = Path(directory) / "events.ipynb.jsonl"
            self.write_jsonl(snapshot_path, snapshots)
            parsed = analytics.parse_log_file(str(log_path))
            labels = analytics.resolve_cell_labels(parsed)
            metrics = analytics.compute_code_work_metrics(
                parsed, labels, analytics.student_writable_code_labels(str(snapshot_path))
            )
            growth = analytics.compute_solution_growth_events(str(snapshot_path))

        self.assertEqual(metrics["task_1_code"]["time_to_first_action_seconds"], 10.0)
        self.assertEqual(metrics["task_1_code"]["time_to_first_execution_seconds"], 20.0)
        self.assertEqual(metrics["task_1_code"]["execution_before_edit_count"], 1)
        self.assertEqual(metrics["task_1_code"]["max_consecutive_errors"], 2)
        self.assertEqual(metrics["task_1_code"]["task_revisit_count"], 1)
        self.assertNotIn("starter_code", metrics)
        self.assertEqual(growth[0]["net_code_characters_from_baseline"], 10)

    def test_process_folder_generates_all_current_csv_reports(self) -> None:
        """A representative log produces coherent notebook- and cell-level CSVs."""
        lines = [
            log_line(0, "assignment-active"),
            log_line(1, "edit", "task_1_code"),
            log_line(11, "copy", "task_1_code", " length: 10"),
            log_line(31, "paste", "task_1_code", " length: 5"),
            log_line(46, "execute", "task_1_code", " status: success"),
            log_line(200, "edit", "task_2_code"),
            log_line(620, "assignment-inactive", "task_2_code"),
        ]
        snapshots = [
            {
                "timestamp": "2026-01-01T12:00:00.000Z",
                "cells": [
                    {
                        "cell_id": "cell-a",
                        "cell_index": 1,
                        "template_cell_key": "task_1_code",
                        "source": "x = 1\n",
                    }
                ],
            },
            {
                "timestamp": "2026-01-01T12:00:30.000Z",
                "cells": [
                    {
                        "cell_id": "cell-a",
                        "cell_index": 1,
                        "template_cell_key": "task_1_code",
                        "source": "x = 2\n",
                    }
                ],
            },
        ]
        filename = "abc111__pilot_study_1.ipynb.log"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changes = root / "changes"
            versions = root / "versions"
            cell_versions = root / "cell_versions"
            reports = root / "reports"
            self.write_text(changes / filename, "".join(lines))
            self.write_text(
                versions / filename,
                "[2026-01-01T12:00:00.000Z] [cell 1]\n"
                "x = 1\n\n"
                "[2026-01-01T12:00:30.000Z] [cell 1]\n"
                "x = 2\n\n",
            )
            self.write_jsonl(
                cell_versions / "abc111__pilot_study_1.ipynb.jsonl", snapshots
            )
            analytics.process_folder(str(changes), str(reports))

            for filename in [
                "student_analytics.csv",
                "cell_mapping.csv",
                "cell_execution_summary.csv",
                "idle_events.csv",
                "active_sessions.csv",
                "clipboard_events.csv",
                "error_recovery_events.csv",
                "solution_growth_events.csv",
            ]:
                self.assertTrue((reports / filename).is_file(), filename)

            overall = pd.read_csv(reports / "student_analytics.csv")
            self.assertEqual(overall.loc[0, "student"], "abc111__pilot_study_1")
            self.assertEqual(overall.loc[0, "total_copy_events"], 1)
            self.assertEqual(overall.loc[0, "total_paste_events"], 1)
            self.assertEqual(overall.loc[0, "execution_status_success"], 1)
            self.assertIn("task_1_code_exec_success", overall.columns)
            self.assertIn("task_1_code_modified_lines", overall.columns)
            self.assertIn("revision_added_characters", overall.columns)
            self.assertIn("error_recovery_rate", overall.columns)
            self.assertTrue(pd.isna(overall.loc[0, "revision_added_characters"]))
            self.assertGreater(overall.loc[0, "char_diff_added_characters"], 0)
            self.assertEqual(
                overall.loc[0, "char_diff_added_characters"],
                overall.loc[0, "task_1_code_char_diff_added_characters"],
            )

            execution = pd.read_csv(reports / "cell_execution_summary.csv")
            self.assertEqual(execution.loc[0, "cell"], "task_1_code")
            self.assertEqual(execution.loc[0, "success_count"], 1)

            recovery = pd.read_csv(reports / "error_recovery_events.csv")
            self.assertEqual(
                list(recovery.columns),
                [
                    "student", "cell", "error_time", "closure_time", "recovered",
                    "closure_reason", "recovery_duration_seconds",
                ],
            )

    def test_process_folder_exports_code_work_metrics_and_growth(self) -> None:
        """New indicators are flattened and the growth report remains plottable."""
        snapshots = [
            {"kind": "baseline", "timestamp": "2026-01-01T12:00:00.000Z", "cells": [
                {"cell_id": "a", "cell_index": 1, "template_cell_key": "task_1_code", "cell_type": "code", "source_editable": True, "source": ""},
            ]},
            {"kind": "save", "timestamp": "2026-01-01T12:00:20.000Z", "cells": [
                {"cell_id": "a", "cell_index": 1, "template_cell_key": "task_1_code", "cell_type": "code", "source_editable": True, "source": "print(1)"},
            ]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changes, versions, cell_versions, reports = (root / name for name in ("changes", "versions", "cell_versions", "reports"))
            filename = "aaa101__pilot.ipynb.log"
            self.write_text(changes / filename, "".join([
                log_line(0, "assignment-active"), log_line(5, "edit"),
                log_line(10, "execute", extra=" status: error"),
                log_line(15, "execute", extra=" status: success"),
            ]))
            self.write_jsonl(cell_versions / "aaa101__pilot.ipynb.jsonl", snapshots)
            analytics.process_folder(str(changes), str(reports))
            overall = pd.read_csv(reports / "student_analytics.csv")
            growth = pd.read_csv(reports / "solution_growth_events.csv")

        self.assertEqual(overall.loc[0, "task_1_code_time_to_first_action_seconds"], 5.0)
        self.assertEqual(overall.loc[0, "task_1_code_execution_before_edit_count"], 1)
        self.assertEqual(overall.loc[0, "task_1_code_max_consecutive_errors"], 1)
        self.assertEqual(growth.loc[0, "net_code_characters_from_baseline"], 8)


if __name__ == "__main__":
    unittest.main()

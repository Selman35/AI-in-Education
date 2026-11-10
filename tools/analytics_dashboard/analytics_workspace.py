"""Pure data-processing helpers for the local analytics dashboard.

The dashboard keeps uploaded data in a temporary directory.  This module is
deliberately independent from Streamlit so its safety and transformation rules
can be tested with ordinary unit tests.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REPORT_FILENAMES = {
    "student_analytics": "student_analytics.csv",
    "cell_mapping": "cell_mapping.csv",
    "cell_execution_summary": "cell_execution_summary.csv",
    "idle_events": "idle_events.csv",
    "active_sessions": "active_sessions.csv",
    "clipboard_events": "clipboard_events.csv",
}
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024


class DashboardInputError(ValueError):
    """Raised when an upload cannot safely be analysed."""


@dataclass(frozen=True)
class ProcessingResult:
    """In-memory results produced during one dashboard session."""

    reports: dict[str, pd.DataFrame]
    plots: dict[str, bytes]
    warnings: tuple[str, ...]
    manifest: dict[str, object]


def _safe_archive_member(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def extract_log_archive(archive_bytes: bytes, destination: Path) -> Path:
    """Safely extract a ZIP and return its one discovered log root.

    A valid package contains a ``changes`` folder.  Its sibling ``versions``
    and ``cell_versions`` folders are optional because older logs do not have
    structured snapshots.
    """
    if not archive_bytes:
        raise DashboardInputError("The uploaded ZIP file is empty.")

    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as error:
        raise DashboardInputError("The uploaded file is not a valid ZIP archive.") from error

    with archive:
        members = archive.infolist()
        total_size = sum(member.file_size for member in members)
        if total_size > MAX_ARCHIVE_BYTES:
            raise DashboardInputError(
                "The uncompressed archive is larger than the 500 MB local safety limit."
            )
        for member in members:
            if not _safe_archive_member(member.filename):
                raise DashboardInputError("The ZIP contains an unsafe file path.")
            # ZIP symlink mode: do not permit archives to create filesystem links.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise DashboardInputError("The ZIP contains an unsupported symbolic link.")
        archive.extractall(destination)

    changes_dirs = [
        directory
        for directory in destination.rglob("changes")
        if directory.is_dir() and any(directory.glob("*.ipynb.log"))
    ]
    if not changes_dirs:
        raise DashboardInputError(
            "No changes folder containing .ipynb.log files was found in the ZIP."
        )
    if len(changes_dirs) > 1:
        raise DashboardInputError(
            "The ZIP contains multiple log packages. Upload one log package at a time."
        )
    return changes_dirs[0].parent


def read_csv_uploads(files: Mapping[str, bytes]) -> dict[str, pd.DataFrame]:
    """Read uploaded CSV files without requiring a fixed report schema."""
    reports: dict[str, pd.DataFrame] = {}
    for name, content in files.items():
        if not name.lower().endswith(".csv"):
            raise DashboardInputError(f"{name} is not a CSV file.")
        try:
            frame = pd.read_csv(io.BytesIO(content))
        except Exception as error:  # pandas exposes multiple parser exceptions
            raise DashboardInputError(f"Could not read {name} as CSV: {error}") from error
        frame = frame.drop(columns=[column for column in frame if column.startswith("Unnamed:")])
        if frame.empty and not list(frame.columns):
            raise DashboardInputError(f"{name} does not contain tabular data.")
        stem = Path(name).stem
        key = stem
        suffix = 2
        while key in reports:
            key = f"{stem}_{suffix}"
            suffix += 1
        reports[key] = frame
    if not reports:
        raise DashboardInputError("Upload at least one CSV report.")
    return reports


def load_reports(directory: Path) -> dict[str, pd.DataFrame]:
    """Load every generated CSV report from an analytics output directory."""
    reports: dict[str, pd.DataFrame] = {}
    for key, filename in REPORT_FILENAMES.items():
        path = directory / filename
        if path.is_file():
            reports[key] = pd.read_csv(path).drop(
                columns=lambda column: str(column).startswith("Unnamed:"), errors="ignore"
            )
    if not reports:
        raise DashboardInputError("The analytics pipeline did not produce any CSV reports.")
    return reports


def validate_group_mapping(content: bytes) -> dict[str, str]:
    """Return a normalized participant-to-group mapping from one CSV upload."""
    try:
        mapping = pd.read_csv(io.BytesIO(content))
    except Exception as error:
        raise DashboardInputError(f"Could not read the group mapping CSV: {error}") from error

    required = {"participant_id", "group"}
    missing = required - set(mapping.columns)
    if missing:
        raise DashboardInputError(
            "Group mapping must contain participant_id and group columns; missing: "
            + ", ".join(sorted(missing))
        )

    mapping = mapping.loc[:, ["participant_id", "group"]].dropna()
    mapping["participant_id"] = mapping["participant_id"].astype(str).str.strip().str.lower()
    mapping["group"] = mapping["group"].astype(str).str.strip()
    mapping = mapping[(mapping["participant_id"] != "") & (mapping["group"] != "")]
    duplicates = mapping[mapping.duplicated("participant_id", keep=False)]
    if not duplicates.empty:
        conflicting = duplicates.groupby("participant_id")["group"].nunique()
        conflicting = conflicting[conflicting > 1]
        if not conflicting.empty:
            raise DashboardInputError(
                "A participant appears in more than one group: "
                + ", ".join(conflicting.index.tolist())
            )
    return dict(mapping.drop_duplicates("participant_id").itertuples(index=False, name=None))


def participant_id_from_student(value: object) -> str:
    """Return the pseudonymous prefix from an analytics student label."""
    student = str(value).strip()
    return student.split("__", 1)[0].lower() if student else ""


def apply_group_mapping(
    reports: Mapping[str, pd.DataFrame], mapping: Mapping[str, str]
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Add participant_id/group columns to reports that include a student column."""
    enriched: dict[str, pd.DataFrame] = {}
    found_participants: set[str] = set()
    for name, report in reports.items():
        frame = report.copy()
        if "student" in frame.columns:
            frame["participant_id"] = frame["student"].map(participant_id_from_student)
            frame["group"] = frame["participant_id"].map(mapping)
            found_participants.update(frame["participant_id"].dropna().astype(str))
        enriched[name] = frame

    warnings: list[str] = []
    unmatched_students = sorted(participant for participant in found_participants if participant and participant not in mapping)
    unmatched_mapping = sorted(participant for participant in mapping if participant not in found_participants)
    if unmatched_students:
        warnings.append("No group mapping for: " + ", ".join(unmatched_students))
    if unmatched_mapping:
        warnings.append("Group mapping has no matching analytics row for: " + ", ".join(unmatched_mapping))
    return enriched, warnings


def numeric_columns(frame: pd.DataFrame) -> list[str]:
    """List fields that contain one or more numeric values."""
    columns: list[str] = []
    for column in frame.columns:
        if pd.to_numeric(frame[column], errors="coerce").notna().any():
            columns.append(str(column))
    return columns


def categorical_columns(frame: pd.DataFrame) -> list[str]:
    """List fields suitable for labels, grouping, or categories."""
    return [str(column) for column in frame.columns if str(column) not in numeric_columns(frame)]


def timestamp_columns(frame: pd.DataFrame) -> list[str]:
    """Find columns whose names and values indicate timestamps."""
    candidates: list[str] = []
    for column in frame.columns:
        name = str(column).lower()
        if any(word in name for word in ("time", "date", "timestamp", "start", "end")):
            parsed = pd.to_datetime(frame[column], errors="coerce", utc=True)
            if parsed.notna().any():
                candidates.append(str(column))
    return candidates


def filtered_frame(
    frame: pd.DataFrame, groups: Iterable[str] | None = None, students: Iterable[str] | None = None
) -> pd.DataFrame:
    """Return a copy filtered by optional group and student selections."""
    result = frame.copy()
    selected_groups = set(groups or [])
    selected_students = set(students or [])
    if selected_groups and "group" in result.columns:
        result = result[result["group"].isin(selected_groups)]
    if selected_students and "student" in result.columns:
        result = result[result["student"].isin(selected_students)]
    return result


def aggregate_for_chart(
    frame: pd.DataFrame,
    x: str,
    y: str | None,
    group: str | None,
    aggregation: str,
) -> pd.DataFrame:
    """Prepare a safe table for the bar/line/scatter chart builder."""
    if x not in frame.columns:
        raise DashboardInputError(f"Chart x-axis column is not available: {x}")
    columns = [x] + ([group] if group and group in frame.columns else [])
    if y is None:
        return frame.groupby(columns, dropna=False).size().reset_index(name="count")
    if y not in frame.columns:
        raise DashboardInputError(f"Chart value column is not available: {y}")
    numeric = pd.to_numeric(frame[y], errors="coerce")
    data = frame.loc[:, columns].copy()
    data[y] = numeric
    data = data.dropna(subset=[y])
    if data.empty:
        raise DashboardInputError(f"{y} contains no numeric values to plot.")
    if aggregation == "None":
        return data
    aggregations = {"Count": "count", "Sum": "sum", "Mean": "mean", "Median": "median"}
    if aggregation not in aggregations:
        raise DashboardInputError(f"Unsupported aggregation: {aggregation}")
    if aggregation == "Count":
        return data.groupby(columns, dropna=False).size().reset_index(name="count")
    return data.groupby(columns, dropna=False)[y].agg(aggregations[aggregation]).reset_index()


def run_raw_log_analytics(archive_bytes: bytes, workspace: Path) -> ProcessingResult:
    """Analyse one uploaded raw-log ZIP in a temporary workspace."""
    from tools.analytics.compute_analytics import process_folder

    extracted = workspace / "uploaded_logs"
    reports_dir = workspace / "analytics"
    plots_dir = workspace / "plots"
    extracted.mkdir(parents=True)
    log_root = extract_log_archive(archive_bytes, extracted)
    changes_dir = log_root / "changes"
    reports_dir.mkdir()
    plots_dir.mkdir()
    process_folder(str(changes_dir), str(reports_dir))
    _create_standard_plots(reports_dir, plots_dir)
    return ProcessingResult(
        reports=load_reports(reports_dir),
        plots={path.name: path.read_bytes() for path in plots_dir.glob("*.png")},
        warnings=(),
        manifest={
            "input_type": "raw_log_zip",
            "source_log_files": len(list(changes_dir.glob("*.ipynb.log"))),
            "generated_reports": sorted(path.name for path in reports_dir.glob("*.csv")),
            "generated_plots": sorted(path.name for path in plots_dir.glob("*.png")),
        },
    )


def result_from_csv_uploads(files: Mapping[str, bytes], workspace: Path) -> ProcessingResult:
    """Create a dashboard result from uploaded analytics reports."""
    reports = read_csv_uploads(files)
    reports_dir = workspace / "analytics"
    plots_dir = workspace / "plots"
    reports_dir.mkdir(parents=True)
    plots_dir.mkdir()
    for name, frame in reports.items():
        frame.to_csv(reports_dir / f"{name}.csv", index=False)
    _create_standard_plots(reports_dir, plots_dir)
    return ProcessingResult(
        reports=reports,
        plots={path.name: path.read_bytes() for path in plots_dir.glob("*.png")},
        warnings=(),
        manifest={
            "input_type": "csv_reports",
            "generated_reports": sorted(path.name for path in reports_dir.glob("*.csv")),
            "generated_plots": sorted(path.name for path in plots_dir.glob("*.png")),
        },
    )


def _create_standard_plots(reports_dir: Path, plots_dir: Path) -> None:
    """Use the established plotting scripts when their required reports exist."""
    scripts = [
        (
            "plot_working_time.py",
            ["--input", str(reports_dir / "student_analytics.csv"), "--output", str(plots_dir / "estimated_working_time_by_student.png")],
        ),
        (
            "plot_idle_time.py",
            ["--input", str(reports_dir / "student_analytics.csv"), "--output", str(plots_dir / "idle_time_by_student.png")],
        ),
        (
            "plot_execution_outcomes.py",
            ["--input", str(reports_dir / "student_analytics.csv"), "--output", str(plots_dir / "execution_outcomes_by_student.png")],
        ),
        (
            "plot_clipboard_distribution.py",
            ["--input", str(reports_dir / "clipboard_events.csv"), "--output-dir", str(plots_dir)],
        ),
    ]
    for script, arguments in scripts:
        if "clipboard" in script and not (reports_dir / "clipboard_events.csv").is_file():
            continue
        if "clipboard" not in script and not (reports_dir / "student_analytics.csv").is_file():
            continue
        subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "tools" / "visualizations" / script), *arguments],
            cwd=REPOSITORY_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def build_download_zip(result: ProcessingResult) -> bytes:
    """Create a portable result package without preserving raw uploaded logs."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, frame in result.reports.items():
            archive.writestr(f"analytics/{name}.csv", frame.to_csv(index=False))
        for name, content in result.plots.items():
            archive.writestr(f"plots/{name}", content)
        archive.writestr("manifest.json", json.dumps(result.manifest, indent=2, sort_keys=True))
    return buffer.getvalue()


def clean_workspace(path: Path) -> None:
    """Remove one temporary workspace when callers need explicit cleanup."""
    shutil.rmtree(path, ignore_errors=True)

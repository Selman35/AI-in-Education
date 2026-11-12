"""Pure data-processing helpers for the local analytics dashboard.

The dashboard keeps uploaded data in a temporary directory.  This module is
deliberately independent from Streamlit so its safety and transformation rules
can be tested with ordinary unit tests.
"""

from __future__ import annotations

import hashlib
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
CELL_METRIC_SUFFIXES = tuple(
    sorted(
        (
            "char_diff_added_characters",
            "char_diff_removed_characters",
            "char_diff_net_character_change",
            "clipboard_lengths",
            "clipboard_events",
            "total_duration",
            "working_duration",
            "copy_events",
            "cut_events",
            "paste_events",
            "exec_success",
            "exec_error",
            "exec_count",
            "longest_idle_time",
            "idle_time",
            "idle_count",
            "modified_lines",
            "removed_lines",
            "added_lines",
        ),
        key=len,
        reverse=True,
    )
)
OVERALL_ANALYTICS_COLUMNS = {
    "total_duration",
    "total_working_duration",
    "total_clipboard_events",
    "total_copy_events",
    "total_cut_events",
    "total_paste_events",
    "clipboard_lengths",
    "average_clipboard_length",
    "clipboard_length_variance",
    "execution_events_count",
    "execution_status_success",
    "execution_status_error",
    "added_lines",
    "removed_lines",
    "modified_lines",
    "total_idle_time",
    "idle_episode_count",
    "longest_idle_time",
    "average_idle_time",
    "active_session_count",
    "total_active_session_duration",
    "longest_active_session_duration",
    "average_active_session_duration",
    "char_diff_added_characters",
    "char_diff_removed_characters",
    "char_diff_net_character_change",
}


class DashboardInputError(ValueError):
    """Raised when an upload cannot safely be analysed."""


@dataclass(frozen=True)
class ProcessingResult:
    """In-memory results produced during one dashboard session."""

    reports: dict[str, pd.DataFrame]
    plots: dict[str, bytes]
    warnings: tuple[str, ...]
    manifest: dict[str, object]


@dataclass(frozen=True)
class SavedChart:
    """Immutable, session-only record of one reviewed dashboard chart."""

    chart_id: str
    title: str
    caption: str
    chart_type: str
    time_unit: str
    specification: str
    plotted_csv: str
    png: bytes

    @property
    def data(self) -> pd.DataFrame:
        """Return the exact table that was visible when the chart was saved."""
        return pd.read_csv(io.StringIO(self.plotted_csv))


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


def is_time_measure(column: object) -> bool:
    """Return whether a numeric analytics field represents an elapsed duration.

    Timestamp fields are intentionally excluded: changing a timestamp from
    seconds to minutes would be meaningless.  This only identifies the
    duration-style fields produced by the existing analytics reports.
    """
    name = str(column).lower()
    if any(token in name for token in ("timestamp", "start_time", "end_time", "datetime", "date")):
        return False
    return (
        name.endswith("_seconds")
        or "duration" in name
        or "idle_time" in name
        or "working_time" in name
        or "active_session" in name
    )


def convert_time_unit(
    reports: Mapping[str, pd.DataFrame], unit: str
) -> dict[str, pd.DataFrame]:
    """Return display-only report copies in seconds or minutes.

    The input reports are never mutated, so raw-log interpretation and the
    downloadable analytics package retain their canonical seconds values.
    """
    if unit not in {"Seconds", "Minutes"}:
        raise DashboardInputError("Time display unit must be Seconds or Minutes.")
    factor = 60 if unit == "Minutes" else 1
    converted: dict[str, pd.DataFrame] = {}
    for name, report in reports.items():
        frame = report.copy()
        if factor != 1:
            for column in frame.columns:
                if is_time_measure(column):
                    values = pd.to_numeric(frame[column], errors="coerce")
                    if values.notna().any():
                        frame[column] = values / factor
        converted[name] = frame
    return converted


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


def search_rows(frame: pd.DataFrame, query: str) -> pd.DataFrame:
    """Return rows containing a case-insensitive query in any visible field."""
    query = query.strip()
    if not query:
        return frame.copy()
    matches = frame.astype("string").apply(
        lambda column: column.str.contains(query, case=False, na=False, regex=False)
    )
    return frame.loc[matches.any(axis=1)].copy()


def data_quality_findings(reports: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Describe structural issues that could make a dashboard misleading.

    This deliberately checks data integrity rather than judging student
    behaviour.  It is equally applicable to reports generated from raw logs
    and researcher-uploaded CSV files.
    """
    findings: list[dict[str, str]] = []
    known_reports = set(reports) & set(REPORT_FILENAMES)
    if "student_analytics" not in reports and known_reports:
        findings.append(
            {
                "severity": "warning",
                "dataset": "student_analytics",
                "message": "The overall participant report is missing; working-time and idle-time views are unavailable.",
            }
        )
    for name, frame in reports.items():
        if frame.empty:
            findings.append(
                {
                    "severity": "info",
                    "dataset": name,
                    "message": "This report has no rows. The matching event type may not have occurred.",
                }
            )
        if "student" in frame.columns:
            student = frame["student"].astype("string").str.strip()
            if student.isna().any() or (student == "").any():
                findings.append(
                    {
                        "severity": "warning",
                        "dataset": name,
                        "message": "One or more rows have no participant label.",
                    }
                )
    overview = reports.get("student_analytics")
    if overview is not None and "student" in overview.columns:
        duplicates = overview["student"].astype("string").duplicated(keep=False)
        if duplicates.any():
            findings.append(
                {
                    "severity": "warning",
                    "dataset": "student_analytics",
                    "message": "The overall report has duplicate participant rows; totals may be duplicated.",
                }
            )
    for name, frame in reports.items():
        for column in numeric_columns(frame):
            values = pd.to_numeric(frame[column], errors="coerce")
            if (values < 0).any():
                findings.append(
                    {
                        "severity": "warning",
                        "dataset": name,
                        "message": f"{column} contains negative values. Check the source report before interpreting it.",
                    }
                )
    return pd.DataFrame(findings, columns=["severity", "dataset", "message"])


def grouped_summary(frame: pd.DataFrame, value: str, group: str | None = None) -> pd.DataFrame:
    """Build a compact numeric cohort summary for a selected dashboard measure."""
    if value not in frame.columns:
        raise DashboardInputError(f"Summary value column is not available: {value}")
    data = pd.DataFrame({"value": pd.to_numeric(frame[value], errors="coerce")}).dropna()
    if data.empty:
        raise DashboardInputError(f"{value} contains no numeric values to summarise.")
    if group and group in frame.columns:
        data[group] = frame.loc[data.index, group].fillna("Unmapped").astype(str)
        return (
            data.groupby(group, dropna=False)["value"]
            .agg(participants="count", mean="mean", median="median", minimum="min", maximum="max", standard_deviation="std")
            .reset_index()
        )
    return pd.DataFrame(
        [
            {
                "participants": data["value"].count(),
                "mean": data["value"].mean(),
                "median": data["value"].median(),
                "minimum": data["value"].min(),
                "maximum": data["value"].max(),
                "standard_deviation": data["value"].std(),
            }
        ]
    )


def cell_label_from_metric_column(column: object) -> str | None:
    """Return a stable cell label from a flattened cell-level metric column."""
    name = str(column)
    if name in OVERALL_ANALYTICS_COLUMNS:
        return None
    for suffix in CELL_METRIC_SUFFIXES:
        marker = f"_{suffix}"
        if name.endswith(marker) and len(name) > len(marker):
            return name[: -len(marker)]
    return None


def available_cell_labels(reports: Mapping[str, pd.DataFrame]) -> list[str]:
    """List all stable cell labels referenced by the generated reports."""
    labels: set[str] = set()
    overview = reports.get("student_analytics")
    if overview is not None:
        labels.update(
            label
            for column in overview.columns
            if (label := cell_label_from_metric_column(column)) is not None
        )
    mapping = reports.get("cell_mapping")
    if mapping is not None and "cell_label" in mapping.columns:
        labels.update(mapping["cell_label"].dropna().astype(str))
    for report_name, cell_columns in {
        "cell_execution_summary": ("cell",),
        "clipboard_events": ("cell",),
        "idle_events": ("start_cell", "next_cell"),
        "active_sessions": ("start_cell", "end_cell"),
    }.items():
        report = reports.get(report_name)
        if report is not None:
            for column in cell_columns:
                if column in report.columns:
                    labels.update(report[column].dropna().astype(str))
    return sorted(label for label in labels if label and label != "unknown")


def filter_reports_to_cells(
    reports: Mapping[str, pd.DataFrame], included_cells: Iterable[str]
) -> dict[str, pd.DataFrame]:
    """Hide excluded cell-level metrics and event rows without changing source data."""
    included = set(included_cells)
    filtered: dict[str, pd.DataFrame] = {}
    for report_name, report in reports.items():
        frame = report.copy()
        if report_name == "student_analytics":
            hidden_columns = [
                column
                for column in frame.columns
                if (label := cell_label_from_metric_column(column)) is not None
                and label not in included
            ]
            frame = frame.drop(columns=hidden_columns)
        elif report_name == "cell_mapping" and "cell_label" in frame.columns:
            frame = frame[frame["cell_label"].isin(included)]
        elif report_name in {"cell_execution_summary", "clipboard_events"} and "cell" in frame.columns:
            frame = frame[frame["cell"].isin(included)]
        elif report_name == "idle_events" and "start_cell" in frame.columns:
            frame = frame[frame["start_cell"].isin(included)]
        elif report_name == "active_sessions" and "start_cell" in frame.columns:
            frame = frame[frame["start_cell"].isin(included)]
        filtered[report_name] = frame.reset_index(drop=True)
    return filtered


def cell_duration_summary(overview: pd.DataFrame) -> pd.DataFrame:
    """Return total, active, and remaining observed duration for each cell.

    ``working_duration`` is part of ``total_duration`` rather than a separate
    additive measure.  The remaining duration lets stacked charts show one
    full total bar without double-counting active work.
    """
    if "student" not in overview.columns:
        return pd.DataFrame(
            columns=[
                "student",
                "cell",
                "total_duration",
                "working_duration",
                "remaining_observed_duration",
            ]
        )
    rows: list[pd.DataFrame] = []
    suffix = "_total_duration"
    for total_column in overview.columns:
        label = cell_label_from_metric_column(total_column)
        if label is None or not str(total_column).endswith(suffix):
            continue
        working_column = f"{label}_working_duration"
        if working_column not in overview.columns:
            continue
        duration = pd.DataFrame(
            {
                "student": overview["student"],
                "cell": label,
                "total_duration": pd.to_numeric(overview[total_column], errors="coerce").fillna(0),
                "working_duration": pd.to_numeric(overview[working_column], errors="coerce").fillna(0),
            }
        )
        duration["total_duration"] = duration["total_duration"].clip(lower=0)
        duration["working_duration"] = duration["working_duration"].clip(
            lower=0, upper=duration["total_duration"]
        )
        duration["remaining_observed_duration"] = (
            duration["total_duration"] - duration["working_duration"]
        )
        rows.append(duration)
    if not rows:
        return pd.DataFrame(
            columns=[
                "student",
                "cell",
                "total_duration",
                "working_duration",
                "remaining_observed_duration",
            ]
        )
    return pd.concat(rows, ignore_index=True)


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


def chart_defaults(frame: pd.DataFrame, chart_type: str) -> dict[str, str | None]:
    """Choose a usable starting configuration from the currently loaded data.

    The dashboard still lets researchers choose every field themselves.  These
    defaults only make the first preview useful instead of showing an empty
    chart or requiring several selections before anything appears.
    """
    columns = [str(column) for column in frame.columns]
    numeric = numeric_columns(frame)
    categorical = categorical_columns(frame)
    timestamps = timestamp_columns(frame)
    category = "student" if "student" in columns else (categorical[0] if categorical else columns[0])
    numeric_value = numeric[0] if numeric else None
    group = "group" if "group" in categorical and chart_type == "Grouped bar" else None

    if chart_type == "Scatter" and len(numeric) >= 2:
        return {"x": numeric[0], "y": numeric[1], "group": None, "aggregation": "None"}
    if chart_type == "Histogram":
        return {"x": category, "y": numeric_value, "group": group, "aggregation": "None"}
    if chart_type == "Box plot":
        return {"x": category, "y": numeric_value, "group": group, "aggregation": "None"}
    if chart_type == "Line":
        return {
            "x": timestamps[0] if timestamps else category,
            "y": numeric_value,
            "group": group,
            "aggregation": "Mean" if numeric_value else "Count",
        }
    return {
        "x": category,
        "y": numeric_value,
        "group": group,
        "aggregation": "Sum" if numeric_value else "Count",
    }


def chart_png(
    frame: pd.DataFrame,
    chart_type: str,
    x: str,
    value: str,
    group: str | None,
    title: str,
    x_label: str,
    y_label: str,
    colour: str,
    palette: str = "tableau10",
) -> bytes:
    """Create a portable PNG export for the table currently shown in the chart.

    Altair is retained for the interactive website preview.  Matplotlib creates
    a self-contained PNG so a downloaded chart can be used in a report without
    an Altair renderer or an internet connection.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(9, 5))
    groups = [None] if not group or group not in frame.columns else list(frame[group].dropna().unique())
    palette_name = {"tableau10": "tab10", "category10": "tab10", "set2": "Set2"}.get(palette, "tab10")
    colour_map = plt.get_cmap(palette_name)
    group_colours = [colour_map(index / max(1, len(groups) - 1)) for index in range(len(groups))]

    if chart_type in {"Bar", "Grouped bar"}:
        categories = list(frame[x].astype(str).drop_duplicates())
        if len(groups) == 1:
            values = frame.set_index(frame[x].astype(str))[value].reindex(categories).fillna(0)
            axis.bar(categories, values, color=colour)
        else:
            width = 0.8 / len(groups)
            positions = list(range(len(categories)))
            for index, group_value in enumerate(groups):
                subset = frame[frame[group] == group_value].copy()
                values = subset.set_index(subset[x].astype(str))[value].reindex(categories).fillna(0)
                offsets = [position - 0.4 + width / 2 + index * width for position in positions]
                axis.bar(offsets, values, width=width, label=str(group_value), color=group_colours[index])
            axis.set_xticks(positions, categories)
            axis.legend(title=group)
    elif chart_type == "Scatter":
        for index, group_value in enumerate(groups):
            subset = frame if group_value is None else frame[frame[group] == group_value]
            axis.scatter(
                subset[x], subset[value], label=None if group_value is None else str(group_value),
                color=colour if group_value is None else group_colours[index],
            )
        if len(groups) > 1:
            axis.legend(title=group)
    elif chart_type == "Histogram":
        axis.hist(pd.to_numeric(frame[value], errors="coerce").dropna(), color=colour, edgecolor="white")
    elif chart_type == "Box plot":
        box_data = [
            pd.to_numeric(frame.loc[frame[x].astype(str) == category, value], errors="coerce").dropna()
            for category in frame[x].astype(str).drop_duplicates()
        ]
        axis.boxplot(box_data, tick_labels=list(frame[x].astype(str).drop_duplicates()))
    else:
        for index, group_value in enumerate(groups):
            subset = frame if group_value is None else frame[frame[group] == group_value]
            axis.plot(
                subset[x], subset[value], marker="o", label=None if group_value is None else str(group_value),
                color=colour if group_value is None else group_colours[index],
            )
        if len(groups) > 1:
            axis.legend(title=group)

    axis.set_title(title)
    axis.set_xlabel(x_label)
    axis.set_ylabel(y_label)
    axis.tick_params(axis="x", rotation=30)
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=160)
    plt.close(figure)
    return buffer.getvalue()


def _safe_chart_stem(value: str) -> str:
    """Return a deterministic archive-safe stem for one saved chart."""
    stem = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return stem or "chart"


def render_vega_lite_png(specification: str) -> bytes:
    """Render a Vega-Lite specification with the same renderer as the preview.

    ``vl-convert-python`` is intentionally imported only when a researcher
    saves a chart. This keeps ordinary CSV exploration available until PNG
    export is needed and gives a direct, actionable error if the optional
    dashboard dependency was not installed.
    """
    try:
        import vl_convert as vlc
    except ImportError as error:
        raise DashboardInputError(
            "Saved-chart PNG export requires vl-convert-python. Install the "
            "analytics dashboard requirements and restart Streamlit."
        ) from error
    return vlc.vegalite_to_png(specification)


def create_saved_chart(
    *,
    title: str,
    caption: str,
    chart_type: str,
    time_unit: str,
    specification: str,
    plotted_data: pd.DataFrame,
) -> SavedChart:
    """Create an immutable snapshot that can be exported after exploration."""
    if time_unit not in {"Seconds", "Minutes"}:
        raise DashboardInputError("Saved chart time unit must be Seconds or Minutes.")
    try:
        canonical_specification = json.dumps(
            json.loads(specification), sort_keys=True, separators=(",", ":")
        )
    except (TypeError, json.JSONDecodeError) as error:
        raise DashboardInputError("The chart does not have a valid Vega-Lite specification.") from error

    plotted_csv = plotted_data.to_csv(index=False)
    fingerprint_input = "\n".join(
        (title.strip(), caption.strip(), chart_type.strip(), time_unit, canonical_specification, plotted_csv)
    )
    chart_id = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
    return SavedChart(
        chart_id=chart_id,
        title=title.strip() or "Untitled chart",
        caption=caption.strip(),
        chart_type=chart_type.strip() or "Chart",
        time_unit=time_unit,
        specification=canonical_specification,
        plotted_csv=plotted_csv,
        png=render_vega_lite_png(canonical_specification),
    )


def add_saved_chart(
    saved_charts: Iterable[SavedChart], chart: SavedChart
) -> tuple[list[SavedChart], bool]:
    """Append a snapshot unless the identical chart/data was already saved."""
    collection = list(saved_charts)
    if any(existing.chart_id == chart.chart_id for existing in collection):
        return collection, False
    return [*collection, chart], True


def remove_saved_chart(saved_charts: Iterable[SavedChart], chart_id: str) -> list[SavedChart]:
    """Return a collection without one saved chart."""
    return [chart for chart in saved_charts if chart.chart_id != chart_id]


def build_saved_charts_zip(charts: Iterable[SavedChart]) -> bytes:
    """Package selected snapshots without including uploads or unselected data."""
    selected = list(charts)
    if not selected:
        raise DashboardInputError("Select at least one saved chart before downloading.")

    manifest_charts: list[dict[str, object]] = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, chart in enumerate(selected, start=1):
            stem = f"{index:02d}_{_safe_chart_stem(chart.title)}"
            files = {
                "png": f"charts/{stem}.png",
                "data": f"charts/{stem}.csv",
                "specification": f"charts/{stem}.vl.json",
            }
            archive.writestr(files["png"], chart.png)
            archive.writestr(files["data"], chart.plotted_csv)
            archive.writestr(files["specification"], chart.specification)
            manifest_charts.append(
                {
                    "chart_id": chart.chart_id,
                    "title": chart.title,
                    "caption": chart.caption,
                    "chart_type": chart.chart_type,
                    "display_time_unit": chart.time_unit,
                    "files": files,
                }
            )
        archive.writestr(
            "manifest.json",
            json.dumps({"saved_charts": manifest_charts}, indent=2, sort_keys=True),
        )
    return buffer.getvalue()


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

"""
Calculates the following metrics per notebook log (one row per notebook):
- total_duration: seconds between first and last event
- total_working_duration: observed time between meaningful actions less than 45 seconds apart
- total_clipboard_events: count of copy+cut+paste
- total_copy_events: count of copy events
- total_cut_events: count of cut events
- total_paste_events: count of paste events
- clipboard_lengths: JSON list of lengths (integers) for clipboard events in timestamp order
- average_clipboard_length: mean character length across clipboard events
- clipboard_length_variance: sample variance of clipboard-event character lengths
- execution_events_count: number of execute events
- execution_status_success: count of execute events with status success
- execution_status_error: count of execute events with status error
- added_lines: actual source lines inserted between saved snapshots
- removed_lines: actual source lines deleted between saved snapshots
- modified_lines: existing source lines changed between saved snapshots
- total_idle_time: time after the idle threshold with no meaningful student action
- idle_episode_count: number of idle periods
- longest_idle_time: duration of the longest idle period
- average_idle_time: mean duration of idle periods
- active_session_count: number of distinct active-work sessions
- total_active_session_duration: total span of active-work sessions
- longest_active_session_duration: longest active-work session span
- average_active_session_duration: mean active-work session span
- char_diff_added_characters: exact characters inserted between snapshots
- char_diff_removed_characters: exact characters deleted between snapshots
- char_diff_net_character_change: inserted characters minus deleted characters
- revision_added_characters: post-template-baseline inserted characters
- revision_removed_characters: post-template-baseline removed characters
- revision_ratio: removed / (added + removed) after the template baseline
- revision_changed_save_count: source-changing saves after the template baseline
- error_episode_count: distinct cell-level debugging episodes
- recovered_error_episode_count: episodes fixed after an observed edit
- error_recovery_rate: recovered episodes divided by all error episodes
- median_error_recovery_seconds: median error-to-success repair duration
Additionally, for each distinct cell seen in the logs, the script creates column groups
using a readable label (for example, ``cell_1``):
  cell_1_total_duration
  cell_1_working_duration
  cell_1_clipboard_events
  cell_1_copy_events
  cell_1_cut_events
  cell_1_paste_events
  cell_1_clipboard_lengths  (JSON list)
  cell_1_exec_count
  cell_1_exec_success
  cell_1_exec_error
  cell_1_char_diff_added_characters
  cell_1_char_diff_removed_characters
  cell_1_char_diff_net_character_change
  cell_1_added_lines
  cell_1_removed_lines
  cell_1_modified_lines

Output: a single CSV file (student_analytics.csv) where each row is a notebook
(student), plus ``cell_mapping.csv`` which maps readable labels to stable Jupyter
cell IDs, ``cell_execution_summary.csv`` with one readable row per cell, and
``idle_events.csv`` and ``active_sessions.csv`` with one row per respective event.

"""

import os
import glob
import re
import json
import difflib
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# CONFIGURATION
LOG_GLOB = "*.ipynb.log"
OUTPUT_FILENAME = "student_analytics.csv"
MAPPING_FILENAME = "cell_mapping.csv"
EXECUTION_SUMMARY_FILENAME = "cell_execution_summary.csv"
IDLE_EVENTS_FILENAME = "idle_events.csv"
ACTIVE_SESSIONS_FILENAME = "active_sessions.csv"
CLIPBOARD_EVENTS_FILENAME = "clipboard_events.csv"
ERROR_RECOVERY_EVENTS_FILENAME = "error_recovery_events.csv"
SOLUTION_GROWTH_EVENTS_FILENAME = "solution_growth_events.csv"
VERSION_LOG_DIRNAME = "versions"
CELL_VERSION_LOG_DIRNAME = "cell_versions"
WORKING_ACTIVITY_GAP = 45.0  # maximum seconds between actions counted as active work
IDLE_ACTIVITY_THRESHOLD = 120.0  # seconds without meaningful student activity
# Longer gaps cannot reliably be separated from a closed notebook or a missed
# browser lifecycle event, so they are excluded from observed idle time.
MAX_IDLE_OBSERVATION_GAP = 60 * 60.0  # one hour
ACTIVE_SESSION_GAP = 5 * 60.0  # seconds between meaningful actions
ERROR_RECOVERY_INACTIVE_GAP = 2 * 60.0  # brief absence does not end a repair attempt
LEGACY_EXECUTION_MATCH_WINDOW = 5.0  # seconds
MEANINGFUL_ACTIVITY_EVENTS = {"edit", "copy", "cut", "paste", "execute"}
ASSIGNMENT_PRESENCE_EVENTS = {"assignment-active", "assignment-inactive"}

# HELPERS
def parse_timestamp(ts_str):
    # Accept a couple of ISO-like formats commonly used
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts_str, fmt)
        except Exception:
            continue
    # fallback: try fromisoformat after removing trailing Z
    try:
        if ts_str.endswith("Z"):
            return datetime.fromisoformat(ts_str[:-1])
        return datetime.fromisoformat(ts_str)
    except Exception:
        raise ValueError(f"Unrecognized timestamp format: {ts_str}")

def sanitize_cell_id(cell_id: str) -> str:
    # Turn arbitrary cell id into safe header fragment (alphanumeric + underscores)
    if cell_id is None:
        return "unknown"
    s = re.sub(r"[^0-9A-Za-z]+", "_", str(cell_id))
    # avoid empty
    return s if s else "cell"

def extract_student_name(filepath: str) -> str:
    return os.path.basename(filepath).split(".")[0]


def parse_log_file(path: str):
    """
    Parse log lines into a list of events dictionaries with keys:
      ts (datetime), event (string), cell (string or None), cell_label (string or None),
      length (int or np.nan), status (str or None), raw (str)
    """
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            # timestamp at start in [..]
            m = re.match(r"^\[([^\]]+)\]", line)
            if not m:
                continue
            ts_field = m.group(1)
            try:
                ts = parse_timestamp(ts_field)
            except Exception:
                continue

            # event type
            ev_m = re.search(r"\[(focusin|focusout|edit|copy|cut|paste|execute|assignment-active|assignment-inactive|initial save|save(?: \d+)?)\]", line)
            if not ev_m:
                # not an event we care about
                continue
            ev = ev_m.group(1)

            # New logs use the consistent positional "cell N" format. Older
            # logs may still use "code cell N", "markdown cell N", or a UUID.
            cell = None
            cell_label = None
            m_cell = re.search(r"\[cell ID:\s*([0-9A-Za-z\-\_:]+)\]", line)
            if m_cell:
                cell = m_cell.group(1)
            else:
                m_alt = re.search(r"\[(?:(code|markdown)\s+)?cell\s*(\d+)\]", line)
                if m_alt:
                    # Keep old positional identifiers separate from UUIDs. This
                    # avoids incorrectly merging them in historical mixed logs.
                    cell_type = m_alt.group(1) or "positional"
                    cell = f"legacy_{cell_type}_cell_{m_alt.group(2)}"
                    cell_label = f"cell_{m_alt.group(2)}"
                else:
                    m_alt2 = re.search(r"\[cell:\s*([0-9A-Za-z\-\_:]+)\]", line)
                    if m_alt2:
                        cell = m_alt2.group(1)

            m_label = re.search(r"\[cell label:\s*([^\]]+)\]", line)
            if m_label:
                cell_label = m_label.group(1).strip()
                # New assignment templates log their stable semantic key without
                # a positional cell number. Use a separate internal reference so
                # per-cell analytics remain available for those events.
                if cell is None:
                    cell = f"template_{cell_label}"

            len_m = re.search(r"length:\s*(\d+)", line)
            length = int(len_m.group(1)) if len_m else np.nan

            status = None
            if ev.startswith("execute") or ev == "execute":
                stat_m = re.search(r"status:\s*(success|error)", line)
                if stat_m:
                    status = stat_m.group(1)

            events.append({"ts": ts, "event": ev, "cell": cell, "cell_label": cell_label,
                           "length": length, "status": status, "raw": line})

    if not events:
        return pd.DataFrame([], columns=["ts", "event", "cell", "cell_label", "length", "status", "raw"])
    df = pd.DataFrame(events).sort_values("ts").reset_index(drop=True)
    return df


def associate_legacy_execution_events(df: pd.DataFrame) -> pd.DataFrame:
    """Attach UUID-only execution events in old logs to a nearby numbered cell.

    Earlier extension versions logged focus and save events as ``[code cell N]``
    but executions as a UUID. An execution is normally surrounded by events for
    the active cell, so we use the nearest numbered event within five seconds.
    Events outside that window remain unlabelled rather than being guessed.
    """
    if df.empty:
        return df

    numbered = df[
        df["cell"].fillna("").str.startswith("legacy_") & df["cell_label"].notna()
    ]
    legacy_exec_indexes = df.index[
        (df["event"] == "execute")
        & df["cell_label"].isna()
        & ~df["cell"].fillna("").str.startswith("legacy_")
    ]

    for index in legacy_exec_indexes:
        if numbered.empty:
            continue
        differences = (numbered["ts"] - df.at[index, "ts"]).abs()
        nearest_index = differences.idxmin()
        if differences.loc[nearest_index].total_seconds() <= LEGACY_EXECUTION_MATCH_WINDOW:
            df.at[index, "cell"] = numbered.at[nearest_index, "cell"]
            df.at[index, "cell_label"] = numbered.at[nearest_index, "cell_label"]
    return df

# METRIC COMPUTATIONS
def compute_total_duration(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    return (df.ts.iloc[-1] - df.ts.iloc[0]).total_seconds()

def compute_working_duration(
    df: pd.DataFrame, activity_gap: float = WORKING_ACTIVITY_GAP
) -> float:
    """Estimate active work from closely spaced meaningful student actions.

    Only edits, clipboard actions, and executions are evidence of work. The
    interval between two such actions is counted only when it is shorter than
    ``activity_gap`` seconds. Focus events and automatic saves therefore cannot
    make an inactive period look like work. Assignment-inactive events end the
    current observation period, so time away from the assignment is excluded.
    """
    if df.empty:
        return 0.0

    relevant = df[
        df.event.isin(MEANINGFUL_ACTIVITY_EVENTS | ASSIGNMENT_PRESENCE_EVENTS)
    ].sort_values("ts")
    working_duration = 0.0
    previous_action = None
    is_assignment_active = True

    for _, event in relevant.iterrows():
        if event.event == "assignment-inactive":
            is_assignment_active = False
            previous_action = None
            continue

        if event.event == "assignment-active":
            is_assignment_active = True
            # Do not count time before the student returned to the assignment.
            previous_action = None
            continue

        if not is_assignment_active:
            continue

        if previous_action is not None:
            gap = (event.ts - previous_action.ts).total_seconds()
            if 0 <= gap < activity_gap:
                working_duration += gap
        previous_action = event

    return working_duration

def compute_clipboard_events(df: pd.DataFrame):
    cb_df = df[df.event.isin(["copy","cut","paste"])]
    lengths = cb_df.length.dropna().astype(int).tolist()
    return {
        "total": cb_df.shape[0],
        "copy": (cb_df.event == "copy").sum(),
        "cut": (cb_df.event == "cut").sum(),
        "paste": (cb_df.event == "paste").sum(),
        "lengths": lengths,
        "average_length": float(np.mean(lengths)) if lengths else np.nan,
        "length_variance": float(np.var(lengths, ddof=1)) if len(lengths) >= 2 else np.nan,
    }

def compute_execution_stats(df: pd.DataFrame):
    exec_df = df[df.event.str.contains("execute")]
    total_exec = exec_df.shape[0]
    success = exec_df[exec_df.status == "success"].shape[0]
    error = exec_df[exec_df.status == "error"].shape[0]
    return total_exec, success, error


def compute_error_recovery_events(df: pd.DataFrame, cell_labels: dict):
    """Identify cell-specific error episodes and edit-based successful recovery."""
    relevant = df[
        df.event.isin(MEANINGFUL_ACTIVITY_EVENTS | ASSIGNMENT_PRESENCE_EVENTS)
    ].sort_values("ts")
    episodes = {}
    results = []
    last_meaningful = None
    inactive_since = None

    def cell_key(event):
        return event.cell if pd.notna(event.cell) else "__unknown__"

    def close_episode(key, timestamp, reason, recovered=False):
        episode = episodes.pop(key, None)
        if episode is None:
            return
        results.append(
            {
                "cell": episode["cell"],
                "error_time": episode["error_time"],
                "closure_time": timestamp,
                "recovered": recovered,
                "closure_reason": reason,
                "recovery_duration_seconds": (
                    (timestamp - episode["error_time"]).total_seconds() if recovered else np.nan
                ),
            }
        )

    def close_all(timestamp, reason):
        for key in list(episodes):
            close_episode(key, timestamp, reason)

    for _, event in relevant.iterrows():
        if event.event == "assignment-inactive":
            inactive_since = event.ts
            continue
        if event.event == "assignment-active":
            if inactive_since is not None:
                absence = (event.ts - inactive_since).total_seconds()
                if absence >= ERROR_RECOVERY_INACTIVE_GAP:
                    close_all(inactive_since + timedelta(seconds=ERROR_RECOVERY_INACTIVE_GAP), "assignment_inactive_2min")
            inactive_since = None
            continue

        if inactive_since is not None:
            absence = (event.ts - inactive_since).total_seconds()
            if absence >= ERROR_RECOVERY_INACTIVE_GAP:
                close_all(inactive_since + timedelta(seconds=ERROR_RECOVERY_INACTIVE_GAP), "assignment_inactive_2min")
            inactive_since = None
        if last_meaningful is not None and (event.ts - last_meaningful).total_seconds() >= ACTIVE_SESSION_GAP:
            close_all(last_meaningful + timedelta(seconds=ACTIVE_SESSION_GAP), "five_minute_gap")
        last_meaningful = event.ts

        key = cell_key(event)
        if event.event == "edit" and key in episodes:
            episodes[key]["edited"] = True
        elif event.event == "execute" and event.status == "error":
            episodes.setdefault(
                key,
                {
                    "cell": cell_labels.get(event.cell, "unknown"),
                    "error_time": event.ts,
                    "edited": False,
                },
            )
        elif event.event == "execute" and event.status == "success" and key in episodes:
            edited = episodes[key]["edited"]
            close_episode(
                key,
                event.ts,
                "recovered_after_edit" if edited else "successful_rerun_without_edit",
                recovered=edited,
            )

    end_time = df.ts.max() if not df.empty else None
    if end_time is not None:
        close_all(end_time, "end_of_log")
    return results


def compute_error_recovery_summary(events):
    """Summarise debugging episodes while preserving no-error as an undefined rate."""
    episode_count = len(events)
    recovered = [event for event in events if event["recovered"]]
    durations = [event["recovery_duration_seconds"] for event in recovered]
    return {
        "error_episode_count": episode_count,
        "recovered_error_episode_count": len(recovered),
        "error_recovery_rate": len(recovered) / episode_count if episode_count else np.nan,
        "median_error_recovery_seconds": float(np.median(durations)) if durations else np.nan,
    }


def parse_version_snapshots(path: str):
    """Read source snapshots from a versions log in chronological order."""
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()

    header_pattern = re.compile(
        r"^\[(\d{4}-\d{2}-\d{2}T[^\]]+)\]\s+\[cell\s+\d+\]\n",
        re.MULTILINE,
    )
    headers = list(header_pattern.finditer(content))
    snapshots = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(content)
        snapshot = content[header.end():end]
        # Each snapshot entry is written with two separator newlines. Remove
        # only those separators, preserving any newlines in the actual source.
        if snapshot.endswith("\n\n"):
            snapshot = snapshot[:-2]
        snapshots.append(snapshot)
    return snapshots


def compute_character_diff_stats(version_log_path: str):
    """Count exact character insertions and removals across source snapshots."""
    snapshots = parse_version_snapshots(version_log_path)
    previous = ""
    added = removed = 0

    for current in snapshots:
        matcher = difflib.SequenceMatcher(a=previous, b=current, autojunk=False)
        for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if operation in ("delete", "replace"):
                removed += old_end - old_start
            if operation in ("insert", "replace"):
                added += new_end - new_start
        previous = current

    return {
        "char_diff_added_characters": added,
        "char_diff_removed_characters": removed,
        "char_diff_net_character_change": added - removed,
    }


def compute_character_diff_summary(cell_character_stats):
    """Sum modern structured per-cell character changes for one notebook."""
    if cell_character_stats is None:
        return None
    added = sum(
        values["char_diff_added_characters"]
        for values in cell_character_stats.values()
    )
    removed = sum(
        values["char_diff_removed_characters"]
        for values in cell_character_stats.values()
    )
    return {
        "char_diff_added_characters": added,
        "char_diff_removed_characters": removed,
        "char_diff_net_character_change": added - removed,
    }


def parse_cell_version_snapshots(path: str):
    """Read structured per-cell snapshots written by newer extension versions.

    A missing file means that the notebook was recorded before cell-level source
    snapshots existed, so cell character metrics must remain unavailable.
    """
    if not os.path.exists(path):
        return None

    snapshots = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            cells = record.get("cells")
            if not isinstance(cells, list):
                continue

            snapshot = {}
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                cell_id = cell.get("cell_id")
                cell_index = cell.get("cell_index")
                template_cell_key = cell.get("template_cell_key")
                source = cell.get("source")
                if not isinstance(cell_id, str) or not isinstance(cell_index, int) or not isinstance(source, str):
                    continue
                snapshot[cell_id] = {
                    "label": (
                        sanitize_cell_id(template_cell_key)
                        if isinstance(template_cell_key, str) and template_cell_key.strip()
                        else f"cell_{cell_index}"
                    ),
                    "source": source,
                    "cell_type": cell.get("cell_type"),
                    "source_editable": cell.get("source_editable"),
                }
            snapshots.append({
                "kind": record.get("kind", "save"),
                "cells": snapshot,
                "timestamp": record.get("timestamp"),
            })
    return snapshots


def student_writable_code_labels(cell_version_log_path: str):
    """Return labels of code cells that a student may edit, when recorded.

    Older snapshots lack policy/type metadata, so new code-only measures are
    intentionally unavailable rather than guessing from a cell-key name.
    """
    snapshots = parse_cell_version_snapshots(cell_version_log_path)
    if snapshots is None:
        return None
    labels = set()
    metadata_available = False
    for snapshot in snapshots:
        for cell in snapshot["cells"].values():
            if cell.get("cell_type") in {"code", "markdown"}:
                metadata_available = True
            if cell.get("cell_type") == "code" and cell.get("source_editable") is True:
                labels.add(cell["label"])
    return labels if metadata_available else None


def _code_cell_events(df: pd.DataFrame, cell_labels: dict, code_labels: set[str] | None):
    """Keep meaningful events for known student-editable code cells only."""
    if code_labels is None:
        return pd.DataFrame(columns=df.columns)
    selected = df[df.event.isin(MEANINGFUL_ACTIVITY_EVENTS)].copy()
    selected["analytics_label"] = selected.cell.map(cell_labels)
    return selected[selected.analytics_label.isin(code_labels)].sort_values("ts")


def compute_code_work_metrics(df: pd.DataFrame, cell_labels: dict, code_labels: set[str] | None):
    """Compute code-writing, testing, persistence, and revisit indicators."""
    empty = {label: {} for label in (code_labels or set())}
    events = _code_cell_events(df, cell_labels, code_labels)
    if events.empty:
        return empty
    start_events = df[df.event == "assignment-active"]
    start_time = start_events.ts.min() if not start_events.empty else df.ts.min()
    results = {}
    for label in sorted(code_labels or set()):
        group = events[events.analytics_label == label]
        if group.empty:
            results[label] = {}
            continue
        executions = group[group.event == "execute"]
        first_action = group.ts.min()
        first_execution = executions.ts.min() if not executions.empty else None
        changed_since_execution = True
        repeat_executions = 0
        execution_before_edit = 0
        error_streak = 0
        max_error_streak = 0
        for _, event in group.iterrows():
            if event.event == "edit":
                changed_since_execution = True
            elif event.event == "execute":
                if repeat_executions and not changed_since_execution:
                    execution_before_edit += 1
                if repeat_executions >= 0:
                    repeat_executions += 1
                changed_since_execution = False
                if event.status == "error":
                    error_streak += 1
                    max_error_streak = max(max_error_streak, error_streak)
                elif event.status == "success":
                    error_streak = 0
        execution_count = len(executions)
        repeat_count = max(0, execution_count - 1)
        working_seconds = compute_working_duration(group)
        results[label] = {
            "time_to_first_action_seconds": (first_action - start_time).total_seconds(),
            "time_to_first_execution_seconds": (
                (first_execution - start_time).total_seconds()
                if first_execution is not None else np.nan
            ),
            "execution_before_edit_count": execution_before_edit,
            "execution_before_edit_ratio": (
                execution_before_edit / repeat_count if repeat_count else np.nan
            ),
            "testing_frequency_per_working_minute": (
                execution_count / (working_seconds / 60.0) if working_seconds else np.nan
            ),
            "max_consecutive_errors": max_error_streak,
            "task_revisit_count": 0,
            "_execution_count": execution_count,
            "_working_seconds": working_seconds,
        }

    # A revisit is a return to a code cell that has already succeeded after
    # meaningful work in another writable code cell.
    succeeded, previous_label = set(), None
    for _, event in events.iterrows():
        label = event.analytics_label
        if previous_label is not None and label != previous_label and label in succeeded:
            results[label]["task_revisit_count"] += 1
        if event.event == "execute" and event.status == "success":
            succeeded.add(label)
        previous_label = label
    return results


def compute_code_work_summary(metrics):
    """Aggregate code-cell indicators without mixing error streaks across tasks."""
    if not metrics:
        return None
    values = list(metrics.values())
    def median(name):
        numbers = [value[name] for value in values if name in value and pd.notna(value[name])]
        return float(np.median(numbers)) if numbers else np.nan
    total_repeats = sum(
        max(0, value.get("_execution_count", 0) - 1) for value in values
    )
    unchanged = sum(value.get("execution_before_edit_count", 0) for value in values)
    return {
        "median_time_to_first_code_action_seconds": median("time_to_first_action_seconds"),
        "median_time_to_first_code_execution_seconds": median("time_to_first_execution_seconds"),
        "execution_before_edit_count": unchanged,
        "execution_before_edit_ratio": unchanged / total_repeats if total_repeats else np.nan,
        "testing_frequency_per_working_minute": (
            sum(value.get("_execution_count", 0) for value in values)
            / (sum(value.get("_working_seconds", 0.0) for value in values) / 60.0)
            if sum(value.get("_working_seconds", 0.0) for value in values) else np.nan
        ),
        "max_consecutive_errors": max((value.get("max_consecutive_errors", 0) for value in values), default=0),
        "task_revisit_count": sum(value.get("task_revisit_count", 0) for value in values),
    }


def compute_solution_growth_events(cell_version_log_path: str):
    """Return writable-code character/line counts for each saved snapshot."""
    snapshots = parse_cell_version_snapshots(cell_version_log_path)
    if snapshots is None or not any(item["kind"] == "baseline" for item in snapshots):
        return []
    baseline = next(item for item in snapshots if item["kind"] == "baseline")["cells"]
    code_ids = {
        cell_id for cell_id, cell in baseline.items()
        if cell.get("cell_type") == "code" and cell.get("source_editable") is True
    }
    if not code_ids:
        return []
    baseline_chars = sum(len(baseline[cell_id]["source"]) for cell_id in code_ids if cell_id in baseline)
    baseline_lines = sum(len(baseline[cell_id]["source"].splitlines()) for cell_id in code_ids if cell_id in baseline)
    events = []
    for snapshot in snapshots:
        if snapshot["kind"] != "save" or not isinstance(snapshot.get("timestamp"), str):
            continue
        current = snapshot["cells"]
        code_cells = [current[cell_id] for cell_id in code_ids if cell_id in current]
        chars = sum(len(cell["source"]) for cell in code_cells)
        lines = sum(len(cell["source"].splitlines()) for cell in code_cells)
        events.append({
            "timestamp": snapshot["timestamp"],
            "code_character_count": chars,
            "code_line_count": lines,
            "net_code_characters_from_baseline": chars - baseline_chars,
            "net_code_lines_from_baseline": lines - baseline_lines,
        })
    return events


def compute_cell_character_diff_stats(cell_version_log_path: str):
    """Return exact insertion/removal totals for each stable notebook cell.

    Cells are matched by Jupyter's cell ID. A newly added cell is compared with
    empty content; a removed cell is compared with empty content. This avoids
    treating an edit to one line as deletion of the entire previous line.
    """
    snapshots = parse_cell_version_snapshots(cell_version_log_path)
    if snapshots is None:
        return None

    stats = {}
    previous = {}
    for snapshot in snapshots:
        current = snapshot["cells"]
        if snapshot["kind"] == "baseline":
            previous = current
            continue
        for cell_id in previous.keys() | current.keys():
            old_cell = previous.get(cell_id, {"source": "", "label": None})
            new_cell = current.get(cell_id, {"source": "", "label": None})
            label = new_cell["label"] or old_cell["label"]
            if label is None:
                continue

            cell_stats = stats.setdefault(label, {"added": 0, "removed": 0})
            matcher = difflib.SequenceMatcher(
                a=old_cell["source"], b=new_cell["source"], autojunk=False
            )
            for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
                if operation in ("delete", "replace"):
                    cell_stats["removed"] += old_end - old_start
                if operation in ("insert", "replace"):
                    cell_stats["added"] += new_end - new_start
        previous = current

    return {
        label: {
            "char_diff_added_characters": values["added"],
            "char_diff_removed_characters": values["removed"],
            "char_diff_net_character_change": values["added"] - values["removed"],
        }
        for label, values in stats.items()
    }


def compute_cell_revision_stats(cell_version_log_path: str):
    """Measure source revisions after a prepared-notebook template baseline.

    Older logs without an explicit baseline are intentionally unavailable: their
    first snapshot may contain the assignment template and cannot safely be
    attributed to the student.
    """
    snapshots = parse_cell_version_snapshots(cell_version_log_path)
    if snapshots is None or not any(snapshot["kind"] == "baseline" for snapshot in snapshots):
        return None

    stats = {}
    previous = {}
    has_baseline = False
    for snapshot in snapshots:
        current = snapshot["cells"]
        if snapshot["kind"] == "baseline":
            previous = current
            has_baseline = True
            for cell in current.values():
                stats.setdefault(cell["label"], {"added": 0, "removed": 0, "changed_saves": 0})
            continue
        if not has_baseline:
            continue
        for cell_id in previous.keys() | current.keys():
            old_cell = previous.get(cell_id, {"source": "", "label": None})
            new_cell = current.get(cell_id, {"source": "", "label": None})
            label = new_cell["label"] or old_cell["label"]
            if label is None or old_cell["source"] == new_cell["source"]:
                continue
            values = stats.setdefault(label, {"added": 0, "removed": 0, "changed_saves": 0})
            values["changed_saves"] += 1
            matcher = difflib.SequenceMatcher(
                a=old_cell["source"], b=new_cell["source"], autojunk=False
            )
            for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
                if operation in ("delete", "replace"):
                    values["removed"] += old_end - old_start
                if operation in ("insert", "replace"):
                    values["added"] += new_end - new_start
        previous = current

    return {
        label: {
            "revision_added_characters": values["added"],
            "revision_removed_characters": values["removed"],
            "revision_ratio": (
                values["removed"] / (values["added"] + values["removed"])
                if values["added"] + values["removed"]
                else 0.0
            ),
            "revision_changed_save_count": values["changed_saves"],
        }
        for label, values in stats.items()
    }


def compute_revision_summary(cell_revision_stats):
    """Aggregate per-cell post-baseline revision metrics for one notebook."""
    if cell_revision_stats is None:
        return None
    added = sum(values["revision_added_characters"] for values in cell_revision_stats.values())
    removed = sum(values["revision_removed_characters"] for values in cell_revision_stats.values())
    return {
        "revision_added_characters": added,
        "revision_removed_characters": removed,
        "revision_ratio": removed / (added + removed) if added + removed else 0.0,
        "revision_changed_save_count": sum(
            values["revision_changed_save_count"] for values in cell_revision_stats.values()
        ),
    }


def compute_cell_line_diff_stats(cell_version_log_path: str):
    """Return semantic inserted, deleted, and modified source-line totals.

    A replacement is a modification, not a deletion plus an addition. When the
    two replacement blocks have different sizes, only the unmatched lines are
    counted as inserted or deleted.
    """
    snapshots = parse_cell_version_snapshots(cell_version_log_path)
    if snapshots is None:
        return None

    stats = {}
    previous = {}
    for snapshot in snapshots:
        current = snapshot["cells"]
        if snapshot["kind"] == "baseline":
            previous = current
            continue
        for cell_id in previous.keys() | current.keys():
            old_cell = previous.get(cell_id, {"source": "", "label": None})
            new_cell = current.get(cell_id, {"source": "", "label": None})
            label = new_cell["label"] or old_cell["label"]
            if label is None:
                continue

            cell_stats = stats.setdefault(
                label, {"added": 0, "removed": 0, "modified": 0}
            )
            matcher = difflib.SequenceMatcher(
                a=old_cell["source"].splitlines(),
                b=new_cell["source"].splitlines(),
                autojunk=False,
            )
            for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
                old_count = old_end - old_start
                new_count = new_end - new_start
                if operation == "insert":
                    cell_stats["added"] += new_count
                elif operation == "delete":
                    cell_stats["removed"] += old_count
                elif operation == "replace":
                    cell_stats["modified"] += min(old_count, new_count)
                    cell_stats["added"] += max(0, new_count - old_count)
                    cell_stats["removed"] += max(0, old_count - new_count)
        previous = current

    return {
        label: {
            "added_lines": values["added"],
            "removed_lines": values["removed"],
            "modified_lines": values["modified"],
        }
        for label, values in stats.items()
    }


def compute_line_diff_summary(cell_line_stats):
    """Sum semantic line metrics across all cells in one notebook."""
    if cell_line_stats is None:
        return {
            "added_lines": np.nan,
            "removed_lines": np.nan,
            "modified_lines": np.nan,
        }
    return {
        "added_lines": sum(values["added_lines"] for values in cell_line_stats.values()),
        "removed_lines": sum(values["removed_lines"] for values in cell_line_stats.values()),
        "modified_lines": sum(values["modified_lines"] for values in cell_line_stats.values()),
    }


def compute_idle_events(
    df: pd.DataFrame,
    cell_labels: dict,
    threshold: float = IDLE_ACTIVITY_THRESHOLD,
    maximum_observation_gap: float = MAX_IDLE_OBSERVATION_GAP,
):
    """Return periods of no meaningful student activity beyond ``threshold``.

    Focus events and automatic saves are deliberately excluded. Only edit,
    clipboard, and execution events reset the inactivity timer. Assignment
    active/inactive events bound the calculation so time outside JupyterLab is
    not counted. Gaps longer than ``maximum_observation_gap`` are excluded:
    they usually mean the notebook was closed or a lifecycle event was missed.
    The idle period is attributed to the last active cell.
    """
    relevant = df[
        df.event.isin(MEANINGFUL_ACTIVITY_EVENTS | ASSIGNMENT_PRESENCE_EVENTS)
    ].sort_values("ts")
    events = []
    is_assignment_active = True
    previous_activity = None

    def add_idle_period(following):
        if previous_activity is None:
            return
        gap = (following.ts - previous_activity.ts).total_seconds()
        if gap <= threshold or gap > maximum_observation_gap:
            return
        events.append({
            "start_time": previous_activity.ts + timedelta(seconds=threshold),
            "end_time": following.ts,
            "duration_seconds": gap - threshold,
            "start_cell": cell_labels.get(previous_activity.cell, "unknown"),
            "next_cell": cell_labels.get(following.cell, "unknown"),
        })

    for _, event in relevant.iterrows():
        if event.event == "assignment-inactive":
            if is_assignment_active:
                add_idle_period(event)
            is_assignment_active = False
            previous_activity = None
        elif event.event == "assignment-active":
            is_assignment_active = True
            # Start a new observation period without carrying inactivity from
            # before the student returned to the assignment.
            previous_activity = event
        elif is_assignment_active:
            add_idle_period(event)
            previous_activity = event
    return events


def compute_idle_summary(idle_events):
    durations = [event["duration_seconds"] for event in idle_events]
    return {
        "total_idle_time": sum(durations),
        "idle_episode_count": len(durations),
        "longest_idle_time": max(durations, default=0.0),
        "average_idle_time": sum(durations) / len(durations) if durations else 0.0,
    }


def compute_active_sessions(df: pd.DataFrame, cell_labels: dict, gap_threshold: float = ACTIVE_SESSION_GAP):
    """Group meaningful activity into sessions separated by five-minute gaps.

    A session includes edits, clipboard actions, and executions. Presence events
    close a session when the student leaves the assignment, preventing separate
    visits from being merged. Historical logs without both edit and presence
    events are not evaluated because their session boundaries are unreliable.
    """
    has_edit_events = df.event.eq("edit").any()
    has_presence_events = df.event.isin(ASSIGNMENT_PRESENCE_EVENTS).any()
    if not has_edit_events or not has_presence_events:
        return None

    relevant = df[
        df.event.isin(MEANINGFUL_ACTIVITY_EVENTS | ASSIGNMENT_PRESENCE_EVENTS)
    ].sort_values("ts")
    sessions = []
    current = None
    # Require an explicit modern presence event before starting measurement.
    # This prevents activity from an older portion of a mixed log from being
    # treated as a reliable session.
    is_assignment_active = False

    def start_session(event):
        return {
            "start_time": event.ts,
            "end_time": event.ts,
            "start_cell": cell_labels.get(event.cell, "unknown"),
            "end_cell": cell_labels.get(event.cell, "unknown"),
            "end_reason": "end_of_log",
        }

    def close_session(reason: str):
        nonlocal current
        if current is None:
            return
        current["end_reason"] = reason
        current["duration_seconds"] = (current["end_time"] - current["start_time"]).total_seconds()
        sessions.append(current)
        current = None

    for _, event in relevant.iterrows():
        if event.event == "assignment-inactive":
            close_session("assignment_inactive")
            is_assignment_active = False
            continue
        if event.event == "assignment-active":
            # A preceding inactive event closes any prior session. Do not start
            # a session until the student performs a meaningful action.
            is_assignment_active = True
            continue

        if not is_assignment_active:
            continue

        if current is None:
            current = start_session(event)
            continue

        gap = (event.ts - current["end_time"]).total_seconds()
        if gap >= gap_threshold:
            close_session("five_minute_gap")
            current = start_session(event)
        else:
            current["end_time"] = event.ts
            current["end_cell"] = cell_labels.get(event.cell, "unknown")

    close_session("end_of_log")
    return sessions


def compute_active_session_summary(sessions):
    if sessions is None:
        return {
            "active_session_count": np.nan,
            "total_active_session_duration": np.nan,
            "longest_active_session_duration": np.nan,
            "average_active_session_duration": np.nan,
        }

    durations = [session["duration_seconds"] for session in sessions]
    return {
        "active_session_count": len(durations),
        "total_active_session_duration": sum(durations),
        "longest_active_session_duration": max(durations, default=0.0),
        "average_active_session_duration": sum(durations) / len(durations) if durations else 0.0,
    }

def compute_per_cell_metrics(df: pd.DataFrame):
    """
    For each non-null cell, compute the same group of metrics but restricted to that cell's events.
    Returns a dict mapping sanitized_cell_id -> metrics dict.
    """
    result = {}
    # consider only non-null cell ids
    cells = [c for c in df.cell.unique() if pd.notna(c)]
    for cell in cells:
        grp = df[df.cell == cell].sort_values("ts")
        if grp.empty:
            continue
        total_duration = compute_total_duration(grp)
        working_duration = compute_working_duration(grp)
        clipboard = compute_clipboard_events(grp)
        exec_count, exec_success, exec_error = compute_execution_stats(grp)
        san = sanitize_cell_id(cell)
        result[san] = {
            "cell_raw": cell,
            "total_duration": total_duration,
            "working_duration": working_duration,
            "clipboard_events": clipboard["total"],
            "copy_events": clipboard["copy"],
            "cut_events": clipboard["cut"],
            "paste_events": clipboard["paste"],
            "clipboard_lengths": clipboard["lengths"],
            "exec_count": exec_count,
            "exec_success": exec_success,
            "exec_error": exec_error
        }
    return result


def resolve_cell_labels(df: pd.DataFrame) -> dict:
    """Map each log cell reference to a readable, unique column prefix.

    Assignment-template logs provide a stable ``[cell label: ...]`` key, while
    ordinary notebooks provide ``[cell N]``. Historical UUID-only execution
    entries are first matched to a nearby numbered event when the evidence is
    strong enough.
    """
    labels = {}
    used = set()
    for cell in [c for c in df.cell.unique() if pd.notna(c)]:
        cell_labels = df.loc[df.cell == cell, "cell_label"].dropna().unique()
        if len(cell_labels):
            label = sanitize_cell_id(cell_labels[0])
            labels[cell] = label
            used.add(label)

    fallback_number = 1
    for cell in [c for c in df.cell.unique() if pd.notna(c) and c not in labels]:
        while f"unlabelled_cell_{fallback_number}" in used:
            fallback_number += 1
        label = f"unlabelled_cell_{fallback_number}"
        labels[cell] = label
        used.add(label)
        fallback_number += 1
    return labels

# MAIN: iterate over logs and build a single wide dataframe
def process_folder(folder: str, output_folder: str | None = None):
    """Generate reports from notebook logs in ``folder``.

    When ``output_folder`` is omitted, reports are written next to the logs for
    backward compatibility. The project runner writes them to a separate
    analytics directory instead.
    """
    output_folder = output_folder or folder
    os.makedirs(output_folder, exist_ok=True)
    rows = []
    mapping_rows = []
    execution_rows = []
    idle_rows = []
    active_session_rows = []
    clipboard_rows = []
    error_recovery_rows = []
    solution_growth_rows = []
    # Collect union of cell IDs across all students to ensure consistent columns
    all_cell_keys = set()

    filepaths = glob.glob(os.path.join(folder, LOG_GLOB))
    if not filepaths:
        print(f"No log files found in {folder} matching {LOG_GLOB}")
        return

    for path in sorted(filepaths):
        student = extract_student_name(path)
        df = parse_log_file(path)
        df = associate_legacy_execution_events(df)
        row = {"student": student}
        cell_labels = resolve_cell_labels(df)
        idle_events = compute_idle_events(df, cell_labels)
        active_sessions = compute_active_sessions(df, cell_labels)
        error_recovery_events = compute_error_recovery_events(df, cell_labels)
        row.update(compute_idle_summary(idle_events))
        row.update(compute_active_session_summary(active_sessions))
        row.update(compute_error_recovery_summary(error_recovery_events))

        for event in idle_events:
            idle_rows.append({"student": student, **event})
        if active_sessions is not None:
            for session in active_sessions:
                active_session_rows.append({"student": student, **session})
        for event in error_recovery_events:
            error_recovery_rows.append({"student": student, **event})

        for _, event in df[df.event.isin(["copy", "cut", "paste"])].iterrows():
            if pd.isna(event.length):
                continue
            clipboard_rows.append({
                "student": student,
                "timestamp": event.ts.isoformat(),
                "event": event.event,
                "cell": cell_labels.get(event.cell, "unknown"),
                "character_length": int(event.length),
            })

        for raw_cell, label in cell_labels.items():
            mapping_rows.append({
                "student": student,
                "cell_label": label,
                "cell_id": raw_cell if not raw_cell.startswith("legacy_") else "",
                "log_cell_reference": raw_cell,
            })

        # Overall metrics
        row["total_duration"] = compute_total_duration(df)
        row["total_working_duration"] = compute_working_duration(df)
        clipboard = compute_clipboard_events(df)
        row["total_clipboard_events"] = clipboard["total"]
        row["total_copy_events"] = clipboard["copy"]
        row["total_cut_events"] = clipboard["cut"]
        row["total_paste_events"] = clipboard["paste"]
        row["average_clipboard_length"] = clipboard["average_length"]
        row["clipboard_length_variance"] = clipboard["length_variance"]
        
        # store clipboard lengths as JSON string to keep as single CSV cell
        row["clipboard_lengths"] = json.dumps(clipboard["lengths"])
        exec_total, exec_success, exec_error = compute_execution_stats(df)
        row["execution_events_count"] = exec_total
        row["execution_status_success"] = exec_success
        row["execution_status_error"] = exec_error
        version_path = os.path.join(
            os.path.dirname(os.path.normpath(folder)),
            VERSION_LOG_DIRNAME,
            os.path.basename(path),
        )
        cell_version_path = os.path.join(
            os.path.dirname(os.path.normpath(folder)),
            CELL_VERSION_LOG_DIRNAME,
            os.path.splitext(os.path.basename(path))[0] + ".jsonl",
        )
        cell_character_stats = compute_cell_character_diff_stats(cell_version_path)
        cell_line_stats = compute_cell_line_diff_stats(cell_version_path)
        cell_revision_stats = compute_cell_revision_stats(cell_version_path)
        code_labels = student_writable_code_labels(cell_version_path)
        code_work_metrics = compute_code_work_metrics(df, cell_labels, code_labels)
        code_work_summary = compute_code_work_summary(code_work_metrics)
        for event in compute_solution_growth_events(cell_version_path):
            solution_growth_rows.append({"student": student, **event})
        row.update(
            compute_character_diff_summary(cell_character_stats)
            or compute_character_diff_stats(version_path)
        )
        row.update(compute_line_diff_summary(cell_line_stats))
        revision_summary = compute_revision_summary(cell_revision_stats)
        if revision_summary is None:
            row.update({
                "revision_added_characters": np.nan,
                "revision_removed_characters": np.nan,
                "revision_ratio": np.nan,
                "revision_changed_save_count": np.nan,
            })
        else:
            row.update(revision_summary)
        if code_work_summary is None:
            row.update({
                "median_time_to_first_code_action_seconds": np.nan,
                "median_time_to_first_code_execution_seconds": np.nan,
                "execution_before_edit_count": np.nan,
                "execution_before_edit_ratio": np.nan,
                "testing_frequency_per_working_minute": np.nan,
                "max_consecutive_errors": np.nan,
                "task_revisit_count": np.nan,
            })
        else:
            row.update(code_work_summary)

        # Per-cell
        cell_metrics = compute_per_cell_metrics(df)
        for san_id, metrics in cell_metrics.items():
            label = cell_labels[metrics["cell_raw"]]
            all_cell_keys.add(label)
            # create flattened columns with clear names
            row[f"{label}_total_duration"] = metrics["total_duration"]
            row[f"{label}_working_duration"] = metrics["working_duration"]
            row[f"{label}_clipboard_events"] = metrics["clipboard_events"]
            row[f"{label}_copy_events"] = metrics["copy_events"]
            row[f"{label}_cut_events"] = metrics["cut_events"]
            row[f"{label}_paste_events"] = metrics["paste_events"]
            row[f"{label}_clipboard_lengths"] = json.dumps(metrics["clipboard_lengths"])
            row[f"{label}_exec_count"] = metrics["exec_count"]
            row[f"{label}_exec_success"] = metrics["exec_success"]
            row[f"{label}_exec_error"] = metrics["exec_error"]
            if metrics["exec_count"]:
                execution_rows.append({
                    "student": student,
                    "cell": label,
                    "execution_count": metrics["exec_count"],
                    "success_count": metrics["exec_success"],
                    "error_count": metrics["exec_error"],
                })

        if cell_character_stats is not None:
            for label, metrics in cell_character_stats.items():
                all_cell_keys.add(label)
                row[f"{label}_char_diff_added_characters"] = metrics["char_diff_added_characters"]
                row[f"{label}_char_diff_removed_characters"] = metrics["char_diff_removed_characters"]
                row[f"{label}_char_diff_net_character_change"] = metrics["char_diff_net_character_change"]

        if cell_line_stats is not None:
            for label, metrics in cell_line_stats.items():
                all_cell_keys.add(label)
                row[f"{label}_added_lines"] = metrics["added_lines"]
                row[f"{label}_removed_lines"] = metrics["removed_lines"]
                row[f"{label}_modified_lines"] = metrics["modified_lines"]

        if cell_revision_stats is not None:
            for label, metrics in cell_revision_stats.items():
                all_cell_keys.add(label)
                row[f"{label}_revision_added_characters"] = metrics["revision_added_characters"]
                row[f"{label}_revision_removed_characters"] = metrics["revision_removed_characters"]
                row[f"{label}_revision_ratio"] = metrics["revision_ratio"]
                row[f"{label}_revision_changed_save_count"] = metrics["revision_changed_save_count"]

        for label, metrics in code_work_metrics.items():
            all_cell_keys.add(label)
            for name, value in metrics.items():
                if not name.startswith("_"):
                    row[f"{label}_{name}"] = value

        recovery_by_cell = {}
        for event in error_recovery_events:
            recovery_by_cell.setdefault(event["cell"], []).append(event)
        for label, events in recovery_by_cell.items():
            all_cell_keys.add(label)
            for key, value in compute_error_recovery_summary(events).items():
                row[f"{label}_{key}"] = value

        idle_by_cell = {}
        for event in idle_events:
            stats = idle_by_cell.setdefault(event["start_cell"], [])
            stats.append(event["duration_seconds"])
        for label, durations in idle_by_cell.items():
            all_cell_keys.add(label)
            row[f"{label}_idle_time"] = sum(durations)
            row[f"{label}_idle_count"] = len(durations)
            row[f"{label}_longest_idle_time"] = max(durations)

        rows.append(row)

    # Build DataFrame and ensure all cell columns appear (fill missing with 0 or empty list)
    df_out = pd.DataFrame(rows).set_index("student")

    # Ensure consistent columns for all cells found across dataset
    for san in sorted(all_cell_keys):
        cols = [
            f"{san}_total_duration",
            f"{san}_working_duration",
            f"{san}_clipboard_events",
            f"{san}_copy_events",
            f"{san}_cut_events",
            f"{san}_paste_events",
            f"{san}_clipboard_lengths",
            f"{san}_exec_count",
            f"{san}_exec_success",
            f"{san}_exec_error",
            f"{san}_idle_time",
            f"{san}_idle_count",
            f"{san}_longest_idle_time",
            f"{san}_char_diff_added_characters",
            f"{san}_char_diff_removed_characters",
            f"{san}_char_diff_net_character_change",
            f"{san}_added_lines",
            f"{san}_removed_lines",
            f"{san}_modified_lines",
            f"{san}_revision_added_characters",
            f"{san}_revision_removed_characters",
            f"{san}_revision_ratio",
            f"{san}_revision_changed_save_count",
            f"{san}_error_episode_count",
            f"{san}_recovered_error_episode_count",
            f"{san}_error_recovery_rate",
            f"{san}_median_error_recovery_seconds",
            f"{san}_time_to_first_action_seconds",
            f"{san}_time_to_first_execution_seconds",
            f"{san}_execution_before_edit_count",
            f"{san}_execution_before_edit_ratio",
            f"{san}_testing_frequency_per_working_minute",
            f"{san}_max_consecutive_errors",
            f"{san}_task_revisit_count",
        ]
        for c in cols:
            if c not in df_out.columns:
                # Fill with sensible defaults
                if c.endswith("_clipboard_lengths"):
                    df_out[c] = json.dumps([])
                elif c.endswith(("_clipboard_events", "_copy_events", "_cut_events", "_paste_events", "_exec_count", "_exec_success", "_exec_error", "_idle_count")):
                    df_out[c] = 0
                elif "_char_diff_" in c:
                    # Older logs have no structured cell snapshots; blank means
                    # unavailable rather than a measured value of zero.
                    df_out[c] = np.nan
                elif "_revision_" in c or c.endswith(("_error_recovery_rate", "_median_error_recovery_seconds")):
                    df_out[c] = np.nan
                elif c.endswith(("_time_to_first_action_seconds", "_time_to_first_execution_seconds", "_execution_before_edit_count", "_execution_before_edit_ratio", "_testing_frequency_per_working_minute", "_max_consecutive_errors", "_task_revisit_count")):
                    df_out[c] = np.nan
                elif c.endswith(("_error_episode_count", "_recovered_error_episode_count")):
                    df_out[c] = 0
                elif c.endswith(("_added_lines", "_removed_lines", "_modified_lines")):
                    # Older logs have no structured cell snapshots, so an
                    # accurate semantic line count is unavailable.
                    df_out[c] = np.nan
                else:
                    df_out[c] = 0.0

    # Reorder columns: overall metrics first, then cells in lexicographic order
    overall_cols = [
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
        "revision_added_characters",
        "revision_removed_characters",
        "revision_ratio",
        "revision_changed_save_count",
        "error_episode_count",
        "recovered_error_episode_count",
        "error_recovery_rate",
        "median_error_recovery_seconds",
        "median_time_to_first_code_action_seconds",
        "median_time_to_first_code_execution_seconds",
        "execution_before_edit_count",
        "execution_before_edit_ratio",
        "testing_frequency_per_working_minute",
        "max_consecutive_errors",
        "task_revisit_count",
    ]
    cell_cols = [c for c in df_out.columns if c not in overall_cols]
    df_out = df_out[overall_cols + sorted(cell_cols)]

    outpath = os.path.join(output_folder, OUTPUT_FILENAME)
    df_out.to_csv(outpath, index=True)
    print(f"Saved analytics to {outpath}")

    mapping_path = os.path.join(output_folder, MAPPING_FILENAME)
    pd.DataFrame(mapping_rows).to_csv(mapping_path, index=False)
    print(f"Saved cell mapping to {mapping_path}")

    execution_path = os.path.join(output_folder, EXECUTION_SUMMARY_FILENAME)
    pd.DataFrame(
        execution_rows,
        columns=["student", "cell", "execution_count", "success_count", "error_count"],
    ).to_csv(execution_path, index=False)
    print(f"Saved execution summary to {execution_path}")

    idle_path = os.path.join(output_folder, IDLE_EVENTS_FILENAME)
    pd.DataFrame(
        idle_rows,
        columns=["student", "start_time", "end_time", "duration_seconds", "start_cell", "next_cell"],
    ).to_csv(idle_path, index=False)
    print(f"Saved idle events to {idle_path}")

    active_sessions_path = os.path.join(output_folder, ACTIVE_SESSIONS_FILENAME)
    pd.DataFrame(
        active_session_rows,
        columns=["student", "start_time", "end_time", "duration_seconds", "start_cell", "end_cell", "end_reason"],
    ).to_csv(active_sessions_path, index=False)
    print(f"Saved active sessions to {active_sessions_path}")

    clipboard_path = os.path.join(output_folder, CLIPBOARD_EVENTS_FILENAME)
    pd.DataFrame(
        clipboard_rows,
        columns=["student", "timestamp", "event", "cell", "character_length"],
    ).to_csv(clipboard_path, index=False)
    print(f"Saved clipboard events to {clipboard_path}")

    recovery_path = os.path.join(output_folder, ERROR_RECOVERY_EVENTS_FILENAME)
    pd.DataFrame(
        error_recovery_rows,
        columns=[
            "student", "cell", "error_time", "closure_time", "recovered",
            "closure_reason", "recovery_duration_seconds",
        ],
    ).to_csv(recovery_path, index=False)
    print(f"Saved error recovery events to {recovery_path}")

    growth_path = os.path.join(output_folder, SOLUTION_GROWTH_EVENTS_FILENAME)
    pd.DataFrame(
        solution_growth_rows,
        columns=[
            "student", "timestamp", "code_character_count", "code_line_count",
            "net_code_characters_from_baseline", "net_code_lines_from_baseline",
        ],
    ).to_csv(growth_path, index=False)
    print(f"Saved solution growth events to {growth_path}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) not in (2, 3):
        print("Usage: python compute_analytics.py /path/to/log/folder [output_folder]")
        sys.exit(1)
    folder = sys.argv[1]
    output_folder = sys.argv[2] if len(sys.argv) == 3 else None
    process_folder(folder, output_folder)

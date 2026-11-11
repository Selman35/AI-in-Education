"""Local no-code analytics workspace for JupyterLab activity logs.

Run from the repository root:
    streamlit run tools/analytics_dashboard/app.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd

from analytics_workspace import (
    DashboardInputError,
    ProcessingResult,
    aggregate_for_chart,
    apply_group_mapping,
    available_cell_labels,
    build_download_zip,
    categorical_columns,
    filtered_frame,
    filter_reports_to_cells,
    numeric_columns,
    result_from_csv_uploads,
    run_raw_log_analytics,
    timestamp_columns,
    validate_group_mapping,
)


def initialise_state(st: Any) -> None:
    st.session_state.setdefault("analytics_result", None)
    st.session_state.setdefault("analytics_group_warnings", [])
    st.session_state.setdefault("analytics_download", None)
    st.session_state.setdefault("analytics_selected_cells", [])


def apply_mapping(
    result: ProcessingResult, mapping_bytes: bytes | None
) -> tuple[ProcessingResult, list[str]]:
    if not mapping_bytes:
        return result, []
    mapping = validate_group_mapping(mapping_bytes)
    reports, warnings = apply_group_mapping(result.reports, mapping)
    mapped_manifest = dict(result.manifest)
    mapped_manifest["group_mapping_rows"] = len(mapping)
    return ProcessingResult(reports, result.plots, result.warnings, mapped_manifest), warnings


def standard_bar_chart(frame: pd.DataFrame, column: str, title: str, unit: str) -> alt.Chart:
    data = frame.loc[:, ["student", column] + (["group"] if "group" in frame.columns else [])].copy()
    data[column] = pd.to_numeric(data[column], errors="coerce").fillna(0)
    encoding: dict[str, Any] = {
        "x": alt.X(f"{column}:Q", title=unit),
        "y": alt.Y("student:N", sort="-x", title="Participant"),
        "tooltip": ["student:N", alt.Tooltip(f"{column}:Q", format=".2f")],
    }
    if "group" in data.columns:
        encoding["color"] = alt.Color("group:N", title="Group")
    return alt.Chart(data, title=title).mark_bar().encode(**encoding).interactive()


def render_preset_views(st: Any, reports: dict[str, pd.DataFrame]) -> None:
    overview = reports.get("student_analytics")
    if overview is not None:
        st.subheader("Core study measures")
        preset_columns = [
            ("total_working_duration", "Estimated working time", "Seconds"),
            ("total_idle_time", "Total idle time", "Seconds"),
        ]
        for column, title, unit in preset_columns:
            if column in overview.columns:
                st.altair_chart(
                    standard_bar_chart(overview, column, title, unit),
                    alt=f"{title} by participant",
                )

        execution = {"execution_status_success", "execution_status_error"}
        if execution.issubset(overview.columns):
            execution_data = overview.loc[:, ["student", *sorted(execution)]].melt(
                id_vars="student", var_name="outcome", value_name="runs"
            )
            st.altair_chart(
                alt.Chart(execution_data, title="Execution outcomes")
                .mark_bar()
                .encode(
                    x=alt.X("runs:Q", title="Cell runs"),
                    y=alt.Y("student:N", title="Participant"),
                    color=alt.Color("outcome:N", title="Outcome"),
                    tooltip=["student:N", "outcome:N", "runs:Q"],
                )
                .interactive(),
                alt="Successful and failed cell runs by participant",
            )

    clipboard = reports.get("clipboard_events")
    if clipboard is not None and {"student", "event", "character_length"}.issubset(clipboard.columns):
        data = clipboard.copy()
        data["character_length"] = pd.to_numeric(data["character_length"], errors="coerce")
        data = data.dropna(subset=["character_length"])
        if not data.empty:
            st.subheader("Clipboard length distribution")
            st.altair_chart(
                alt.Chart(data, title="Copy, cut, and paste lengths")
                .mark_boxplot(size=24)
                .encode(
                    x=alt.X("character_length:Q", title="Characters"),
                    y=alt.Y("event:N", title="Event"),
                    color=alt.Color("event:N", legend=None),
                    row=alt.Row("student:N", title="Participant"),
                    tooltip=["student:N", "event:N", "character_length:Q"],
                ),
                alt="Clipboard event lengths by participant and event type",
            )
        else:
            st.info("Clipboard events are available, but none contain a usable character length.")


def render_event_tables(st: Any, reports: dict[str, pd.DataFrame]) -> None:
    available = [
        name
        for name in ("active_sessions", "idle_events", "cell_execution_summary", "clipboard_events", "cell_mapping")
        if name in reports
    ]
    if not available:
        return
    st.subheader("Event reports")
    selected = st.selectbox("Report", available, key="event_report")
    st.dataframe(reports[selected], hide_index=True, alt=f"{selected.replace('_', ' ')} report")


def render_chart_builder(st: Any, reports: dict[str, pd.DataFrame]) -> None:
    st.header("Build a custom chart")
    dataset_name = st.selectbox("Dataset", sorted(reports), key="chart_dataset")
    source = reports[dataset_name]
    if source.empty:
        st.info("This dataset has no rows to chart.")
        return

    groups = sorted(source["group"].dropna().astype(str).unique()) if "group" in source.columns else []
    students = sorted(source["student"].dropna().astype(str).unique()) if "student" in source.columns else []
    with st.expander("Filters", expanded=False):
        chosen_groups = st.multiselect("Groups", groups, default=groups, disabled=not groups)
        chosen_students = st.multiselect("Participants", students, default=students, disabled=not students)
    data = filtered_frame(source, chosen_groups, chosen_students)
    if data.empty:
        st.warning("The selected filters leave no rows to chart.")
        return

    numeric = numeric_columns(data)
    labels = list(data.columns)
    chart_type = st.selectbox(
        "Chart type", ["Bar", "Grouped bar", "Scatter", "Histogram", "Box plot", "Line"], key="chart_type"
    )
    x_default = "student" if "student" in labels else labels[0]
    x = st.selectbox("X-axis", labels, index=labels.index(x_default), key="chart_x")
    y_options = ["Count rows", *numeric]
    y_choice = st.selectbox("Value", y_options, key="chart_y")
    group_options = ["None", *categorical_columns(data)]
    default_group = "group" if chart_type == "Grouped bar" and "group" in group_options else "None"
    group = st.selectbox(
        "Colour/group", group_options, index=group_options.index(default_group), key="chart_group"
    )
    aggregation_options = ["Count", "Sum", "Mean", "Median"] if y_choice != "Count rows" else ["Count"]
    aggregation = st.selectbox("Aggregation", aggregation_options, key="chart_aggregation")

    y = None if y_choice == "Count rows" else y_choice
    if chart_type in {"Scatter", "Histogram", "Box plot"}:
        aggregation = "None"
    try:
        plotted = aggregate_for_chart(data, x, y, None if group == "None" else group, aggregation)
    except DashboardInputError as error:
        st.warning(str(error))
        return
    value_column = "count" if y is None or aggregation == "Count" else y
    color = None if group == "None" or group == x else alt.Color(f"{group}:N", title=group)
    color_encoding = {"color": color} if color is not None else {}
    title = f"{value_column} by {x}"

    if chart_type in {"Bar", "Grouped bar"}:
        chart = alt.Chart(plotted, title=title).mark_bar().encode(
            x=alt.X(f"{x}:N", title=x), y=alt.Y(f"{value_column}:Q", title=value_column),
            tooltip=list(plotted.columns),
            **color_encoding,
        )
    elif chart_type == "Scatter":
        if y is None:
            st.warning("Scatter plots need a numeric value column.")
            return
        chart = alt.Chart(plotted, title=title).mark_circle(size=70).encode(
            x=alt.X(f"{x}:Q" if x in numeric else f"{x}:N", title=x),
            y=alt.Y(f"{y}:Q", title=y), tooltip=list(plotted.columns), **color_encoding,
        )
    elif chart_type == "Histogram":
        if y is None:
            st.warning("Histograms need a numeric value column.")
            return
        chart = alt.Chart(plotted, title=title).mark_bar().encode(
            x=alt.X(f"{y}:Q", bin=True, title=y), y=alt.Y("count():Q", title="Rows"),
            **color_encoding,
        )
    elif chart_type == "Box plot":
        if y is None:
            st.warning("Box plots need a numeric value column.")
            return
        chart = alt.Chart(plotted, title=title).mark_boxplot().encode(
            x=alt.X(f"{y}:Q", title=y), y=alt.Y(f"{x}:N", title=x),
            tooltip=list(plotted.columns),
            **color_encoding,
        )
    else:
        date_columns = timestamp_columns(plotted)
        x_type = "T" if x in date_columns else "N"
        chart = alt.Chart(plotted, title=title).mark_line(point=True).encode(
            x=alt.X(f"{x}:{x_type}", title=x), y=alt.Y(f"{value_column}:Q", title=value_column),
            tooltip=list(plotted.columns), **color_encoding,
        )
    st.altair_chart(chart.interactive(), alt=f"Custom {chart_type.lower()} chart: {title}")
    st.dataframe(plotted, hide_index=True, alt="Data currently shown in the custom chart")


def render_cell_selection(st: Any, original_reports: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Let researchers hide whole cell-level metric groups from this session's views."""
    available = available_cell_labels(original_reports)
    if not available:
        return original_reports

    selected = [
        cell for cell in st.session_state["analytics_selected_cells"] if cell in available
    ]
    if selected != st.session_state["analytics_selected_cells"]:
        st.session_state["analytics_selected_cells"] = selected

    with st.container(border=True):
        st.subheader("Cell-level analytics")
        st.caption(
            "Choose the assignment cells to show. This hides related cell metrics and "
            "event rows only in this website session; it does not alter raw logs or downloads."
        )
        all_cells, no_cells = st.columns(2)
        if all_cells.button("Include all cells", key="include_all_cells"):
            st.session_state["analytics_selected_cells"] = available
            st.rerun()
        if no_cells.button("Exclude all cells", key="exclude_all_cells"):
            st.session_state["analytics_selected_cells"] = []
            st.rerun()
        st.multiselect(
            "Included cells",
            available,
            key="analytics_selected_cells",
            help="For example, remove course_header and participant_id while retaining task_1_code.",
        )
    return filter_reports_to_cells(original_reports, st.session_state["analytics_selected_cells"])


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Notebook analytics workspace", page_icon=":material/analytics:", layout="wide")
    initialise_state(st)
    st.title("Notebook analytics workspace")
    st.caption("Process local study logs and explore results without running analysis commands.")

    with st.form("upload_data", border=True):
        uploads = st.file_uploader(
            "Raw log ZIP or analytics CSV reports",
            type=["zip", "csv"],
            accept_multiple_files=True,
            help="Upload one ZIP containing changes/ and optional companion folders, or one or more CSV reports.",
        )
        mapping = st.file_uploader(
            "Optional participant group mapping CSV",
            type=["csv"],
            accept_multiple_files=False,
            help="Columns: participant_id,group. For example: aaa101,non_ai.",
        )
        submitted = st.form_submit_button("Generate analytics", type="primary", icon=":material/analytics:")

    if submitted:
        if not uploads:
            st.error("Upload a raw-log ZIP or one or more analytics CSV files first.")
        else:
            zip_uploads = [uploaded for uploaded in uploads if uploaded.name.lower().endswith(".zip")]
            csv_uploads = [uploaded for uploaded in uploads if uploaded.name.lower().endswith(".csv")]
            if zip_uploads and (len(zip_uploads) != 1 or csv_uploads):
                st.error("Upload one raw-log ZIP by itself, or upload one or more CSV files without a ZIP.")
            else:
                try:
                    with tempfile.TemporaryDirectory(prefix="notebook_analytics_") as temporary:
                        workspace = Path(temporary)
                        result = (
                            run_raw_log_analytics(zip_uploads[0].getvalue(), workspace)
                            if zip_uploads
                            else result_from_csv_uploads(
                                {uploaded.name: uploaded.getvalue() for uploaded in csv_uploads}, workspace
                            )
                        )
                        result, mapping_warnings = apply_mapping(
                            result, mapping.getvalue() if mapping else None
                        )
                        st.session_state["analytics_result"] = result
                        st.session_state["analytics_group_warnings"] = mapping_warnings
                        st.session_state["analytics_download"] = build_download_zip(result)
                        st.session_state["analytics_selected_cells"] = available_cell_labels(result.reports)
                except DashboardInputError as error:
                    st.error(str(error))
                except Exception as error:
                    st.exception(error)

    result: ProcessingResult | None = st.session_state["analytics_result"]
    if result is None:
        st.info("Your uploads are processed locally in this browser session and discarded when the session ends.")
        return

    for warning in [*result.warnings, *st.session_state["analytics_group_warnings"]]:
        st.warning(warning)

    reports = render_cell_selection(st, result.reports)
    overview = reports.get("student_analytics")
    with st.container(horizontal=True):
        st.metric("Reports", len(reports), border=True)
        st.metric("Participants", overview["student"].nunique() if overview is not None and "student" in overview else "Not available", border=True)
        st.metric("Generated plots", len(result.plots), border=True)

    tabs = st.tabs(["Overview", "Preset views", "Event reports", "Custom chart", "Download"])
    with tabs[0]:
        st.subheader("Loaded datasets")
        summary = pd.DataFrame(
            [{"dataset": name, "rows": len(frame), "columns": len(frame.columns)} for name, frame in reports.items()]
        )
        st.dataframe(summary, hide_index=True, alt="Loaded analytics datasets")
        if overview is not None:
            st.dataframe(overview, hide_index=True, alt="Overall participant analytics")
    with tabs[1]:
        render_preset_views(st, reports)
    with tabs[2]:
        render_event_tables(st, reports)
    with tabs[3]:
        render_chart_builder(st, reports)
    with tabs[4]:
        st.download_button(
            "Download analytics package",
            data=st.session_state["analytics_download"],
            file_name="notebook_analytics_results.zip",
            mime="application/zip",
            icon=":material/download:",
        )
        st.caption("The download contains generated reports, plots, and a manifest. It does not include uploaded raw logs.")


if __name__ == "__main__":
    main()

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
    chart_defaults,
    chart_png,
    data_quality_findings,
    filtered_frame,
    filter_reports_to_cells,
    grouped_summary,
    numeric_columns,
    result_from_csv_uploads,
    run_raw_log_analytics,
    search_rows,
    timestamp_columns,
    validate_group_mapping,
)


CHART_TYPES = ["Bar", "Grouped bar", "Scatter", "Histogram", "Box plot", "Line"]
CHART_GUIDANCE = {
    "Bar": "Compare one value across categories, such as estimated working time by participant.",
    "Grouped bar": "Compare the same value across categories and groups, such as AI and non-AI participants.",
    "Scatter": "Explore the relationship between two numeric measures. It needs two numeric columns.",
    "Histogram": "Show the distribution of one numeric measure, such as idle-time duration.",
    "Box plot": "Compare numeric distributions across categories. It needs one category and one numeric measure.",
    "Line": "Show a value over time or another ordered category. A timestamp column gives the clearest result.",
}


def initialise_state(st: Any) -> None:
    st.session_state.setdefault("analytics_result", None)
    st.session_state.setdefault("analytics_group_warnings", [])
    st.session_state.setdefault("analytics_download", None)
    st.session_state.setdefault("analytics_selected_cells", [])


def clear_analysis_session(st: Any) -> None:
    """Forget in-memory uploads and derived views without touching local files."""
    st.session_state["analytics_result"] = None
    st.session_state["analytics_group_warnings"] = []
    st.session_state["analytics_download"] = None
    st.session_state["analytics_selected_cells"] = []
    for key in list(st.session_state):
        if key.startswith(("chart_", "event_", "cohort_summary_", "analytics_workspace_")):
            del st.session_state[key]


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


def render_chart_downloads(
    st: Any, chart: alt.Chart, data: pd.DataFrame, filename: str, key_prefix: str
) -> None:
    """Offer the reproducible data and specification behind an Altair preview."""
    with st.container(horizontal=True, wrap=True):
        st.download_button(
            "Download chart data",
            data.to_csv(index=False).encode("utf-8"),
            f"{filename}_data.csv",
            "text/csv",
            key=f"{key_prefix}_data",
            icon=":material/table_view:",
            on_click="ignore",
        )
        st.download_button(
            "Download chart specification",
            chart.to_json().encode("utf-8"),
            f"{filename}.vl.json",
            "application/json",
            key=f"{key_prefix}_specification",
            icon=":material/code:",
            on_click="ignore",
        )


def render_preset_views(st: Any, reports: dict[str, pd.DataFrame]) -> None:
    overview = reports.get("student_analytics")
    if overview is not None:
        st.subheader("Core study measures", icon=":material/insights:")
        st.caption("These established plots are based on the included cells and participants selected above.")
        preset_columns = [
            ("total_working_duration", "Estimated working time", "Seconds"),
            ("total_idle_time", "Total idle time", "Seconds"),
        ]
        for column, title, unit in preset_columns:
            if column in overview.columns:
                chart = standard_bar_chart(overview, column, title, unit)
                data = overview.loc[:, ["student", column] + (["group"] if "group" in overview.columns else [])]
                with st.container(border=True):
                    st.altair_chart(chart, alt=f"{title} by participant")
                    render_chart_downloads(st, chart, data, safe_filename(title), f"preset_{column}")

        execution = {"execution_status_success", "execution_status_error"}
        if execution.issubset(overview.columns):
            execution_data = overview.loc[:, ["student", *sorted(execution)]].melt(
                id_vars="student", var_name="outcome", value_name="runs"
            )
            chart = (
                alt.Chart(execution_data, title="Execution outcomes")
                .mark_bar()
                .encode(
                    x=alt.X("runs:Q", title="Cell runs"),
                    y=alt.Y("student:N", title="Participant"),
                    color=alt.Color("outcome:N", title="Outcome"),
                    tooltip=["student:N", "outcome:N", "runs:Q"],
                )
                .interactive()
            )
            with st.container(border=True):
                st.altair_chart(chart, alt="Successful and failed cell runs by participant")
                render_chart_downloads(st, chart, execution_data, "execution_outcomes", "preset_executions")

    clipboard = reports.get("clipboard_events")
    if clipboard is not None and {"student", "event", "character_length"}.issubset(clipboard.columns):
        data = clipboard.copy()
        data["character_length"] = pd.to_numeric(data["character_length"], errors="coerce")
        data = data.dropna(subset=["character_length"])
        if not data.empty:
            st.subheader("Clipboard length distribution", icon=":material/content_paste:")
            chart = (
                alt.Chart(data, title="Copy, cut, and paste lengths")
                .mark_boxplot(size=24)
                .encode(
                    x=alt.X("character_length:Q", title="Characters"),
                    y=alt.Y("event:N", title="Event"),
                    color=alt.Color("event:N", legend=None),
                    row=alt.Row("student:N", title="Participant"),
                    tooltip=["student:N", "event:N", "character_length:Q"],
                )
            )
            with st.container(border=True):
                st.altair_chart(chart, alt="Clipboard event lengths by participant and event type")
                render_chart_downloads(st, chart, data, "clipboard_length_distribution", "preset_clipboard")
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
    st.subheader("Event reports", icon=":material/table_chart:")
    st.caption("Search, select visible columns, and download exactly the event rows currently displayed.")
    selected = st.selectbox("Report", available, key="event_report")
    report = reports[selected]
    search = st.text_input(
        "Search rows", key=f"event_search_{selected}", placeholder="Type a participant, cell label, or event"
    )
    visible_columns = st.multiselect(
        "Visible columns", list(report.columns), default=list(report.columns), key=f"event_visible_columns_{selected}"
    )
    visible = report.loc[:, visible_columns] if visible_columns else pd.DataFrame(index=report.index)
    visible = search_rows(visible, search)
    st.caption(f"Showing {len(visible):,} of {len(report):,} rows.")
    st.dataframe(visible, hide_index=True, alt=f"Filtered {selected.replace('_', ' ')} report")
    st.download_button(
        "Download displayed table",
        visible.to_csv(index=False).encode("utf-8"),
        f"{selected}_displayed_rows.csv",
        "text/csv",
        key=f"download_event_table_{selected}",
        icon=":material/download:",
        on_click="ignore",
    )


def render_data_quality(st: Any, reports: dict[str, pd.DataFrame]) -> None:
    """Render integrity feedback without changing any uploaded or derived data."""
    st.subheader("Data-quality check", icon=":material/fact_check:")
    findings = data_quality_findings(reports)
    if findings.empty:
        st.success("No structural data-quality issues were detected in the loaded reports.")
        st.caption("This confirms report structure only; it does not validate research interpretation.")
        return
    for finding in findings.itertuples(index=False):
        message = f"**{finding.dataset}:** {finding.message}"
        if finding.severity == "warning":
            st.warning(message)
        else:
            st.info(message)
    st.dataframe(findings, hide_index=True, alt="Data-quality findings")


def render_cohort_summary(st: Any, overview: pd.DataFrame | None) -> None:
    if overview is None or overview.empty:
        return
    measures = numeric_columns(overview)
    if not measures:
        return
    st.subheader("Cohort summary", icon=":material/groups:")
    default = "total_working_duration" if "total_working_duration" in measures else measures[0]
    measure = select_with_default(
        st,
        "Measure for cohort summary",
        measures,
        default,
        "cohort_summary_measure",
        format_func=lambda item: item.replace("_", " ").title(),
    )
    try:
        summary = grouped_summary(overview, measure, "group" if "group" in overview.columns else None)
    except DashboardInputError as error:
        st.info(str(error))
        return
    st.dataframe(summary, hide_index=True, alt=f"Cohort summary for {measure}")
    st.download_button(
        "Download cohort summary",
        summary.to_csv(index=False).encode("utf-8"),
        f"{measure}_cohort_summary.csv",
        "text/csv",
        key="download_cohort_summary",
        icon=":material/download:",
        on_click="ignore",
    )


def render_downloads(st: Any, result: ProcessingResult) -> None:
    st.subheader("Take your results with you", icon=":material/download:")
    st.caption("The full package contains generated reports, plots, and a manifest, but never your raw uploads.")
    st.download_button(
        "Download analytics package",
        data=st.session_state["analytics_download"],
        file_name="notebook_analytics_results.zip",
        mime="application/zip",
        icon=":material/folder_zip:",
        type="primary",
        on_click="ignore",
    )
    if not result.plots:
        return
    st.subheader("Generated standard plots", icon=":material/image:")
    st.caption("Download individual established plots when you do not need the full result package.")
    for name, content in sorted(result.plots.items()):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.code(name, language=None)
            st.download_button(
                "Download PNG",
                content,
                name,
                "image/png",
                key=f"download_standard_plot_{name}",
                icon=":material/image:",
                on_click="ignore",
            )


def reset_chart_controls(st: Any) -> None:
    """Allow a new chart type or dataset to receive appropriate defaults."""
    for key in ("chart_x", "chart_y", "chart_group", "chart_aggregation"):
        st.session_state.pop(key, None)


def select_with_default(
    st: Any, label: str, options: list[str], default: str, key: str, **kwargs: Any
) -> str:
    """Render a selectbox with a safe, data-aware initial choice."""
    if st.session_state.get(key) not in options:
        st.session_state[key] = default if default in options else options[0]
    return st.selectbox(label, options, key=key, **kwargs)


def safe_filename(value: str) -> str:
    """Return a readable, filesystem-safe stem for a chart download."""
    clean = "".join(character.lower() if character.isalnum() else " " for character in value)
    return "_".join(clean.split()) or "custom_chart"


def render_chart_builder(st: Any, reports: dict[str, pd.DataFrame]) -> None:
    st.header("Build a custom chart", icon=":material/bar_chart:")
    st.caption(
        "Choose a chart type to see a useful preview from your data. Every setting updates the preview immediately; "
        "download the finished chart, its data, or its Vega-Lite specification below."
    )
    if st.session_state.get("chart_dataset") not in reports:
        st.session_state["chart_dataset"] = sorted(reports)[0]
    dataset_name = st.selectbox(
        "Dataset", sorted(reports), key="chart_dataset", on_change=reset_chart_controls, args=(st,)
    )
    source = reports[dataset_name]
    if source.empty:
        st.info("This dataset has no rows to chart.")
        return

    groups = sorted(source["group"].dropna().astype(str).unique()) if "group" in source.columns else []
    students = sorted(source["student"].dropna().astype(str).unique()) if "student" in source.columns else []
    with st.expander("Filters", expanded=False, icon=":material/filter_list:"):
        chosen_groups = st.multiselect("Groups", groups, default=groups, disabled=not groups)
        chosen_students = st.multiselect("Participants", students, default=students, disabled=not students)
    data = filtered_frame(source, chosen_groups, chosen_students)
    if data.empty:
        st.warning("The selected filters leave no rows to chart.")
        return

    numeric = numeric_columns(data)
    labels = list(data.columns)
    if not numeric:
        st.warning("This dataset has no numeric columns, so it cannot produce a chart yet.")
        return
    if st.session_state.get("chart_type") not in CHART_TYPES:
        st.session_state["chart_type"] = "Bar"
    chart_type = st.segmented_control(
        "Chart type",
        CHART_TYPES,
        key="chart_type",
        required=True,
        width="stretch",
        wrap=True,
        on_change=reset_chart_controls,
        args=(st,),
    ) or "Bar"
    if chart_type == "Scatter" and len(numeric) < 2:
        st.warning("Scatter plots need two numeric columns. Choose a different chart type or dataset.")
        return
    defaults = chart_defaults(data, chart_type)

    with st.container(border=True):
        st.caption(f":material/info: {CHART_GUIDANCE[chart_type]}")
        x_options = numeric if chart_type == "Scatter" else labels
        x = select_with_default(st, "X-axis", x_options, str(defaults["x"]), "chart_x")
        requires_numeric_value = chart_type in {"Scatter", "Histogram", "Box plot"}
        y_options = numeric if requires_numeric_value else ["Count rows", *numeric]
        y_default = defaults["y"] if defaults["y"] in y_options else y_options[0]
        y_choice = select_with_default(st, "Value", y_options, str(y_default), "chart_y")
        y = None if y_choice == "Count rows" else y_choice
        group_options = ["None", *[column for column in categorical_columns(data) if column != x]]
        group_default = defaults["group"] if defaults["group"] in group_options else "None"
        group_choice = select_with_default(st, "Colour/group", group_options, str(group_default), "chart_group")
        group = None if group_choice == "None" else group_choice
        if chart_type in {"Scatter", "Histogram", "Box plot"}:
            aggregation = "None"
            st.caption("This chart uses individual rows, so no aggregation is applied.")
        else:
            aggregation_options = ["Count", "Sum", "Mean", "Median"] if y is not None else ["Count"]
            aggregation = select_with_default(
                st, "Aggregation", aggregation_options, str(defaults["aggregation"]), "chart_aggregation"
            )

        with st.expander(
            "Advanced chart settings",
            expanded=False,
            icon=":material/tune:",
            type="compact",
        ):
            st.caption("Optional presentation settings. Leave these at their defaults for a quick chart.")
            default_title = f"{y_choice if y is not None else 'Count'} by {x}"
            title = st.text_input("Chart title", value=default_title, key="chart_title")
            caption = st.text_input(
                "Caption", value="", key="chart_caption", placeholder="Optional note shown below the chart"
            )
            x_label = st.text_input("X-axis label", value=x.replace("_", " ").title(), key="chart_x_label")
            default_y_label = "Rows" if chart_type == "Histogram" else (y_choice if y is not None else "Count rows")
            y_label = st.text_input("Y-axis label", value=default_y_label.replace("_", " ").title(), key="chart_y_label")
            colour = st.color_picker("Single-series colour", "#1F77B4", key="chart_colour")
            palette = "tableau10"
            if group:
                palette = st.selectbox(
                    "Grouped colour palette", ["tableau10", "set2", "category10"], key="chart_palette",
                    help="Used when a grouping field is selected; the single-series colour is used otherwise.",
                )

    try:
        plotted = aggregate_for_chart(data, x, y, group, aggregation)
    except DashboardInputError as error:
        st.warning(str(error))
        return
    value_column = "count" if y is None or aggregation == "Count" else y
    color_encoding: dict[str, Any]
    if group and group != x:
        color_encoding = {"color": alt.Color(f"{group}:N", title=group, scale=alt.Scale(scheme=palette))}
    else:
        color_encoding = {"color": alt.value(colour)}

    if chart_type in {"Bar", "Grouped bar"}:
        chart = alt.Chart(plotted, title=title).mark_bar().encode(
            x=alt.X(f"{x}:N", title=x_label), y=alt.Y(f"{value_column}:Q", title=y_label),
            tooltip=list(plotted.columns), **color_encoding,
        )
    elif chart_type == "Scatter" and y is not None:
        chart = alt.Chart(plotted, title=title).mark_circle(size=70).encode(
            x=alt.X(f"{x}:Q", title=x_label), y=alt.Y(f"{y}:Q", title=y_label),
            tooltip=list(plotted.columns), **color_encoding,
        )
    elif chart_type == "Histogram" and y is not None:
        chart = alt.Chart(plotted, title=title).mark_bar().encode(
            x=alt.X(f"{y}:Q", bin=True, title=x_label), y=alt.Y("count():Q", title=y_label), **color_encoding,
        )
    elif chart_type == "Box plot" and y is not None:
        chart = alt.Chart(plotted, title=title).mark_boxplot().encode(
            x=alt.X(f"{y}:Q", title=x_label), y=alt.Y(f"{x}:N", title=y_label),
            tooltip=list(plotted.columns), **color_encoding,
        )
    else:
        date_columns = timestamp_columns(plotted)
        x_type = "T" if x in date_columns else "N"
        chart = alt.Chart(plotted, title=title).mark_line(point=True).encode(
            x=alt.X(f"{x}:{x_type}", title=x_label), y=alt.Y(f"{value_column}:Q", title=y_label),
            tooltip=list(plotted.columns), **color_encoding,
        )

    with st.container(border=True):
        st.subheader("Preview", icon=":material/visibility:")
        st.altair_chart(chart.interactive(), alt=f"Custom {chart_type.lower()} chart: {title}")
        if caption:
            st.caption(caption)
        st.dataframe(plotted, hide_index=True, alt="Data currently shown in the custom chart")
        filename = safe_filename(title)
        try:
            png = chart_png(
                plotted, chart_type, x, value_column, group, title, x_label, y_label, colour, palette
            )
        except Exception as error:
            st.warning(f"The interactive preview is available, but its PNG could not be created: {error}")
            png = None
        with st.container(horizontal=True, wrap=True):
            if png is not None:
                st.download_button("Download PNG", png, f"{filename}.png", "image/png", icon=":material/image:", on_click="ignore")
            st.download_button("Download chart data", plotted.to_csv(index=False).encode("utf-8"), f"{filename}_data.csv", "text/csv", icon=":material/table_view:", on_click="ignore")
            st.download_button("Download chart specification", chart.to_json().encode("utf-8"), f"{filename}.vl.json", "application/json", icon=":material/code:", on_click="ignore")


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
    st.title("Notebook analytics workspace", icon=":material/analytics:")
    st.caption("Process local study logs, check their quality, and explore results without running analysis commands.")

    with st.expander("How to use this workspace", expanded=False, icon=":material/help:"):
        st.markdown(
            "1. Upload **one raw-log ZIP** or one or more analytics CSV files.  \n"
            "2. Optionally add a `participant_id,group` mapping to compare conditions.  \n"
            "3. Review the data-quality check before interpreting results.  \n"
            "4. Use preset views for established measures or the custom chart builder for other compatible fields."
        )
        st.caption("Uploads are processed only in this browser session and are discarded when the session ends.")

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
        st.info("Upload data above, then select Generate analytics once. No analysis runs while you are choosing files.")
        return

    if st.button("Start a new analysis", icon=":material/restart_alt:", help="Clears this browser session only."):
        clear_analysis_session(st)
        st.rerun()

    for warning in [*result.warnings, *st.session_state["analytics_group_warnings"]]:
        st.warning(warning)

    reports = render_cell_selection(st, result.reports)
    overview = reports.get("student_analytics")
    with st.container(horizontal=True):
        st.metric("Reports", len(reports), border=True)
        st.metric(
            "Participants",
            overview["student"].nunique() if overview is not None and "student" in overview else "Not available",
            border=True,
        )
        st.metric("Generated plots", len(result.plots), border=True)

    view = st.segmented_control(
        "Workspace view",
        ["Overview", "Data quality", "Preset views", "Event reports", "Custom chart", "Download"],
        default="Overview",
        required=True,
        key="analytics_workspace_view",
        width="stretch",
        wrap=True,
    )
    if view == "Overview":
        st.subheader("Loaded datasets", icon=":material/dataset:")
        summary = pd.DataFrame(
            [{"dataset": name, "rows": len(frame), "columns": len(frame.columns)} for name, frame in reports.items()]
        )
        st.dataframe(summary, hide_index=True, alt="Loaded analytics datasets")
        if overview is not None:
            with st.container(border=True):
                st.subheader("Overall participant analytics")
                st.dataframe(overview, hide_index=True, alt="Overall participant analytics")
                st.download_button(
                    "Download overall analytics",
                    overview.to_csv(index=False).encode("utf-8"),
                    "student_analytics_displayed.csv",
                    "text/csv",
                    icon=":material/download:",
                    key="download_overview",
                    on_click="ignore",
                )
            render_cohort_summary(st, overview)
    elif view == "Data quality":
        render_data_quality(st, reports)
    elif view == "Preset views":
        render_preset_views(st, reports)
    elif view == "Event reports":
        render_event_tables(st, reports)
    elif view == "Custom chart":
        render_chart_builder(st, reports)
    elif view == "Download":
        render_downloads(st, result)


if __name__ == "__main__":
    main()

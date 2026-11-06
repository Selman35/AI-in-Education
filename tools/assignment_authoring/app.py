"""Local Streamlit review app for draft notebook assignment configurations.

Run with:
    streamlit run tools/assignment_authoring/app.py

This app is intentionally local and export-only. Uploaded notebooks are read
in memory to create a JSON configuration for teacher review; they are never
modified or sent to an external service.
"""

from __future__ import annotations

import io
import json
import copy
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from prepare_assignment import (
    build_manifest,
    prepare_assignment,
    validate_notebook_matches_config,
)
from scan_notebook import (
    VALID_ROLES,
    build_draft_config,
    build_reviewed_config,
    cell_validation_errors,
    policy_for_role,
    review_summary,
    validate_reviewed_config,
)


ROLE_LABELS = {
    "locked_text": "Locked text or instruction",
    "starter_code": "Locked, runnable starter code",
    "answer_code": "Editable code answer",
    "answer_markdown": "Editable Markdown answer",
    "unrestricted": "Unrestricted cell",
}


def widget_prefix(draft_cell: dict[str, Any]) -> str:
    """Return stable Streamlit widget keys for one source-notebook cell."""
    return f"cell_{draft_cell['position']}"


def initialise_review_state(
    draft: dict[str, Any], uploaded_notebook: dict[str, Any], st: Any
) -> None:
    """Initialise or reset review widgets when the uploaded notebook changes."""
    upload_identity = (
        f"{draft['source_notebook']}:{draft.get('source_notebook_sha256', '')}"
    )
    if st.session_state.get("assignment_authoring_upload") == upload_identity:
        return

    for draft_cell in draft["cells"]:
        prefix = widget_prefix(draft_cell)
        st.session_state[f"{prefix}_role"] = draft_cell["proposed_role"]
        st.session_state[f"{prefix}_key"] = draft_cell["proposed_key"]
        st.session_state[f"{prefix}_section"] = draft_cell["proposed_section"]
        st.session_state[f"{prefix}_show_advanced_permissions"] = False
        default_policy = draft_cell["proposed_policy"]
        for permission, value in default_policy.items():
            st.session_state[f"{prefix}_{permission}"] = value
        st.session_state[f"{prefix}_source"] = draft_cell["source"]
    st.session_state["assignment_authoring_upload"] = upload_identity
    st.session_state["assignment_authoring_uploaded_notebook"] = copy.deepcopy(
        uploaded_notebook
    )
    st.session_state["assignment_authoring_editable_notebook"] = copy.deepcopy(
        uploaded_notebook
    )
    st.session_state["assignment_authoring_selected_position"] = (
        draft["cells"][0]["position"] if draft["cells"] else None
    )


def editable_notebook(st: Any) -> dict[str, Any]:
    """Return the session-only notebook copy used for preview and export."""
    return st.session_state["assignment_authoring_editable_notebook"]


def update_cell_source(
    notebook: dict[str, Any], position: int, source: str
) -> None:
    """Update one cell in the editable copy without changing its identity."""
    notebook["cells"][position - 1]["source"] = source


def suggested_cell_for_position(
    notebook: dict[str, Any], source_notebook: str, position: int
) -> dict[str, Any]:
    """Return fresh deterministic suggestions for one editable cell only."""
    draft = build_draft_config(notebook, source_notebook)
    return draft["cells"][position - 1]


def refresh_selected_suggestion(
    notebook: dict[str, Any], source_notebook: str, position: int, st: Any
) -> None:
    """Apply a fresh suggestion to one cell, leaving all other reviews intact."""
    suggestion = suggested_cell_for_position(notebook, source_notebook, position)
    prefix = widget_prefix(suggestion)
    st.session_state[f"{prefix}_role"] = suggestion["proposed_role"]
    st.session_state[f"{prefix}_key"] = suggestion["proposed_key"]
    st.session_state[f"{prefix}_section"] = suggestion["proposed_section"]
    st.session_state[f"{prefix}_show_advanced_permissions"] = False
    for permission, value in suggestion["proposed_policy"].items():
        st.session_state[f"{prefix}_{permission}"] = value


def reviewed_cell_from_state(draft_cell: dict[str, Any], st: Any) -> dict[str, Any]:
    """Read one cell's current review decision, including optional overrides."""
    prefix = widget_prefix(draft_cell)
    role = st.session_state[f"{prefix}_role"]
    default_policy = policy_for_role(role)
    permissions = default_policy
    if st.session_state[f"{prefix}_show_advanced_permissions"]:
        permissions = {
            permission: st.session_state[f"{prefix}_{permission}"]
            for permission in default_policy
        }
    return {
        "source_cell_id": draft_cell["source_cell_id"],
        "position": draft_cell["position"],
        "cell_type": draft_cell["cell_type"],
        "analytics_key": st.session_state[f"{prefix}_key"].strip(),
        "role": role,
        "section": st.session_state[f"{prefix}_section"].strip(),
        "policy": permissions,
    }


def notebook_from_upload(uploaded_file: Any) -> dict[str, Any]:
    """Read and validate the uploaded notebook without writing it to disk."""
    notebook = json.loads(uploaded_file.getvalue().decode("utf-8"))
    if not isinstance(notebook.get("cells"), list):
        raise ValueError("The selected file is not a valid Jupyter notebook.")
    return notebook


def reviewed_cell_from_widget(draft_cell: dict[str, Any], st: Any) -> dict[str, Any]:
    """Render teacher controls for one cell and return the reviewed selection."""
    position = draft_cell["position"]
    prefix = widget_prefix(draft_cell)
    default_role = draft_cell["proposed_role"]
    role = st.selectbox(
        "Role",
        options=sorted(VALID_ROLES),
        index=sorted(VALID_ROLES).index(default_role),
        format_func=lambda value: ROLE_LABELS[value],
        key=f"{prefix}_role",
    )
    default_policy = policy_for_role(role)
    analytics_key = st.text_input(
        "Stable analytics key",
        value=draft_cell["proposed_key"],
        key=f"{prefix}_key",
        help="Use lowercase letters, numbers, and underscores. This key is used in logs and analytics.",
    )
    section = st.text_input(
        "Section or task key",
        value=draft_cell["proposed_section"],
        key=f"{prefix}_section",
    )
    permissions = default_policy
    if st.checkbox(
        "Show advanced permissions",
        value=False,
        key=f"{prefix}_show_advanced_permissions",
        help="Roles already provide the usual permissions. Change these only for an exception.",
    ):
        st.caption(
            "These settings override the permissions normally supplied by the selected role."
        )
        permissions = {
            "source_editable": st.checkbox(
                "Students may edit source",
                value=default_policy["source_editable"],
                key=f"{prefix}_source_editable",
            ),
            "executable": st.checkbox(
                "Students may run this cell",
                value=default_policy["executable"],
                key=f"{prefix}_executable",
            ),
            "deletable": st.checkbox(
                "Students may delete this original cell",
                value=default_policy["deletable"],
                key=f"{prefix}_deletable",
            ),
            "movable": st.checkbox(
                "Students may move this original cell",
                value=default_policy["movable"],
                key=f"{prefix}_movable",
            ),
        }
    return reviewed_cell_from_state(draft_cell, st)


def review_overview_rows(
    draft_cells: list[dict[str, Any]],
    reviewed_cells: list[dict[str, Any]],
    errors_by_position: dict[int, list[str]],
) -> list[dict[str, str | int]]:
    """Build compact rows for the teacher-facing notebook overview table."""
    rows: list[dict[str, str | int]] = []
    for draft_cell, reviewed_cell in zip(draft_cells, reviewed_cells):
        position = reviewed_cell["position"]
        messages = errors_by_position.get(position, [])
        rows.append(
            {
                "Cell": position,
                "Type": reviewed_cell["cell_type"],
                "Section": reviewed_cell["section"] or "—",
                "Role": ROLE_LABELS.get(reviewed_cell["role"], reviewed_cell["role"]),
                "Stable key": reviewed_cell["analytics_key"] or "—",
                "Preview": draft_cell["source_preview"] or "(empty cell)",
                "Status": "Needs attention" if messages else "Ready",
            }
        )
    return rows


def apply_bulk_changes(
    draft_cells: list[dict[str, Any]],
    positions: list[int],
    role: str | None,
    section: str,
    st: Any,
) -> None:
    """Apply an optional common role and section to selected reviewed cells."""
    for draft_cell in draft_cells:
        if draft_cell["position"] not in positions:
            continue
        prefix = widget_prefix(draft_cell)
        if role:
            st.session_state[f"{prefix}_role"] = role
            st.session_state[f"{prefix}_show_advanced_permissions"] = False
        if section.strip():
            st.session_state[f"{prefix}_section"] = section.strip()


def local_test_guide(assignment_id: str) -> str:
    """Return concise instructions bundled with a downloaded assignment package."""
    return f"""# Local assignment-package test

This package contains a prepared notebook and its review records. It does not
include a copy of the JupyterLab extension.

## Run locally

1. Extract this folder inside the existing `AI-in-Education` repository.
2. Activate the Conda environment used for that repository.
3. Rebuild the installed local extension:

   ```bash
   jlpm run build
   ```

4. Start JupyterLab:

   ```bash
   jupyter lab
   ```

5. Open `{assignment_id}.ipynb` from this extracted folder.

The prepared notebook preserves the teacher-reviewed roles, stable cell keys,
sections, and permissions as metadata. After the extension is rebuilt, the
prepared-assignment policy controls protected source, movement, deletion, and
cell execution. Existing logging continues to use the configured logging root.
"""


def build_assignment_package(
    assignment_id: str,
    prepared_notebook_json: str,
    configuration_json: str,
    manifest_json: str,
) -> bytes:
    """Build the single ZIP download for a reviewed assignment."""
    package_root = f"{assignment_id}/"
    package = io.BytesIO()
    with ZipFile(package, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{package_root}{assignment_id}.ipynb", prepared_notebook_json
        )
        archive.writestr(
            f"{package_root}assignment-config.json", configuration_json
        )
        archive.writestr(
            f"{package_root}assignment.manifest.json", manifest_json
        )
        archive.writestr(f"{package_root}LOCAL_TEST.md", local_test_guide(assignment_id))
    return package.getvalue()


def render_notebook_cell(
    draft_cell: dict[str, Any], reviewed_cell: dict[str, Any], selected: bool, errors: list[str], st: Any
) -> None:
    """Render one read-only, notebook-like cell card in the canvas."""
    position = draft_cell["position"]
    status = "Needs attention" if errors else "Ready"
    with st.container(border=True):
        left, right = st.columns([5, 1])
        with left:
            st.caption(
                f"Cell {position} · {reviewed_cell['cell_type']} · "
                f"{ROLE_LABELS[reviewed_cell['role']]} · "
                f"key: {reviewed_cell['analytics_key'] or '—'} · {status}"
            )
        with right:
            if st.button(
                "Selected" if selected else "Select",
                key=f"select_cell_{position}",
                disabled=selected,
                use_container_width=True,
            ):
                st.session_state["assignment_authoring_selected_position"] = position
                st.rerun()

        source = draft_cell["source"]
        if reviewed_cell["cell_type"] == "markdown":
            st.markdown(source or "_Empty Markdown cell_")
        else:
            st.code(source or "# Empty code cell", language="python")
        if errors:
            st.error(" ".join(errors))


def render_selected_cell_inspector(
    draft_cell: dict[str, Any], editable: dict[str, Any], st: Any
) -> None:
    """Render all editable review controls for the currently selected cell."""
    position = draft_cell["position"]
    prefix = widget_prefix(draft_cell)
    st.subheader(f"Cell {position} settings")
    st.caption(
        "Edit the source and assignment settings for this cell. Changes apply "
        "only to the downloadable prepared notebook."
    )

    source = st.text_area(
        "Cell source",
        key=f"{prefix}_source",
        height=220,
        help="Markdown is shown rendered in the notebook preview; code is shown as Python.",
    )
    if source != draft_cell["source"]:
        update_cell_source(editable, position, source)

    if st.button("Refresh suggestion for this cell", key=f"refresh_cell_{position}"):
        refresh_selected_suggestion(editable, st.session_state["assignment_authoring_source_name"], position, st)
        st.rerun()

    st.info(f"Current suggestion: {draft_cell['suggestion_reason']}")
    reviewed_cell_from_widget(draft_cell, st)


def main() -> None:
    try:
        import streamlit as st
    except (ImportError, ModuleNotFoundError) as error:
        raise SystemExit(
            "A compatible Streamlit installation is required for the authoring app. "
            "Install or upgrade the optional requirements first: "
            "pip install --upgrade -r tools/assignment_authoring/requirements.txt"
        ) from error

    st.set_page_config(page_title="Notebook assignment authoring", layout="wide")
    st.title("Notebook assignment authoring")
    st.caption(
        "Review an existing notebook locally and download a prepared student version. The uploaded file is never modified."
    )

    with st.expander("How to use this page", expanded=True):
        st.markdown(
            """
1. Upload an existing Jupyter notebook.
2. Open each cell review and read the complete original cell content.
3. Confirm or change its role, stable analytics key, and section.
4. Download the prepared student notebook once there are no validation errors.

The uploaded notebook stays unchanged. The prepared notebook is downloaded as
a separate file for later use with generic assignment policies.

**Roles set the normal student permissions automatically.** Use **Show advanced
permissions** only when one cell needs behaviour that differs from its role.
            """
        )

    uploaded = st.file_uploader("Upload an existing Jupyter notebook", type=["ipynb"])
    if uploaded is None:
        st.info("Choose a notebook to create a reviewable assignment configuration.")
        return

    try:
        notebook = notebook_from_upload(uploaded)
        draft = build_draft_config(notebook, uploaded.name)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        st.error(str(error))
        return
    initialise_review_state(draft, notebook, st)
    st.session_state["assignment_authoring_source_name"] = uploaded.name
    notebook = editable_notebook(st)
    draft = build_draft_config(notebook, uploaded.name)

    st.subheader("Assignment details")
    st.caption("Names are for teacher reference. Stable IDs are created automatically when you export.")
    details_left, details_right = st.columns(2)
    with details_left:
        course_name = st.text_input("Course name", value=draft["course"]["proposed_name"])
    with details_right:
        assignment_name = st.text_input(
            "Assignment name", value=draft["assignment"]["proposed_name"]
        )
    allow_extra_cells = st.checkbox(
        "Allow students to add extra cells",
        value=draft["structure"]["allow_extra_cells"],
        help="Extra cells receive stable keys automatically when the prepared notebook is opened.",
    )

    reviewed_cells = [reviewed_cell_from_state(cell, st) for cell in draft["cells"]]
    errors_by_position = cell_validation_errors(reviewed_cells)

    st.subheader("Assignment notebook")
    st.caption(
        "Read the notebook as students will see it. Select a cell to edit its "
        "content, role, stable key, section, or exceptional permissions."
    )
    selected_position = st.session_state["assignment_authoring_selected_position"]
    if selected_position not in {cell["position"] for cell in draft["cells"]}:
        selected_position = draft["cells"][0]["position"] if draft["cells"] else None
        st.session_state["assignment_authoring_selected_position"] = selected_position

    notebook_column, inspector_column = st.columns([3, 2], gap="large")
    with notebook_column:
        for draft_cell, reviewed_cell in zip(draft["cells"], reviewed_cells):
            render_notebook_cell(
                draft_cell,
                reviewed_cell,
                draft_cell["position"] == selected_position,
                errors_by_position.get(draft_cell["position"], []),
                st,
            )
    with inspector_column:
        selected_cell = next(
            (cell for cell in draft["cells"] if cell["position"] == selected_position),
            None,
        )
        if selected_cell is not None:
            render_selected_cell_inspector(selected_cell, notebook, st)

    reviewed_cells = [reviewed_cell_from_state(cell, st) for cell in draft["cells"]]

    reviewed = build_reviewed_config(
        draft,
        course_name,
        assignment_name,
        allow_extra_cells,
        reviewed_cells,
    )
    errors = validate_reviewed_config(reviewed)
    errors.extend(validate_notebook_matches_config(notebook, reviewed, uploaded.name))

    errors_by_position = cell_validation_errors(reviewed_cells)
    summary = review_summary(reviewed_cells)
    st.subheader("Review summary")
    total_column, code_column, markdown_column, section_column = st.columns(4)
    total_column.metric("Cells", summary["total_cells"])
    code_column.metric("Code cells", summary["code_cells"])
    markdown_column.metric("Markdown cells", summary["markdown_cells"])
    section_column.metric("Sections", summary["section_count"])
    role_summary = ", ".join(
        f"{ROLE_LABELS.get(role, role)}: {count}"
        for role, count in summary["roles"].items()
    )
    st.caption(f"Selected roles — {role_summary or 'none'}")
    if errors_by_position:
        st.warning("Some cells need attention before the assignment can be exported.")
        for position, messages in sorted(errors_by_position.items()):
            if st.button(
                f"Go to cell {position}: {' '.join(messages)}",
                key=f"go_to_error_{position}",
            ):
                st.session_state["assignment_authoring_selected_position"] = position
                st.rerun()

    st.subheader("Export")
    if errors:
        st.error("Resolve these issues before exporting:")
        for error in errors:
            st.write(f"- {error}")
        st.caption("Downloads are unavailable until the review is valid.")
        return

    st.success("The reviewed configuration is valid. The uploaded notebook has not changed.")
    config_json = json.dumps(reviewed, indent=2) + "\n"
    prepared = prepare_assignment(notebook, reviewed)
    uploaded_path = Path(uploaded.name)
    assignment_id = reviewed["assignment"]["id"] or uploaded_path.stem
    package_notebook_name = f"{assignment_id}.ipynb"
    package_manifest = build_manifest(
        uploaded_path,
        Path("assignment-config.json"),
        Path(package_notebook_name),
        notebook,
        reviewed,
    )
    prepared_json = json.dumps(prepared, indent=1) + "\n"
    manifest_json = json.dumps(package_manifest, indent=2) + "\n"
    package_json = build_assignment_package(
        assignment_id, prepared_json, config_json, manifest_json
    )
    st.download_button(
        "Download assignment package (.zip)",
        data=package_json,
        file_name=f"{assignment_id}.zip",
        mime="application/zip",
        type="primary",
    )
    st.caption(
        "The package contains the prepared notebook, configuration, manifest, and local-testing guide."
    )
    with st.expander("Individual downloads"):
        individual_left, individual_right = st.columns(2)
        with individual_left:
            st.download_button(
                "Prepared student notebook",
                data=prepared_json,
                file_name=package_notebook_name,
                mime="application/x-ipynb+json",
            )
            st.download_button(
                "Reviewed configuration",
                data=config_json,
                file_name="assignment-config.json",
                mime="application/json",
            )
        with individual_right:
            st.download_button(
                "Preparation manifest",
                data=manifest_json,
                file_name="assignment.manifest.json",
                mime="application/json",
            )
    with st.expander("Preview configuration JSON"):
        st.code(config_json, language="json")


if __name__ == "__main__":
    main()

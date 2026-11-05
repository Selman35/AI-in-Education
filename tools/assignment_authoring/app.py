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


def initialise_review_state(draft: dict[str, Any], st: Any) -> None:
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
    st.session_state["assignment_authoring_upload"] = upload_identity


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
sections, and permissions as metadata. Existing logging continues to use the
configured logging root. Generic role enforcement is introduced separately
from this package-export step.
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


def display_cell_source(draft_cell: dict[str, Any], st: Any) -> None:
    """Show the complete original cell source before its review controls."""
    source = draft_cell["source"] or "(empty cell)"
    language = "python" if draft_cell["cell_type"] == "code" else "markdown"
    st.caption("Original cell content")
    st.code(source, language=language)
    st.info(f"Suggestion: {draft_cell['suggestion_reason']}")


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
    initialise_review_state(draft, st)

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
        help="Extra-cell naming and live extension support will be added in a later step.",
    )

    st.subheader("Notebook overview")
    st.caption(
        "This table shows every cell. Use the filters and bulk updates below to make common changes faster."
    )

    type_options = sorted({cell["cell_type"] for cell in draft["cells"]})
    role_options = sorted(VALID_ROLES)
    filter_left, filter_middle, filter_right = st.columns(3)
    with filter_left:
        selected_types = st.multiselect(
            "Show cell types", type_options, default=type_options
        )
    with filter_middle:
        selected_roles = st.multiselect(
            "Show roles", role_options, default=role_options,
            format_func=lambda value: ROLE_LABELS[value],
        )
    with filter_right:
        search_text = st.text_input(
            "Find a cell", help="Searches suggested key, section, or source preview."
        ).strip().lower()

    st.subheader("Bulk updates")
    position_labels = {
        cell["position"]: f"Cell {cell['position']}: {cell['proposed_key']}"
        for cell in draft["cells"]
    }
    selected_positions = st.multiselect(
        "Cells to update",
        options=list(position_labels),
        format_func=lambda position: position_labels[position],
        help="Select one or more cells, then apply a common role or section.",
    )
    bulk_left, bulk_middle, bulk_right = st.columns(3)
    with bulk_left:
        bulk_role_option = st.selectbox(
            "Common role",
            options=["Keep current"] + role_options,
            format_func=lambda value: "Keep current" if value == "Keep current" else ROLE_LABELS[value],
        )
    with bulk_middle:
        bulk_section = st.text_input(
            "Common section", help="Leave blank to keep each cell's current section."
        )
    with bulk_right:
        st.write("")
        if st.button("Apply to selected cells"):
            if not selected_positions:
                st.warning("Select at least one cell before applying a bulk update.")
            else:
                apply_bulk_changes(
                    draft["cells"],
                    selected_positions,
                    None if bulk_role_option == "Keep current" else bulk_role_option,
                    bulk_section,
                    st,
                )
                st.rerun()

    reviewed_cells = [reviewed_cell_from_state(cell, st) for cell in draft["cells"]]
    errors_by_position = cell_validation_errors(reviewed_cells)
    st.dataframe(
        review_overview_rows(draft["cells"], reviewed_cells, errors_by_position),
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Detailed cell review")
    visible_cells = []
    for draft_cell in draft["cells"]:
        current = reviewed_cell_from_state(draft_cell, st)
        searchable = " ".join(
            [
                current["analytics_key"],
                current["section"],
                draft_cell["source_preview"],
            ]
        ).lower()
        if (
            current["cell_type"] in selected_types
            and current["role"] in selected_roles
            and (not search_text or search_text in searchable)
        ):
            visible_cells.append(draft_cell)
    st.caption(f"Showing {len(visible_cells)} of {len(draft['cells'])} cells.")
    for draft_cell in visible_cells:
        label = f"Cell {draft_cell['position']}: {draft_cell['cell_type']} — {draft_cell['proposed_key']}"
        with st.expander(label, expanded=False):
            display_cell_source(draft_cell, st)
            reviewed_cell_from_widget(draft_cell, st)

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

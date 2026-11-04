"""Local Streamlit review app for draft notebook assignment configurations.

Run with:
    streamlit run tools/assignment_authoring/app.py

This app is intentionally local and export-only. Uploaded notebooks are read
in memory to create a JSON configuration for teacher review; they are never
modified or sent to an external service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prepare_assignment import (
    build_manifest,
    prepare_assignment,
    validate_notebook_matches_config,
)
from scan_notebook import (
    VALID_ROLES,
    build_draft_config,
    build_reviewed_config,
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


def notebook_from_upload(uploaded_file: Any) -> dict[str, Any]:
    """Read and validate the uploaded notebook without writing it to disk."""
    notebook = json.loads(uploaded_file.getvalue().decode("utf-8"))
    if not isinstance(notebook.get("cells"), list):
        raise ValueError("The selected file is not a valid Jupyter notebook.")
    return notebook


def reviewed_cell_from_widget(draft_cell: dict[str, Any], st: Any) -> dict[str, Any]:
    """Render teacher controls for one cell and return the reviewed selection."""
    position = draft_cell["position"]
    widget_prefix = f"cell_{position}"
    default_role = draft_cell["proposed_role"]
    role = st.selectbox(
        "Role",
        options=sorted(VALID_ROLES),
        index=sorted(VALID_ROLES).index(default_role),
        format_func=lambda value: ROLE_LABELS[value],
        key=f"{widget_prefix}_role",
    )
    default_policy = policy_for_role(role)
    analytics_key = st.text_input(
        "Stable analytics key",
        value=draft_cell["proposed_key"],
        key=f"{widget_prefix}_key",
        help="Use lowercase letters, numbers, and underscores. This key is used in logs and analytics.",
    )
    section = st.text_input(
        "Section or task key",
        value=draft_cell["proposed_section"],
        key=f"{widget_prefix}_section",
    )
    permissions = default_policy
    if st.checkbox(
        "Show advanced permissions",
        value=False,
        key=f"{widget_prefix}_show_advanced_permissions",
        help="Roles already provide the usual permissions. Change these only for an exception.",
    ):
        st.caption(
            "These settings override the permissions normally supplied by the selected role."
        )
        permissions = {
            "source_editable": st.checkbox(
                "Students may edit source",
                value=default_policy["source_editable"],
                key=f"{widget_prefix}_source_editable",
            ),
            "executable": st.checkbox(
                "Students may run this cell",
                value=default_policy["executable"],
                key=f"{widget_prefix}_executable",
            ),
            "deletable": st.checkbox(
                "Students may delete this original cell",
                value=default_policy["deletable"],
                key=f"{widget_prefix}_deletable",
            ),
            "movable": st.checkbox(
                "Students may move this original cell",
                value=default_policy["movable"],
                key=f"{widget_prefix}_movable",
            ),
        }
    return {
        "source_cell_id": draft_cell["source_cell_id"],
        "position": position,
        "cell_type": draft_cell["cell_type"],
        "analytics_key": analytics_key.strip(),
        "role": role,
        "section": section.strip(),
        "policy": permissions,
    }


def display_cell_source(draft_cell: dict[str, Any], st: Any) -> None:
    """Show the complete original cell source before its review controls."""
    source = draft_cell["source"] or "(empty cell)"
    language = "python" if draft_cell["cell_type"] == "code" else "markdown"
    st.caption("Original cell content")
    st.code(source, language=language)


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

    st.subheader("Cell review")
    reviewed_cells = []
    for draft_cell in draft["cells"]:
        label = f"Cell {draft_cell['position']}: {draft_cell['cell_type']} — {draft_cell['proposed_key']}"
        with st.expander(label, expanded=False):
            display_cell_source(draft_cell, st)
            reviewed_cells.append(reviewed_cell_from_widget(draft_cell, st))

    reviewed = build_reviewed_config(
        draft,
        course_name,
        assignment_name,
        allow_extra_cells,
        reviewed_cells,
    )
    errors = validate_reviewed_config(reviewed)
    errors.extend(validate_notebook_matches_config(notebook, reviewed, uploaded.name))

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
    st.download_button(
        "Download reviewed configuration (optional record)",
        data=config_json,
        file_name=uploaded.name.removesuffix(".ipynb") + ".assignment-config.json",
        mime="application/json",
    )
    prepared = prepare_assignment(notebook, reviewed)
    uploaded_path = Path(uploaded.name)
    prepared_name = uploaded_path.with_name(f"{uploaded_path.stem}.prepared.ipynb").name
    manifest_name = uploaded_path.with_name(f"{uploaded_path.stem}.prepared.manifest.json").name
    manifest = build_manifest(
        uploaded_path,
        uploaded_path.with_name(uploaded.name.removesuffix(".ipynb") + ".assignment-config.json"),
        Path(prepared_name),
        notebook,
        reviewed,
    )
    prepared_json = json.dumps(prepared, indent=1) + "\n"
    manifest_json = json.dumps(manifest, indent=2) + "\n"
    downloads_left, downloads_right = st.columns(2)
    with downloads_left:
        st.download_button(
            "Download prepared student notebook",
            data=prepared_json,
            file_name=prepared_name,
            mime="application/x-ipynb+json",
            type="primary",
        )
    with downloads_right:
        st.download_button(
            "Download preparation manifest",
            data=manifest_json,
            file_name=manifest_name,
            mime="application/json",
        )
    with st.expander("Preview configuration JSON"):
        st.code(config_json, language="json")


if __name__ == "__main__":
    main()

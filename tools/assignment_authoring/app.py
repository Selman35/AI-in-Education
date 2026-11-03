"""Local Streamlit review app for draft notebook assignment configurations.

Run with:
    streamlit run tools/assignment_authoring/app.py

This app is intentionally local and export-only. Uploaded notebooks are read
in memory to create a JSON configuration for teacher review; they are never
modified or sent to an external service.
"""

from __future__ import annotations

import json
from typing import Any

from scan_notebook import (
    VALID_ROLES,
    build_draft_config,
    build_reviewed_config,
    policy_for_role,
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
        "Step 2: review deterministic suggestions locally. This app does not modify notebooks or publish assignments."
    )

    with st.expander("How to use this page", expanded=True):
        st.markdown(
            """
1. Upload an existing Jupyter notebook.
2. Open each cell review and read the complete original cell content.
3. Confirm or change its role, stable analytics key, section, and student permissions.
4. Download the reviewed configuration once there are no validation errors.

The uploaded notebook stays unchanged. This step only creates a configuration
file for later publishing and student provisioning.
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

    st.subheader("Export")
    if errors:
        st.error("Resolve these issues before exporting:")
        for error in errors:
            st.write(f"- {error}")
        return

    st.success("The reviewed configuration is valid. It has not changed the uploaded notebook.")
    config_json = json.dumps(reviewed, indent=2) + "\n"
    st.download_button(
        "Download reviewed configuration",
        data=config_json,
        file_name=uploaded.name.removesuffix(".ipynb") + ".assignment-config.json",
        mime="application/json",
    )
    with st.expander("Preview configuration JSON"):
        st.code(config_json, language="json")


if __name__ == "__main__":
    main()

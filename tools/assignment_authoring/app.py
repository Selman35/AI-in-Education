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
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from prepare_assignment import (
    build_manifest,
    json_fingerprint,
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
    notebook_fingerprint,
    validate_reviewed_config,
)


ROLE_LABELS = {
    "locked_text": "Locked text or instruction",
    "starter_code": "Locked, runnable starter code",
    "answer_code": "Editable code answer",
    "answer_markdown": "Editable Markdown answer",
    "unrestricted": "Unrestricted cell",
}

ROLE_DESCRIPTIONS = {
    "locked_text": "Students can read this instruction, but cannot edit, move, delete, or run it.",
    "starter_code": "Students can run the provided code, but cannot change, move, or delete it.",
    "answer_code": "Students can write and run their answer, but cannot remove or move the original cell.",
    "answer_markdown": "Students can write an explanation, but cannot remove or move the original cell.",
    "unrestricted": "Students can edit, run, move, and delete this cell.",
}

PACKAGE_SCHEMA_VERSION = 2
TEACHER_SOURCE_FILENAME = "teacher_source.ipynb"


def apply_page_theme(st: Any) -> None:
    """Apply the calm, high-contrast visual treatment used by the local app."""
    st.markdown(
        """
<style>
    .stApp { background: linear-gradient(135deg, #f6f8fc 0%, #edf4f7 100%); }
    [data-testid="stHeader"] { background: rgba(246, 248, 252, 0.85); }
    h1, h2, h3 { color: #17324d; }
    div[data-testid="stMetric"] {
        background: #ffffff; border: 1px solid #dbe5ee; border-radius: 12px;
        padding: 0.6rem 0.8rem; box-shadow: 0 2px 8px rgba(23, 50, 77, 0.05);
    }
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: #ffffff; border-color: #dbe5ee; border-radius: 12px;
        box-shadow: 0 2px 10px rgba(23, 50, 77, 0.05);
    }
    div.stButton > button { border-radius: 8px; font-weight: 600; }
    div[data-testid="stCodeBlock"] { border-radius: 9px; }
    .authoring-hero {
        background: linear-gradient(120deg, #17324d, #246b7b); color: white;
        padding: 1.25rem 1.5rem; border-radius: 14px; margin: 0.5rem 0 1.25rem;
    }
    .authoring-hero h2 { color: white; margin: 0 0 0.35rem; }
    .authoring-hero p { margin: 0; opacity: 0.94; }
</style>
        """,
        unsafe_allow_html=True,
    )


def matching_cell_positions(
    draft_cells: list[dict[str, Any]], reviewed_cells: list[dict[str, Any]], query: str
) -> list[int]:
    """Find cells by their visible content or current review fields."""
    normalized = query.strip().lower()
    if not normalized:
        return []
    positions = []
    for draft_cell, reviewed_cell in zip(draft_cells, reviewed_cells):
        searchable = " ".join(
            [
                draft_cell["source"],
                reviewed_cell["analytics_key"],
                reviewed_cell["section"],
                reviewed_cell["role"],
            ]
        ).lower()
        if normalized in searchable:
            positions.append(draft_cell["position"])
    return positions


def adjacent_position(positions: list[int], current: int | None, direction: int) -> int | None:
    """Return the previous or next cell position without wrapping around."""
    if not positions:
        return None
    if current not in positions:
        return positions[0]
    index = positions.index(current)
    target = index + direction
    return positions[target] if 0 <= target < len(positions) else current


@lru_cache(maxsize=16)
def cached_draft(notebook_json: str, source_notebook: str) -> dict[str, Any]:
    """Cache deterministic scans; repeated UI navigation should not rescan cells."""
    return build_draft_config(json.loads(notebook_json), source_notebook)


def draft_for_notebook(notebook: dict[str, Any], source_notebook: str) -> dict[str, Any]:
    """Return an independent cached draft for the current editable notebook."""
    canonical_notebook = json.dumps(
        notebook, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return copy.deepcopy(cached_draft(canonical_notebook, source_notebook))


def widget_prefix(draft_cell: dict[str, Any]) -> str:
    """Return stable Streamlit widget keys for one source-notebook cell."""
    return f"cell_{draft_cell['position']}"


def initialise_review_state(
    draft: dict[str, Any],
    uploaded_notebook: dict[str, Any],
    st: Any,
    restored_cells: list[dict[str, Any]] | None = None,
    assignment_details: dict[str, Any] | None = None,
    package_context: dict[str, Any] | None = None,
    pending_review_ids: set[str] | None = None,
    removed_cells: list[dict[str, Any]] | None = None,
) -> None:
    """Initialise or reset review widgets when the uploaded notebook changes."""
    package_identity = (package_context or {}).get("package_id", "new")
    upload_identity = f"{package_identity}:{draft['source_notebook']}:{draft.get('source_notebook_sha256', '')}"
    is_new_upload = (
        st.session_state.get("assignment_authoring_upload") != upload_identity
        or "assignment_authoring_editable_notebook" not in st.session_state
    )
    if is_new_upload:
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
        st.session_state["assignment_authoring_package_context"] = copy.deepcopy(
            package_context
            or {
                "package_id": str(uuid.uuid4()),
                "revision": 1,
                "parent_revision": None,
            }
        )
        st.session_state["assignment_authoring_pending_review_ids"] = set(
            pending_review_ids or set()
        )
        st.session_state["assignment_authoring_removed_cells"] = copy.deepcopy(
            removed_cells or []
        )
        st.session_state["assignment_authoring_removed_cells_confirmed"] = False

        course = (assignment_details or {}).get("course", {})
        assignment = (assignment_details or {}).get("assignment", {})
        structure = (assignment_details or {}).get("structure", {})
        st.session_state["assignment_authoring_course_name"] = str(
            course.get("name", draft["course"]["proposed_name"])
        )
        st.session_state["assignment_authoring_assignment_name"] = str(
            assignment.get("name", draft["assignment"]["proposed_name"])
        )
        st.session_state["assignment_authoring_allow_extra_cells"] = bool(
            structure.get("allow_extra_cells", draft["structure"]["allow_extra_cells"])
        )

    restored_by_id = {
        cell.get("source_cell_id"): cell
        for cell in (restored_cells or [])
        if isinstance(cell, dict)
    }
    for draft_cell in draft["cells"]:
        prefix = widget_prefix(draft_cell)
        restored = restored_by_id.get(draft_cell.get("source_cell_id"))
        role = (restored or {}).get("role", draft_cell["proposed_role"])
        policy = copy.deepcopy(
            (restored or {}).get("policy", policy_for_role(role))
        )
        values = {
            f"{prefix}_role": role,
            f"{prefix}_key": (restored or {}).get("analytics_key", draft_cell["proposed_key"]),
            f"{prefix}_section": (restored or {}).get("section", draft_cell["proposed_section"]),
            f"{prefix}_show_advanced_permissions": policy != policy_for_role(role),
            f"{prefix}_source": draft_cell["source"],
        }
        for permission, value in policy.items():
            values[f"{prefix}_{permission}"] = value
        for key, value in values.items():
            if is_new_upload:
                st.session_state[key] = value
            else:
                # A browser can retain an older session after the app changes.
                # Repair missing keys without discarding the teacher's review.
                st.session_state.setdefault(key, value)

    st.session_state.setdefault("assignment_authoring_selected_position", None)


def editable_notebook(st: Any) -> dict[str, Any]:
    """Return the session-only notebook copy used for preview and export."""
    return st.session_state["assignment_authoring_editable_notebook"]


def update_cell_source(
    notebook: dict[str, Any], position: int, source: str
) -> None:
    """Update one cell in the editable copy without changing its identity."""
    notebook["cells"][position - 1]["source"] = source


def apply_selected_cell_changes(
    notebook: dict[str, Any],
    draft_cell: dict[str, Any],
    source: str,
    role: str,
    analytics_key: str,
    section: str,
    permissions: dict[str, bool] | None,
    st: Any,
) -> None:
    """Save a teacher's selected-cell review without changing the toggle state."""
    position = draft_cell["position"]
    prefix = widget_prefix(draft_cell)
    update_cell_source(notebook, position, source)
    st.session_state[f"{prefix}_role"] = role
    st.session_state[f"{prefix}_key"] = analytics_key.strip()
    st.session_state[f"{prefix}_section"] = section.strip()
    for permission, value in (permissions or policy_for_role(role)).items():
        st.session_state[f"{prefix}_{permission}"] = value
    pending_review_ids = set(
        st.session_state.get("assignment_authoring_pending_review_ids", set())
    )
    pending_review_ids.discard(draft_cell.get("source_cell_id"))
    st.session_state["assignment_authoring_pending_review_ids"] = pending_review_ids


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
    return notebook_from_bytes(uploaded_file.getvalue())


def notebook_from_bytes(payload: bytes) -> dict[str, Any]:
    """Read a notebook payload and verify that it has a cells list."""
    notebook = json.loads(payload.decode("utf-8"))
    if not isinstance(notebook.get("cells"), list):
        raise ValueError("The selected file is not a valid Jupyter notebook.")
    return notebook


def json_from_bytes(payload: bytes, description: str) -> dict[str, Any]:
    """Read one JSON object from an assignment-package entry."""
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"The package {description} must contain a JSON object.")
    return value


def package_entry_name(names: list[str], basename: str) -> str:
    """Return exactly one archive entry matching a required package filename."""
    matches = [name for name in names if name.rstrip("/").split("/")[-1] == basename]
    if len(matches) != 1:
        raise ValueError(f"The assignment package must contain exactly one '{basename}' file.")
    return matches[0]


def archive_sibling(entry_name: str, sibling_name: str) -> str:
    """Return a filename inside the same archive directory as an entry."""
    parent = entry_name.rsplit("/", 1)[0] if "/" in entry_name else ""
    return f"{parent}/{sibling_name}" if parent else sibling_name


def source_from_legacy_prepared_notebook(prepared: dict[str, Any]) -> dict[str, Any]:
    """Recover a source notebook from the baseline kept by version-one packages.

    Early website packages did not include a clean source notebook. Their prepared
    notebook does, however, retain the original cells in its baseline metadata.
    This recovery path lets those packages be revised while removing only metadata
    added by the authoring workflow.
    """
    authoring = prepared.get("metadata", {}).get("assignment_authoring", {})
    baseline = authoring.get("baseline", {}).get("template_cells", [])
    if not isinstance(baseline, list) or not baseline:
        raise ValueError(
            "This older assignment package has no recoverable teacher source notebook."
        )

    source = copy.deepcopy(prepared)
    source["cells"] = []
    for entry in baseline:
        content = entry.get("content") if isinstance(entry, dict) else None
        if not isinstance(content, dict):
            raise ValueError("The older package has an invalid baseline cell record.")
        cell = copy.deepcopy(content)
        metadata = cell.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("assignment_authoring", None)
        source["cells"].append(cell)

    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("assignment_authoring", None)
        analytics = metadata.get("student_analytics")
        if isinstance(analytics, dict):
            for key in (
                "course_id",
                "assignment_id",
                "assignment_authoring_schema_version",
                "assignment_configuration_sha256",
            ):
                analytics.pop(key, None)
            if not analytics:
                metadata.pop("student_analytics", None)
    return source


def load_assignment_package(payload: bytes) -> dict[str, Any]:
    """Load and validate a previously exported assignment package in memory."""
    try:
        archive = ZipFile(io.BytesIO(payload))
    except Exception as error:
        raise ValueError("The selected file is not a readable ZIP assignment package.") from error

    with archive:
        names = archive.namelist()
        config_entry = package_entry_name(names, "assignment-config.json")
        manifest_entry = package_entry_name(names, "assignment.manifest.json")
        configuration = json_from_bytes(archive.read(config_entry), "configuration")
        manifest = json_from_bytes(archive.read(manifest_entry), "manifest")

        expected_config_fingerprint = manifest.get("reviewed_configuration_sha256")
        if expected_config_fingerprint != json_fingerprint(configuration):
            raise ValueError(
                "The package configuration does not match its manifest. Please use an unmodified package ZIP."
            )
        config_errors = validate_reviewed_config(configuration)
        if config_errors:
            raise ValueError("The package configuration is invalid: " + " ".join(config_errors))

        package_schema = manifest.get("assignment_package_schema_version")
        is_legacy_package = package_schema is None
        if is_legacy_package:
            prepared_name = manifest.get("prepared_notebook")
            if not isinstance(prepared_name, str):
                raise ValueError("The older package manifest does not name its prepared notebook.")
            prepared_entry = archive_sibling(config_entry, prepared_name)
            if prepared_entry not in names:
                raise ValueError("The older package is missing its prepared notebook.")
            prepared_notebook = notebook_from_bytes(archive.read(prepared_entry))
            stored_configuration_hash = (
                prepared_notebook.get("metadata", {})
                .get("assignment_authoring", {})
                .get("configuration_sha256")
            )
            if stored_configuration_hash != json_fingerprint(configuration):
                raise ValueError(
                    "The older package prepared notebook does not match its configuration."
                )
            source_notebook = source_from_legacy_prepared_notebook(prepared_notebook)
            # Older exports did not preserve reliable cell IDs or an exact clean
            # source fingerprint. Upgrade their configuration from the baseline
            # recovered above so all subsequent revisions use the current format.
            configuration = copy.deepcopy(configuration)
            recovered_draft = draft_for_notebook(
                source_notebook,
                str(configuration.get("source_notebook") or "teacher_source.ipynb"),
            )
            if len(recovered_draft["cells"]) != len(configuration.get("cells", [])):
                raise ValueError(
                    "The older package baseline does not match its saved cell review."
                )
            for draft_cell, configured_cell in zip(
                recovered_draft["cells"], configuration["cells"]
            ):
                if configured_cell.get("cell_type") != draft_cell["cell_type"]:
                    raise ValueError(
                        "The older package baseline does not match its saved cell types."
                    )
                configured_cell["source_cell_id"] = draft_cell["source_cell_id"]
                configured_cell["position"] = draft_cell["position"]
            configuration["source_notebook_sha256"] = notebook_fingerprint(source_notebook)
        else:
            if package_schema != PACKAGE_SCHEMA_VERSION:
                raise ValueError(
                    f"This package uses unsupported schema version {package_schema}."
                )
            source_name = manifest.get("teacher_source_notebook")
            if not isinstance(source_name, str):
                raise ValueError("The package manifest does not name its teacher source notebook.")
            source_entry = archive_sibling(config_entry, source_name)
            if source_entry not in names:
                raise ValueError("The package is missing its teacher source notebook.")
            source_notebook = notebook_from_bytes(archive.read(source_entry))
            if manifest.get("teacher_source_notebook_sha256") != notebook_fingerprint(source_notebook):
                raise ValueError(
                    "The teacher source notebook does not match the package manifest."
                )
            prepared_name = manifest.get("prepared_notebook")
            if not isinstance(prepared_name, str):
                raise ValueError("The package manifest does not name its prepared notebook.")
            prepared_entry = archive_sibling(config_entry, prepared_name)
            if prepared_entry not in names:
                raise ValueError("The package is missing its prepared student notebook.")
            prepared_notebook = notebook_from_bytes(archive.read(prepared_entry))
            if manifest.get("prepared_notebook_sha256") != notebook_fingerprint(
                prepared_notebook
            ):
                raise ValueError(
                    "The prepared notebook does not match the package manifest."
                )

        source_name = str(configuration.get("source_notebook") or "teacher_source.ipynb")
        matching_errors = validate_notebook_matches_config(
            source_notebook, configuration, source_name
        )
        if matching_errors:
            raise ValueError(
                "The package source notebook does not match its saved review: "
                + " ".join(matching_errors)
            )

    package_id = manifest.get("package_id")
    if not isinstance(package_id, str) or not package_id:
        package_id = f"legacy-{json_fingerprint(configuration)[:16]}"
    current_revision = manifest.get("revision", 1)
    if not isinstance(current_revision, int) or current_revision < 1:
        raise ValueError("The package manifest has an invalid revision number.")

    return {
        "source_notebook": source_notebook,
        "source_name": source_name,
        "configuration": configuration,
        "manifest": manifest,
        "legacy": is_legacy_package,
        "package_context": {
            "package_id": package_id,
            "revision": current_revision + 1,
            "parent_revision": current_revision,
        },
    }


def reviewed_cells_for_source(
    draft: dict[str, Any], previous_configuration: dict[str, Any]
) -> tuple[list[dict[str, Any]], set[str], list[dict[str, Any]]]:
    """Carry reviews onto an externally revised source notebook by cell ID."""
    previous_cells = {
        cell.get("source_cell_id"): cell
        for cell in previous_configuration.get("cells", [])
        if isinstance(cell, dict) and isinstance(cell.get("source_cell_id"), str)
    }
    current_ids = {
        cell["source_cell_id"]
        for cell in draft["cells"]
        if isinstance(cell.get("source_cell_id"), str)
    }
    reviewed_cells: list[dict[str, Any]] = []
    pending_review_ids: set[str] = set()

    for draft_cell in draft["cells"]:
        previous = previous_cells.get(draft_cell.get("source_cell_id"))
        if previous and previous.get("cell_type") == draft_cell["cell_type"]:
            reviewed_cells.append(
                {
                    "source_cell_id": draft_cell["source_cell_id"],
                    "position": draft_cell["position"],
                    "cell_type": draft_cell["cell_type"],
                    "analytics_key": previous["analytics_key"],
                    "role": previous["role"],
                    "section": previous.get("section", ""),
                    "policy": copy.deepcopy(previous["policy"]),
                }
            )
        else:
            reviewed_cells.append(
                {
                    "source_cell_id": draft_cell["source_cell_id"],
                    "position": draft_cell["position"],
                    "cell_type": draft_cell["cell_type"],
                    "analytics_key": draft_cell["proposed_key"],
                    "role": draft_cell["proposed_role"],
                    "section": draft_cell["proposed_section"],
                    "policy": copy.deepcopy(draft_cell["proposed_policy"]),
                }
            )
            if isinstance(draft_cell.get("source_cell_id"), str):
                pending_review_ids.add(draft_cell["source_cell_id"])

    removed_cells = [
        cell
        for cell_id, cell in previous_cells.items()
        if cell_id not in current_ids
    ]
    return reviewed_cells, pending_review_ids, removed_cells


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


def local_test_guide(assignment_id: str) -> str:
    """Return concise instructions bundled with a downloaded assignment package."""
    return f"""# Local assignment-package test

This package contains a prepared notebook and its review records. It does not
include a copy of the JupyterLab extension.

`teacher_source.ipynb` is the clean teacher version. Keep it separate from
student submissions. To revise an assignment, reopen this package in the
authoring website and, if needed, attach an edited copy of that source file.

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
    package_name: str,
    assignment_id: str,
    teacher_source_notebook_json: str,
    prepared_notebook_json: str,
    configuration_json: str,
    manifest_json: str,
) -> bytes:
    """Build the single ZIP download for a reviewed assignment."""
    package_root = f"{package_name}/"
    package = io.BytesIO()
    with ZipFile(package, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{package_root}{TEACHER_SOURCE_FILENAME}", teacher_source_notebook_json
        )
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


def build_package_manifest(
    source_notebook_name: str,
    assignment_id: str,
    teacher_source: dict[str, Any],
    prepared_notebook: dict[str, Any],
    configuration: dict[str, Any],
    package_context: dict[str, Any],
) -> dict[str, Any]:
    """Create a revision-aware manifest for a downloadable assignment package."""
    manifest = build_manifest(
        Path(source_notebook_name),
        Path("assignment-config.json"),
        Path(f"{assignment_id}.ipynb"),
        teacher_source,
        configuration,
    )
    manifest.update(
        {
            "assignment_package_schema_version": PACKAGE_SCHEMA_VERSION,
            "package_id": package_context["package_id"],
            "revision": package_context["revision"],
            "parent_revision": package_context.get("parent_revision"),
            "teacher_source_notebook": TEACHER_SOURCE_FILENAME,
            "teacher_source_notebook_sha256": notebook_fingerprint(teacher_source),
            "prepared_notebook_sha256": notebook_fingerprint(prepared_notebook),
        }
    )
    return manifest


def render_notebook_cell(
    draft_cell: dict[str, Any],
    reviewed_cell: dict[str, Any],
    selected: bool,
    errors: list[str],
    show_full_content: bool,
    st: Any,
) -> None:
    """Render one read-only, notebook-like cell card in the canvas."""
    position = draft_cell["position"]
    status = "⚠ Needs attention" if errors else "✓ Ready"
    cell_icon = "⌘" if reviewed_cell["cell_type"] == "code" else "¶"
    with st.container(border=True):
        left, right = st.columns([5, 1])
        with left:
            st.caption(
                f"{cell_icon} Cell {position} · {reviewed_cell['cell_type']} · "
                f"{ROLE_LABELS[reviewed_cell['role']]} · "
                f"key: {reviewed_cell['analytics_key'] or '—'} · {status}"
            )
        with right:
            if st.button(
                "Selected" if selected else "Select",
                key=f"select_cell_{position}",
                disabled=selected,
                width="stretch",
            ):
                st.session_state["assignment_authoring_selected_position"] = position
                st.rerun()

        source = draft_cell["source"]
        if not selected and not show_full_content:
            preview = draft_cell["source_preview"] or "(empty cell)"
            if reviewed_cell["cell_type"] == "code":
                st.code(preview, language="python")
            else:
                st.caption(preview)
        elif reviewed_cell["cell_type"] == "markdown":
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
        "Edits stay in this panel until you apply them. This prevents the "
        "notebook from rerendering while you type."
    )
    advanced = st.checkbox(
        "Edit advanced permissions",
        key=f"{prefix}_show_advanced_permissions",
        help="Use this only when the normal permissions for the selected role are not suitable.",
    )
    current_role = st.session_state[f"{prefix}_role"]
    role_options = sorted(VALID_ROLES)
    with st.form(f"selected_cell_form_{position}", border=False):
        source = st.text_area(
            "Cell source",
            value=draft_cell["source"],
            height=220,
            help="Markdown is rendered in the notebook view; code is displayed as Python.",
        )
        role = st.selectbox(
            "Role",
            options=role_options,
            index=role_options.index(current_role),
            format_func=lambda value: ROLE_LABELS[value],
        )
        analytics_key = st.text_input(
            "Stable analytics key",
            value=st.session_state[f"{prefix}_key"],
            help="Use lowercase letters, numbers, and underscores. This name appears in logs and analytics.",
        )
        section = st.text_input(
            "Section or task key", value=st.session_state[f"{prefix}_section"]
        )
        permissions: dict[str, bool] | None = None
        if advanced:
            st.caption("These settings override the normal permissions of the selected role.")
            permissions = {
                permission: st.checkbox(
                    label.replace("_", " ").capitalize(),
                    value=st.session_state[f"{prefix}_{permission}"],
                    key=f"pending_{prefix}_{permission}",
                )
                for permission, label in {
                    "source_editable": "students may edit source",
                    "executable": "students may run this cell",
                    "deletable": "students may delete this original cell",
                    "movable": "students may move this original cell",
                }.items()
            }
        apply_changes = st.form_submit_button(
            "Apply changes", type="primary", width="stretch"
        )

    if apply_changes:
        apply_selected_cell_changes(
            editable,
            draft_cell,
            source,
            role,
            analytics_key,
            section,
            permissions,
            st,
        )
        st.rerun()

    st.info(
        "Automatic suggestion: "
        f"{draft_cell['suggestion_reason']}\n\n"
        "Applying a source edit refreshes this explanation automatically, but "
        "keeps your role, key, section, and permission choices."
    )


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
    apply_page_theme(st)
    st.title("Notebook assignment authoring")
    st.markdown(
        """
<div class="authoring-hero">
  <h2>Turn a teaching notebook into a student-ready assignment.</h2>
  <p>Review the notebook visually, choose what students can do in each cell, and download a prepared copy. Your uploaded notebook is never changed.</p>
</div>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.header("Teacher guide")
        st.markdown(
            """**1. Upload** a teaching notebook.

**2. Select** a cell in the notebook view.

**3. Review** its content and student permissions.

**4. Export** the prepared assignment package."""
        )
        with st.expander("What do cell roles mean?"):
            for role in sorted(VALID_ROLES):
                st.markdown(f"**{ROLE_LABELS[role]}**  \n{ROLE_DESCRIPTIONS[role]}")
        st.caption("Advanced permissions are only for exceptions to a role's normal behaviour.")

    with st.expander("How to use this page", expanded=True):
        st.markdown(
            """
1. Upload an existing Jupyter notebook.
2. Select a cell in the notebook view.
3. Review or change its source, role, stable key, and section in the side panel.
4. Download the prepared student notebook once there are no validation errors.

The uploaded notebook stays unchanged. The prepared notebook is downloaded as
a separate file for later use with generic assignment policies.

**Roles set the normal student permissions automatically.** Use **Show advanced
permissions** only when one cell needs behaviour that differs from its role.
            """
        )

    authoring_mode = st.segmented_control(
        "Start from",
        ["Create a new assignment", "Revise an assignment package"],
        default="Create a new assignment",
        required=True,
        key="assignment_authoring_mode",
        width="stretch",
    )

    restored_cells: list[dict[str, Any]] | None = None
    assignment_details: dict[str, Any] | None = None
    package_context: dict[str, Any] | None = None
    pending_review_ids: set[str] = set()
    removed_cells: list[dict[str, Any]] = []

    if authoring_mode == "Create a new assignment":
        uploaded = st.file_uploader(
            "Upload an existing Jupyter notebook", type=["ipynb"], key="new_notebook_upload"
        )
        if uploaded is None:
            st.info("Choose a notebook to begin. Nothing is uploaded to a server or changed on disk.")
            return
        try:
            notebook = notebook_from_upload(uploaded)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            st.error(str(error))
            return
        source_notebook_name = uploaded.name
    else:
        package_upload = st.file_uploader(
            "Upload an assignment package ZIP", type=["zip"], key="revision_package_upload"
        )
        if package_upload is None:
            st.info(
                "Upload a package previously downloaded from this website. "
                "The ZIP establishes the assignment identity and saved review decisions."
            )
            return
        try:
            package = load_assignment_package(package_upload.getvalue())
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            st.error(str(error))
            return

        if package["legacy"]:
            st.warning(
                "This is an older package. Its clean source was recovered from the "
                "prepared-notebook baseline and the next download will use the new package format."
            )
        else:
            st.success(
                f"Loaded revision {package['package_context']['parent_revision']} of this assignment."
            )

        source_notebook_name = package["source_name"]
        notebook = package["source_notebook"]
        assignment_details = package["configuration"]
        package_context = package["package_context"]
        revision_source = st.file_uploader(
            "Optional updated teacher-source notebook",
            type=["ipynb"],
            key="revision_source_upload",
            help=(
                "Use this only after editing the clean teacher source outside the website. "
                "The package ZIP remains the required assignment identifier."
            ),
        )
        if revision_source is None:
            restored_cells = copy.deepcopy(package["configuration"]["cells"])
        else:
            try:
                notebook = notebook_from_upload(revision_source)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                st.error(str(error))
                return
            source_notebook_name = revision_source.name
            revised_draft = draft_for_notebook(notebook, source_notebook_name)
            (
                restored_cells,
                pending_review_ids,
                removed_cells,
            ) = reviewed_cells_for_source(revised_draft, package["configuration"])
            st.caption(
                "Existing cells with the same Jupyter cell ID retain their settings. "
                "New or type-changed cells require review before export."
            )

    try:
        draft = draft_for_notebook(notebook, source_notebook_name)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        st.error(str(error))
        return
    initialise_review_state(
        draft,
        notebook,
        st,
        restored_cells=restored_cells,
        assignment_details=assignment_details,
        package_context=package_context,
        pending_review_ids=pending_review_ids,
        removed_cells=removed_cells,
    )
    notebook = editable_notebook(st)
    draft = draft_for_notebook(notebook, source_notebook_name)

    st.subheader("Assignment details")
    st.caption("Names are for teacher reference. Stable IDs are created automatically when you export.")
    details_left, details_right = st.columns(2)
    with details_left:
        course_name = st.text_input(
            "Course name", key="assignment_authoring_course_name"
        )
    with details_right:
        assignment_name = st.text_input(
            "Assignment name", key="assignment_authoring_assignment_name"
        )
    allow_extra_cells = st.checkbox(
        "Allow students to add extra cells",
        key="assignment_authoring_allow_extra_cells",
        help="Extra cells receive stable keys automatically when the prepared notebook is opened.",
    )

    reviewed_cells = [reviewed_cell_from_state(cell, st) for cell in draft["cells"]]
    errors_by_position = cell_validation_errors(reviewed_cells)
    source_revision_errors: list[str] = []
    pending_review_ids = set(
        st.session_state.get("assignment_authoring_pending_review_ids", set())
    )
    for draft_cell in draft["cells"]:
        if draft_cell.get("source_cell_id") in pending_review_ids:
            errors_by_position.setdefault(draft_cell["position"], []).append(
                "This externally added or type-changed cell needs review. Open it and select Apply changes."
            )
            source_revision_errors.append(
                f"Cell {draft_cell['position']} needs review after the external source update."
            )

    removed_cells = st.session_state.get("assignment_authoring_removed_cells", [])
    if removed_cells:
        removed_keys = ", ".join(
            str(cell.get("analytics_key", "unnamed cell")) for cell in removed_cells
        )
        st.warning(
            "The updated teacher source removes previously configured cells: "
            + removed_keys
        )
        removal_confirmed = st.checkbox(
            "I confirm that these configured cells should be removed from the next revision.",
            key="assignment_authoring_removed_cells_confirmed",
        )
        if not removal_confirmed:
            source_revision_errors.append(
                "Confirm the removal of previously configured cells before exporting."
            )

    quick_left, quick_middle, quick_right = st.columns([2, 2, 3])
    with quick_left:
        st.metric("Ready cells", len(draft["cells"]) - len(errors_by_position), f"of {len(draft['cells'])}")
    with quick_middle:
        st.metric("Needs attention", len(errors_by_position))
    with quick_right:
        find_text = st.text_input(
            "Find a cell",
            placeholder="Search content, key, section, or role",
        )
        matches = matching_cell_positions(draft["cells"], reviewed_cells, find_text)
        if find_text:
            if matches:
                if st.button(f"Go to first match (cell {matches[0]})", key="go_to_search_match"):
                    st.session_state["assignment_authoring_selected_position"] = matches[0]
                    st.rerun()
            else:
                st.caption("No matching cells.")

    st.subheader("Assignment notebook")
    st.caption(
        "Read the notebook as students will see it. Select a cell to edit its "
        "content, role, stable key, section, or exceptional permissions."
    )
    show_full_content = st.checkbox(
        "Show complete content for every cell",
        value=False,
        help="Off is faster for large notebooks. The selected cell always shows its full content.",
    )
    selected_position = st.session_state["assignment_authoring_selected_position"]
    if selected_position not in {cell["position"] for cell in draft["cells"]}:
        selected_position = draft["cells"][0]["position"] if draft["cells"] else None
        st.session_state["assignment_authoring_selected_position"] = selected_position

    navigation_left, navigation_middle, navigation_right = st.columns([1, 1, 5])
    with navigation_left:
        if st.button("← Previous", disabled=selected_position == 1, width="stretch"):
            st.session_state["assignment_authoring_selected_position"] = adjacent_position(
                [cell["position"] for cell in draft["cells"]], selected_position, -1
            )
            st.rerun()
    with navigation_middle:
        if st.button(
            "Next →",
            disabled=selected_position == len(draft["cells"]),
            width="stretch",
        ):
            st.session_state["assignment_authoring_selected_position"] = adjacent_position(
                [cell["position"] for cell in draft["cells"]], selected_position, 1
            )
            st.rerun()
    with navigation_right:
        st.caption(f"Selected cell {selected_position or '—'} of {len(draft['cells'])}")

    selected_cell = next(
        (cell for cell in draft["cells"] if cell["position"] == selected_position),
        None,
    )
    with st.sidebar:
        st.divider()
        if selected_cell is not None:
            render_selected_cell_inspector(selected_cell, notebook, st)

    for draft_cell, reviewed_cell in zip(draft["cells"], reviewed_cells):
        render_notebook_cell(
            draft_cell,
            reviewed_cell,
            draft_cell["position"] == selected_position,
            errors_by_position.get(draft_cell["position"], []),
            show_full_content,
            st,
        )

    reviewed_cells = [reviewed_cell_from_state(cell, st) for cell in draft["cells"]]

    reviewed = build_reviewed_config(
        draft,
        course_name,
        assignment_name,
        allow_extra_cells,
        reviewed_cells,
    )
    errors = validate_reviewed_config(reviewed)
    errors.extend(validate_notebook_matches_config(notebook, reviewed, source_notebook_name))
    errors.extend(source_revision_errors)

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

    st.success("The reviewed configuration is valid. The source notebook has not changed on disk.")
    config_json = json.dumps(reviewed, indent=2) + "\n"
    prepared = prepare_assignment(notebook, reviewed)
    source_path = Path(source_notebook_name)
    assignment_id = reviewed["assignment"]["id"] or source_path.stem
    package_context = st.session_state["assignment_authoring_package_context"]
    package_name = f"{assignment_id}_r{package_context['revision']}"
    package_notebook_name = f"{assignment_id}.ipynb"
    package_manifest = build_package_manifest(
        source_notebook_name,
        assignment_id,
        notebook,
        prepared,
        reviewed,
        package_context,
    )
    teacher_source_json = json.dumps(notebook, indent=1) + "\n"
    prepared_json = json.dumps(prepared, indent=1) + "\n"
    manifest_json = json.dumps(package_manifest, indent=2) + "\n"
    package_json = build_assignment_package(
        package_name,
        assignment_id,
        teacher_source_json,
        prepared_json,
        config_json,
        manifest_json,
    )
    st.download_button(
        "Download assignment package (.zip)",
        data=package_json,
        file_name=f"{package_name}.zip",
        mime="application/zip",
        type="primary",
    )
    st.caption(
        f"Revision {package_context['revision']} contains the clean teacher source, "
        "prepared notebook, configuration, manifest, and local-testing guide."
    )
    with st.expander("Individual downloads"):
        individual_left, individual_right = st.columns(2)
        with individual_left:
            st.download_button(
                "Teacher source notebook",
                data=teacher_source_json,
                file_name=TEACHER_SOURCE_FILENAME,
                mime="application/x-ipynb+json",
            )
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

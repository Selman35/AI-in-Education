# Assignment authoring tools

This folder contains standalone tools for a future teacher-facing assignment
authoring platform. Nothing in this folder changes the current JupyterLab
extension, pilot template, logging, or analytics workflow.

## Step 1: inspect a notebook

Create a deterministic draft configuration from an existing notebook:

```bash
python tools/assignment_authoring/scan_notebook.py \
  path/to/assignment.ipynb \
  --output path/to/assignment.assignment-draft.json
```

The input notebook remains unchanged. The JSON output is only a proposal for
later teacher review; it does not publish an assignment or apply locks.

Current deterministic suggestions are intentionally simple:

- Markdown headings begin a new proposed section;
- prompt-like Markdown cells are proposed as locked questions or instructions;
- non-empty code cells are proposed as locked but runnable starter code;
- empty code cells are proposed as editable code answers;
- empty Markdown cells are proposed as editable Markdown explanations;
- suggested keys include the current section, such as
  `task_1_optimisation_code`.

Each proposed cell shows its suggestion reason in the review page. These are
only starting points: the teacher remains responsible for every final role,
key, section, and permission.

Later steps may add an Ollama-based proposal layer and baseline creation.

## Step 2: review suggestions locally

Install the optional local-app dependency:

```bash
pip install --upgrade -r tools/assignment_authoring/requirements.txt
```

Then start the local browser app:

```bash
streamlit run tools/assignment_authoring/app.py
```

The page displays the entire notebook in reading order. Markdown is rendered
as students will see it and code is displayed as formatted source. Select a
cell to open its settings panel, where a teacher can edit its source, role,
stable key, and section. A role sets the normal student permissions; individual
permissions are available only under **Show advanced permissions** for
exceptional cells. Course and assignment IDs are generated automatically from
the names entered in the app.

Source edits affect only the session-only editable copy and the exported
prepared notebook; the uploaded file remains unchanged. Existing cell reviews
are preserved after source edits. Use **Refresh suggestion for this cell** only
when the deterministic suggestion should be recalculated for that cell.

Before export, the page shows a live summary of the reviewed notebook: total
cells, code and Markdown cell counts, section count, and the selected role
distribution. If any validation issue remains, it is listed and downloads stay
unavailable until it is resolved.

The summary identifies invalid cells and provides direct links to select them.

When review is valid, the primary download is one revisioned assignment ZIP,
such as `assignment_1_r1.zip`. It contains the clean `teacher_source.ipynb`,
prepared student notebook, preparation manifest, reviewed JSON configuration,
and `LOCAL_TEST.md`. Individual source and prepared notebooks remain available
as optional downloads. Teachers do not need to run the Step 3 command-line tool
when using the website. The uploaded notebook remains unchanged.

## Revise a downloaded assignment package

Choose **Revise an assignment package** in the website and upload a ZIP that
the website previously created. The package restores its course and assignment
details plus every approved cell role, analytics key, section, and permission.

To make structural changes in JupyterLab, edit the clean `teacher_source.ipynb`
from that package, then upload both the original ZIP and the edited source in
the revision flow. Cells with the same Jupyter cell ID keep their decisions;
new or type-changed cells require review, and removals require confirmation.
The next export receives the next revision number, for example
`assignment_1_r2.zip`.

Older website packages can also be reopened when their prepared notebook has a
recoverable baseline. Their next export is converted to the current package
format.

## Step 3: prepare an approved assignment

After reviewing a notebook, create a separate prepared copy with the approved
cell metadata:

```bash
python tools/assignment_authoring/prepare_assignment.py \
  path/to/assignment.ipynb \
  path/to/assignment.assignment-config.json
```

By default, this writes `assignment.prepared.ipynb` and
`assignment.prepared.manifest.json` next to the original notebook. The source
notebook and reviewed configuration remain unchanged. The tool stops if the
notebook no longer matches the reviewed configuration by filename, cell order,
cell IDs, cell types, or (for newly exported configurations) content
fingerprint.

The prepared notebook stores generic metadata under `assignment_authoring`.
It does not activate any new JupyterLab protections yet; adapting the
extension to enforce these generic policies will be considered after the pilot
study. The source notebook and current pilot-extension behaviour remain
unchanged.

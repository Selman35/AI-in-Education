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

- non-empty Markdown cells are proposed as locked text;
- non-empty code cells are proposed as locked but runnable starter code;
- empty code cells are proposed as editable code answers;
- empty Markdown cells are proposed as editable Markdown answers.

Later steps will add an Ollama-based proposal layer, a Streamlit review screen,
generic publishing/provisioning, baseline creation, and dynamic extension
support for approved per-cell policies.

## Step 2: review suggestions locally

Install the optional local-app dependency:

```bash
pip install --upgrade -r tools/assignment_authoring/requirements.txt
```

Then start the local browser app:

```bash
streamlit run tools/assignment_authoring/app.py
```

When the page opens, it explains the short review process. For every cell, the
teacher can view its complete original content, then review its proposed key,
role, and section before downloading a reviewed JSON configuration. A role
sets the normal student permissions; individual permissions are available only
under **Show advanced permissions** for exceptional cells. Course and
assignment IDs are generated automatically from the names entered in the app.
It does not modify the uploaded notebook, call an LLM, publish an assignment,
or change the current extension behaviour.

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
It deliberately does not activate any new JupyterLab protections yet; dynamic
extension support is a later step.

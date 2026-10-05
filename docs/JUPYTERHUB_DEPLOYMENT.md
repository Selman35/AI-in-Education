# JupyterHub pilot deployment

This document describes the expected file layout and integration points for the
Pilot Study 1 JupyterLab extension deployment. It is intended for the JupyterHub
and storage administrators.

## Expected locations

Student notebooks are provisioned separately from research logs:

```text
/srv/scistor/ai_in_education/
├── students/
│   └── <vunetid>/
│       └── pilot_study_1.ipynb
└── logs/
    ├── changes/
    ├── versions/
    └── cell_versions/
```

For a user authenticated as `aaa100`, the extension writes these files:

```text
logs/changes/aaa100__pilot_study_1.ipynb.log
logs/versions/aaa100__pilot_study_1.ipynb.log
logs/cell_versions/aaa100__pilot_study_1.ipynb.jsonl
```

The logical `logs/` path must resolve to:

```text
/srv/scistor/ai_in_education/logs/
```

## Extension behaviour

The extension reads the authenticated JupyterHub username from the JupyterLab
page configuration value `hubUser`. It validates the value before including it
in a filename. The username is not requested from the student and is not read
from a notebook cell or notebook filename.

The extension creates `logs/`, `logs/changes/`, `logs/versions/`, and
`logs/cell_versions/` when they do not already exist. It writes chronological
activity/diff logs, human-readable source snapshots, and structured cell
snapshots in the existing repository formats.

Outside JupyterHub, when `hubUser` is unavailable, the extension continues to
use `internal_diff_logs/` for local development.

## Requirements to confirm before deployment

- JupyterLab exposes the authenticated username as `hubUser`.
- Student workspaces are provisioned under `students/<vunetid>/`.
- The JupyterLab Contents API resolves the extension's logical `logs/` path to
  `/srv/scistor/ai_in_education/logs/`.
- The extension can create and append files in the three log categories.
- Students can work normally in their own workspace but cannot browse, edit, or
  delete research logs.
- Authorised researchers can access the raw logs and generated analytics.

The current extension writes through JupyterLab's Contents API. The staging
deployment must therefore verify that the chosen storage and permission model
supports logging without exposing research data to students.

## Notebook provisioning

Research staff can provision a participant notebook into a student workspace
with:

```bash
python tools/create_pilot_notebook.py participant_001 \
  --student-id aaa100 \
  --student-workspaces-root /srv/scistor/ai_in_education/students
```

This creates:

```text
/srv/scistor/ai_in_education/students/aaa100/pilot_study_1.ipynb
```

`participant_001` is study metadata. `aaa100` is the authenticated university
user used in the production log filename.

## Analytics

Research staff can generate reports and plots from the shared logging root:

```bash
python tools/generate_analytics_and_plots.py \
  --log-root /srv/scistor/ai_in_education/logs \
  --analytics-dir /srv/scistor/ai_in_education/analytics \
  --plots-dir /srv/scistor/ai_in_education/plots
```

## Staging acceptance check

Before participant sessions, test with one staging account:

1. Open the provisioned notebook and edit an answer cell.
2. Run one successful cell and one cell that produces an error.
3. Confirm the three `aaa100__pilot_study_1` log files are created in the
   intended categories.
4. Refresh JupyterLab, make another edit, and confirm the log continues with
   the next save number.
5. Generate analytics and confirm one coherent row is produced for the test
   notebook.
6. Confirm the staging student account cannot browse or change the log data.

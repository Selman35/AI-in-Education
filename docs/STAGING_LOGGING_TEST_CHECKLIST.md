# Staging logging test checklist

Use this checklist with one dedicated staging account and a newly provisioned
pilot notebook before participant sessions. It tests browser and JupyterHub
behaviour that cannot be reproduced by the repository's Python regression
tests.

## Before starting

- Confirm the extension is enabled in JupyterLab.
- Confirm `loggingRoot` is centrally set to
  `/srv/scistor/ai_in_education/logs`.
- Use a test account with a known VUnetID, for example `aaa100`.
- Open that user's `pilot_study_1.ipynb` notebook.
- Record the start time and use a fresh or clearly named test notebook so its
  files can be identified easily.

## Notebook interaction checks

Perform each action once, in this order.

| Action | Expected raw-log evidence |
| --- | --- |
| Click an answer cell. | A `focusin` event with its stable cell label. |
| Type several characters, then continue typing within two seconds. | At most one immediate `edit` event for that cell during the debounce period. |
| Wait more than two seconds and type again. | A new `edit` event. |
| Copy selected text. | A `copy` event with a positive `length`. |
| Cut selected text. | A `cut` event with a positive `length`. |
| Paste text into an answer cell. | A `paste` event with a positive `length`. |
| Run valid code. | An `execute` event with `status: success`. |
| Run deliberately invalid code. | An `execute` event with `status: error`. |
| Move to another cell. | `focusout` for the prior cell and `focusin` for the new cell. |
| Leave the browser/tab, then return. | `assignment-inactive`, then `assignment-active`. |
| Make a change and wait for the configured periodic interval. | A `save N` entry and new version/cell-version snapshot. |
| Refresh JupyterLab, edit again, and trigger a save. | Save numbering continues rather than restarting with another `initial save`. |

Clipboard text should not appear in the raw event log. The event type and its
character length are expected. An external paste can be recorded as a paste,
but its external source cannot be proven by the extension.

## Assignment-template checks

- Attempt to edit, delete, move, or cut a protected instruction cell. Its
  content and position should be restored or the attempted action should be
  blocked.
- Confirm that designated student-answer cells remain editable.
- Attempt to insert a new cell. The fixed template structure should not retain
  an unlabelled inserted cell.

## Server file checks

From an authorised researcher account, confirm the following files exist:

```bash
find /srv/scistor/ai_in_education/logs -type f
```

For staging user `aaa100`, the expected files are similar to:

```text
logs/changes/aaa100__pilot_study_1.ipynb.log
logs/versions/aaa100__pilot_study_1.ipynb.log
logs/cell_versions/aaa100__pilot_study_1.ipynb.jsonl
```

Confirm that the staging student cannot browse, edit, or delete the shared
`logs` directory.

## Analytics checks

Run:

```bash
python tools/generate_analytics_and_plots.py \
  --log-root /srv/scistor/ai_in_education/logs \
  --analytics-dir /srv/scistor/ai_in_education/analytics \
  --plots-dir /srv/scistor/ai_in_education/plots
```

Confirm that `student_analytics.csv`, `cell_mapping.csv`,
`cell_execution_summary.csv`, `idle_events.csv`, `active_sessions.csv`, and
`clipboard_events.csv` are produced. Verify the staging notebook has one
overall row, correct copy/cut/paste counts, one successful execution, one
failed execution, and readable stable cell labels.

## Pass condition

The staging test passes only when the expected events, source snapshots, and
analytics outputs agree with the actions above and the student account cannot
access protected research logs.

# Notebook analytics workspace

This is a local Streamlit app for researchers. It turns a ZIP of raw notebook
logs into the existing CSV analytics reports, or opens existing analytics CSVs,
without requiring command-line analysis steps.

## Run locally

From the repository root, activate an environment with the dependencies in
`requirements.txt`, then run:

```bash
streamlit run tools/analytics_dashboard/app.py
```

The app runs on your computer. Uploaded raw logs are extracted only into a
temporary directory for the current browser session and are not sent to a
service or retained by the app.

## Upload data

Choose one input mode:

- Upload one ZIP containing `changes/` and, where available, sibling
  `versions/` and `cell_versions/` folders. The app generates the analytics
  reports and standard plots.
- Upload one or more existing CSV analytics reports. A file named
  `student_analytics.csv` enables the overview and core preset charts; event
  CSVs add their relevant views.

To compare study conditions, optionally upload a mapping CSV:

```csv
participant_id,group
aaa101,non_ai
bbb101,ai
```

The participant ID is the part of a `student` value before `__`. For example,
`aaa101__pilot_study_1` maps to `aaa101`.

## Results

The workspace provides preset views for working time, idle time, execution
outcomes, clipboard distributions, and event tables. The custom chart builder
works with any compatible uploaded CSV columns. Downloaded result packages
contain generated CSVs, available PNG plots, and a manifest, but never the raw
uploaded logs.

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

The workspace provides a guided workflow with these views:

- **Overview:** loaded datasets, overall participant analytics, and a
  downloadable cohort summary for any numeric measure.
- **Data quality:** structural warnings for missing participant labels,
  duplicate overall participant rows, empty reports, and negative numeric
  values. It checks report integrity, not the validity of research conclusions.
- **Preset views:** established working-time, idle-time, execution-outcome, and
  clipboard-distribution views. Their chart data and Vega-Lite specifications
  can be downloaded individually.
- **Event reports:** searchable event tables with a visible-column chooser and
  a download for the currently displayed rows.
- **Custom chart:** a flexible chart builder for any compatible report columns.

The custom chart builder
starts with a useful chart based on the selected data and updates the preview
as settings change. It explains what each chart type needs and lets you choose
the source columns, filters, aggregation, title, caption, axis labels, and
colours. Download each custom chart as a PNG, the exact plotted table as CSV,
or its Vega-Lite chart specification as JSON.

Chart requirements are kept deliberately simple:

- **Bar / grouped bar:** a category and either a numeric value or row count.
- **Scatter:** two numeric columns.
- **Histogram:** one numeric value.
- **Box plot:** one category and one numeric value.
- **Line:** a numeric value and an ordered category; timestamps work best on
  the x-axis.

Downloaded result packages contain generated CSVs, available PNG plots, and a
manifest, but never the raw uploaded logs.

Use **Start a new analysis** to remove uploads and derived results from the
current browser session. This does not alter any local source files.

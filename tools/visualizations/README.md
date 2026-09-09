# Analytics visualizations

This folder contains reproducible plot scripts. Each script reads generated CSV
reports from `internal_diff_logs/changes/` and writes an image to
`internal_diff_logs/plots/`. Plot scripts do not modify raw logs or analytics.

## Plot 1: Estimated working time by student

```bash
python tools/visualizations/plot_working_time.py
```

The script reads `student_analytics.csv` and creates:

```text
internal_diff_logs/plots/estimated_working_time_by_student.png
```

It visualizes `total_working_duration` in minutes. This metric is an estimate
of observable work: it counts intervals shorter than 45 seconds between
meaningful student actions.

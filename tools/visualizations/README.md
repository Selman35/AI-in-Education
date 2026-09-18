# Analytics plots

This folder contains the plot scripts used by the analytics workflow. The
scripts read CSV reports from `internal_diff_logs/analytics/` and write PNG
images to `internal_diff_logs/plots/`. They do not modify raw logs.

From the repository root, regenerate all reports and plots with:

```bash
python tools/generate_analytics_and_plots.py
```

The current workflow creates:

| Plot | Output file |
| --- | --- |
| Estimated working time by student | `estimated_working_time_by_student.png` |
| Total idle time by student | `idle_time_by_student.png` |
| Total, successful, and failed cell runs by student | `execution_outcomes_by_student.png` |
| Copy, paste, and cut length distribution for each student | `clipboard_distribution_<student>.png` |

Each plot script can also be run directly. For example:

```bash
python tools/visualizations/plot_working_time.py
python tools/visualizations/plot_clipboard_distribution.py --student clipboard_test
```

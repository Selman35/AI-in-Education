"""Create Plot 1: estimated working time by student/notebook.

The script reads the generated analytics CSV and writes a PNG chart. It does
not modify the source logs or analytics CSV files.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

# Use a writable cache location on shared or restricted installations.
os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


DEFAULT_INPUT = Path("internal_diff_logs/analytics/student_analytics.csv")
DEFAULT_OUTPUT = Path(
    "internal_diff_logs/plots/estimated_working_time_by_student.png"
)


def load_working_time_data(path: Path) -> pd.DataFrame:
    """Load and validate the two columns required for the chart."""
    data = pd.read_csv(path)
    data = data.drop(columns=["Unnamed: 0"], errors="ignore")
    required_columns = {"student", "total_working_duration"}
    missing_columns = required_columns - set(data.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Missing required analytics column(s): {missing}")

    plot_data = data[["student", "total_working_duration"]].copy()
    plot_data["total_working_duration"] = pd.to_numeric(
        plot_data["total_working_duration"], errors="coerce"
    )
    plot_data = plot_data.dropna(subset=["student", "total_working_duration"])
    if plot_data.empty:
        raise ValueError("No working-duration data is available to plot.")

    plot_data["working_minutes"] = plot_data["total_working_duration"] / 60
    return plot_data.sort_values("working_minutes", ascending=True)


def create_plot(data: pd.DataFrame, output_path: Path) -> None:
    """Write a labelled horizontal bar chart of working minutes."""
    figure_height = max(3.5, 0.55 * len(data) + 1.5)
    figure, axis = plt.subplots(figsize=(10, figure_height))

    bars = axis.barh(
        data["student"],
        data["working_minutes"],
        color="#377eb8",
        edgecolor="none",
    )
    axis.set_title("Estimated Working Time by Student", pad=14, weight="bold")
    axis.set_xlabel("Estimated working time (minutes)")
    axis.set_ylabel("Student / notebook")
    axis.xaxis.grid(True, color="#d9d9d9", linewidth=0.8)
    axis.set_axisbelow(True)
    axis.spines[["top", "right", "left"]].set_visible(False)

    maximum_minutes = data["working_minutes"].max()
    label_offset = max(maximum_minutes * 0.01, 0.05)
    axis.set_xlim(0, maximum_minutes * 1.15 if maximum_minutes else 1)
    for bar, minutes in zip(bars, data["working_minutes"]):
        axis.text(
            bar.get_width() + label_offset,
            bar.get_y() + bar.get_height() / 2,
            f"{minutes:.1f} min",
            va="center",
            fontsize=9,
        )

    figure.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an estimated working-time chart from student analytics."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Analytics CSV to read (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"PNG file to create (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    create_plot(load_working_time_data(args.input), args.output)
    print(f"Saved plot to {args.output}")


if __name__ == "__main__":
    main()

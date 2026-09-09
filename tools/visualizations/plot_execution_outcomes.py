"""Create Plot 3: successful and failed cell runs by student/notebook."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


DEFAULT_INPUT = Path("internal_diff_logs/changes/student_analytics.csv")
DEFAULT_OUTPUT = Path("internal_diff_logs/plots/execution_outcomes_by_student.png")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a cell-run success/error chart from student analytics."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    data = pd.read_csv(args.input).drop(columns=["Unnamed: 0"], errors="ignore")
    columns = {"student", "execution_status_success", "execution_status_error"}
    missing = columns - set(data.columns)
    if missing:
        raise ValueError(f"Missing required analytics column(s): {', '.join(sorted(missing))}")

    data = data[list(columns)].copy()
    data["successful_runs"] = pd.to_numeric(
        data["execution_status_success"], errors="coerce"
    ).fillna(0)
    data["error_runs"] = pd.to_numeric(
        data["execution_status_error"], errors="coerce"
    ).fillna(0)
    data["total_runs"] = data["successful_runs"] + data["error_runs"]
    data = data.sort_values("total_runs", ascending=True)

    figure_height = max(3.5, 0.55 * len(data) + 1.5)
    figure, axis = plt.subplots(figsize=(10, figure_height))
    positions = np.arange(len(data))
    bar_height = 0.24
    axis.barh(
        positions + bar_height,
        data["total_runs"],
        height=bar_height,
        color="#377eb8",
        label="Total runs",
    )
    axis.barh(
        positions,
        data["successful_runs"],
        height=bar_height,
        color="#4daf4a",
        label="Successful runs",
    )
    axis.barh(
        positions - bar_height,
        data["error_runs"],
        height=bar_height,
        color="#e41a1c",
        label="Error runs",
    )

    axis.set_title("Cell Runs by Student", pad=14, weight="bold")
    axis.set_xlabel("Number of cell runs")
    axis.set_ylabel("Student / notebook")
    axis.set_yticks(positions, data["student"])
    axis.xaxis.grid(True, color="#d9d9d9", linewidth=0.8)
    axis.set_axisbelow(True)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.legend(loc="lower right")

    maximum_runs = data["total_runs"].max()
    axis.set_xlim(0, maximum_runs * 1.18 if maximum_runs else 1)

    figure.tight_layout()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=180, bbox_inches="tight")
    plt.close(figure)
    print(f"Saved plot to {args.output}")


if __name__ == "__main__":
    main()

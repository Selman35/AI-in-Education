"""Create Plot 2: total idle time by student/notebook."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


DEFAULT_INPUT = Path("internal_diff_logs/analytics/student_analytics.csv")
DEFAULT_OUTPUT = Path("internal_diff_logs/plots/idle_time_by_student.png")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a total idle-time chart from student analytics."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    data = pd.read_csv(args.input).drop(columns=["Unnamed: 0"], errors="ignore")
    required = {"student", "total_idle_time"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"Missing required analytics column(s): {', '.join(sorted(missing))}")

    data = data[["student", "total_idle_time"]].copy()
    data["total_idle_time"] = pd.to_numeric(
        data["total_idle_time"], errors="coerce"
    ).fillna(0)
    data["idle_minutes"] = data["total_idle_time"] / 60
    data = data.sort_values("idle_minutes", ascending=True)

    figure_height = max(3.5, 0.55 * len(data) + 1.5)
    figure, axis = plt.subplots(figsize=(10, figure_height))
    bars = axis.barh(
        data["student"],
        data["idle_minutes"],
        color="#f781bf",
        edgecolor="none",
    )
    axis.set_title("Total Idle Time by Student", pad=14, weight="bold")
    axis.set_xlabel("Total idle time (minutes)")
    axis.set_ylabel("Student / notebook")
    axis.xaxis.grid(True, color="#d9d9d9", linewidth=0.8)
    axis.set_axisbelow(True)
    axis.spines[["top", "right", "left"]].set_visible(False)

    maximum_minutes = data["idle_minutes"].max()
    label_offset = max(maximum_minutes * 0.01, 0.05)
    axis.set_xlim(0, maximum_minutes * 1.15 if maximum_minutes else 1)
    for bar, minutes in zip(bars, data["idle_minutes"]):
        axis.text(
            bar.get_width() + label_offset,
            bar.get_y() + bar.get_height() / 2,
            f"{minutes:.1f} min",
            va="center",
            fontsize=9,
        )

    figure.tight_layout()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=180, bbox_inches="tight")
    plt.close(figure)
    print(f"Saved plot to {args.output}")


if __name__ == "__main__":
    main()

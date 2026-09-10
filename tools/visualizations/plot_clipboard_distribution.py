"""Create per-student copy, paste, and cut length distribution plots."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


DEFAULT_INPUT = Path("internal_diff_logs/changes/clipboard_events.csv")
DEFAULT_OUTPUT_DIR = Path("internal_diff_logs/plots")
EVENT_TYPES = ["copy", "paste", "cut"]
EVENT_COLORS = {"copy": "#377eb8", "paste": "#4daf4a", "cut": "#e41a1c"}


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


def create_plot(data: pd.DataFrame, student: str, output_path: Path) -> None:
    """Create one horizontal box-and-dot plot for a student's clipboard events."""
    figure, axis = plt.subplots(figsize=(11, 5))
    positions = {event: index + 1 for index, event in enumerate(EVENT_TYPES)}
    rng = np.random.default_rng(42)
    maximum_length = max(data["character_length"].max(), 1)

    for event in EVENT_TYPES:
        values = data.loc[data["event"] == event, "character_length"].to_numpy()
        position = positions[event]
        if len(values) == 0:
            axis.text(
                maximum_length * 0.03,
                position,
                "No events",
                va="center",
                color="#666666",
            )
            continue

        box = axis.boxplot(
            values,
            positions=[position],
            vert=False,
            widths=0.45,
            patch_artist=True,
            showfliers=False,
        )
        for patch in box["boxes"]:
            patch.set_facecolor(EVENT_COLORS[event])
            patch.set_alpha(0.35)

        jitter = rng.uniform(-0.12, 0.12, len(values))
        axis.scatter(
            values,
            np.full(len(values), position) + jitter,
            color=EVENT_COLORS[event],
            edgecolor="white",
            linewidth=0.5,
            s=45,
            zorder=3,
        )

        mean = values.mean()
        standard_deviation = values.std(ddof=1) if len(values) > 1 else 0.0
        axis.text(
            maximum_length * 1.04,
            position,
            f"n={len(values)}, mean={mean:.1f}, SD={standard_deviation:.1f}",
            va="center",
            fontsize=9,
        )

    axis.set_yticks(list(positions.values()), [event.title() for event in EVENT_TYPES])
    axis.set_ylim(0.5, len(EVENT_TYPES) + 0.5)
    axis.invert_yaxis()
    axis.set_xlim(0, maximum_length * 1.42)
    axis.set_xlabel("Clipboard length (characters)")
    axis.set_ylabel("Clipboard event")
    axis.set_title(f"Clipboard Length Distribution — {student}", pad=14, weight="bold")
    axis.xaxis.grid(True, color="#d9d9d9", linewidth=0.8)
    axis.set_axisbelow(True)
    axis.spines[["top", "right", "left"]].set_visible(False)

    figure.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create clipboard-length distribution plots by student."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--student",
        help="Student/notebook to plot. Omit to create a plot for every student.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    data = pd.read_csv(args.input)
    required = {"student", "event", "character_length"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"Missing required clipboard column(s): {', '.join(sorted(missing))}")
    data = data[data["event"].isin(EVENT_TYPES)].copy()
    data["character_length"] = pd.to_numeric(data["character_length"], errors="coerce")
    data = data.dropna(subset=["student", "character_length"])

    students = [args.student] if args.student else sorted(data["student"].unique())
    for student in students:
        student_data = data[data["student"] == student]
        if student_data.empty:
            print(f"No clipboard events found for {student}; no plot created.")
            continue
        output_path = args.output_dir / f"clipboard_distribution_{safe_filename(student)}.png"
        create_plot(student_data, student, output_path)
        print(f"Saved plot to {output_path}")


if __name__ == "__main__":
    main()

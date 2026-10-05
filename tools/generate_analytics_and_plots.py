"""Generate all analytics reports and available plots with one command."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = REPOSITORY_ROOT / "internal_diff_logs" / "changes"
DEFAULT_ANALYTICS_DIR = REPOSITORY_ROOT / "internal_diff_logs" / "analytics"
DEFAULT_PLOTS_DIR = REPOSITORY_ROOT / "internal_diff_logs" / "plots"


def run(command: list[str]) -> None:
    """Run one report step and stop immediately if it fails."""
    cache_dir = Path(tempfile.gettempdir()) / "jupyterlab_analytics_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.setdefault("MPLCONFIGDIR", str(cache_dir / "matplotlib"))
    environment.setdefault("XDG_CACHE_HOME", str(cache_dir))
    print("\nRunning:", " ".join(command), flush=True)
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True, env=environment)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate all notebook analytics reports and plots."
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        help="Changes directory to analyse; overrides --log-root.",
    )
    parser.add_argument(
        "--log-root",
        type=Path,
        help="Logging root containing changes/, versions/, and cell_versions/.",
    )
    parser.add_argument("--analytics-dir", type=Path, default=DEFAULT_ANALYTICS_DIR)
    parser.add_argument("--plots-dir", type=Path, default=DEFAULT_PLOTS_DIR)
    args = parser.parse_args()

    if args.log_dir and args.log_root:
        parser.error("Use either --log-dir or --log-root, not both.")
    args.log_dir = args.log_dir or (
        args.log_root / "changes" if args.log_root else DEFAULT_LOG_DIR
    )

    args.analytics_dir.mkdir(parents=True, exist_ok=True)
    args.plots_dir.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    run([
        python,
        "tools/analytics/compute_analytics.py",
        str(args.log_dir),
        str(args.analytics_dir),
    ])

    analytics_csv = args.analytics_dir / "student_analytics.csv"
    run([
        python,
        "tools/visualizations/plot_working_time.py",
        "--input", str(analytics_csv),
        "--output", str(args.plots_dir / "estimated_working_time_by_student.png"),
    ])
    run([
        python,
        "tools/visualizations/plot_idle_time.py",
        "--input", str(analytics_csv),
        "--output", str(args.plots_dir / "idle_time_by_student.png"),
    ])
    run([
        python,
        "tools/visualizations/plot_execution_outcomes.py",
        "--input", str(analytics_csv),
        "--output", str(args.plots_dir / "execution_outcomes_by_student.png"),
    ])
    run([
        python,
        "tools/visualizations/plot_clipboard_distribution.py",
        "--input", str(args.analytics_dir / "clipboard_events.csv"),
        "--output-dir", str(args.plots_dir),
    ])

    print(f"\nAnalytics reports: {args.analytics_dir}")
    print(f"Plots: {args.plots_dir}")


if __name__ == "__main__":
    main()

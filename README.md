# Jupyterlab Autosave on Focus Change

[![PyPi Version](https://img.shields.io/pypi/v/jupyterlab_autosave_on_focus_change.svg)](https://pypi.org/project/jupyterlab_autosave_on_focus_change/)
[![Supported Python Versions](https://img.shields.io/pypi/pyversions/jupyterlab_autosave_on_focus_change.svg)](https://pypi.org/project/jupyterlab_autosave_on_focus_change/)
![Github Actions Status](https://github.com/s-weigand/jupyterlab_autosave_on_focus_change/workflows/Build/badge.svg)
[![Binder](https://mybinder.org/badge_logo.svg)](https://mybinder.org/v2/gh/s-weigand/jupyterlab_autosave_on_focus_change/main?urlpath=lab)
[![Docs](https://img.shields.io/badge/documentation-yes-brightgreen.svg)](https://s-weigand.github.io/jupyterlab_autosave_on_focus_change/)

<!-- prettier-ignore-start -->
<!-- ALL-CONTRIBUTORS-BADGE:START - Do not remove or modify this section -->
[![All Contributors](https://img.shields.io/badge/all_contributors-3-orange.svg?style=flat-square)](#contributors-)
<!-- ALL-CONTRIBUTORS-BADGE:END -->
<!-- prettier-ignore-end -->

A Jupyterlab extension to autosave files on focus change.

<br>

<img style="display: block; margin: auto;" src="https://github.com/s-weigand/jupyterlab_autosave_on_focus_change/blob/main/assets/demo.gif?raw=true">

Who doesn't know this scenario, you changed some code or input file and the changes don't apply because (**again!**)
you forgot to save the file.

This is where editor settings to save files when the tab (or the whole editor) loose focus come in super handy.

This extension aims to do the same for jupyterlab as the following setting in VS-Code.

```json
  "files.autoSave": "onFocusChange",
```

## Features

- Autosave on focus change
- Periodic autosave of changed notebooks (30 seconds by default)
- De-/Activation via Settings Menu
- File exclusion with glob patterns
- Per-cell edit, clipboard, and execution logging
- Analytics for code changes, idle time, active sessions, and cell execution

## Requirements

- JupyterLab >= 4.0

## Install

```bash
pip install jupyterlab-autosave-on-focus-change
```

## Notebook analytics workflow

This project extends the autosave extension with local notebook analytics. It
records student activity, turns it into CSV reports, and creates plots for
review. The reports describe how a notebook was worked on; they are not an
automatic score or judgement of a student.

### First-time setup with Conda

The following commands create an environment suitable for running and
developing this project:

```bash
conda create -n jupyterlab_autosave_on_focus_change-demo \
  -c conda-forge python=3.10 jupyterlab=4.5 nodejs=20
conda activate jupyterlab_autosave_on_focus_change-demo
```

Install the libraries used to create the analytics reports and plots:

```bash
conda install -c conda-forge pandas matplotlib numpy
```

From the repository root, install the local package, link the extension to
JupyterLab, build it, and start JupyterLab:

```bash
export PATH="$CONDA_PREFIX/bin:$PATH"
pip install -e .
jupyter labextension develop . --overwrite
jlpm run build
jupyter lab
```

If you change a TypeScript file in `src/`, run `jlpm run build` again and refresh
the JupyterLab page. Restart JupyterLab if the change does not appear.

### Configure collection

In JupyterLab, open:

`Settings → Settings Editor → Autosave on Focus Change`

For normal data collection, enable the extension and set **Periodic autosave
interval (seconds)** to `30`. A value of `0` disables periodic autosave.

Periodic saving uses the same normal save and logging path as focus-change
saving. It saves only notebooks with unsaved changes, so an unchanged notebook
does not create a new snapshot every 30 seconds.

### What is recorded

Events are connected to readable notebook locations such as `cell 1` and
`cell 2`. The extension records edits, copy/cut/paste events, cell executions
(including success or error), focus activity, assignment-window activity, and
saved code changes.

The files are written locally under:

```text
internal_diff_logs/
├── changes/        # raw event logs and saved line diffs
├── versions/       # whole-notebook source snapshots
├── cell_versions/  # structured per-cell source snapshots
├── analytics/      # generated CSV reports
└── plots/          # generated plot images
```

These folders can contain student work and activity data. They are ignored by
Git, but `.gitignore` does not restrict access. Use filesystem and Jupyter
server permissions if students must not be able to view or modify the data.

### Create the analytics reports

After notebook activity has been recorded, run the following from the
repository root:

```bash
conda activate jupyterlab_autosave_on_focus_change-demo
python tools/generate_analytics_and_plots.py
```

This command regenerates the reports below and all available plots:

| File | What it contains |
| --- | --- |
| `student_analytics.csv` | One overall analytics row per notebook. |
| `cell_mapping.csv` | Readable cell labels and their logged references. |
| `cell_execution_summary.csv` | Execution, success, and error counts for each cell. |
| `idle_events.csv` | Each detected period of inactivity. |
| `active_sessions.csv` | Each detected active editing session. |
| `clipboard_events.csv` | One row per copy, cut, or paste event. |

Reports are written to `internal_diff_logs/analytics/`. Images are written to
`internal_diff_logs/plots/`.

### Understanding the main metrics

The report includes activity, execution, line-change, and character-change
measures.

`total_working_duration` estimates observable work. It counts the time between
meaningful actions when the gap is under 45 seconds. Meaningful actions are
editing, copying, cutting, pasting, and running a cell.

`added_lines` and `removed_lines` count actual inserted and deleted source lines,
while `modified_lines` counts changed existing lines. Character-level measures
(`char_diff_added_characters` and `char_diff_removed_characters`) compare
successive code snapshots, so a one-character correction is counted as one
character rather than as a complete line replacement. Newly recorded notebooks
also include equivalent `cell_N_*` character and line-change measures for each
cell.

An idle episode starts after 120 seconds without a meaningful action. Editing,
copying, cutting, pasting, and running a cell are meaningful actions; focus
events and automatic saves are not. Only the time after the first 120 seconds
is counted as idle time. For example, 185 seconds without activity produces
65 seconds of recorded idle time.

An active editing session contains meaningful actions less than five minutes
apart. Leaving the assignment closes the session, and time away is not counted
as idle time.

### Quick check

To verify a new installation, create a fresh notebook and:

1. Type code and wait about 35 seconds without changing focus.
2. Run one successful cell and one cell that produces an error, for example
   `1 / 0`.
3. Change one character in a cell and wait for another save.
4. Leave the notebook visible but untouched for 185 seconds, then edit again.
5. Run the workflow command and inspect the CSV files in
   `internal_diff_logs/analytics` and the images in `internal_diff_logs/plots`.

The test should show a periodic save, correct execution results, character
changes, and an idle event of roughly 65 seconds for the 185-second pause. It
should also create working-time, idle-time, execution, and clipboard plots.

## Contributing

### Development install

Note: You will need NodeJS to build the extension package.

The `jlpm` command is JupyterLab's pinned version of
[yarn](https://yarnpkg.com/) that is installed with JupyterLab. You may use
`yarn` or `npm` in lieu of `jlpm` below.

```bash
# Clone the repo to your local environment
# Change directory to the jupyterlab_autosave_on_focus_change directory
# Install package in development mode
pip install -e .
# Link your development version of the extension with JupyterLab
jupyter labextension develop . --overwrite
# Rebuild extension Typescript source after making changes
jlpm run build
```

You can watch the source directory and run JupyterLab at the same time in different terminals to watch for changes in the extension's source and automatically rebuild the extension.

```bash
# Watch the source directory in one terminal, automatically rebuilding when needed
jlpm run watch
# Run JupyterLab in another terminal
jupyter lab
```

With the watch command running, every saved change will immediately be built locally and available in your running JupyterLab. Refresh JupyterLab to load the change in your browser (you may need to wait several seconds for the extension to be rebuilt).

By default, the `jlpm run build` command generates the source maps for this extension to make it easier to debug using the browser dev tools. To also generate source maps for the JupyterLab core extensions, you can run the following command:

```bash
jupyter lab build --minimize=False
```

### Uninstall

```bash
pip uninstall jupyterlab-autosave-on-focus-change
```

## Contributors ✨

Thanks goes to these wonderful people ([emoji key](https://allcontributors.org/docs/en/emoji-key)):

<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->
<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->
<table>
  <tbody>
    <tr>
      <td align="center" valign="top" width="14.28%"><a href="https://github.com/s-weigand"><img src="https://avatars.githubusercontent.com/u/9513634?v=4?s=100" width="100px;" alt="Sebastian Weigand"/><br /><sub><b>Sebastian Weigand</b></sub></a><br /><a href="https://github.com/s-weigand/jupyterlab_autosave_on_focus_change/commits?author=s-weigand" title="Code">💻</a> <a href="#ideas-s-weigand" title="Ideas, Planning, & Feedback">🤔</a> <a href="#maintenance-s-weigand" title="Maintenance">🚧</a> <a href="#projectManagement-s-weigand" title="Project Management">📆</a> <a href="#infra-s-weigand" title="Infrastructure (Hosting, Build-Tools, etc)">🚇</a> <a href="https://github.com/s-weigand/jupyterlab_autosave_on_focus_change/commits?author=s-weigand" title="Tests">⚠️</a> <a href="https://github.com/s-weigand/jupyterlab_autosave_on_focus_change/commits?author=s-weigand" title="Documentation">📖</a></td>
      <td align="center" valign="top" width="14.28%"><a href="https://www.wumengyan.com/"><img src="https://avatars.githubusercontent.com/u/85606983?v=4?s=100" width="100px;" alt="mengyanw"/><br /><sub><b>mengyanw</b></sub></a><br /><a href="https://github.com/s-weigand/jupyterlab_autosave_on_focus_change/commits?author=mengyanw" title="Code">💻</a> <a href="#maintenance-mengyanw" title="Maintenance">🚧</a></td>
      <td align="center" valign="top" width="14.28%"><a href="https://github.com/lahwaacz"><img src="https://avatars.githubusercontent.com/u/1289205?v=4?s=100" width="100px;" alt="Jakub Klinkovský"/><br /><sub><b>Jakub Klinkovský</b></sub></a><br /><a href="https://github.com/s-weigand/jupyterlab_autosave_on_focus_change/issues?q=author%3Alahwaacz" title="Bug reports">🐛</a></td>
    </tr>
  </tbody>
</table>

<!-- markdownlint-restore -->
<!-- prettier-ignore-end -->

<!-- ALL-CONTRIBUTORS-LIST:END -->

This project follows the [all-contributors](https://github.com/all-contributors/all-contributors) specification. Contributions of any kind welcome!

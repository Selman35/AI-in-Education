import { ICellModel } from '@jupyterlab/cells';
import { INotebookTracker, NotebookPanel } from '@jupyterlab/notebook';
import * as nbformat from '@jupyterlab/nbformat';

const COURSE_TAG = 'course-information';
const NAME_TAG = 'identity-entry';
const PROTECTED_TAGS = new Set([
  'assignment-header',
  'assignment-instructions',
  'course-information',
  'student-instructions',
  'identity-entry'
]);

interface IProtectedCell {
  id: string;
  index: number;
  content: nbformat.ICell;
  allowSourceEdit: boolean;
  allowExecution: boolean;
}

interface ITemplateState {
  panel: NotebookPanel;
  protectedCells: Map<string, IProtectedCell>;
  initialCellIds: Set<string>;
  watchedEditableCellIds: Set<string>;
  reconcileScheduled: boolean;
  confirmingName: boolean;
}

/**
 * Protect the fixed cells in the assignment template and confirm a student's
 * name exactly once. This is a client-side safeguard for the local draft; the
 * future JupyterHub deployment must still enforce permissions server-side.
 */
export class AssignmentTemplateController {
  constructor(options: AssignmentTemplateController.IOptions) {
    this._notebookTracker = options.notebookTracker;

    this._notebookTracker.widgetAdded.connect((_, panel) => {
      void panel.context.ready.then(() => this._trackPanel(panel));
    });
    this._notebookTracker.forEach(panel => {
      void panel.context.ready.then(() => this._trackPanel(panel));
    });
  }

  /** Handle the completed execution of a notebook cell. */
  async handleCellExecution(cell: ICellModel): Promise<void> {
    const panel = this._notebookTracker.find(candidate =>
      candidate.content.widgets.some(widget => widget.model === cell)
    );
    if (!panel) {
      return;
    }

    const state = this._states.get(panel);
    if (!state || !this._hasTag(cell, NAME_TAG) || cell.type !== 'code') {
      return;
    }

    const analyticsMetadata = this._analyticsMetadata(panel);
    if (analyticsMetadata.identity_confirmed || state.confirmingName) {
      return;
    }

    const studentName = this._studentNameFromSource(
      cell.sharedModel.getSource()
    );
    if (!studentName) {
      window.alert(
        'Enter your full name as student_name = "Your Name" before running this cell.'
      );
      return;
    }

    if (
      !window.confirm(
        `Confirm the notebook name as “${studentName}”? This cannot be changed afterwards.`
      )
    ) {
      return;
    }

    state.confirmingName = true;
    try {
      const filename = this._filenameForStudent(studentName);
      const currentFilename = panel.context.path.split('/').pop();
      if (currentFilename !== filename) {
        await panel.context.rename(filename);
      }

      panel.model?.setMetadata('student_analytics', {
        ...analyticsMetadata,
        student_name: studentName,
        identity_confirmed: true,
        identity_confirmed_at: new Date().toISOString()
      });

      this._lockStudentNameCell(state, cell, studentName);
    } catch (error) {
      console.error('Could not confirm the student name:', error);
      window.alert(
        'The notebook could not be renamed. Check whether another notebook already uses this name, then try again.'
      );
    } finally {
      state.confirmingName = false;
    }
  }

  private _trackPanel(panel: NotebookPanel): void {
    if (this._states.has(panel) || !this._isAssignmentTemplate(panel)) {
      return;
    }

    const protectedCells = new Map<string, IProtectedCell>();
    const model = panel.model;
    if (!model) {
      return;
    }

    for (let index = 0; index < model.cells.length; index++) {
      const cell = model.cells.get(index);
      if (!this._isProtectedCell(cell)) {
        continue;
      }
      protectedCells.set(cell.id, {
        id: cell.id,
        index,
        content: this._copyCell(cell.toJSON()),
        allowSourceEdit:
          this._hasTag(cell, 'student-answer') ||
          (this._hasTag(cell, NAME_TAG) &&
            !this._analyticsMetadata(panel).identity_confirmed),
        allowExecution: this._hasTag(cell, 'provided-data')
      });
    }

    if (protectedCells.size === 0) {
      return;
    }

    const state: ITemplateState = {
      panel,
      protectedCells,
      // Keep pre-existing cells intact when an older notebook is opened. New
      // cells are not added to this set, so the fixed template cannot grow.
      initialCellIds: new Set([...model.cells].map(cell => cell.id)),
      watchedEditableCellIds: new Set(),
      reconcileScheduled: false,
      confirmingName: false
    };
    this._states.set(panel, state);

    for (const cell of model.cells) {
      this._watchEditableSource(state, cell);
    }

    model.cells.changed.connect(() => this._scheduleReconcile(state));
    model.contentChanged.connect(() => this._scheduleReconcile(state));
    this._installEventGuards(state);
    panel.disposed.connect(() => this._states.delete(panel));

    this._markProtectedCells(state);
    this._scheduleReconcile(state);
  }

  private _installEventGuards(state: ITemplateState): void {
    const node = state.panel.node;
    const isLockedTarget = (target: EventTarget | null): boolean => {
      const protectedCell = this._protectedCellFromTarget(state, target);
      return !!protectedCell && !protectedCell.allowSourceEdit;
    };

    node.addEventListener(
      'beforeinput',
      event => {
        if (isLockedTarget(event.target)) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );
    node.addEventListener(
      'paste',
      event => {
        if (isLockedTarget(event.target)) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );
    node.addEventListener(
      'cut',
      event => {
        if (isLockedTarget(event.target)) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );
    node.addEventListener(
      'dragstart',
      event => {
        if (this._protectedCellFromTarget(state, event.target)) {
          event.preventDefault();
        }
      },
      true
    );
    node.addEventListener(
      'drop',
      event => {
        if (isLockedTarget(event.target)) {
          event.preventDefault();
        }
      },
      true
    );
    node.addEventListener(
      'contextmenu',
      event => {
        if (this._protectedCellFromTarget(state, event.target)) {
          event.preventDefault();
        }
      },
      true
    );
    node.addEventListener(
      'keydown',
      event => {
        const protectedCell = this._protectedCellFromTarget(
          state,
          event.target
        );
        if (!protectedCell) {
          return;
        }

        const isStructuralShortcut =
          (event.altKey && ['ArrowUp', 'ArrowDown'].includes(event.key)) ||
          ((event.ctrlKey || event.metaKey) &&
            ['x', 'X', 'Backspace', 'Delete'].includes(event.key));
        const isExecutionShortcut =
          event.key === 'Enter' &&
          (event.shiftKey || event.ctrlKey || event.metaKey);
        if (
          (!protectedCell.allowSourceEdit &&
            !(protectedCell.allowExecution && isExecutionShortcut)) ||
          isStructuralShortcut
        ) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );
  }

  private _scheduleReconcile(state: ITemplateState): void {
    if (state.reconcileScheduled) {
      return;
    }
    state.reconcileScheduled = true;
    window.setTimeout(() => {
      state.reconcileScheduled = false;
      this._reconcile(state);
    }, 0);
  }

  private _reconcile(state: ITemplateState): void {
    const model = state.panel.model;
    if (!model || state.confirmingName) {
      return;
    }

    // A template may only contain the cells it had when it was opened. This
    // removes cells inserted through JupyterLab menus, toolbar buttons, or
    // shortcuts before they can become unlabelled analytics cells.
    for (let index = model.cells.length - 1; index >= 0; index--) {
      const cell = model.cells.get(index);
      if (
        !state.protectedCells.has(cell.id) &&
        !state.initialCellIds.has(cell.id)
      ) {
        model.sharedModel.deleteCell(index);
      }
    }

    this._markProtectedCells(state);

    const entries = [...state.protectedCells.values()].sort(
      (first, second) => first.index - second.index
    );
    for (const entry of entries) {
      let currentIndex = this._cellIndex(model, entry.id);
      if (currentIndex === -1) {
        const sharedCell = model.sharedModel.insertCell(
          Math.min(entry.index, model.cells.length),
          this._copyCell(entry.content)
        );
        if (sharedCell.id !== entry.id) {
          state.protectedCells.delete(entry.id);
          entry.id = sharedCell.id;
          entry.content.id = sharedCell.id;
          state.protectedCells.set(entry.id, entry);
        }
        currentIndex = this._cellIndex(model, entry.id);
      }

      if (currentIndex === -1) {
        this._scheduleReconcile(state);
        return;
      }

      const actual = model.cells.get(currentIndex);
      this._watchEditableSource(state, actual);
      if (actual.type !== entry.content.cell_type) {
        model.sharedModel.deleteCell(currentIndex);
        const sharedCell = model.sharedModel.insertCell(
          entry.index,
          this._copyCell(entry.content)
        );
        if (sharedCell.id !== entry.id) {
          state.protectedCells.delete(entry.id);
          entry.id = sharedCell.id;
          entry.content.id = sharedCell.id;
          state.protectedCells.set(entry.id, entry);
        }
        this._scheduleReconcile(state);
        return;
      }

      if (!entry.allowSourceEdit) {
        const expectedSource = this._sourceText(entry.content.source);
        if (actual.sharedModel.getSource() !== expectedSource) {
          actual.sharedModel.setSource(expectedSource);
        }
      }

      const expectedMetadata = entry.content.metadata ?? {};
      if (
        JSON.stringify(actual.metadata) !== JSON.stringify(expectedMetadata)
      ) {
        actual.sharedModel.metadata = this._copyMetadata(expectedMetadata);
      }

      if (currentIndex !== entry.index) {
        model.sharedModel.moveCell(currentIndex, entry.index);
      }
    }
  }

  private _lockStudentNameCell(
    state: ITemplateState,
    cell: ICellModel,
    studentName: string
  ): void {
    const model = state.panel.model;
    if (!model) {
      return;
    }
    const index = this._cellIndex(model, cell.id);
    if (index === -1) {
      return;
    }

    const metadata = {
      deletable: false,
      editable: false,
      tags: ['student-name', NAME_TAG, 'identity-confirmed', 'protected-cell'],
      // Preserve the same stable identity after converting the entry cell to
      // locked Markdown. Analytics must never fall back to a positional label.
      analytics_cell_key: 'student_name'
    };
    const content: nbformat.IMarkdownCell = {
      cell_type: 'markdown',
      metadata,
      source: `## Student: ${this._escapeMarkdown(studentName)}\n\n_Name confirmed and notebook renamed._`
    };

    // The replacement has a new Jupyter cell ID. Remove the original ID from
    // the initial-cell allow-list so a stale reconciliation cannot restore it
    // later as a duplicate cell at the end of the notebook.
    state.initialCellIds.delete(cell.id);
    model.sharedModel.deleteCell(index);
    const sharedCell = model.sharedModel.insertCell(index, content);
    state.initialCellIds.add(sharedCell.id);
    state.protectedCells.delete(cell.id);
    state.protectedCells.set(sharedCell.id, {
      id: sharedCell.id,
      index,
      content: { ...content, id: sharedCell.id },
      allowSourceEdit: false,
      allowExecution: false
    });
    this._scheduleReconcile(state);
  }

  private _protectedCellFromTarget(
    state: ITemplateState,
    target: EventTarget | null
  ): IProtectedCell | undefined {
    if (!(target instanceof Element)) {
      return undefined;
    }
    const cellWidget = state.panel.content.widgets.find(widget =>
      widget.node.contains(target)
    );
    return cellWidget
      ? state.protectedCells.get(cellWidget.model.id)
      : undefined;
  }

  private _markProtectedCells(state: ITemplateState): void {
    for (const cellWidget of state.panel.content.widgets) {
      if (!state.protectedCells.has(cellWidget.model.id)) {
        continue;
      }
      cellWidget.node.classList.add('student-analytics-protected-cell');
      cellWidget.node.title = 'Protected assignment cell';
    }
  }

  private _watchEditableSource(state: ITemplateState, cell: ICellModel): void {
    const entry = state.protectedCells.get(cell.id);
    if (!entry?.allowSourceEdit || state.watchedEditableCellIds.has(cell.id)) {
      return;
    }

    state.watchedEditableCellIds.add(cell.id);
    cell.contentChanged.connect(() => {
      const currentEntry = state.protectedCells.get(cell.id);
      if (!currentEntry?.allowSourceEdit) {
        return;
      }
      currentEntry.content.source = this._copyCell(cell.toJSON()).source;
    });
  }

  private _isAssignmentTemplate(panel: NotebookPanel): boolean {
    const metadata = this._analyticsMetadata(panel);
    if (metadata.template_version) {
      return true;
    }
    const model = panel.model;
    return (
      !!model && [...model.cells].some(cell => this._hasTag(cell, COURSE_TAG))
    );
  }

  private _isProtectedCell(cell: ICellModel): boolean {
    const analyticsCellKey = cell.getMetadata('analytics_cell_key');
    if (typeof analyticsCellKey === 'string' && analyticsCellKey.trim()) {
      return true;
    }
    const tags = cell.getMetadata('tags');
    return (
      Array.isArray(tags) && tags.some(tag => PROTECTED_TAGS.has(String(tag)))
    );
  }

  private _hasTag(cell: ICellModel, tag: string): boolean {
    const tags = cell.getMetadata('tags');
    return Array.isArray(tags) && tags.includes(tag);
  }

  private _analyticsMetadata(panel: NotebookPanel): Record<string, any> {
    const metadata = panel.model?.getMetadata('student_analytics');
    return metadata && typeof metadata === 'object' && !Array.isArray(metadata)
      ? metadata
      : {};
  }

  private _studentNameFromSource(source: string): string | undefined {
    const match = source.match(/^\s*student_name\s*=\s*(['"])(.*?)\1\s*$/m);
    const name = match?.[2]?.trim();
    if (!name || /^enter your full name$/i.test(name) || name.length > 100) {
      return undefined;
    }
    return name
      .replace(/[\r\n\t]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  private _filenameForStudent(studentName: string): string {
    const safeStem = studentName
      .replace(/[\\/:*?"<>|]/g, ' ')
      .replace(/\s+/g, ' ')
      .replace(/^\.+|\.+$/g, '')
      .trim();
    if (!safeStem) {
      throw new Error('The student name cannot be used as a filename.');
    }
    return `${safeStem}.ipynb`;
  }

  private _cellIndex(
    model: NonNullable<NotebookPanel['model']>,
    cellId: string
  ): number {
    for (let index = 0; index < model.cells.length; index++) {
      if (model.cells.get(index).id === cellId) {
        return index;
      }
    }
    return -1;
  }

  private _sourceText(source: unknown): string {
    return Array.isArray(source) ? source.join('') : String(source ?? '');
  }

  private _copyCell(cell: nbformat.ICell): nbformat.ICell {
    return JSON.parse(JSON.stringify(cell)) as nbformat.ICell;
  }

  private _copyMetadata(metadata: object): any {
    return JSON.parse(JSON.stringify(metadata));
  }

  private _escapeMarkdown(value: string): string {
    return value
      .replace(/[\\`*_{}<>()#+.!|]/g, '\\$&')
      .split('[')
      .join('\\[')
      .split(']')
      .join('\\]');
  }

  private _notebookTracker: INotebookTracker;
  private _states = new WeakMap<NotebookPanel, ITemplateState>();
}

export namespace AssignmentTemplateController {
  export interface IOptions {
    notebookTracker: INotebookTracker;
  }
}

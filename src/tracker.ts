import { JupyterFrontEnd } from '@jupyterlab/application';
import { IDocumentManager } from '@jupyterlab/docmanager';

import { INotebookTracker } from '@jupyterlab/notebook';
import { IEditorTracker } from '@jupyterlab/fileeditor';

import { Widget } from '@lumino/widgets';
import { toArray } from '@lumino/algorithm';

import { Minimatch } from 'minimatch';

import { debug_printer, create_debug_printer } from './utils';
import { IFocusChangeAutoSaveSettings } from './settings';

import { diffLines } from 'diff';

import { DocumentRegistry } from '@jupyterlab/docregistry';
import { Cell } from '@jupyterlab/cells';

/**
 * Arguments to initialize FocusSaveTracker.
 */
export interface IFocusSaveTrackerArgs {
  /** Instance of IShell (app.shell) passed to the extension.  */
  shell: JupyterFrontEnd.IShell;
  /** Instance of IDocumentManager passed to the extension.  */
  docManager: IDocumentManager;
  /** Instance of INotebookTracker passed to the extension.  */
  notebookTracker: INotebookTracker;
  /** Instance of IEditorTracker passed to the extension.  */
  editorTracker: IEditorTracker;
  /** Whether to use the debug printer or not.  */
  debug?: boolean;
}

/**
 * Tracker to react to focus changes of all document widgets.
 */
export class FocusChangeAutoSaveTracker {

  /** Instance of IShell (app.shell) passed to the extension.  */
  private _shell: JupyterFrontEnd.IShell;
  /** Instance of IDocumentManager passed to the extension.  */
  private _docManager: IDocumentManager;
  /** Instance of INotebookTracker passed to the extension.  */
  private _notebookTracker: INotebookTracker;
  /** Instance of IEditorTracker passed to the extension.  */
  private _editorTracker: IEditorTracker;

  /** Mapping of widget nodes to widget objects to be used with event handlers */
  private _nodes = new Map<HTMLElement, Widget>();
  /** Glob pattern matcher to check if a document is excluded. */
  private _excludeMatcher: Minimatch;
  /** Save cells of focus change auto save tracker */
  /** Debug printer */
  private _debug_printer: (...args: any[]) => void;

  /** cache of last known content per path */
  private _previousContent = new Map<string, string>();
  /** save counters per path */
  private _stepCounters: Map<string, number> = new Map();

  // handle async operations by queuing them
  private _operationQueues = new Map<string, Promise<void>>();
  private _lastFocusedLocation = new Map<string, string>();
  private _cellOrder = new Map<string, Map<string, { type: 'code' | 'markdown'; ord: number }>>();
  private _periodicSaveTimer: number | undefined;
  private _lastEditLogTime = new Map<string, number>();

  /**
   * Initialization of FocusChangeAutoSaveTracker.
   *
   * @param initArgs Arguments to instantiate FocusChangeAutoSaveTracker.
   */
  constructor(initArgs: IFocusSaveTrackerArgs) {
    const args: IFocusSaveTrackerArgs = { debug: false, ...initArgs };
    this._shell = args.shell;
    this._docManager = args.docManager;
    this._notebookTracker = args.notebookTracker;
    this._editorTracker = args.editorTracker;

    this._debug_printer = create_debug_printer(args.debug);
    // initialise with a matcher that never excludes (updated by updateSettings)
    this._excludeMatcher = new Minimatch('*', { nocomment: true });
  }

  /**
   * Get all or only untracked document widgets
   *
   * @param skipTracked Whether to skip already tracked widgets or not.
   * @returns Array of document widgets
   */
  documentWidgets(skipTracked = true): Array<Widget> {
    const widgetArray: Widget[] = [];
    for (const widget of toArray(this._shell.widgets('main'))) {
      if (widget.node.classList.contains('saves-on-lose-focus') && skipTracked) {
        continue;
      }
      if (this.isDocumentWidget(widget)) {
        widgetArray.push(widget);
      }
    }
    return widgetArray;
  }

  /**
   * Determine if a widget is a document widget.
   *
   * @param widget Widget to check if it is a document Widget
   * @returns True if the widget is a document widget, else false.
   */
  isDocumentWidget(widget: Widget): boolean {
    const context = this._docManager.contextForWidget(widget);
    return context !== undefined;
  }

  /** Add all untracked widgets to the FocusTracker. */
  trackWidgets(): void {
    for (const widget of this.documentWidgets()) {
      this._nodes.set(widget.node, widget);
      widget.node.classList.add('saves-on-lose-focus');
      widget.node.addEventListener('focusin', this);
      widget.node.addEventListener('focusout', this);
    }
  }

  /** Remove all document widgets from the FocusTracker. */
  unTrackWidgets(): void {
    for (const widget of this.documentWidgets(false)) {
      widget.node.classList.remove('saves-on-lose-focus');
      widget.node.removeEventListener('focusin', this);
      widget.node.removeEventListener('focusout', this);
    }
    this._nodes.clear();
  }

  //queue operation to avoid race condition
  private async queueOperation<T>(filePath: string, operation: () => Promise<T>): Promise<T> {
    const currentQueue = this._operationQueues.get(filePath) || Promise.resolve();
    
    const newQueue = currentQueue
      .then(() => operation())
      .catch((error) => {
        console.error(`Error in queued operation for ${filePath}:`, error);
        throw error;
      });
    
    // Store the queue as Promise<void> to prevent type issues
    this._operationQueues.set(filePath, newQueue.then(() => {}).catch(() => {}));
    
    return newQueue;
  }

  private ensureCellOrder(path: string, widget: Widget): void {
    const panel = this._notebookTracker.find(p => p.id === widget.id);
    if (!panel) {
      return;
    }

    // Load existing mapping or start fresh
    const map = this._cellOrder.get(path) || new Map<string, { type: 'code' | 'markdown'; ord: number }>();
    // Compute next available counters based on existing entries
    let nextCode = 1, nextMd = 1;
    for (const info of map.values()) {
      if (info.type === 'code') nextCode = Math.max(nextCode, info.ord + 1);
      else nextMd = Math.max(nextMd, info.ord + 1);
    }

    // Assign any new cells
    for (const cellWidget of panel.content.widgets) {
      const src = String(cellWidget.model.toJSON().source).trim();
      if (!src) continue;
      const id = cellWidget.model.id;
      if (!map.has(id)) {
        const isCode = cellWidget.model.type === 'code';
        const ord = isCode ? nextCode++ : nextMd++;
        map.set(id, { type: isCode ? 'code' : 'markdown', ord });
      }
    }

    this._cellOrder.set(path, map);
  }

  /**
   * Logs events to diff log.
   */
  private async logEvent(logEntry: string, context: DocumentRegistry.IContext<DocumentRegistry.IModel>): Promise<void> {
    
    const safeFileName = context.path.replace(/\//g, '__');
    const internalDiffLogPath = `internal_diff_logs/changes/${safeFileName}.log`;

    let prevLog = '';
    try {
      const logModel = await this._docManager.services.contents.get(internalDiffLogPath);
      if (logModel.format === 'text' && typeof logModel.content === 'string') {
        prevLog = logModel.content;
      }
    } catch (err) {
      // No previous log is fine.
    }
    
    try {
      await this._docManager.services.contents.save(internalDiffLogPath, {
        type: 'file',
        format: 'text',
        content: prevLog + logEntry
      });
    } catch (err) {

    }
  }

  /**
   *  focus-logging helper
   */
  private async focusEventHelper(eventType: string, widget: Widget): Promise<void> {
    if (!widget) {
      return;
    }
    const context = this._docManager.contextForWidget(widget);
    if (!context || context.isDisposed) {
      return;
    }

    const timestamp = new Date().toISOString();
    let location = '';

    if (eventType === 'focusin') {
      location = this.getChangeLocation(widget);
      if (location) {
        this._lastFocusedLocation.set(widget.id, location);
      }
    } else if (eventType === 'focusout') {
      location = this._lastFocusedLocation.get(widget.id);
    }

    if (!location) {
      return;
    }

    const logEntry = `[${timestamp}][${eventType}]${location}\n`;
    await this.logEvent(logEntry, context);
  }

  /**
   * Handle the DOM events for focusout.
   *
   * @param event - The DOM event triggered on the tracked node.
   *
   * #### Notes
   * See: https://stackoverflow.com/a/58149336/3990615
   */
  handleEvent(event: Event): void {
    let widget: Widget | undefined;
    switch (event.type) {
      case 'focusin':
        widget = this.getWidgetFromEvent(event as FocusEvent);
        if (widget !== undefined) {
          // Queue the event logging to prevent race conditions
          const context = this._docManager.contextForWidget(widget);
          if (context) {
            console.log(this.getChangeLocation(widget));
            this.ensureCellOrder(context.path, widget);
            void this.queueOperation(context.path, () => this.focusEventHelper('focusin', widget!));
          }
        }
        break;
      case 'focusout':
        widget = this.getWidgetFromEvent(event as FocusEvent);
        if (widget !== undefined) {
          const context = this._docManager.contextForWidget(widget);
          if (context) {
            // Queue both the focus event logging and saving to prevent race conditions
            void this.queueOperation(context.path, async () => {
              await this.focusEventHelper('focusout', widget!);
              await this.saveDocumentWidget(widget!);
            });
          }
        }
        break;
    }
  }

  /**
   * Get Widget depending what triggered the event.
   * This allows filtering of bubbled up focusout events from changing
   * the focussed cell.
   *
   * @param event Focus event triggered by an editor or child widget
   * @returns Document Widget or undefined
   */
  getWidgetFromEvent(event: FocusEvent): Widget | undefined {
    const currentTarget = event.currentTarget as HTMLElement;
    return this._nodes.get(currentTarget);
  }

  /**
   *  Clipboard-logging helper 
   */
  public async clipboardEventLogger(eventType: string, length: number): Promise<void> {
    const widget = this._shell.currentWidget;
    if (!widget) {
      return;
    }

    const context = this._docManager.contextForWidget(widget);
    if (!context || context.isDisposed) {
      return;
    }

    const timestamp = new Date().toISOString();
    const location = this.getChangeLocation(widget);
    const logEntry = `[${timestamp}][${eventType}]${location} length: ${length}\n`;
    void this.logEvent(logEntry, context);
  }

  /** Log a debounced source edit as meaningful student activity. */
  public async editEventLogger(): Promise<void> {
    const widget = this._shell.currentWidget;
    if (!widget) {
      return;
    }

    const context = this._docManager.contextForWidget(widget);
    if (!context || context.isDisposed) {
      return;
    }

    const location = this.getChangeLocation(widget);
    if (!location) {
      return;
    }

    const now = Date.now();
    const key = `${context.path}:${location}`;
    const previous = this._lastEditLogTime.get(key) ?? 0;
    if (now - previous < 2000) {
      return;
    }
    this._lastEditLogTime.set(key, now);

    const timestamp = new Date(now).toISOString();
    const logEntry = `[${timestamp}][edit]${location}\n`;
    await this.queueOperation(context.path, () => this.logEvent(logEntry, context));
  }

  /** Log whether the JupyterLab tab/window is available for student activity. */
  public async assignmentPresenceEventLogger(isActive: boolean): Promise<void> {
    const widget = this._shell.currentWidget;
    if (!widget) {
      return;
    }

    const context = this._docManager.contextForWidget(widget);
    if (!context || context.isDisposed) {
      return;
    }

    const timestamp = new Date().toISOString();
    const location = this.getChangeLocation(widget);
    const event = isActive ? 'assignment-active' : 'assignment-inactive';
    const logEntry = `[${timestamp}][${event}]${location}\n`;
    await this.queueOperation(context.path, () => this.logEvent(logEntry, context));
  }

  /**
   * Return the same positional location format for every notebook event.
   */
  private getCellLocation(widget: Widget, cellIndex: number): string {
    const notebookPanel = this._notebookTracker.find(p => p.id === widget.id);
    const cells = notebookPanel?.content.model?.cells;
    const activeCell = cells?.get(cellIndex);
    if (!activeCell) {
      return '';
    }

    // One notebook-wide position keeps all event types (including execute)
    // on the same identifier and avoids a code-cell/markdown-cell collision.
    return `[cell ${cellIndex + 1}]`;
  }

  private getChangeLocation(widget: Widget): string {
    const notebookPanel = this._notebookTracker.find(p => p.id === widget.id);
    if (!notebookPanel) {
      return '';
    }
    const notebook = notebookPanel.content;
    return this.getCellLocation(widget, notebook.activeCellIndex);
  }

  public async executionEventLogger(success: boolean, cell: Cell): Promise<void> {
    const widget = this._shell.currentWidget!;
    const context = this._docManager.contextForWidget(widget);
    if (!context) { return; }
    const notebookPanel = this._notebookTracker.find(p => p.id === widget.id);
    const cellIndex = notebookPanel?.content.widgets.findIndex(
      cellWidget => cellWidget.model.id === cell.model.id
    );
    const location = cellIndex === undefined || cellIndex < 0
      ? ''
      : this.getCellLocation(widget, cellIndex);
    if (!location) {
      return;
    }
    const timestamp = new Date().toISOString();
    const status = success ? 'success' : 'error';

    const logEntry = `[${timestamp}][execute]${location} status: ${status}\n`;
    await this.queueOperation(context.path, () => this.logEvent(logEntry, context));
  }

  /**
   * Save a widget if it is a document widget *and* has unsaved changes.
   *
   * The very first save for a file now *also* generates a diff log that treats the whole file as an addition.
   */
  async saveDocumentWidget(widget: Widget): Promise<void> {
    const context = this._docManager.contextForWidget(widget);
    if (!context) {
      return;
    }

    // ensure the folders for saving the files exist in the system
    const ensureDirExists = async (dirPath: string) => {
      try {
        await this._docManager.services.contents.get(dirPath);
      } catch {
        try {
          await this._docManager.services.contents.save(dirPath, {
            type: 'directory',
            format: 'json',
            content: []
          });
        } catch (err) {
          console.error('Failed to create directory:', dirPath, err);
        }
      }
    };

    await ensureDirExists('internal_diff_logs');
    await ensureDirExists('internal_diff_logs/changes');
    await ensureDirExists('internal_diff_logs/versions');
    await ensureDirExists('internal_diff_logs/cell_versions');

    if (
      this._excludeMatcher.match(context.path) === false &&
      context.model.dirty === true &&
      context.isDisposed === false
    ) {
      const model = context.model;
      const timestamp = new Date().toISOString();
      let currentContent = '';
      let currentCellSnapshots: Array<{
        cell_id: string;
        cell_index: number;
        source: string;
      }> = [];

      try {
        // Try to parse as notebook and extract cell sources
        const nb = JSON.parse(model.toString());
        if (Array.isArray(nb.cells)) {
          currentCellSnapshots = nb.cells
            .map((cell: any, index: number) => {
              if (cell.cell_type === 'code' || cell.cell_type === 'markdown') {
                const source = Array.isArray(cell.source)
                  ? cell.source.join('')
                  : cell.source ?? '';
                return {
                  cell_id: typeof cell.id === 'string' ? cell.id : `position_${index + 1}`,
                  cell_index: index + 1,
                  source
                };
              }
              return null;
            })
            .filter((cell: unknown): cell is {
              cell_id: string;
              cell_index: number;
              source: string;
            } => cell !== null);
          currentContent = currentCellSnapshots
            .map(cell => cell.source)
            .join('\n\n');
        }
      } catch {
        // Not a notebook → just string‑ify the model
        currentContent = model.toString();
      }

      if (!this._previousContent.has(context.path)) {
        this._previousContent.set(context.path, '');
        this._stepCounters.set(context.path, 1);
      }

      const normalize = (text: string): string => (text.endsWith('\n') ? text : text + '\n');

      const prevContent = this._previousContent.get(context.path) ?? '';
      const changes = diffLines(normalize(prevContent), normalize(currentContent));

      const diffBody = changes
        .map(part => {
          const lines = (part.value || '').split('\n').filter(Boolean);
          if (part.added || part.removed) {
            const prefix = part.added ? '+ ' : '- ';
            return lines.map(line => `${prefix}${line}`).join('\n');
          }
          return '';
        })
        .filter(Boolean)
        .join('\n');

      if (diffBody.trim() !== '') {
        const step = this._stepCounters.get(context.path) ?? 1;
        const location = this.getChangeLocation(widget);
        const headerLabel = step === 1 ? 'initial save' : `save ${step}`;
        const header = `[${timestamp}]${location ? location + ' ' : ''}[${headerLabel}]`;
        const logEntry = `${header}\n${diffBody}\n`;

        const safeFileName = context.path.replace(/\//g, '__');
        const internalDiffLogPath = `internal_diff_logs/changes/${safeFileName}.log`;
        const snapshotLogPath = `internal_diff_logs/versions/${safeFileName}.log`;
        const cellSnapshotLogPath = `internal_diff_logs/cell_versions/${safeFileName}.jsonl`;
        let prevInternalLog = '';
        try {
          const internalLogModel = await this._docManager.services.contents.get(internalDiffLogPath);
          if (internalLogModel.format === 'text' && typeof internalLogModel.content === 'string') {
            prevInternalLog = internalLogModel.content;
          }
        } catch (err) {
          console.log('No previous internal log found:', internalDiffLogPath);
        }

        try {
          await this._docManager.services.contents.save(internalDiffLogPath, {
            type: 'file',
            format: 'text',
            content: prevInternalLog + logEntry
          });
        } catch (err) {
          console.error('Failed to write internal diff log:', err);
        }

        let prevSnapshotLog = '';
        try {
          const snapshotLogModel = await this._docManager.services.contents.get(snapshotLogPath);
          if (snapshotLogModel.format === 'text' && typeof snapshotLogModel.content === 'string') {
            prevSnapshotLog = snapshotLogModel.content;
          }
        } catch (err) {
          console.log('No previous snapshot log found:', snapshotLogPath);
        }

        const snapshotHeader = `[${timestamp}]${location ? ' ' + location : ''}`;
        const snapshotEntry = `${snapshotHeader}\n${currentContent}\n\n`;

        try {
          await this._docManager.services.contents.save(snapshotLogPath, {
            type: 'file',
            format: 'text',
            content: prevSnapshotLog + snapshotEntry
          });
        } catch (err) {
          console.error('Failed to write snapshot log:', err);
        }

        // Keep structured cell snapshots separate from the existing human-readable
        // version log. Analytics can therefore compare a cell only with its own
        // previous content, rather than mistaking a line edit for a deletion.
        if (currentCellSnapshots.length > 0) {
          let previousCellSnapshotLog = '';
          try {
            const cellSnapshotLogModel = await this._docManager.services.contents.get(cellSnapshotLogPath);
            if (cellSnapshotLogModel.format === 'text' && typeof cellSnapshotLogModel.content === 'string') {
              previousCellSnapshotLog = cellSnapshotLogModel.content;
            }
          } catch (err) {
            console.log('No previous cell snapshot log found:', cellSnapshotLogPath);
          }

          const cellSnapshotEntry = JSON.stringify({
            timestamp,
            cells: currentCellSnapshots
          });
          try {
            await this._docManager.services.contents.save(cellSnapshotLogPath, {
              type: 'file',
              format: 'text',
              content: previousCellSnapshotLog + cellSnapshotEntry + '\n'
            });
          } catch (err) {
            console.error('Failed to write cell snapshot log:', err);
          }
        }

        this._stepCounters.set(context.path, step + 1);
      }

      this._previousContent.set(context.path, currentContent);

      await context.save();
      this._debug_printer('Saved: ', context.path);
    }
  }

  /** Save all document widgets. */
  saveAllDocumentWidgets(): void {
    for (const widget of this.documentWidgets(false)) {
      const context = this._docManager.contextForWidget(widget);
      if (context) {
        // Use the same queue as focus-out saves so concurrent triggers cannot
        // write duplicate or out-of-order diff records.
        void this.queueOperation(context.path, () => this.saveDocumentWidget(widget));
      }
    }
  }

  /** Start, replace, or disable the timer that uses the normal save path. */
  private configurePeriodicAutosave(intervalSeconds: number): void {
    if (this._periodicSaveTimer !== undefined) {
      window.clearInterval(this._periodicSaveTimer);
      this._periodicSaveTimer = undefined;
    }

    if (!Number.isFinite(intervalSeconds) || intervalSeconds <= 0) {
      return;
    }

    this._periodicSaveTimer = window.setInterval(() => {
      this.saveAllDocumentWidgets();
    }, intervalSeconds * 1000);
  }

  /**
   * Activate or deactivate the tracking, with new settings.
   */
  updateSettings(trackerSetting: IFocusChangeAutoSaveSettings): void {
    this._excludeMatcher = new Minimatch(`{${trackerSetting.exclude.join(',')}}`, {
      nocomment: true
    });

    this._debug_printer('_excludeMatcher: ', this._excludeMatcher);
    debug_printer(true, 'Setting active state to: ', trackerSetting.active);

    // Perform a save pass before switching state so we never lose data when disabling
    this.saveAllDocumentWidgets();

    if (trackerSetting.active) {
      this.trackWidgets();
      this._notebookTracker.widgetAdded.connect(this.trackWidgets, this);
      this._editorTracker.widgetAdded.connect(this.trackWidgets, this);
      this.configurePeriodicAutosave(trackerSetting.autosaveIntervalSeconds);
    } else {
      this.unTrackWidgets();
      this._notebookTracker.widgetAdded.disconnect(this.trackWidgets, this);
      this._editorTracker.widgetAdded.disconnect(this.trackWidgets, this);
      this.configurePeriodicAutosave(0);
    }
  }
}

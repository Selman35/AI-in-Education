import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';

import { INotebookTracker, NotebookActions } from '@jupyterlab/notebook';

import { ISettingRegistry } from '@jupyterlab/settingregistry';

import { IEditorTracker } from '@jupyterlab/fileeditor';
import { IDocumentManager } from '@jupyterlab/docmanager';

import { IMainMenu } from '@jupyterlab/mainmenu';

import { FocusChangeAutoSaveTracker } from './tracker';
import { FocusChangeAutoSaveSettings } from './settings';
import { PLUGIN_ID } from './consts';

/**
 * Initialization data for the jupyterlab_autosave_on_focus_change extension.
 */
const extension: JupyterFrontEndPlugin<void> = {
  id: PLUGIN_ID,
  autoStart: true,
  requires: [
    // @ts-expect-error is not assignable to type 'Token<any>'
    INotebookTracker,
    // @ts-expect-error is not assignable to type 'Token<any>'
    IEditorTracker,
    // @ts-expect-error is not assignable to type 'Token<any>'
    IDocumentManager,
    // @ts-expect-error is not assignable to type 'Token<any>'
    ISettingRegistry,
    // @ts-expect-error is not assignable to type 'Token<any>'
    IMainMenu
  ],
  activate: (
    app: JupyterFrontEnd,
    notebookTracker: INotebookTracker,
    editorTracker: IEditorTracker,
    docManager: IDocumentManager,
    settingRegistry: ISettingRegistry,
    mainMenu: IMainMenu
  ) => {
    console.log(
      'JupyterLab extension jupyterlab_autosave_on_focus_change is activated!'
    );

    const focusSaveTracker = new FocusChangeAutoSaveTracker({
      shell: app.shell,
      docManager,
      notebookTracker,
      editorTracker
      // debug: true,
    });

    document.addEventListener('copy', function(){
      const selection = window.getSelection()?.toString();
      if (selection) {
        focusSaveTracker.clipboardEventLogger('copy', selection.length);
      }
    }, true);

    document.addEventListener('cut', function(){
      const selection = window.getSelection()?.toString();
      if (selection) {
        focusSaveTracker.clipboardEventLogger('cut', selection.length);
      }
    }, true);

    document.addEventListener('paste', function(event: ClipboardEvent){
      let pastedText = '';
      if (event.clipboardData){
        pastedText = event.clipboardData.getData('text/plain');
        if (pastedText){
          focusSaveTracker.clipboardEventLogger('paste', pastedText.length);
        }
      }
    }, true);

    NotebookActions.executed.connect((_, args) => {
      focusSaveTracker.executionEventLogger(args.success, args.cell);
    });

    const settings = new FocusChangeAutoSaveSettings({
      app,
      settingRegistry,
      focusSaveTracker,
      mainMenu
      // debug: true,
    });

    settings.trackSettingChanges();
  }
};

export default extension;

'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Only a small, explicit bridge is exposed to the sandboxed local renderer.
contextBridge.exposeInMainWorld('greatsage', Object.freeze({
  getConnection: () => ipcRenderer.invoke('greatsage:connection'),
  showPet: () => ipcRenderer.invoke('greatsage:show-pet'),
  hidePet: () => ipcRenderer.invoke('greatsage:hide-pet'),
  showConsole: () => ipcRenderer.invoke('greatsage:show-console'),
  setClickThrough: (enabled) => ipcRenderer.invoke('greatsage:click-through', Boolean(enabled)),
  minimize: () => ipcRenderer.invoke('greatsage:minimize'),
  openExternal: (url) => ipcRenderer.invoke('greatsage:open-external', String(url)),
  chooseSkillDirectory: () => ipcRenderer.invoke('greatsage:choose-skill-directory'),
}));

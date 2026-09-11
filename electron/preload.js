'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pcu', {
  submitInstruction: (text) => ipcRenderer.invoke('submit-instruction', text),
  stopTask: () => ipcRenderer.invoke('stop-task'),
  confirm: (id, approved) => ipcRenderer.invoke('confirm', id, approved),
  getConfig: () => ipcRenderer.invoke('get-config'),
  saveConfig: (cfg) => ipcRenderer.invoke('save-config', cfg),
  hideBar: () => ipcRenderer.send('hide-bar'),
  onBackendMessage: (cb) => {
    const wrapped = (_e, msg) => cb(msg);
    ipcRenderer.on('backend-message', wrapped);
    return () => ipcRenderer.removeListener('backend-message', wrapped);
  },
  onBackendConnection: (cb) => {
    const wrapped = (_e, info) => cb(info);
    ipcRenderer.on('backend-connection', wrapped);
    return () => ipcRenderer.removeListener('backend-connection', wrapped);
  },
  onBarShown: (cb) => {
    const wrapped = () => cb();
    ipcRenderer.on('bar-shown', wrapped);
    return () => ipcRenderer.removeListener('bar-shown', wrapped);
  },
  onOverlayCursor: (cb) => {
    const wrapped = (_e, pt) => cb(pt);
    ipcRenderer.on('overlay-cursor', wrapped);
    return () => ipcRenderer.removeListener('overlay-cursor', wrapped);
  }
});

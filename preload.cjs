const { contextBridge, ipcRenderer } = require('electron');
if (process.platform === 'win32') {
  contextBridge.exposeInMainWorld('desktopUpdates', {
    available: true,
    check: () => ipcRenderer.invoke('updates:check'),
  });
}

const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("hp", {
  getSettings: () => ipcRenderer.invoke("settings:get"),
  test: (url) => ipcRenderer.invoke("settings:test", url),
  save: (url) => ipcRenderer.invoke("settings:save", url),
  reset: () => ipcRenderer.invoke("settings:reset"),
  close: () => ipcRenderer.invoke("settings:close"),
  retry: () => ipcRenderer.invoke("error:retry"),
  openSettings: () => ipcRenderer.invoke("error:settings"),
});

// Runs in the portal page (isolated world). Handles PDFs dropped onto the window.
const { ipcRenderer, webUtils } = require("electron");

function isDropTarget(el) {
  // Let the portal handle drops on its own upload areas / file inputs.
  return !!(el && el.closest && el.closest('input[type="file"], [data-dropzone], .dropzone, .drop-zone, label'));
}

window.addEventListener("dragover", (e) => {
  if (!isDropTarget(e.target)) e.preventDefault();
}, true);

window.addEventListener("drop", (e) => {
  if (isDropTarget(e.target)) return;
  e.preventDefault();               // never let Electron navigate to a dropped file
  const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
  files.forEach((f) => {
    const p = webUtils.getPathForFile(f);
    if (p && /\.pdf$/i.test(p)) ipcRenderer.invoke("pdf:open-dropped", p);
  });
}, true);

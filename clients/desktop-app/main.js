// Health Portal - Hospital desktop shell (DEMO ONLY, synthetic data).
// Opens <serverUrl>/staff/ in a normal window. Server URL is configurable (Settings dialog).
const { app, BrowserWindow, Menu, dialog, ipcMain, shell, session, net } = require("electron");
const path = require("path");
const fs = require("fs");
const { pathToFileURL } = require("url");

const FALLBACK_URL = "http://192.168.1.50:8000";
const STAFF_PATH = "/staff/";

// ---------- config: userData/config.json overrides bundled server-default.json ----------
function readJson(p) {
  try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch { return null; }
}
function normalizeUrl(u) {
  u = String(u || "").trim();
  if (!u) return "";
  if (!/^https?:\/\//i.test(u)) u = "http://" + u;
  return u.replace(/\/+$/, "");
}
function configPath() { return path.join(app.getPath("userData"), "config.json"); }
function bundledDefault() {
  const j = readJson(path.join(__dirname, "server-default.json"));
  return normalizeUrl(j && j.serverUrl) || FALLBACK_URL;
}
function getServerUrl() {
  const j = readJson(configPath());
  return normalizeUrl(j && j.serverUrl) || bundledDefault();
}
function saveServerUrl(u) {
  fs.mkdirSync(path.dirname(configPath()), { recursive: true });
  fs.writeFileSync(configPath(), JSON.stringify({ serverUrl: normalizeUrl(u) }, null, 2));
}
function resetServerUrl() { try { fs.unlinkSync(configPath()); } catch {} }

// Plain-HTTP LAN demo: allow camera (QR scan) on the insecure server origin.
// Must be set before app is ready, so a URL change relaunches the app.
function originOf(u) { try { return new URL(u).origin; } catch { return ""; } }
{
  const o = originOf(getServerUrl());
  if (o) app.commandLine.appendSwitch("unsafely-treat-insecure-origin-as-secure", o);
}

function relaunch() {
  // Portable builds run from a temp folder; relaunch the real portable .exe instead.
  const portable = process.env.PORTABLE_EXECUTABLE_FILE;
  if (portable) app.relaunch({ execPath: portable });
  else app.relaunch();
  app.exit(0);
}
// ---------- windows ----------
let mainWin = null;
let settingsWin = null;

function portalUrl(p) { return getServerUrl() + (p || STAFF_PATH); }

function isPortalOrigin(u) { return originOf(u) === originOf(getServerUrl()); }

function wirePortalWindow(win) {
  const wc = win.webContents;

  // Never navigate the app window to a local file or a foreign site.
  wc.on("will-navigate", (e, url) => {
    if (url.startsWith("file:") && !url.includes("/renderer/")) { e.preventDefault(); return; }
    if (/^https?:/i.test(url) && !isPortalOrigin(url)) { e.preventDefault(); shell.openExternal(url); }
  });

  // target=_blank / window.open: portal pages (e.g. a document in a new tab) open in an app window.
  wc.setWindowOpenHandler(({ url }) => {
    if (isPortalOrigin(url)) {
      return { action: "allow", overrideBrowserWindowOptions: { width: 1000, height: 800, autoHideMenuBar: true } };
    }
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });

  wc.on("did-fail-load", (_e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return; // -3 = aborted
    win.loadFile(path.join(__dirname, "renderer", "error.html"), {
      query: { url, desc: desc || String(code), server: getServerUrl() },
    });
  });
}

function createMainWindow() {
  mainWin = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "Health Portal - Hospital (DEMO)",
    backgroundColor: "#eef4f7",
    webPreferences: {
      preload: path.join(__dirname, "preload-portal.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      plugins: true, // built-in PDF viewer
    },
  });
  wirePortalWindow(mainWin);
  mainWin.loadURL(portalUrl(STAFF_PATH));
  mainWin.on("closed", () => { mainWin = null; });
}

function openPdfViewer(filePath) {
  if (!filePath || !/\.pdf$/i.test(filePath)) {
    dialog.showErrorBox("Not a PDF", "Only .pdf files can be opened in the viewer.");
    return;
  }
  const win = new BrowserWindow({
    width: 1000, height: 850, title: path.basename(filePath), autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, plugins: true },
  });
  win.webContents.on("will-navigate", (e) => e.preventDefault());
  win.loadURL(pathToFileURL(filePath).href);
}

async function pickAndOpenPdf() {
  const r = await dialog.showOpenDialog(mainWin || undefined, {
    title: "Open PDF", properties: ["openFile", "multiSelections"],
    filters: [{ name: "PDF", extensions: ["pdf"] }],
  });
  if (!r.canceled) r.filePaths.forEach(openPdfViewer);
}

function openSettings() {
  if (settingsWin) { settingsWin.focus(); return; }
  settingsWin = new BrowserWindow({
    width: 520, height: 430, resizable: false, minimizable: false, maximizable: false,
    parent: mainWin || undefined, modal: !!mainWin, autoHideMenuBar: true, title: "Server settings",
    webPreferences: {
      preload: path.join(__dirname, "preload-settings.js"),
      contextIsolation: true, nodeIntegration: false, sandbox: true,
    },
  });
  settingsWin.setMenu(null);
  settingsWin.loadFile(path.join(__dirname, "renderer", "settings.html"));
  settingsWin.on("closed", () => { settingsWin = null; });
}

// ---------- IPC ----------
ipcMain.handle("settings:get", () => ({ serverUrl: getServerUrl(), defaultUrl: bundledDefault() }));
ipcMain.handle("settings:test", async (_e, url) => {
  const u = normalizeUrl(url);
  if (!u) return { ok: false, message: "Enter a URL." };
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 6000);
    const r = await net.fetch(u + STAFF_PATH, { signal: ctl.signal });
    clearTimeout(t);
    return r.ok ? { ok: true, message: `Reachable (HTTP ${r.status}).` }
                : { ok: false, message: `Server answered HTTP ${r.status} for ${STAFF_PATH}.` };
  } catch (err) {
    return { ok: false, message: "Cannot reach server: " + (err && err.message ? err.message : err) };
  }
});
ipcMain.handle("settings:save", (_e, url) => {
  const u = normalizeUrl(url);
  if (!u) return { ok: false, message: "Enter a URL." };
  saveServerUrl(u);
  relaunch(); // relaunch so the camera/insecure-origin switch uses the new URL
  return { ok: true };
});
ipcMain.handle("settings:reset", () => { resetServerUrl(); relaunch(); return { ok: true }; });
ipcMain.handle("settings:close", () => { if (settingsWin) settingsWin.close(); });
ipcMain.handle("error:retry", () => { if (mainWin) mainWin.loadURL(portalUrl(STAFF_PATH)); });
ipcMain.handle("error:settings", () => openSettings());
ipcMain.handle("pdf:open-dropped", (_e, filePath) => openPdfViewer(filePath));

// ---------- menu ----------
function buildMenu() {
  const nav = (p) => () => mainWin && mainWin.loadURL(portalUrl(p));
  const template = [
    { label: "&File", submenu: [
      { label: "Open PDF...", accelerator: "CmdOrCtrl+O", click: pickAndOpenPdf },
      { type: "separator" },
      { label: "Server Settings...", accelerator: "CmdOrCtrl+,", click: openSettings },
      { type: "separator" },
      { role: "quit" },
    ]},
    { label: "&Navigate", submenu: [
      { label: "Staff Portal (home)", accelerator: "CmdOrCtrl+H", click: nav(STAFF_PATH) },
      { label: "Patient Portal (for demo comparison)", click: nav("/") },
      { type: "separator" },
      { label: "Back", accelerator: "Alt+Left", click: () => mainWin && mainWin.webContents.canGoBack() && mainWin.webContents.goBack() },
      { label: "Forward", accelerator: "Alt+Right", click: () => mainWin && mainWin.webContents.canGoForward() && mainWin.webContents.goForward() },
      { role: "reload" }, { role: "forceReload" },
    ]},
    { label: "&View", submenu: [
      { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" },
      { type: "separator" }, { role: "togglefullscreen" }, { role: "toggleDevTools" },
    ]},
    { label: "&Help", submenu: [
      { label: "Open Server in Browser", click: () => shell.openExternal(portalUrl(STAFF_PATH)) },
      { label: "About", click: () => dialog.showMessageBox({
          type: "info", title: "About",
          message: "Health Portal - Hospital Desktop",
          detail: `Version ${app.getVersion()}\nServer: ${getServerUrl()}\n\nDEMO ONLY. Synthetic data. Not for real patient information.`,
        }) },
    ]},
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------- downloads: save + open ----------
function setupDownloads() {
  session.defaultSession.on("will-download", (_e, item) => {
    const dl = app.getPath("downloads");
    const target = path.join(dl, item.getFilename() || "download");
    item.setSavePath(target);
    item.once("done", (_ev, state) => {
      if (state === "completed") {
        shell.openPath(target).then((err) => { if (err) shell.showItemInFolder(target); });
      } else if (state !== "cancelled") {
        dialog.showErrorBox("Download failed", `Could not download ${item.getFilename()} (${state}).`);
      }
    });
  });
}

function setupPermissions() {
  const allowed = new Set(["media", "mediaKeySystem", "fullscreen", "clipboard-sanitized-write", "notifications"]);
  session.defaultSession.setPermissionRequestHandler((wc, permission, cb, details) => {
    const origin = originOf(details.requestingUrl || wc.getURL());
    cb(allowed.has(permission) && origin === originOf(getServerUrl()));
  });
}

// ---------- lifecycle ----------
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_e, argv) => {
    if (mainWin) { if (mainWin.isMinimized()) mainWin.restore(); mainWin.focus(); }
    argv.filter((a) => /\.pdf$/i.test(a) && fs.existsSync(a)).forEach(openPdfViewer);
  });
  app.whenReady().then(() => {
    setupDownloads();
    setupPermissions();
    buildMenu();
    createMainWindow();
    // PDFs passed on the command line / "Open with"
    process.argv.slice(1).filter((a) => /\.pdf$/i.test(a) && fs.existsSync(a)).forEach(openPdfViewer);
    app.on("activate", () => { if (!BrowserWindow.getAllWindows().length) createMainWindow(); });
  });
  app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
}

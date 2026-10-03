/* Portal shell: DOM helpers, API client, auth screens, hash router.
 * Modules are plain <script> tags in index.html (loaded in order after this file). They add pages with:
 *   Portal.registerSection({id, title, icon, order, render(container)})
 * Useful helpers: Portal.el, Portal.api, Portal.toast, Portal.openForm, Portal.confirm, Portal.fmt*, Portal.setBadge
 * Everything uses textContent (never innerHTML with data) to avoid XSS.
 */
(function () {
  const Portal = (window.Portal = { sections: [], user: null, version: "1" });

  // ---------- loud failures: a module script that fails to load or throws while loading ----------
  const loadProblems = [];
  let booted = false;
  function showLoadProblem(text) {
    loadProblems.push(text);
    let bar = document.getElementById("load-error");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "load-error"; bar.className = "card err"; bar.setAttribute("role", "alert");
      bar.style.cssText = "position:sticky;top:0;z-index:1000;margin:0;border-radius:0";
      (document.body || document.documentElement).prepend(bar);
    }
    while (bar.firstChild) bar.removeChild(bar.firstChild);
    const p = document.createElement("p");
    p.textContent = "Part of the app failed to load, so some pages may be missing or broken: " + loadProblems.join("; ") +
      ". Check your connection and reload the page.";
    const btn = document.createElement("button");
    btn.className = "btn"; btn.textContent = "Reload"; btn.addEventListener("click", () => location.reload());
    bar.append(p, btn);
  }
  window.addEventListener("error", (ev) => {
    const t = ev.target;
    if (t && t !== window && t.tagName === "SCRIPT") showLoadProblem(t.getAttribute("src") + " (not loaded)");
    else if (!booted && ev.filename) showLoadProblem((ev.filename.split("/").pop() || "a script") + " (error: " + ev.message + ")");
  }, true);

  // ---------- DOM helper ----------
  Portal.el = function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, "");
      else n.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  };
  const el = Portal.el;
  Portal.clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };

  // ---------- formatting ----------
  Portal.fmtDate = (s) => {
    if (!s) return "";
    const d = new Date(s.length <= 10 ? s + "T00:00:00" : s.endsWith("Z") ? s : s + "Z");
    if (isNaN(d)) return s;
    return s.length <= 10 ? d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
      : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  Portal.fmtMoney = (n) => (n === null || n === undefined || n === "" ? "—" : Number(n).toLocaleString(undefined, { style: "currency", currency: "USD" }));
  Portal.label = (s) => (s || "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

  // ---------- toasts / announcements ----------
  Portal.toast = function (msg, kind) {
    const t = el("div", { class: "toast " + (kind || ""), role: kind === "error" ? "alert" : "status", text: msg });
    document.getElementById("toasts").append(t);
    setTimeout(() => t.remove(), kind === "error" ? 8000 : 4000);
  };

  // ---------- connection banner (network down -> visible Retry; hidden again after any successful request) ----------
  function setOffline(on) {
    let bar = document.getElementById("conn-error");
    if (!on) { if (bar) bar.remove(); return; }
    if (bar) return;
    bar = el("div", { id: "conn-error", class: "card err", role: "alert", style: "position:sticky;top:0;z-index:999;margin:0;border-radius:0" },
      el("p", { text: "Can't reach the server. Check your internet connection." }),
      el("button", { class: "btn", text: "Retry", onclick: () => { setOffline(false); Portal.user ? route() : boot(); } }));
    document.body.prepend(bar);
  }

  // ---------- API client: the ONE way the patient app talks to the server ----------
  // opts: {noAuthRedirect, timeoutMs}. Errors are Error objects with .status (HTTP), .fields (422),
  // .network (no answer / timeout) or .sessionExpired (401).
  Portal.DEFAULT_TIMEOUT_MS = 30000;
  Portal.UPLOAD_TIMEOUT_MS = 180000;
  Portal.api = async function (method, url, body, opts) {
    opts = opts || {};
    const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined && body !== null) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
    const ctl = new AbortController(); init.signal = ctl.signal;
    const ms = opts.timeoutMs || (body instanceof FormData ? Portal.UPLOAD_TIMEOUT_MS : Portal.DEFAULT_TIMEOUT_MS);
    const timer = setTimeout(() => ctl.abort(), ms);
    let res;
    try { res = await fetch(url, init); }
    catch (e) {
      const timedOut = e && e.name === "AbortError";
      const err = new Error(timedOut ? "The server took too long to answer. Please try again."
        : "Can't reach the server. Check your connection and try again.");
      err.network = true; err.timeout = timedOut;
      setOffline(true);
      throw err;
    } finally { clearTimeout(timer); }
    setOffline(false);
    const ct = res.headers.get("content-type") || "";
    let data = null;
    if (ct.includes("json")) {
      try { data = await res.json(); }
      catch (e) { if (res.ok) { const err = new Error("The server sent an unreadable answer. Please try again."); err.status = res.status; throw err; } }
    }
    if (res.status === 401 && !opts.noAuthRedirect) {
      const wasSignedIn = !!Portal.user;
      Portal.user = null;
      if (wasSignedIn) showAuth(null, "Your session has expired. Please sign in again.");
      const err = new Error("Your session has expired. Please sign in again.");
      err.status = 401; err.sessionExpired = true; err.fields = {};
      throw err;
    }
    if (!res.ok) {
      let msg = data && (typeof data.detail === "string" ? data.detail : null);
      if (!msg && data && Array.isArray(data.detail)) msg = data.detail.map((d) => d.msg).join("; ");
      const err = new Error(msg || `Something went wrong (error ${res.status}).`);
      err.status = res.status; err.fields = (data && data.fields) || {};
      throw err;
    }
    return data;
  };
  ["get", "post", "put", "delete"].forEach((m) => (Portal[m] = (url, body, o) => Portal.api(m.toUpperCase(), url, body, o)));

  // ---------- dialogs ----------
  Portal.dialog = function (title, buildBody) {
    return new Promise((resolve) => {
      const dlg = el("dialog", { "aria-labelledby": "dlg-title" });
      const close = (v) => { dlg.close(); dlg.remove(); resolve(v); };
      dlg.append(el("h3", { id: "dlg-title", text: title }));
      dlg.append(buildBody(close));
      dlg.addEventListener("cancel", () => { dlg.remove(); resolve(undefined); });
      document.body.append(dlg);
      dlg.showModal();
    });
  };

  Portal.confirm = (message, okLabel) =>
    Portal.dialog("Please confirm", (close) =>
      el("div", {}, el("p", { text: message }),
        el("div", { class: "row" },
          el("button", { class: "btn danger", onclick: () => close(true), text: okLabel || "Yes, continue" }),
          el("button", { class: "btn", onclick: () => close(false), text: "Cancel" })))
    ).then((v) => !!v);

  /* openForm({title, fields, values, submitLabel, onSubmit(values)}) -> Promise(result of onSubmit | undefined)
   * field: {name,label,type(text|textarea|date|number|select|checkbox|email|tel|password),required,options,help,placeholder,maxlength}
   * Optional fields are labelled "(optional)". Server errors with err.fields are shown next to the fields. */
  Portal.buildFields = function (fields, values) {
    const wrap = el("div"); const inputs = {};
    fields.forEach((f, i) => {
      const id = "f-" + f.name + "-" + i + "-" + Math.random().toString(36).slice(2, 6);
      const v = values && values[f.name] !== undefined && values[f.name] !== null ? values[f.name] : f.default;
      let input;
      const common = { id, name: f.name, "aria-required": f.required ? "true" : null, required: f.required ? true : null,
        "aria-describedby": id + "-help " + id + "-err" };
      if (f.type === "textarea") input = el("textarea", { ...common, maxlength: f.maxlength || 2000, placeholder: f.placeholder }), input.value = v ?? "";
      else if (f.type === "select") {
        input = el("select", common);
        if (!f.required || v === undefined || v === null || v === "") input.append(el("option", { value: "", text: f.required ? "Choose…" : "— not specified —" }));
        (f.options || []).forEach((o) => { const val = typeof o === "object" ? o.value : o; const lab = typeof o === "object" ? o.label : Portal.label(o);
          input.append(el("option", { value: val, text: lab })); });
        input.value = v ?? "";
      } else if (f.type === "checkbox") {
        input = el("input", { ...common, type: "checkbox" }); input.checked = !!v;
      } else {
        const legacyDate = f.type === "date" && v && /^\d{4}(-\d{2})?$/.test(String(v));
        const t = f.type === "partial_date" || legacyDate ? "text" : (f.type || "text");
        input = el("input", { ...common, type: t, maxlength: f.maxlength || 300, placeholder: f.placeholder || (f.type === "partial_date" ? "e.g. 2019 or 2019-05" : null),
          step: t === "number" ? "any" : null, autocomplete: f.autocomplete || null });
        input.value = v ?? "";
      }
      inputs[f.name] = input;
      const opt = f.required ? null : el("span", { class: "opt", text: " (optional)" });
      if (f.type === "checkbox") wrap.append(el("label", { class: "check", for: id }, input, f.label, opt));
      else wrap.append(el("label", { for: id }, f.label, opt));
      if (f.type !== "checkbox") wrap.append(input);
      wrap.append(el("p", { class: "help", id: id + "-help", text: f.help || (f.type === "date" && v && /^\d{4}(-\d{2})?$/.test(String(v)) ? "Existing date is approximate. Keep it as recorded or enter a full date (YYYY-MM-DD)." : "") }));
      wrap.append(el("p", { class: "field-error", id: id + "-err", role: "alert" }));
    });
    const updateRequirements = () => {
      fields.filter((f) => f.requiredIf).forEach((f) => {
        const required = inputs[f.requiredIf.field]?.value === f.requiredIf.value;
        const input = inputs[f.name]; input.required = required;
        input.setAttribute("aria-required", String(required));
        const optional = wrap.querySelector('label[for="' + input.id + '"] .opt');
        if (optional) optional.hidden = required;
      });
    };
    wrap.addEventListener("change", updateRequirements);
    updateRequirements();
    wrap.values = () => {
      const out = {};
      for (const f of fields) {
        const i = inputs[f.name];
        if (f.type === "checkbox") out[f.name] = i.checked;
        else if (f.type === "number") out[f.name] = i.value === "" ? null : Number(i.value);
        else out[f.name] = i.value.trim() === "" ? null : i.value.trim();
      }
      return out;
    };
    wrap.showErrors = (fieldErrors) => {
      let first = null;
      fields.forEach((f) => {
        const i = inputs[f.name]; const e = document.getElementById(i.id + "-err");
        if (e) e.textContent = (fieldErrors && fieldErrors[f.name]) || "";
        if (fieldErrors && fieldErrors[f.name] && !first) first = i;
      });
      if (first) first.focus();
    };
    wrap.validate = () => {
      const errs = {};
      fields.forEach((f) => {
        if (!(f.required || (f.requiredIf && inputs[f.requiredIf.field]?.value === f.requiredIf.value)) || f.type === "checkbox") return;
        const i = inputs[f.name];
        const empty = f.type === "select" ? i.value === "" : i.value.trim() === "";
        if (empty) errs[f.name] = "This field is required.";
      });
      if (Object.keys(errs).length) { wrap.showErrors(errs); return false; }
      return true;
    };
    return wrap;
  };

  Portal.openForm = function ({ title, fields, values, submitLabel, onSubmit, intro }) {
    return Portal.dialog(title, (close) => {
      const form = el("form", { novalidate: true });
      const fieldsEl = Portal.buildFields(fields, values);
      const status = el("p", { class: "field-error", role: "alert" });
      const submit = el("button", { class: "btn primary", type: "submit", text: submitLabel || "Save" });
      form.append(intro ? el("p", { class: "muted", text: intro }) : "", fieldsEl, status,
        el("div", { class: "row" }, submit, el("button", { class: "btn", type: "button", onclick: () => close(undefined), text: "Cancel" })));
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault(); status.textContent = ""; fieldsEl.showErrors({});
        if (!fieldsEl.validate()) return;
        submit.disabled = true;
        try { const r = await onSubmit(fieldsEl.values()); close(r === undefined ? true : r); }
        catch (e) { status.textContent = e.message; fieldsEl.showErrors(e.fields); submit.disabled = false; }
      });
      return form;
    });
  };

  // ---------- badges ----------
  Portal.sourceBadge = function (rec) {
    if (rec && rec.source === "clinician_confirmed")
      return el("span", { class: "badge good", title: rec.confirmed_by ? "Confirmed by " + rec.confirmed_by : "" }, "✔ Clinician-confirmed");
    return el("span", { class: "badge", text: "Patient-entered" });
  };
  Portal.badge = (text, kind) => el("span", { class: "badge " + (kind || ""), text });

  // ---------- sections / nav / router ----------
  Portal.registerSection = function (s) {
    if (!s || !s.id || typeof s.render !== "function") { console.error("bad section", s); return; }
    const i = Portal.sections.findIndex((x) => x.id === s.id);
    s = { order: 500, icon: "•", ...s };
    if (i >= 0) Portal.sections[i] = s; else Portal.sections.push(s);
    Portal.sections.sort((a, b) => a.order - b.order);
    if (Portal.user) buildNav();
  };
  Portal.badges = {};
  Portal.setBadge = function (id, n) { Portal.badges[id] = n; buildNav(); };
  Portal.navigate = (id) => { location.hash = "#/" + id; };

  function buildNav() {
    const list = document.getElementById("nav-list"); Portal.clear(list);
    const cur = currentId();
    Portal.sections.filter((s) => !s.hidden).forEach((s) => {
      const n = Portal.badges[s.id];
      const a = el("a", { href: "#/" + s.id, "aria-current": s.id === cur ? "page" : null },
        el("span", { "aria-hidden": "true", text: s.icon }), s.title,
        n ? el("span", { class: "nav-badge", "aria-label": n + " new", text: String(n) }) : null);
      list.append(el("li", {}, a));
    });
  }
  function currentId() { return (location.hash.replace(/^#\//, "").split(/[?/]/)[0]) || "dashboard"; }

  let renderToken = 0;
  async function route() {
    if (!Portal.user) return;
    const id = currentId();
    if (id === "care") { Portal.navigate("dashboard"); return; }
    if (["med_overview", "med_careteam", "med_corrections"].includes(id)) { Portal.navigate("med_history"); return; }
    const s = Portal.sections.find((x) => x.id === id) || Portal.sections.find((x) => x.id === "dashboard") || Portal.sections[0];
    const main = Portal.clear(document.getElementById("main"));
    document.getElementById("nav-main").classList.remove("open");
    document.getElementById("menu-btn").setAttribute("aria-expanded", "false");
    buildNav();
    if (!s) { main.append(el("p", { class: "spinner", text: "Loading…" })); return; }
    const my = ++renderToken;
    main.append(el("h2", { text: s.title, tabindex: "-1", id: "page-title" }));
    const body = el("div", {}, el("p", { class: "spinner", text: "Loading…" }));
    main.append(body);
    document.title = s.title + " – Platypus" + (document.documentElement.dataset.demo === "1" ? " (Demo)" : "");
    try {
      const c = el("div"); await s.render(c);
      if (my === renderToken) { body.replaceWith(c); }
    } catch (e) {
      console.error(e);
      if (my === renderToken) body.replaceWith(el("div", { class: "card err", role: "alert" },
        el("p", { text: "This page couldn't load: " + e.message }),
        el("button", { class: "btn", onclick: route, text: "Try again" })));
    }
    document.getElementById("page-title")?.focus();
  }
  Portal.refresh = route;
  window.addEventListener("hashchange", route);

  // ---------- accessibility preferences (non-language) ----------
  Portal.prefs = {
    get() { try { return JSON.parse(localStorage.getItem("hp_prefs") || "{}"); } catch { return {}; } },
    set(p) { localStorage.setItem("hp_prefs", JSON.stringify(p)); Portal.prefs.apply(); },
    apply() {
      const p = Portal.prefs.get();
      document.documentElement.dataset.size = p.size || "normal";
      document.documentElement.classList.toggle("hc", !!p.contrast);
    },
  };
  Portal.prefs.apply();

  // ---------- auth screens ----------
  // The sign-in screens come from auth_patient.js (Portal.authRenderer, email + password + emailed code).
  function showAuth(mode, notice) {
    document.getElementById("app-shell").hidden = true;
    const host = Portal.clear(document.getElementById("auth-view")); host.hidden = false;
    host.removeAttribute("aria-hidden");
    if (typeof Portal.authRenderer !== "function") {
      host.append(el("div", { class: "card err auth", role: "alert", text: "The sign-in screen failed to load. Please reload the page." }),
        el("button", { class: "btn", onclick: () => location.reload(), text: "Reload" }));
      return;
    }
    Portal.authRenderer(host, { mode: mode || "login", notice, onSuccess: () => boot() });
  }

  async function logout() {
    try { await Portal.post("/api/auth/logout", {}, { noAuthRedirect: true }); }
    catch (e) { Portal.toast("Signed out on this device. The server could not be told (" + e.message + ").", "error"); }
    Portal.user = null; location.hash = ""; showAuth();
  }

  async function boot() {
    try { Portal.user = await Portal.get("/api/auth/me", null, { noAuthRedirect: true }); }
    catch (e) {
      if (e.status === 401) { Portal.user = null; return showAuth(); }
      // Network down or server error: say so, offer Retry (never show a blank page or an endless spinner).
      const host = Portal.clear(document.getElementById("auth-view")); host.hidden = false;
      host.append(el("div", { class: "card err auth", role: "alert" },
        el("p", { text: e.network ? e.message : "The server had a problem (" + e.message + ")." }),
        el("button", { class: "btn", onclick: () => boot(), text: "Retry" })));
      return;
    }
    // Empty the sign-in/OTP screen (not just hide it) so screen readers can't reach it after sign-in.
    const authView = Portal.clear(document.getElementById("auth-view"));
    authView.hidden = true;
    authView.setAttribute("aria-hidden", "true");
    document.getElementById("app-shell").hidden = false;
    document.getElementById("who").textContent = Portal.user.display_name;
    await route();
    Portal.emit("signin");
    (document.getElementById("page-title") || document.getElementById("main"))?.focus();
  }

  Portal.boot = boot;
  Portal.showAuth = showAuth;

  // ---------- tiny event bus ----------
  const handlers = {};
  Portal.on = (ev, fn) => ((handlers[ev] = handlers[ev] || []).push(fn));
  Portal.emit = (ev, d) => (handlers[ev] || []).forEach((fn) => { try { fn(d); } catch (e) { console.error(e); } });

  // All module <script> tags in index.html have run by DOMContentLoaded (they are plain, ordered, non-deferred).
  document.addEventListener("DOMContentLoaded", async () => {
    booted = true;
    document.getElementById("menu-btn").addEventListener("click", (e) => {
      const nav = document.getElementById("nav-main"); const open = nav.classList.toggle("open");
      e.currentTarget.setAttribute("aria-expanded", String(open));
    });
    document.getElementById("logout-btn").addEventListener("click", logout);
    await boot();
    if ("serviceWorker" in navigator && window.isSecureContext)
      navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("Offline support unavailable:", e));
  });
})();

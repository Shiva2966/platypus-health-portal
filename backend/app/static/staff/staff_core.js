/* Staff portal core: DOM helper (textContent only - never innerHTML with data), API client,
 * dialogs, toasts, formatting. Everything hangs off window.S. */
(function () {
  const S = (window.S = { me: null, pages: {}, nav: [] });

  // ---------- loud failures: a script in index.html that fails to load or throws while loading ----------
  const loadProblems = [];
  let loaded = false;
  window.addEventListener("DOMContentLoaded", () => { loaded = true; });
  window.addEventListener("error", (ev) => {
    const t = ev.target;
    let what = null;
    if (t && t !== window && t.tagName === "SCRIPT") what = t.getAttribute("src") + " (not loaded)";
    else if (!loaded && ev.filename) what = (ev.filename.split("/").pop() || "a script") + " (error: " + ev.message + ")";
    if (!what) return;
    loadProblems.push(what);
    let bar = document.getElementById("load-error");
    if (!bar) {
      bar = document.createElement("div"); bar.id = "load-error"; bar.className = "card err"; bar.setAttribute("role", "alert");
      (document.body || document.documentElement).prepend(bar);
    }
    S.clear(bar);
    const p = document.createElement("p");
    p.textContent = "Part of the staff portal failed to load: " + loadProblems.join("; ") + ". Check your connection and reload.";
    const b = document.createElement("button"); b.className = "btn"; b.textContent = "Reload"; b.addEventListener("click", () => location.reload());
    bar.append(p, b);
  }, true);

  S.el = function el(tag, attrs, ...kids) {
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
  const el = S.el;
  S.clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };

  // ---------- formatting ----------
  S.fmtDate = (s) => {
    if (!s) return "";
    const d = new Date(s.length <= 10 ? s + "T00:00:00" : (/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z"));
    if (isNaN(d)) return s;
    return s.length <= 10 ? d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
      : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  S.label = (s) => (s || "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  S.size = (n) => (n == null ? "" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB");
  S.ROLE = { front_desk: "Front desk", nurse: "Nurse", physician: "Physician", admin: "Administrator" };
  S.statusBadge = (st) => {
    const kind = { pending: "warn", requested: "warn", approved: "good", booked: "good", denied: "bad", declined: "bad",
      expired: "", rescheduled: "info", cancelled: "", revoked: "bad", active: "good" }[st] || "";
    return el("span", { class: "badge " + kind, text: S.label(st) });
  };

  // ---------- announcements ----------
  S.announce = (msg) => { const n = document.getElementById("sr-live"); n.textContent = ""; setTimeout(() => (n.textContent = msg), 50); };
  S.toast = function (msg, kind) {
    const t = el("div", { class: "toast " + (kind || ""), role: kind === "error" ? "alert" : "status", text: msg });
    document.getElementById("toasts").append(t);
    setTimeout(() => t.remove(), kind === "error" ? 8000 : 4500);
  };

  // ---------- API: the ONE way the staff portal talks to the server ----------
  // opts: {noAuthRedirect, timeoutMs}. Errors carry .status, .fields, .network (no answer / timeout), .sessionExpired.
  S.DEFAULT_TIMEOUT_MS = 30000;
  S.api = async function (method, url, body, opts) {
    opts = opts || {};
    const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined && body !== null) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
    const ctl = new AbortController(); init.signal = ctl.signal;
    const timer = setTimeout(() => ctl.abort(), opts.timeoutMs || S.DEFAULT_TIMEOUT_MS);
    let res;
    try { res = await fetch(url, init); }
    catch (e) {
      const timedOut = e && e.name === "AbortError";
      const err = new Error(timedOut ? "The server took too long to answer. Please try again."
        : navigator.onLine ? "Can't reach the server. Please try again." : "You are offline. Reconnect and try again.");
      err.network = true; err.timeout = timedOut;
      S.setOffline(err.message);
      throw err;
    } finally { clearTimeout(timer); }
    S.setOffline(null);
    const ct = res.headers.get("content-type") || "";
    let data = null;
    if (ct.includes("json")) {
      try { data = await res.json(); }
      catch (e) { if (res.ok) { const err = new Error("The server sent an unreadable answer. Please try again."); err.status = res.status; throw err; } }
    }
    if (res.status === 401 && !opts.noAuthRedirect) {
      const wasSignedIn = !!S.me;
      S.me = null;
      if (wasSignedIn) S.onSignedOut("Your session has expired. Please sign in again.");
      const err = new Error("Your session has expired. Please sign in again.");
      err.status = 401; err.sessionExpired = true; err.fields = {};
      throw err;
    }
    if (!res.ok) {
      if (res.status === 403 && url !== "/api/staff/me" && S.syncIdentity) {
        try { if (await S.syncIdentity()) { S.toast("The staff account changed in another tab. Updating this page."); S.route(); } } catch (_) {}
      }
      let msg = data && (typeof data.detail === "string" ? data.detail : null);
      if (!msg && data && Array.isArray(data.detail)) msg = data.detail.map((d) => d.msg).join("; ");
      const err = new Error(msg || (res.status === 503 ? "This service is temporarily unavailable." : `Something went wrong (error ${res.status}).`));
      err.status = res.status; err.fields = (data && data.fields) || {};
      throw err;
    }
    return data;
  };
  ["get", "post", "put", "patch", "delete"].forEach((m) => (S[m] = (u, b, o) => S.api(m.toUpperCase(), u, b, o)));
  S.qs = (o) => { const p = new URLSearchParams(); Object.entries(o).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") p.set(k, v); }); const s = p.toString(); return s ? "?" + s : ""; };

  // ---------- reusable states ----------
  S.errorBox = (err, retry) => el("div", { class: "card err", role: "alert" },
    el("p", { text: (err && err.message) || "Something went wrong." }),
    retry ? el("button", { class: "btn", onclick: retry, text: "Try again" }) : null);
  S.partial = (errors, retry) => {
    const msgs = Object.values(errors || {});
    if (!msgs.length) return null;
    return el("div", { class: "card warn", role: "status" },
      el("strong", { text: "Some information couldn't be loaded. " }), msgs.join(" "), " ",
      retry ? el("button", { class: "btn sm", onclick: retry, text: "Retry" }) : null);
  };
  S.empty = (title, text, action) => el("div", { class: "empty" }, el("h3", { text: title }), text ? el("p", { class: "muted", text }) : null, action || null);

  // ---------- dialogs ----------
  S.dialog = function (title, build, cls) {
    return new Promise((resolve) => {
      const dlg = el("dialog", { "aria-labelledby": "dlg-t", class: cls || "" });
      let done = false;
      const close = (v) => { if (done) return; done = true; dlg.close(); dlg.remove(); resolve(v); };
      dlg.append(el("h3", { id: "dlg-t", text: title }), build(close));
      dlg.addEventListener("cancel", () => { done = true; dlg.remove(); resolve(undefined); });
      document.body.append(dlg);
      dlg.showModal();
    });
  };
  /* fields: [{name,label,type,required,options,help,value,placeholder,maxlength}] ; onSubmit(values) may throw */
  S.form = function ({ title, fields, submitLabel, onSubmit, intro, cls }) {
    return S.dialog(title, (close) => {
      const form = el("form", { novalidate: true });
      const inputs = {};
      if (intro) form.append(el("p", { class: "muted", text: intro }));
      fields.forEach((f, i) => {
        const id = `f${i}-${f.name}`;
        let input;
        if (f.type === "textarea") input = el("textarea", { id, maxlength: f.maxlength || 1000, placeholder: f.placeholder });
        else if (f.type === "select") { input = el("select", { id }); (f.options || []).forEach((o) => input.append(el("option", { value: o.value ?? o, text: o.label ?? S.label(o) }))); }
        else input = el("input", { id, type: f.type || "text", maxlength: f.maxlength || 300, placeholder: f.placeholder, autocomplete: f.autocomplete || "off", min: f.min, max: f.max });
        if (f.value !== undefined) input.value = f.value;
        if (f.required) input.setAttribute("aria-required", "true");
        inputs[f.name] = input;
        form.append(el("label", { for: id }, f.label, f.required ? null : el("span", { class: "muted", text: " (optional)" })), input,
          el("p", { class: "help", text: f.help || "" }));
      });
      const status = el("p", { class: "field-error", role: "alert" });
      const submit = el("button", { class: "btn primary", type: "submit", text: submitLabel || "Save" });
      form.append(status, el("div", { class: "row" }, submit, el("button", { class: "btn", type: "button", onclick: () => close(undefined), text: "Cancel" })));
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault(); status.textContent = "";
        const vals = {};
        for (const f of fields) vals[f.name] = inputs[f.name].value.trim();
        const missing = fields.find((f) => f.required && !vals[f.name]);
        if (missing) { status.textContent = `${missing.label} is required.`; inputs[missing.name].focus(); return; }
        submit.disabled = true;
        try { const r = await onSubmit(vals); close(r === undefined ? true : r); }
        catch (e) { status.textContent = e.message; submit.disabled = false; }
      });
      setTimeout(() => form.querySelector("input,textarea,select")?.focus(), 30);
      return form;
    }, cls);
  };
  S.confirm = (msg, ok) => S.dialog("Please confirm", (close) =>
    el("div", {}, el("p", { text: msg }), el("div", { class: "row" },
      el("button", { class: "btn danger", onclick: () => close(true), text: ok || "Yes, continue" }),
      el("button", { class: "btn", onclick: () => close(false), text: "Cancel" }))), "narrow").then((v) => !!v);

  // ---------- generic list loader with retry ----------
  S.load = async function (container, fn) {
    S.clear(container).append(el("p", { class: "spinner", role: "status", text: "Loading…" }));
    try { const out = await fn(); S.clear(container); return out; }
    catch (e) { S.clear(container).append(S.errorBox(e, () => S.load(container, fn))); throw e; }
  };

  // ---------- connection banner: browser offline OR the server didn't answer ----------
  const off = document.getElementById("offline-banner");
  const offText = document.getElementById("offline-text");
  let serverDown = null;
  const sync = () => {
    off.hidden = navigator.onLine && !serverDown;
    offText.textContent = !navigator.onLine ? "You appear to be offline. Changes can't be saved until you reconnect."
      : (serverDown || "") + " Check your connection, then retry.";
  };
  S.setOffline = (msg) => { if ((msg || null) !== serverDown) { serverDown = msg || null; sync(); } };
  document.getElementById("offline-retry").addEventListener("click", () => { S.setOffline(null); S.me ? S.route() : location.reload(); });
  window.addEventListener("online", () => { sync(); S.toast("Back online.", "ok"); S.refreshBell(); });
  window.addEventListener("offline", sync);
  sync();
})();

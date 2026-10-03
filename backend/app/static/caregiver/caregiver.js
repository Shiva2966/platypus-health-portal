/* Caregiver portal (W9). Separate account from patients. Uses textContent only (no innerHTML with data). */
(function () {
  const main = document.getElementById("cg-main");
  const $ = (s) => document.querySelector(s);
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, "");
      else n.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) { if (kid === null || kid === undefined || kid === false) continue; n.append(kid.nodeType ? kid : document.createTextNode(String(kid))); }
    return n;
  }
  function toast(msg, kind) {
    const t = el("div", { class: "toast " + (kind || ""), role: kind === "error" ? "alert" : "status", text: msg });
    $("#toasts").append(t); setTimeout(() => t.remove(), 5000);
  }
  // The ONE way this page talks to the server: 30 s timeout; errors carry .status, .fields, .network.
  async function api(method, url, body) {
    const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
    const ctl = new AbortController(); init.signal = ctl.signal;
    const timer = setTimeout(() => ctl.abort(), 30000);
    let res;
    try { res = await fetch(url, init); }
    catch (e) {
      const err = new Error(e && e.name === "AbortError" ? "The server took too long to answer. Please try again."
        : "Can't reach the server. Check your connection and try again.");
      err.network = true; throw err;
    } finally { clearTimeout(timer); }
    let data = null;
    if ((res.headers.get("content-type") || "").includes("json")) {
      try { data = await res.json(); }
      catch (e) { if (res.ok) throw new Error("The server sent an unreadable answer. Please try again."); }
    }
    if (!res.ok) {
      const err = new Error((data && typeof data.detail === "string" && data.detail) || `Something went wrong (error ${res.status}).`);
      err.status = res.status; err.fields = (data && data.fields) || {}; throw err;
    }
    return data;
  }
  const fmt = (s) => { if (!s) return ""; const d = new Date(s); return isNaN(d) ? s : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); };
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
  const errBox = () => el("p", { class: "field-error", role: "alert" });

  function field(label, input, help) {
    const id = "f" + Math.random().toString(36).slice(2, 8); input.id = id;
    return el("div", {}, el("label", { for: id, text: label }), input, help ? el("p", { class: "help", text: help }) : null);
  }

  // ---------------- invitation + login ----------------
  async function showInvite(token) {
    clear(main);
    let prev;
    try { prev = await api("GET", "/api/caregiver/invite/" + encodeURIComponent(token)); }
    catch (e) { main.append(el("div", { class: "card err", role: "alert", text: e.message })); return; }
    const name = el("input", { type: "text", autocomplete: "name", maxlength: 200 });
    const pw = el("input", { type: "password", autocomplete: prev.has_account ? "current-password" : "new-password", maxlength: 128 });
    const status = errBox();
    const list = prev.record_categories.map((c) => el("li", { text: "See " + c.toLowerCase() }));
    if (prev.manage_appointments) list.push(el("li", { text: "Request, reschedule and cancel appointments" }));
    if (prev.manage_bills) list.push(el("li", { text: "See bills and insurance claims" }));
    const accept = el("button", { class: "btn primary", type: "submit", text: "Accept and continue" });
    const form = el("form", { novalidate: true },
      prev.has_account ? null : field("Your name", name),
      field(prev.has_account ? "Your caregiver password" : "Choose a password (at least 10 characters)", pw), status,
      el("div", { class: "row" }, accept, el("button", { class: "btn", type: "button", text: "Decline", onclick: async () => {
        try { await api("POST", "/api/caregiver/decline", { token }); clear(main).append(el("div", { class: "card", text: "You declined. Nothing was shared." })); } catch (e) { toast(e.message, "error"); } } })));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault(); status.textContent = ""; accept.disabled = true;
      try { await api("POST", "/api/caregiver/accept", { token, name: name.value, password: pw.value }); history.replaceState(null, "", "/caregiver"); boot(); }
      catch (e) { status.textContent = e.message + " " + Object.values(e.fields || {}).join(" "); accept.disabled = false; }
    });
    main.append(el("div", { class: "card" }, el("h2", { text: prev.invited_by + " invited you as a caregiver" }),
      el("p", { text: "If you accept, you will be able to:" }), el("ul", {}, list),
      el("p", { class: "muted", text: "Access ends on " + fmt(prev.access_until) + ". " + prev.invited_by + " can remove your access at any time, and they can see everything you do." }), form));
  }

  function showLogin() {
    clear(main);
    const email = el("input", { type: "email", autocomplete: "username", maxlength: 254 });
    const pw = el("input", { type: "password", autocomplete: "current-password", maxlength: 128 });
    const status = errBox();
    const form = el("form", { novalidate: true }, field("Email", email), field("Password", pw), status,
      el("button", { class: "btn primary", type: "submit", text: "Sign in" }));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault(); status.textContent = "";
      try { await api("POST", "/api/caregiver/login", { email: email.value, password: pw.value }); boot(); } catch (e) { status.textContent = e.message; }
    });
    main.append(el("div", { class: "card" }, el("h2", { text: "Caregiver sign-in" }),
      el("p", { class: "muted", text: "This is for people who were invited to help someone with their health account. Patients sign in on the main page." }), form,
      el("p", {}, el("a", { href: "/", text: "Go to the patient sign-in" }))));
  }

  // ---------------- home ----------------
  async function showHome(me) {
    $("#cg-who").textContent = me.name; $("#cg-logout").hidden = false;
    clear(main);
    const data = await api("GET", "/api/caregiver/patients");
    main.append(el("h2", { text: "People you help" }));
    if (!data.items.length) { main.append(el("div", { class: "card empty", text: "You don't have access to anyone right now. Access may have ended or been removed." })); return; }
    data.items.forEach((p) => main.append(el("div", { class: "card" }, el("h3", { text: p.patient_name }),
      el("p", { class: "muted", text: (p.relationship ? "You are their " + p.relationship + ". " : "") + "Access until " + fmt(p.access_until) + "." }),
      el("button", { class: "btn primary", text: "Open", onclick: () => showPatient(p) }))));
  }

  async function showPatient(p) {
    clear(main);
    const body = el("div");
    const tabs = el("div", { class: "tabs", role: "tablist" });
    const items = [];
    if (p.manage_appointments) items.push(["Appointments", () => apptTab(p, body)]);
    p.record_categories.forEach((c) => items.push([c.label, () => recordsTab(p, c, body)]));
    if (p.manage_bills) items.push(["Bills", () => billsTab(p, body)]);
    items.forEach(([label, fn], i) => {
      const b = el("button", { class: "btn small", role: "tab", "aria-selected": "false", text: label, onclick: () => { tabs.querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", "false")); b.setAttribute("aria-selected", "true"); run(fn); } });
      tabs.append(b); if (i === 0) setTimeout(() => b.click(), 0);
    });
    async function run(fn) { clear(body).append(el("p", { class: "spinner", text: "Loading..." })); try { await fn(); } catch (e) { clear(body).append(el("div", { class: "card err", role: "alert", text: e.message })); if (e.status === 401) setTimeout(boot, 1500); } }
    main.append(el("button", { class: "btn small", text: "Back", onclick: boot }), el("h2", { text: p.patient_name }),
      el("p", { class: "muted", text: "You are acting as a caregiver. Everything you do here is recorded and shown to " + p.patient_name + "." }), tabs, body);
    if (!items.length) body.append(el("p", { class: "empty", text: "No permissions were granted." }));
  }

  function kv(obj, skip) {
    const dl = el("dl", { class: "kv" });
    Object.entries(obj).forEach(([k, v]) => { if (skip.includes(k) || v === null || v === "" || v === undefined) return; dl.append(el("dt", { text: k.replace(/_/g, " ") }), el("dd", { text: typeof v === "object" ? JSON.stringify(v) : String(v) })); });
    return dl;
  }
  async function recordsTab(p, c, body) {
    const d = await api("GET", `/api/caregiver/patients/${p.patient_id}/records/${c.key}`);
    clear(body);
    if (!d.items.length) return body.append(el("div", { class: "card empty", text: "Nothing recorded here." }));
    d.items.forEach((r) => body.append(el("div", { class: "card" }, kv(r, ["id", "patient_id", "created_at", "updated_at", "confirmed_at"]))));
  }
  async function billsTab(p, body) {
    const d = await api("GET", `/api/caregiver/patients/${p.patient_id}/bills`);
    clear(body);
    body.append(el("h3", { text: "Bills" }));
    if (!d.bills.length) body.append(el("p", { class: "muted", text: "No bills." }));
    d.bills.forEach((r) => body.append(el("div", { class: "card" }, kv(r, ["id", "patient_id", "dedupe_key", "created_at", "updated_at"]))));
    body.append(el("h3", { text: "Insurance claims" }));
    if (!d.claims.length) body.append(el("p", { class: "muted", text: "No claims." }));
    d.claims.forEach((r) => body.append(el("div", { class: "card" }, kv(r, ["id", "patient_id", "claim_key", "created_at", "updated_at"]))));
  }

  async function apptTab(p, body) {
    const d = await api("GET", `/api/caregiver/patients/${p.patient_id}/appointments`);
    clear(body);
    body.append(el("div", { class: "card warn" }, el("strong", { text: "Not a diagnosis. " }), d.disclaimer));
    body.append(el("button", { class: "btn primary", text: "Request an appointment", onclick: () => newAppt(p, body) }));
    if (!d.items.length) body.append(el("div", { class: "card empty", text: "No appointments yet." }));
    d.items.forEach((a) => {
      const acts = [];
      if (a.can_reschedule) acts.push(el("button", { class: "btn small", text: "Ask to reschedule", onclick: async () => {
        const t = prompt("When would suit better?"); if (!t) return;
        try { await api("POST", `/api/caregiver/patients/${p.patient_id}/appointments/${a.id}/reschedule`, { preferred_times: t }); apptTab(p, body); } catch (e) { toast(e.message, "error"); } } }));
      if (a.needs_my_confirmation) acts.push(el("button", { class: "btn small primary", text: "Accept new time", onclick: async () => {
        try { await api("POST", `/api/caregiver/patients/${p.patient_id}/appointments/${a.id}/confirm`); apptTab(p, body); } catch (e) { toast(e.message, "error"); } } }));
      if (a.can_cancel && a.status !== "draft") acts.push(el("button", { class: "btn small danger", text: "Cancel", onclick: async () => {
        if (!confirm("Cancel this appointment?")) return;
        try { await api("POST", `/api/caregiver/patients/${p.patient_id}/appointments/${a.id}/cancel`); apptTab(p, body); } catch (e) { toast(e.message, "error"); } } }));
      body.append(el("div", { class: "card" }, el("h3", { text: (a.provider ? a.provider.name : "No provider") }),
        el("p", {}, el("span", { class: "badge info", text: a.status }), a.scheduled_for ? " " + a.scheduled_for : ""),
        el("p", { text: a.intake.reason || "" }), a.staff_note ? el("p", { class: "muted", text: "Clinic note: " + a.staff_note }) : null, el("div", { class: "row" }, acts)));
    });
  }

  async function newAppt(p, body) {
    clear(body);
    const provs = await api("GET", `/api/caregiver/patients/${p.patient_id}/providers`);
    const sel = el("select", {}, el("option", { value: "", text: "Choose..." }), provs.items.map((x) => el("option", { value: x.id, text: x.name + (x.specialty ? " - " + x.specialty : "") })));
    const reason = el("textarea", { maxlength: 1000 }), avail = el("textarea", { maxlength: 500 });
    const vt = el("select", {}, el("option", { value: "in_person", text: "In person" }), el("option", { value: "video", text: "Video visit" }), el("option", { value: "phone", text: "Phone call" }));
    const status = errBox();
    const form = el("form", { novalidate: true }, field("Who to see", sel), field("Why? (in their own words)", reason), field("When are they available?", avail), field("Type of visit", vt), status,
      el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit", text: "Send request" }), el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => apptTab(p, body) })));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault(); status.textContent = "";
      try { await api("POST", `/api/caregiver/patients/${p.patient_id}/appointments`, { provider_id: sel.value || null, submit: true, intake: { reason: reason.value, availability: avail.value, visit_type: vt.value } }); toast("Request sent", "ok"); apptTab(p, body); }
      catch (e) { status.textContent = e.message + " " + Object.values(e.fields || {}).join(" "); }
    });
    body.append(el("div", { class: "card warn" }, el("strong", { text: "Not a diagnosis. " }), "This app does not triage emergencies. For an emergency, call local emergency services."), form);
  }

  async function boot() {
    const token = new URLSearchParams(location.search).get("invite");
    let me = null;
    try { me = await api("GET", "/api/caregiver/me"); }
    catch (e) {
      if (e.status !== 401) { // network down or server error: say so, never a blank page
        clear(main).append(el("div", { class: "card err", role: "alert" }, el("p", { text: e.message }),
          el("button", { class: "btn", text: "Retry", onclick: boot })));
        return;
      }
    }
    if (token) return showInvite(token);
    if (!me) { $("#cg-who").textContent = ""; $("#cg-logout").hidden = true; return showLogin(); }
    try { await showHome(me); }
    catch (e) {
      if (e.status === 401) return showLogin();
      clear(main).append(el("div", { class: "card err", role: "alert" }, el("p", { text: e.message }),
        el("button", { class: "btn", text: "Retry", onclick: boot })));
    }
  }
  $("#cg-logout").addEventListener("click", async () => {
    try { await api("POST", "/api/caregiver/logout", {}); } catch (e) { toast("Signed out here, but the server could not be told: " + e.message, "error"); }
    boot();
  });
  boot();
})();

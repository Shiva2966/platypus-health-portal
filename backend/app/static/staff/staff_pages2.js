/* Staff pages (2/2): appointments inbox + intake, document search, notifications, audit log, staff admin. */
(function () {
  const { el } = S;

  // ------------------------------------------------------------------ appointments inbox
  S.pages.appointments = async function (c, [aid]) {
    if (aid) return appointmentDetail(c, aid);
    c.append(el("div", { class: "row between" }, el("h2", { text: "Appointment & intake inbox" }), el("button", { class: "btn", onclick: () => draw(), text: "Refresh" })));
    const sel = el("select", { id: "ap-status" }, [["", "All appointments"], ["requested", "New requests"], ["booked", "Booked"], ["rescheduled", "Rescheduled (waiting for patient)"], ["declined", "Declined"], ["cancelled", "Cancelled"]]
      .map(([v, t]) => el("option", { value: v, text: t })));
    const list = el("div");
    c.append(el("label", { for: "ap-status", text: "Show" }), sel, el("div", { class: "spacer" }), list);
    let loadingAppointments = false;
    const draw = async () => {
      if (loadingAppointments || !c.isConnected) return;
      loadingAppointments = true;
      try {
        const r = await S.load(list, () => S.get("/api/staff/appointments" + S.qs({ status: sel.value })));
        if (!c.isConnected) return;
        if (!S.me.provider_id) return list.append(S.empty("Hospital assignment needed", "An administrator must link your account to the hospital under Staff accounts → Organization."));
        if (!r.appointments.length) return list.append(S.empty("No appointments here", sel.value
          ? "No appointments match this status. Select All appointments to check other statuses."
          : "Sent requests for your hospital appear here. Patient drafts are not sent; the hospital selected by the patient must match your staff organization."));
        list.append(el("div", { class: "table-wrap" }, el("table", {}, el("caption", { class: "sr", text: "Appointment requests" }),
          el("thead", {}, el("tr", {}, ["Submitted", "Patient", "Time", "Status", ""].map((h) => el("th", { scope: "col", text: h })))),
          el("tbody", {}, r.appointments.map((a) => el("tr", {},
            el("td", { text: S.fmtDate(a.submitted_at) }), el("td", {}, el("strong", { text: a.patient ? a.patient.name : "—" }), a.patient ? el("div", { class: "small muted", text: "Born " + a.patient.dob_masked.slice(0, 4) }) : null),
            el("td", { text: a.scheduled_for ? S.fmtDate(a.scheduled_for) : "—" }), el("td", {}, S.statusBadge(a.status)),
            el("td", {}, el("a", { class: "btn sm primary", href: `#/appointments/${a.id}`, text: "Review", "aria-label": `Review request from ${a.patient ? a.patient.name : "patient"}` }))))))));
      } catch (e) { /* S.load displays errors. */ }
      finally { loadingAppointments = false; }
    };
    sel.addEventListener("change", draw);
    c.append(el("p", { class: "small muted", text: "Updates automatically every 15 seconds while this page is open." }));
    await draw();
    S.timers.push(setInterval(() => {
      if (!document.hidden && c.isConnected && !document.querySelector("dialog[open]")) draw();
    }, 15000));
  };

  async function appointmentDetail(c, aid) {
    const a = await S.load(c, () => S.get(`/api/staff/appointments/${aid}`));
    const reload = () => S.route();
    c.append(el("p", {}, el("a", { href: "#/appointments", text: "← Back to inbox" })),
      el("div", { class: "row between" }, el("h2", { text: `Appointment request – ${a.patient ? a.patient.name : ""}` }), S.statusBadge(a.status)));
    c.append(el("section", { class: "card", "aria-labelledby": "h-int" }, el("h3", { id: "h-int", text: "Intake (as written by the patient)" }),
      el("p", { class: "small muted", text: "This is the patient's own description for scheduling. It is not a diagnosis." }),
      a.intake_items.length ? el("dl", { class: "kv" }, a.intake_items.flatMap((i) => [el("dt", { text: i.label }), el("dd", { text: i.value })]))
        : el("p", { class: "muted", text: "No intake details were provided." })));
    if (a.contact.length) c.append(el("section", { class: "card" }, el("h3", { text: "Contact details sent with the request" }),
      el("dl", { class: "kv" }, a.contact.filter((i) => i.key !== "patient_id").flatMap((i) => [el("dt", { text: i.label }), el("dd", { text: i.value })]))));
    c.append(el("section", { class: "card" }, el("h3", { text: "Scheduling" }),
      el("dl", { class: "kv" }, el("dt", { text: "Time" }), el("dd", { text: a.scheduled_for ? S.fmtDate(a.scheduled_for) : "Not set" }),
        el("dt", { text: "Note to patient" }), el("dd", { text: a.staff_note || "—" })),
      el("div", { class: "spacer" }),
      el("div", { class: "row" },
        el("button", { class: "btn primary", text: "Accept", onclick: () => decide(a, "accept", reload) }),
        el("button", { class: "btn", text: "Propose new time", onclick: () => decide(a, "reschedule", reload) }),
        el("button", { class: "btn danger", text: "Decline", onclick: () => decide(a, "decline", reload) }),
        el("button", { class: "btn", text: "View / export JSON", onclick: () => exportJson(a) }),
        a.patient ? el("a", { class: "btn", href: `#/patient/${a.patient.id}`, text: "Open patient" }) : null)));
  }

  function decide(a, action, done) {
    const title = { accept: "Accept appointment", reschedule: "Propose a new time", decline: "Decline request" }[action];
    const fields = [];
    if (action !== "decline") fields.push({ name: "scheduled_for", label: "Date and time", type: "datetime-local", required: true, value: (a.scheduled_for || "").slice(0, 16) });
    fields.push({ name: "note", label: "Message to the patient", type: "textarea", maxlength: 1000, help: action === "decline" ? "Say why, and suggest another option if you can." : "For example: bring your insurance card." });
    S.form({ title, fields, submitLabel: title, onSubmit: async (v) => {
      await S.post(`/api/staff/appointments/${a.id}/decision`, { action, scheduled_for: v.scheduled_for || null, note: v.note || null });
      S.toast("Saved. The patient was notified.", "ok");
    } }).then((ok) => ok && done());
  }

  async function exportJson(a) {
    let data;
    try { data = await S.get(`/api/staff/appointments/${a.id}/export.json`); } catch (e) { S.toast(e.message, "error"); return; }
    const text = JSON.stringify(data, null, 2);
    S.dialog("Intake export (FHIR-style JSON)", (close) => el("div", {},
      el("p", { class: "small muted", text: "Loosely FHIR-shaped bundle: Patient, Appointment, QuestionnaireResponse. Demo data only." }),
      el("pre", { class: "json", tabindex: "0", text }),
      el("div", { class: "row" },
        el("button", { class: "btn primary", text: "Download .json", onclick: () => {
          const l = el("a", { href: URL.createObjectURL(new Blob([text], { type: "application/json" })), download: `intake-${a.id}.json` });
          document.body.append(l); l.click(); l.remove(); } }),
        el("button", { class: "btn", text: "Copy", onclick: () => navigator.clipboard?.writeText(text).then(() => S.toast("Copied.", "ok"), () => S.toast("Copy failed.", "error")) }),
        el("button", { class: "btn", text: "Close", onclick: () => close(true) }))));
  }

  // ------------------------------------------------------------------ global document search
  S.pages.documents = async function (c) {
    c.append(el("h2", { text: "Search shared documents" }),
      el("p", { class: "muted", text: "Searches only documents patients have shared with your organization (and that your role may see)." }));
    const q = el("input", { id: "ds-q", type: "search", maxlength: 100, "aria-required": "true", value: S.pendingSearch || "" });
    const out = el("div", { "aria-live": "polite" });
    const form = el("form", { class: "card", novalidate: true }, el("label", { for: "ds-q", text: "Document name" }), q, el("div", { class: "spacer" }), el("button", { class: "btn primary", type: "submit", text: "Search" }));
    c.append(form, out);
    const run = async () => {
      if (q.value.trim().length < 2) { S.clear(out).append(el("p", { class: "field-error", role: "alert", text: "Type at least 2 letters." })); return; }
      try {
        const r = await S.load(out, () => S.get("/api/staff/documents/search" + S.qs({ q: q.value.trim() })));
        if (!r.results.length) return out.append(S.empty("No matching shared documents", "Documents that patients have not shared with you never appear here."));
        S.announce(`${r.results.length} document(s) found`);
        out.append(el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Document", "Patient", "Category", "Source", "Access ends"].map((h) => el("th", { scope: "col", text: h })))),
          el("tbody", {}, r.results.map((d) => el("tr", {}, el("td", {}, el("strong", { text: d.name })), el("td", {}, el("a", { href: `#/patient/${d.patient_id}`, text: d.patient_name || "Patient" })),
            el("td", { text: S.label(d.category) }), el("td", { text: d.source_label }), el("td", { text: S.fmtDate(d.grant_expires_at) })))))));
      } catch (e) { /* shown */ }
    };
    form.addEventListener("submit", (ev) => { ev.preventDefault(); run(); });
    if (S.pendingSearch) { S.pendingSearch = ""; run(); } else q.focus();
  };

  // ------------------------------------------------------------------ notifications
  S.pages.notifications = async function (c) {
    c.append(el("div", { class: "row between" }, el("h2", { text: "Notifications" }),
      el("div", { class: "row" }, el("button", { class: "btn", id: "mark-all", text: "Mark all as read" }), el("button", { class: "btn", id: "prefs-btn", text: "Settings" }))));
    const filters = el("div", { class: "row" });
    const list = el("div", { "aria-live": "polite" });
    c.append(filters, el("div", { class: "spacer" }), list);
    let unreadOnly = false, kind = "";
    const unreadCb = el("input", { type: "checkbox", id: "nf-unread" });
    const kindSel = el("select", { id: "nf-kind", "aria-label": "Filter by type" }, el("option", { value: "", text: "All types" }));
    filters.append(el("label", { class: "check", for: "nf-unread" }, unreadCb, "Unread only"), kindSel);
    const draw = async () => {
      try {
        const r = await S.load(list, () => S.get("/api/notifications" + S.qs({ as: "staff", unread: unreadOnly ? "true" : "", kind, limit: 60 })));
        const kl = (k) => (r.kind_labels && r.kind_labels[k]) || S.label(k);
        if (kindSel.children.length === 1) r.kinds.forEach((k) => kindSel.append(el("option", { value: k, text: kl(k) })));
        S.setBell(r.unread);
        if (!r.items.length) return list.append(S.empty(unreadOnly ? "You're all caught up" : "No notifications yet", "New activity from patients and the system will show up here."));
        r.items.forEach((n) => list.append(el("div", { class: "notif" + (n.read ? "" : " unread") },
          el("div", {}, el("h4", {}, n.title, n.read ? null : el("span", { class: "sr", text: " (unread)" })), n.body ? el("div", { text: n.body }) : null,
            el("div", { class: "small muted", text: `${kl(n.kind)} · ${S.fmtDate(n.ts)}` })),
          el("div", { class: "row" },
            n.link ? el("a", { class: "btn sm primary", href: n.link.startsWith("#") ? n.link : "#/dashboard", text: "Open", onclick: () => markRead(n) }) : null,
            n.read ? null : el("button", { class: "btn sm", text: "Mark read", onclick: async () => { await markRead(n); draw(); } })))));
      } catch (e) { /* shown */ }
    };
    const markRead = (n) => S.post(`/api/notifications/${n.id}/read?as=staff`).then((r) => S.setBell(r.unread)).catch((e) => S.toast("Couldn't mark it as read: " + e.message, "error"));
    unreadCb.addEventListener("change", () => { unreadOnly = unreadCb.checked; draw(); });
    kindSel.addEventListener("change", () => { kind = kindSel.value; draw(); });
    c.querySelector("#mark-all").addEventListener("click", async () => { try { await S.post("/api/notifications/read-all?as=staff"); S.setBell(0); S.toast("All marked as read.", "ok"); draw(); } catch (e) { S.toast(e.message, "error"); } });
    c.querySelector("#prefs-btn").addEventListener("click", prefsDialog);
    await draw();
    S.timers.push(setInterval(() => { if (!document.hidden && !document.querySelector("dialog[open]")) draw(); }, 15000));
  };

  async function prefsDialog() {
    let prefs;
    try { prefs = (await S.get("/api/notifications/prefs?as=staff")).prefs; } catch (e) { S.toast(e.message, "error"); return; }
    S.dialog("Notification settings", (close) => {
      const boxes = prefs.map((p) => { const i = el("input", { type: "checkbox", id: "np-" + p.kind }); i.checked = p.enabled; return [p, i]; });
      const status = el("p", { class: "field-error", role: "alert" });
      return el("div", {}, el("p", { class: "muted", text: "Choose which kinds of notifications you want to receive." }),
        boxes.map(([p, i]) => el("label", { class: "check", for: i.id }, i, p.label)), status,
        el("div", { class: "row" }, el("button", { class: "btn primary", text: "Save", onclick: async () => {
          try { await S.put("/api/notifications/prefs?as=staff", { prefs: Object.fromEntries(boxes.map(([p, i]) => [p.kind, i.checked])) }); S.toast("Settings saved.", "ok"); S.refreshBell(); close(true); }
          catch (e) { status.textContent = e.message; } } }),
          el("button", { class: "btn", text: "Cancel", onclick: () => close(false) })));
    }, "narrow");
  }

  // ------------------------------------------------------------------ audit log (admin)
  S.pages.audit = async function (c) {
    c.append(el("h2", { text: "Audit log" }), el("p", { class: "muted", text: "Every view, request, approval and sign-in. Visible to administrators only." }));
    const f = { actor_type: "", action: "", q: "", since: "", until: "", patient_id: "" };
    let offset = 0; const limit = 50;
    const actions = await S.get("/api/staff/admin/audit/actions").then((r) => r.actions)
      .catch((e) => { S.toast("The action filter list couldn't be loaded: " + e.message, "error"); return []; });
    const mk = (id, label, ctl) => el("div", { style: "min-width:150px;flex:1" }, el("label", { for: id, text: label }), ctl);
    const actorSel = el("select", { id: "au-actor" }, [["", "Anyone"], ["staff", "Staff"], ["patient", "Patients"], ["system", "System"]].map(([v, t]) => el("option", { value: v, text: t })));
    const actionSel = el("select", { id: "au-action" }, el("option", { value: "", text: "Any action" }), actions.map((a) => el("option", { value: a, text: S.label(a) })));
    const since = el("input", { id: "au-since", type: "date" }), until = el("input", { id: "au-until", type: "date" });
    const qIn = el("input", { id: "au-q", type: "search", maxlength: 100, placeholder: "Search details" });
    const out = el("div", { "aria-live": "polite" });
    const form = el("form", { class: "card", novalidate: true }, el("div", { class: "row" }, mk("au-actor", "Who", actorSel), mk("au-action", "Action", actionSel), mk("au-since", "From", since), mk("au-until", "To", until), mk("au-q", "Contains", qIn)),
      el("div", { class: "spacer" }), el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit", text: "Apply filters" }), el("button", { class: "btn", type: "button", text: "Reset", onclick: () => { form.reset(); offset = 0; draw(); } })));
    c.append(form, out);
    const draw = async () => {
      try {
        const r = await S.load(out, () => S.get("/api/staff/admin/audit" + S.qs({ actor_type: actorSel.value, action: actionSel.value, since: since.value, until: until.value, q: qIn.value.trim(), limit, offset })));
        if (!r.items.length) return out.append(S.empty("No matching events"));
        out.append(el("p", { class: "muted", text: `Showing ${offset + 1}–${offset + r.items.length} of ${r.total}` }),
          el("div", { class: "table-wrap" }, el("table", {}, el("caption", { class: "sr", text: "Audit events" }),
            el("thead", {}, el("tr", {}, ["When", "Who", "Action", "Patient", "Detail"].map((h) => el("th", { scope: "col", text: h })))),
            el("tbody", {}, r.items.map((i) => el("tr", {}, el("td", { text: S.fmtDate(i.ts) }), el("td", { text: i.actor_name || i.actor_type }),
              el("td", {}, el("span", { class: "badge", text: S.label(i.action) })), el("td", { text: i.patient_name || "—" }),
              el("td", { class: "small", text: i.detail ? (typeof i.detail === "string" ? i.detail : JSON.stringify(i.detail)) : "" })))))),
          el("div", { class: "row" },
            el("button", { class: "btn", text: "← Newer", disabled: offset === 0 ? true : null, onclick: () => { offset = Math.max(0, offset - limit); draw(); } }),
            el("button", { class: "btn", text: "Older →", disabled: offset + limit >= r.total ? true : null, onclick: () => { offset += limit; draw(); } })));
      } catch (e) { /* shown */ }
    };
    form.addEventListener("submit", (ev) => { ev.preventDefault(); offset = 0; draw(); });
    await draw();
  };

  // ------------------------------------------------------------------ staff management (admin)
  S.pages.users = async function (c) {
    const data = await S.load(c, () => S.get("/api/staff/admin/users"));
    const reload = () => S.route();
    c.append(el("div", { class: "row between" }, el("h2", { text: "Staff accounts" }), el("div", { class: "row" },
      el("button", { class: "btn", text: "Add organization", onclick: () => addProvider(reload) }),
      el("button", { class: "btn primary", text: "Add staff member", onclick: () => data.providers.length ? addUser(data, reload) : S.toast("Add an organization first.", "error") }))));
    if (!data.providers.length) c.append(el("p", { class: "muted", text: "No organizations yet. Add your hospital or clinic first; patients share records with an organization, and staff only see what was shared with theirs." }));
    c.append(el("div", { class: "table-wrap" }, el("table", {}, el("caption", { class: "sr", text: "Staff accounts" }),
      el("thead", {}, el("tr", {}, ["Name", "Email", "Role", "Organization", "Status", ""].map((h) => el("th", { scope: "col", text: h })))),
      el("tbody", {}, data.users.map((u) => el("tr", {}, el("td", { text: u.name }), el("td", { text: u.email }), el("td", { text: S.ROLE[u.role] }), el("td", { text: u.provider_name || "—" }),
        el("td", {}, el("span", { class: "badge " + (u.active ? "good" : "bad"), text: u.active ? "Active" : "Disabled" })),
        el("td", {}, el("div", { class: "row" },
          el("button", { class: "btn sm", text: "Change role", "aria-label": `Change role for ${u.name}`, onclick: () => S.form({ title: `Role for ${u.name}`, fields: [{ name: "role", label: "Role", type: "select", options: data.roles.map((r) => ({ value: r, label: S.ROLE[r] })), value: u.role, required: true }], onSubmit: (v) => S.patch(`/api/staff/admin/users/${u.id}`, { role: v.role }) }).then((ok) => ok && reload()) }),
          el("button", { class: "btn sm", text: "Organization", "aria-label": `Change organization for ${u.name}`, onclick: () => S.form({ title: `Organization for ${u.name}`, fields: [{ name: "provider_id", label: "Organization", type: "select", options: [{ value: "", label: "None" }].concat(data.providers.map((p) => ({ value: p.id, label: p.name }))), value: u.provider_id || "" }], onSubmit: (v) => S.patch(`/api/staff/admin/users/${u.id}`, { provider_id: v.provider_id || "" }) }).then((ok) => ok && reload()) }),
          el("button", { class: "btn sm", text: "Reset password", "aria-label": `Reset password for ${u.name}`, onclick: () => S.form({ title: `New password for ${u.name}`, fields: [{ name: "password", label: "New password (10+ characters)", type: "password", required: true, autocomplete: "new-password" }], onSubmit: (v) => S.patch(`/api/staff/admin/users/${u.id}`, { password: v.password }) }).then((ok) => ok && S.toast("Password changed. They were signed out.", "ok")) }),
          u.id === S.me.id ? null : el("button", { class: "btn sm " + (u.active ? "danger" : ""), text: u.active ? "Disable" : "Enable", "aria-label": `${u.active ? "Disable" : "Enable"} ${u.name}`,
            onclick: async () => { if (u.active && !(await S.confirm(`Disable ${u.name}? They will be signed out immediately.`, "Disable"))) return; try { await S.patch(`/api/staff/admin/users/${u.id}`, { active: !u.active }); reload(); } catch (e) { S.toast(e.message, "error"); } } })))))))));
  };

  function addUser(data, done) {
    S.form({ title: "Add staff member", submitLabel: "Create account", fields: [
      { name: "name", label: "Full name", required: true }, { name: "email", label: "Work email", type: "email", required: true, autocomplete: "off" },
      { name: "password", label: "Temporary password (10+ characters)", type: "password", required: true, autocomplete: "new-password" },
      { name: "role", label: "Role", type: "select", required: true, options: data.roles.map((r) => ({ value: r, label: S.ROLE[r] })) },
      { name: "provider_id", label: "Organization", type: "select", required: true, options: data.providers.map((p) => ({ value: p.id, label: p.name })) }],
    onSubmit: (v) => S.post("/api/staff/admin/users", v) }).then((ok) => ok && done());
  }

  function addProvider(done) {
    S.form({ title: "Add organization", submitLabel: "Create organization", fields: [
      { name: "name", label: "Organization name", required: true }, { name: "specialty", label: "Type (e.g. Hospital, Family medicine)" },
      { name: "address", label: "Address" }, { name: "phone", label: "Phone" }],
    onSubmit: (v) => S.post("/api/staff/admin/providers", v) }).then((ok) => ok && done());
  }
})();

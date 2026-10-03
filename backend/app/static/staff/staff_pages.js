/* Staff pages (1/2): dashboard, patient search, patient page, access requests, redeem code. */
(function () {
  const { el } = S;
  const CATEGORIES = ["lab_result", "imaging", "prescription", "visit_summary", "insurance", "billing",
    "identification", "vaccination", "referral", "other"];
  S.timers = [];
  S.clearTimers = () => { S.timers.forEach(clearInterval); S.timers = []; };

  const link = (href, text) => el("a", { href, text });

  // ------------------------------------------------------------------ dashboard
  S.pages.dashboard = async function (c) {
    const d = await S.load(c, () => S.get("/api/staff/dashboard"));
    c.append(el("h2", { text: `Welcome, ${S.me.name.split(" ")[0]}` }),
      el("p", { class: "muted", text: `${S.ROLE[S.me.role]} · ${S.me.provider_name || "No organization"}` }));
    const part = S.partial(d.errors, () => S.route());
    if (part) c.append(part);
    const stat = (n, label, href, sub) => el("a", { class: "card", href, style: "text-decoration:none;color:inherit;display:block" },
      el("div", { class: "stat", text: n == null ? "—" : String(n) }), el("div", { class: "stat-label", text: label }),
      sub ? el("div", { class: "small muted", text: sub }) : null);
    const ar = d.access_requests;
    c.append(el("div", { class: "grid" },
      stat(d.new_appointment_requests, "New appointment requests", "#/appointments"),
      stat(ar ? ar.pending : null, "Access requests waiting for patient", "#/requests", ar ? `${ar.approved} approved · ${ar.denied} denied` : "Unavailable"),
      stat(d.shares_granted ? d.shares_granted.patients : null, "Patients sharing records with us", "#/documents",
        d.shares_granted ? `${d.shares_granted.grants} active permission(s)` : "Unavailable"),
      stat(d.unread_notifications, "Unread notifications", "#/notifications")));
    c.append(el("h3", { text: "Recent access requests" }));
    if (!d.recent_requests.length) c.append(S.empty("No access requests yet", "Search for a patient to request access to their records.",
      el("a", { class: "btn primary", href: "#/patients", text: "Find a patient" })));
    else c.append(requestsTable(d.recent_requests));
  };

  function requestsTable(rows) {
    return el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Requested", "Patient", "Records", "Purpose", "Days", "Status"].map((h) => el("th", { scope: "col", text: h })))),
      el("tbody", {}, rows.map((r) => el("tr", {},
        el("td", { text: S.fmtDate(r.created_at) }),
        el("td", {}, link(`#/patient/${r.patient_id}`, r.patient_name || "Patient")),
        el("td", { text: (r.categories || []).map(S.label).join(", ") || `${(r.document_ids || []).length} document(s)` }),
        el("td", { text: r.purpose || "—" }), el("td", { text: String(r.duration_days) }),
        el("td", {}, S.statusBadge(r.status), r.status === "approved" && r.decided_at ? el("div", { class: "small muted", text: S.fmtDate(r.decided_at) }) : null))))));
  }

  // ------------------------------------------------------------------ patient search
  S.pages.patients = async function (c) {
    c.append(el("h2", { text: "Find a patient" }),
      el("p", { class: "muted", text: "Search by name. Only the name and birth year are shown until you confirm the full date of birth." }));
    const q = el("input", { id: "ps-q", type: "search", maxlength: 100, "aria-required": "true", autocomplete: "off" });
    const dob = el("input", { id: "ps-dob", type: "date" });
    const out = el("div", { "aria-live": "polite" });
    const form = el("form", { class: "card", novalidate: true },
      el("div", { class: "row" }, el("div", { style: "flex:2;min-width:220px" }, el("label", { for: "ps-q", text: "Patient name" }), q),
        el("div", { style: "flex:1;min-width:180px" }, el("label", { for: "ps-dob", text: "Date of birth (optional, narrows the search)" }), dob)),
      el("div", { class: "spacer" }), el("button", { class: "btn primary", type: "submit", text: "Search" }));
    c.append(form, out);
    q.focus();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (q.value.trim().length < 2) { S.clear(out).append(el("p", { class: "field-error", role: "alert", text: "Type at least 2 letters of the name." })); return; }
      S.clear(out).append(el("p", { class: "spinner", text: "Searching…" }));
      try {
        const r = await S.get("/api/staff/patients/search" + S.qs({ q: q.value.trim(), dob: dob.value }));
        S.clear(out);
        if (!r.results.length) { out.append(S.empty("No patients found", "Check the spelling, or search with fewer letters.")); S.announce("No patients found"); return; }
        S.announce(`${r.results.length} patient(s) found`);
        out.append(el("div", { class: "table-wrap" }, el("table", {},
          el("caption", { class: "sr", text: "Search results" }),
          el("thead", {}, el("tr", {}, ["Name", "Birth year", ""].map((h) => el("th", { scope: "col", text: h })))),
          el("tbody", {}, r.results.map((p) => el("tr", {}, el("td", {}, el("strong", { text: p.name })), el("td", { text: p.dob_masked }),
            el("td", {}, el("button", { class: "btn sm primary", text: "Open…", "aria-label": `Open ${p.name}`, onclick: () => (location.hash = `#/patient/${p.id}`) }))))))),
          el("p", { class: "small muted", text: r.note }));
      } catch (e) { S.clear(out).append(S.errorBox(e, () => form.requestSubmit())); }
    });
  };

  // ------------------------------------------------------------------ patient page
  S.pages.patient = async function (c, [pid]) {
    let min;
    try { min = await S.load(c, () => S.get(`/api/staff/patients/${pid}/minimal`)); }
    catch (e) { return; }
    if (!min.confirmed) return confirmStep(c, min);
    const page = await S.load(c, () => S.get(`/api/staff/patients/${pid}`)).catch((e) => {
      if (e.status === 403) { return confirmStep(c, min); }
      throw e;
    });
    if (page) renderPatient(c, pid, page);
  };

  function confirmStep(c, min) {
    S.clear(c);
    const dob = el("input", { id: "cd-dob", type: "date", "aria-required": "true" });
    const status = el("p", { class: "field-error", role: "alert" });
    const form = el("form", { class: "card", novalidate: true },
      el("h2", { text: "Confirm this is the right patient" }),
      el("p", {}, "You selected ", el("strong", { text: min.name }), ` (born ${min.dob_masked.slice(0, 4)}). Ask the patient for their full date of birth and enter it to continue. This prevents mix-ups between people with similar names.`),
      el("label", { for: "cd-dob", text: "Date of birth" }), dob, status, el("div", { class: "spacer" }),
      el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit", text: "Confirm and open" }), el("a", { class: "btn", href: "#/patients", text: "Back to search" })));
    c.append(form); dob.focus();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault(); status.textContent = "";
      if (!dob.value) { status.textContent = "Enter the date of birth."; return; }
      try { await S.post(`/api/staff/patients/${min.id}/confirm`, { dob: dob.value }); S.route(); }
      catch (e) { status.textContent = e.message; }
    });
  }

  function renderPatient(c, pid, page) {
    const id = page.identity;
    const reload = () => S.route();
    c.append(el("div", { class: "row between" }, el("h2", { text: id.legal_name }),
      el("button", { class: "btn primary", onclick: () => requestAccess(pid, page, reload), text: "Request access" })));
    const part = S.partial(page.errors, reload); if (part) c.append(part);
    const recHost = el("div");
    c.append(recHost);
    S.records.mount(recHost, pid, page);

    c.append(el("section", { class: "card", "aria-labelledby": "h-id" }, el("h3", { id: "h-id", text: "Identity" }),
      el("dl", { class: "kv" },
        el("dt", { text: "Legal name" }), el("dd", { text: id.legal_name }),
        el("dt", { text: "Preferred name" }), el("dd", { text: id.preferred_name || "—" }),
        el("dt", { text: "Date of birth" }), el("dd", { text: S.fmtDate(id.dob) || "—" }),
        el("dt", { text: "Phone" }), el("dd", { text: id.phone || "—" }),
        el("dt", { text: "Address" }), el("dd", { text: id.address || "—" }),
        el("dt", { text: "Identity" }), el("dd", {}, el("span", { class: "badge " + (id.verification_status === "verified" ? "good" : "warn"), text: S.label(id.verification_status) })),
        el("dt", { text: "Patient ID" }), el("dd", {}, el("code", { class: "small", text: id.id })))));

    const sc = page.scope;
    const scope = el("section", { class: "card", "aria-labelledby": "h-scope" }, el("h3", { id: "h-scope", text: "Authorization" }));
    if (sc.authorized) {
      scope.append(el("p", {}, el("span", { class: "badge good", text: "Authorized" }), ` until ${S.fmtDate(sc.authorized_until)}`),
        el("ul", {}, sc.grants.map((g) => el("li", { text: `${g.scope_type === "category" ? "All " + S.label(g.category) : "One document"} — expires ${S.fmtDate(g.expires_at)}${g.purpose ? " — " + g.purpose : ""}` }))));
    } else scope.append(el("p", {}, el("span", { class: "badge warn", text: "Not authorized" }), " The patient has not shared records with your organization."));
    c.append(scope);

    // documents
    const docs = el("section", { class: "card", "aria-labelledby": "h-docs" }, el("h3", { id: "h-docs", text: "Shared documents" }));
    if (!page.documents.length) {
      docs.append(S.empty("No shared documents", page.errors.documents ? "Documents are temporarily unavailable." : "Ask the patient for a share code, or send an access request.",
        el("button", { class: "btn primary", onclick: () => requestAccess(pid, page, reload), text: "Request access" })));
    } else {
      const filter = el("input", { type: "search", id: "doc-filter", placeholder: "Filter by document name", maxlength: 100 });
      const body = el("tbody");
      const draw = () => {
        S.clear(body);
        const f = filter.value.trim().toLowerCase();
        page.documents.filter((d) => !f || d.name.toLowerCase().includes(f)).forEach((d) => body.append(el("tr", {},
          el("td", {}, el("strong", { text: d.name }), d.description ? el("div", { class: "small muted", text: d.description }) : null),
          el("td", { text: S.label(d.category) }), el("td", { text: S.fmtDate(d.uploaded_at) }),
          el("td", {}, el("span", { class: "badge " + (d.source_label.startsWith("Clinician") ? "good" : ""), text: d.source_label })),
          el("td", { text: S.fmtDate(d.grant_expires_at) }),
          el("td", {}, el("div", { class: "row" },
            el("button", { class: "btn sm primary", text: "Open", "aria-label": `Open ${d.name}`, onclick: () => previewDoc(pid, d) }),
            el("button", { class: "btn sm", text: "Download", "aria-label": `Download ${d.name}`, onclick: () => downloadDoc(pid, d) }))))));
        if (!body.children.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "muted", text: "No document matches your filter." })));
      };
      filter.addEventListener("input", draw); draw();
      docs.append(el("label", { for: "doc-filter", class: "sr", text: "Filter documents" }), filter, el("div", { class: "spacer" }),
        el("div", { class: "table-wrap" }, el("table", {}, el("caption", { class: "sr", text: "Documents shared with your organization" }),
          el("thead", {}, el("tr", {}, ["Document", "Category", "Uploaded", "Source", "Access ends", "Actions"].map((h) => el("th", { scope: "col", text: h })))), body)));
      if (S.me.role === "front_desk") docs.append(el("p", { class: "small muted", text: "Front desk accounts only see insurance, billing and ID documents." }));
    }
    c.append(docs);

    // insurance & billing: the "Billing" tab in Health records (consent role matrix + billing grant)

    // requests tracker
    const rq = el("section", { class: "card", "aria-labelledby": "h-rq" }, el("h3", { id: "h-rq", text: "Access requests" }));
    if (!page.requests.length) rq.append(el("p", { class: "muted", text: "No requests for this patient yet." }));
    else rq.append(requestsTable(page.requests.map((r) => ({ ...r, patient_name: id.legal_name }))));
    c.append(rq);

    // appointments
    const ap = el("section", { class: "card", "aria-labelledby": "h-ap" }, el("h3", { id: "h-ap", text: "Appointments with us" }));
    if (!page.appointments.length) ap.append(el("p", { class: "muted", text: "None." }));
    else ap.append(el("ul", {}, page.appointments.map((a) => el("li", {}, link(`#/appointments/${a.id}`, a.scheduled_for ? S.fmtDate(a.scheduled_for) : "Time to be set"), " ", S.statusBadge(a.status)))));
    c.append(ap);
  }

  // ------------------------------------------------------------------ preview / download
  async function fetchDoc(pid, d, download) {
    let res;
    try { res = await fetch(`/api/staff/patients/${pid}/documents/${d.id}/file` + (download ? "?download=1" : ""), { credentials: "same-origin", signal: AbortSignal.timeout(120000) }); }
    catch (e) { throw new Error(e && e.name === "TimeoutError" ? "The document took too long to download. Please try again."
      : navigator.onLine ? "Can't reach the server. Please try again." : "You are offline."); }
    if (!res.ok) {
      const j = await res.json().catch(() => null);
      throw new Error((j && typeof j.detail === "string" && j.detail) || `The document could not be opened (error ${res.status}).`);
    }
    return { blob: await res.blob(), type: res.headers.get("content-type") || "" };
  }
  async function downloadDoc(pid, d) {
    try {
      const { blob } = await fetchDoc(pid, d, true);
      const a = el("a", { href: URL.createObjectURL(blob), download: d.name }); document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      S.toast("Download started. The patient can see that you opened this document.");
    } catch (e) { S.toast(e.message, "error"); }
  }
  async function previewDoc(pid, d) {
    let got;
    try { got = await fetchDoc(pid, d, false); } catch (e) { S.toast(e.message, "error"); return; }
    const url = URL.createObjectURL(got.blob);
    const isPdf = got.type.startsWith("application/pdf"), isImg = got.type.startsWith("image/");
    await S.dialog(d.name, (close) => el("div", {},
      el("p", { class: "small muted", text: `${S.label(d.category)} · ${d.source_label} · access ends ${S.fmtDate(d.grant_expires_at)}` }),
      isPdf ? el("iframe", { class: "preview", src: url, title: `Preview of ${d.name}` })
        : isImg ? el("img", { class: "preview-img", src: url, alt: `${d.name} (image)` })
          : el("p", { class: "card warn", text: "This file type can't be previewed here. Use Download." }),
      el("div", { class: "spacer" }), el("div", { class: "row" },
        el("button", { class: "btn", onclick: () => downloadDoc(pid, d), text: "Download" }),
        el("button", { class: "btn primary", onclick: () => close(true), text: "Close" }))));
    URL.revokeObjectURL(url);
  }

  // ------------------------------------------------------------------ request access
  const RECORD_CATS = [["allergies", "Allergies"], ["medications", "Medications"], ["history", "Conditions & history"],
    ["vaccinations", "Vaccinations"], ["results", "Test results"], ["billing", "Billing (bills, claims, insurance)"]];
  S.requestAccess = requestAccess;
  function requestAccess(pid, page, done, preselect) {
    const allowed = page.allowed_categories ? CATEGORIES.filter((x) => page.allowed_categories.includes(x)) : CATEGORIES;
    const records = RECORD_CATS.filter(([v]) => S.me.role !== "front_desk" || v === "billing");
    const pre = new Set(preselect || []);
    S.dialog("Request access to records", (close) => {
      const box = (cat, text) => el("label", { class: "check" }, el("input", { type: "checkbox", value: cat, checked: pre.has(cat) ? true : null }), text);
      const recBoxes = records.map(([v, t]) => box(v, t));
      const boxes = recBoxes.concat(allowed.map((cat) => box(cat, S.label(cat))));
      const preLabels = records.filter(([v]) => pre.has(v)).map(([, t]) => t.toLowerCase());
      const purpose = el("input", { id: "ra-purpose", maxlength: 300, "aria-required": "true", placeholder: "e.g. Pre-visit review for cardiology consult",
        value: preLabels.length ? `Review of ${preLabels.join(", ")} for upcoming care` : null });
      const days = el("select", { id: "ra-days" }, [1, 3, 7, 14, 30, 90].map((n) => el("option", { value: n, text: n === 1 ? "1 day" : n + " days", selected: n === 7 ? true : null })));
      const status = el("p", { class: "field-error", role: "alert" });
      const form = el("form", { novalidate: true },
        el("p", { class: "muted", text: `${page.identity.legal_name} will get a notification and decides what to share. Nothing is visible until they approve.` }),
        el("fieldset", {}, el("legend", { text: "Health data" }), el("div", { class: "cats" }, recBoxes)),
        el("fieldset", {}, el("legend", { text: "Documents" }), el("div", { class: "cats" }, boxes.slice(recBoxes.length))),
        page.allowed_categories ? el("p", { class: "small muted", text: "As front desk you can request billing data and insurance, billing and ID documents." }) : null,
        el("label", { for: "ra-purpose", text: "Purpose (the patient will see this)" }), purpose,
        el("label", { for: "ra-days", text: "How long do you need access?" }), days, status, el("div", { class: "spacer" }),
        el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit", text: "Send request" }),
          el("button", { class: "btn", type: "button", onclick: () => close(false), text: "Cancel" })));
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault(); status.textContent = "";
        const categories = boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value);
        if (!categories.length) { status.textContent = "Pick at least one kind of record."; return; }
        if (!purpose.value.trim()) { status.textContent = "Please state the purpose."; purpose.focus(); return; }
        try {
          await S.post(`/api/staff/patients/${pid}/request-access`, { categories, purpose: purpose.value.trim(), duration_days: Number(days.value) });
          S.toast("Request sent. The patient was notified.", "ok"); close(true);
        } catch (e) { status.textContent = e.message; }
      });
      setTimeout(() => form.querySelector("input")?.focus(), 30);
      return form;
    }).then((ok) => ok && done());
  }

  // ------------------------------------------------------------------ requests tracker
  S.pages.requests = async function (c) {
    c.append(el("div", { class: "row between" }, el("h2", { text: "Access requests" }), el("button", { class: "btn", onclick: () => S.route(), text: "Refresh" })));
    let status = "";
    const sel = el("select", { id: "rq-status" }, ["", "pending", "approved", "denied", "expired"].map((s) => el("option", { value: s, text: s ? S.label(s) : "All statuses" })));
    const list = el("div");
    c.append(el("label", { for: "rq-status", text: "Show" }), sel, el("div", { class: "spacer" }), list);
    const draw = async () => {
      try {
        const r = await S.load(list, () => S.get("/api/staff/requests" + S.qs({ status })));
        if (!r.requests.length) list.append(S.empty("Nothing here", "Open a patient and choose Request access."));
        else list.append(requestsTable(r.requests));
      } catch (e) { /* error box already shown with retry */ }
    };
    sel.addEventListener("change", () => { status = sel.value; draw(); });
    await draw();
    S.timers.push(setInterval(() => { if (!document.hidden) S.get("/api/staff/requests" + S.qs({ status })).then((r) => { S.clear(list); list.append(r.requests.length ? requestsTable(r.requests) : S.empty("Nothing here")); })
      .catch((e) => console.warn("access request list not refreshed:", e.message)); }, 20000));
  };

  // Find a patient + Access requests share ONE page (the requests tracker sits under the search).
  const _searchPage = S.pages.patients, _requestsPage = S.pages.requests;
  S.pages.patients = async function (c) {
    await _searchPage(c);
    c.append(el("hr", { style: "margin:1.5rem 0" }));
    const sub = el("div"); c.append(sub);
    await _requestsPage(sub);
  };
  S.pages.requests = S.pages.patients;

  // ------------------------------------------------------------------ redeem code
  S.pages.redeem = async function (c) {
    c.append(el("h2", { text: "Use a patient's share code" }),
      el("p", { class: "muted", text: "Paste the code the patient gave you, or click in the box and scan their QR code with a scanner (it types the code for you). Codes are single-use and expire quickly." }));
    if (!S.me.provider_id) {
      c.append(el("div", { class: "card warn", role: "alert" },
        el("h3", { text: "Hospital assignment needed" }),
        el("p", { text: "An administrator must link this staff account to your hospital under Staff accounts → Organization. Your share code has not been used." }),
        el("button", { class: "btn", text: "Check assignment again", onclick: () => S.route() })));
      return;
    }
    const tok = el("input", { id: "rd-tok", autocomplete: "off", spellcheck: "false", maxlength: 300, "aria-required": "true", "aria-describedby": "rd-help" });
    const out = el("div", { "aria-live": "polite" });
    const form = el("form", { class: "card", novalidate: true }, el("label", { for: "rd-tok", text: "Share code" }), tok,
      el("p", { id: "rd-help", class: "help", text: "Scanner tip: most QR scanners press Enter automatically." }),
      el("button", { class: "btn primary", type: "submit", text: "Redeem code" }));
    c.append(form, out); tok.focus();
    let redeemBusy = false;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault(); if (redeemBusy) return; S.clear(out);
      const t = tok.value.trim(); if (!t) { out.append(el("p", { class: "field-error", role: "alert", text: "Enter or scan a code." })); return; }
      try {
        redeemBusy = true;
        form.querySelector("button[type=submit]").disabled = true;
        const r = await S.post("/api/staff/redeem", { token: t });
        tok.value = "";
        S.announce("Code accepted");
        out.append(el("div", { class: "card", role: "status" }, el("h3", { text: "Code accepted" }),
          el("p", {}, `${r.patient_name || "The patient"} shared ${(r.categories || []).map(S.label).join(", ") || (r.document_ids || []).length + " document(s)"} until ${S.fmtDate(r.expires_at)}.`),
          el("a", { class: "btn primary", href: `#/patient/${r.patient_id}`, text: "Open patient" })));
      } catch (e) { out.append(el("div", { class: "card err", role: "alert" }, e.message)); tok.focus(); }
      finally { redeemBusy = false; form.querySelector("button[type=submit]").disabled = false; }
    });
  };
})();

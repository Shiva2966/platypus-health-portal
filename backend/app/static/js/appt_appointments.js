/* W9 - Appointments + intake (patient). Section id "appointments" (order 80).
 * Sub-routes (hash): #/appointments  list | #/appointments/new  new request | #/appointments/<id>  view or continue a draft.
 * NOT a diagnosis, NO triage: free text is sent as written. Uses textContent only.
 */
(function () {
  const P = window.Portal;
  if (!P) return;
  const el = P.el;
  const API = "/api/appt";
  const DISCLAIMER = "This form is not a diagnosis, and this app does not triage emergencies. If you think this is an emergency, call your local emergency services now.";

  const STATUS = {
    draft: ["Draft - not sent", "warn"], requested: ["Waiting for the clinic", "info"], booked: ["Confirmed", "good"],
    rescheduled: ["New time proposed - please confirm", "warn"], cancelled: ["Cancelled", "bad"], declined: ["The clinic could not take this", "bad"],
  };
  const VISIT = { in_person: "In person", video: "Video visit", phone: "Phone call" };
  const subRoute = () => location.hash.replace(/^#\//, "").split("?")[0].split("/").slice(1);

  function emergencyLabel() {
    return el("div", { class: "card warn", role: "note" }, el("strong", { text: "Not a diagnosis. " }), DISCLAIMER);
  }
  const badge = (status) => { const s = STATUS[status] || [status, ""]; return P.badge(s[0], s[1]); };
  const go = (path) => { location.hash = "#/appointments" + (path ? "/" + path : ""); };
  const errText = (e) => e.message + (e.fields && Object.keys(e.fields).length ? " " + Object.values(e.fields).join(" ") : "");

  // ---------------------------------------------------------------- list
  async function renderList(c) {
    const data = await P.get(API + "/appointments");
    const items = data.items;
    c.append(emergencyLabel());
    c.append(el("div", { class: "row" }, el("button", { class: "btn primary", text: "Request an appointment", onclick: () => go("new") })));
    if (!items.length) { c.append(el("div", { class: "card empty", text: "You have no appointments yet. Choose \"Request an appointment\" to start. Nothing is sent until you press Send." })); return; }
    const attention = items.filter((a) => a.status === "rescheduled" || a.status === "draft");
    const upcoming = items.filter((a) => a.status === "requested" || a.status === "booked");
    const closed = items.filter((a) => a.status === "cancelled" || a.status === "declined");
    section(c, "Needs your attention", attention);
    section(c, "Upcoming", upcoming);
    section(c, "Closed", closed);
  }
  function section(c, title, list) {
    if (!list.length) return;
    c.append(el("h3", { text: title }));
    list.forEach((a) => c.append(card(a)));
  }
  function card(a) {
    const acts = [];
    const open = () => go(a.id);
    if (a.status === "draft") acts.push(el("button", { class: "btn small primary", text: "Continue", onclick: open }));
    else acts.push(el("button", { class: "btn small", text: "Details", onclick: open }));
    if (a.needs_my_confirmation) acts.push(el("button", { class: "btn small primary", text: "Accept this time", onclick: () => confirmTime(a) }));
    if (a.can_reschedule) acts.push(el("button", { class: "btn small", text: "Ask to reschedule", onclick: () => reschedule(a) }));
    if (a.can_cancel) acts.push(el("button", { class: "btn small danger", text: a.status === "draft" ? "Delete draft" : "Cancel", onclick: () => cancel(a) }));
    return el("div", { class: "card" },
      el("div", { class: "row between" }, el("h3", { text: (a.provider ? a.provider.name : "No provider chosen yet") }), badge(a.status)),
      a.for_name ? el("p", { class: "muted", text: "For: " + a.for_name }) : null,
      a.scheduled_for && a.status !== "cancelled" && a.status !== "declined" ? el("p", {}, el("strong", { text: "When: " }), a.scheduled_for) : null,
      a.intake.reason ? el("p", { text: a.intake.reason }) : null,
      a.status === "draft" ? progress(a.progress) : null,
      a.staff_note ? el("p", { class: "muted", text: "Message from the clinic: " + a.staff_note }) : null,
      el("div", { class: "row" }, acts));
  }
  function progress(p) {
    return el("div", {},
      el("div", { role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(p.percent), "aria-label": "Form completion",
        style: "background:var(--line);border-radius:999px;height:.6rem;overflow:hidden" },
        el("div", { style: "background:var(--primary);height:100%;width:" + p.percent + "%" })),
      el("p", { class: "help", text: p.missing_labels.length ? "Still needed: " + p.missing_labels.map((label) => label === "when you are available" ? "your preferred visit time" : label).join(", ") + "." : "Ready to review and send." }));
  }

  async function confirmTime(a) {
    try { await P.post(`${API}/appointments/${a.id}/confirm`); P.toast("Time confirmed.", "ok"); P.refresh(); } catch (e) { P.toast(e.message, "error"); }
  }
  async function cancel(a) {
    const draft = a.status === "draft";
    if (!(await P.confirm(draft ? "Delete this draft?" : "Cancel this appointment? The clinic will be told.", draft ? "Yes, delete" : "Yes, cancel it"))) return;
    try {
      if (draft) await P.delete(`${API}/appointments/${a.id}`); else await P.post(`${API}/appointments/${a.id}/cancel`, {});
      P.toast(draft ? "Draft deleted." : "Appointment cancelled.", "ok"); if (subRoute().length) go(); else P.refresh();
    } catch (e) { P.toast(e.message, "error"); }
  }
  function reschedule(a) {
    return P.openForm({
      title: "Ask to reschedule", submitLabel: "Send request",
      intro: "Tell the clinic when would suit you better. They will propose a new time and you can accept it.",
      fields: [{ name: "preferred_times", label: "When would suit you better?", type: "textarea", required: true, maxlength: 500 },
        { name: "reason", label: "Reason", type: "text", maxlength: 500 }],
      onSubmit: async (v) => { await P.post(`${API}/appointments/${a.id}/reschedule`, v); P.toast("Reschedule request sent.", "ok"); P.refresh(); },
    });
  }

  // ---------------------------------------------------------------- detail
  async function renderDetail(c, id) {
    const a = await P.get(`${API}/appointments/${id}`);
    if (a.status === "draft") return wizard(c, a);
    c.append(emergencyLabel(), el("button", { class: "btn small", text: "Back to appointments", onclick: () => go() }), card(a));
    const intake = a.intake;
    const rows = [["Why", intake.reason], ["Symptoms", intake.symptoms], ["Started", intake.onset], ["How long / how often", intake.duration],
      ["Preferred visit time", intake.availability], ["Visit type", VISIT[intake.visit_type]], ["Preparations", intake.accommodations], ["Asked to reschedule from", intake.reschedule_from]]
      .filter((r) => r[1]);
    const dl = el("dl", { class: "kv" }); rows.forEach((r) => dl.append(el("dt", { text: r[0] }), el("dd", { text: r[1] })));
    c.append(el("div", { class: "card" }, el("h3", { text: "What you sent" }), dl));
    if (a.contact && a.contact.medical_summary) c.append(el("p", { class: "muted", text: "You also shared your medication, allergy and condition names with this request." }));
    c.append(el("div", { class: "card" }, el("h3", { text: "History" }), el("ul", { class: "list" },
      (a.history || []).map((h) => el("li", {}, P.fmtDate(h.ts) + " - " + (STATUS[h.to] ? STATUS[h.to][0] : h.to) + (h.note ? " (" + h.note + ")" : ""))))));
  }

  // ---------------------------------------------------------------- wizard (new / draft)
  async function wizard(c, existing) {
    const st = { id: existing ? existing.id : null, forId: existing ? existing.patient_id : null, provider: existing ? existing.provider : null,
      intake: existing ? { ...existing.intake } : {}, includeMedical: false, step: existing && existing.provider ? 2 : 1 };
    const host = el("div"); c.append(host);

    const steps = ["Who to see", "Your details", "Review and send"];
    function frame(n, body) {
      P.clear(host);
      host.append(emergencyLabel(), el("button", { class: "btn small", text: "Back to appointments", onclick: () => go() }),
        el("ol", { class: "tabs", "aria-label": "Steps" }, steps.map((s, i) => el("li", { style: "list-style:none" }, el("span", { class: "badge " + (i + 1 === n ? "info" : ""), "aria-current": i + 1 === n ? "step" : null, text: (i + 1) + ". " + s })))), body);
    }

    // ---- step 1: who
    async function step1() {
      const q = el("input", { type: "search", placeholder: "Search by name or specialty", "aria-label": "Search providers", maxlength: 100 });
      const spec = el("select", { "aria-label": "Specialty" }, el("option", { value: "", text: "All specialties" }));
      try { (await P.get(API + "/specialties")).items.forEach((s) => spec.append(el("option", { value: s, text: s }))); } catch (e) { /* optional */ }
      const results = el("div", { "aria-live": "polite" });
      const who = { value: st.forId || "" }; // Keep the owner of existing drafts unchanged.
      async function search() {
        try {
          const params = new URLSearchParams({ q: q.value, specialty: spec.value });
          const d = await P.get(API + "/providers?" + params);
          P.clear(results);
          if (!d.items.length) results.append(el("p", { class: "empty", text: "No providers match. Try a different word." }));
          d.items.forEach((p) => results.append(el("div", { class: "card" }, el("h3", { text: p.name }),
            el("p", { class: "muted", text: [p.specialty, p.address, p.phone].filter(Boolean).join(" - ") }),
            p.availability && p.availability.length ? el("p", { text: "Usually available: " + p.availability.join(", ") }) : null,
            el("button", { class: "btn small primary", text: st.provider && st.provider.id === p.id ? "Selected - continue" : "Choose this provider",
              onclick: () => { st.provider = { id: p.id, name: p.name, specialty: p.specialty, availability: p.availability }; st.forId = who.value || null; st.step = 2; step2(); } }))));
        } catch (e) { results.textContent = e.message; }
      }
      let t; q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(search, 250); }); spec.addEventListener("change", search);
      q.id = "appt-provider-q";
      frame(1, el("div", {}, 
        el("label", { for: "appt-provider-q", text: "Find a provider" }), el("div", { class: "row" }, q, spec), results));
      search();
    }

    // ---- step 2: intake
    async function step2() {
      let form;
      try { form = await P.get(API + "/intake-form" + (st.forId ? "?for_patient_id=" + encodeURIComponent(st.forId) : "")); }
      catch (e) { frame(2, el("div", { class: "card err", role: "alert", text: e.message })); return; }
      const fields = form.fields.map((f) => {
        const field = { ...f, label: f.label.replace(/\s*\(in your own words\)|,?\s+in your own words/gi, "") };
        if (f.name === "visit_type") field.options = f.options.filter((option) => option.value !== "video");
        if (f.name === "availability") {
          field.label = "Preferred visit time";
          field.help = "For example: weekday mornings. The hospital confirms the actual appointment time.";
        }
        return field;
      });
      const prov = st.provider || {};
      const slots = (prov.availability || []);
      if (slots.length) fields.push({ name: "preferred_slot", label: "A time the clinic usually has available (optional)", type: "select", options: slots.map((s) => ({ value: s, label: s })), required: false });
      const fb = P.buildFields(fields, st.intake);
      const pf = form.prefill;
      const prefill = el("div", { class: "card info" }, el("h3", { text: form.is_dependent ? "Details we will send (from their profile)" : "Details we will send (from your profile)" }),
        el("dl", { class: "kv" }, [["Name", pf.legal_name], ["Date of birth", pf.dob], ["Phone", pf.phone || pf.guardian_phone], ["Email", pf.email || pf.guardian_email]]
          .filter((r) => r[1]).map((r) => [el("dt", { text: r[0] }), el("dd", { text: r[1] })])),
        form.missing_profile.length ? el("p", { class: "field-error", text: "Missing in the profile: " + form.missing_profile.join(", ") + ". " }, el("a", { href: "#/profile", text: "Add it" })) : null,
        el("p", { class: "help", text: "These come from your saved profile. Change them in your profile, not here." }));
      const ms = form.medical_summary || {}; const msItems = [].concat(ms.medications || [], ms.allergies || [], ms.conditions || []);
      const incl = el("input", { type: "checkbox", id: "incl-med" }); incl.checked = st.includeMedical;
      const medBox = msItems.length ? el("div", { class: "card" }, el("label", { class: "check", for: "incl-med" }, incl, " Also send the names of my current medications, allergies and conditions"),
        el("p", { class: "help", text: "Medications: " + ((ms.medications || []).join(", ") || "none listed") + ". Allergies: " + ((ms.allergies || []).join(", ") || "none listed") + ". Conditions: " + ((ms.conditions || []).join(", ") || "none listed") + ". Off unless you tick it." })) : null;
      const status = el("p", { class: "field-error", role: "alert" });
      const collect = () => { const v = fb.values(); const out = {}; Object.keys(v).forEach((k) => { out[k] = v[k] === null ? "" : v[k]; }); return out; };
      async function save() {
        st.intake = collect(); st.includeMedical = incl.checked;
        const body = { provider_id: st.provider.id, intake: st.intake };
        if (st.id) await P.put(`${API}/appointments/${st.id}`, body);
        else { const r = await P.post(API + "/appointments", { ...body, for_patient_id: st.forId, submit: false }); st.id = r.id; }
      }
      frame(2, el("div", {}, el("p", {}, "Booking with ", el("strong", { text: prov.name || "" }), ". ", el("button", { class: "btn small", text: "Change", onclick: () => { st.step = 1; step1(); } })),
        prefill, el("h3", { text: "Tell the clinic what's going on" }), el("p", { class: "muted", text: "Describe your symptoms and reason for the appointment." }), fb, medBox, status,
        el("div", { class: "row" },
          el("button", { class: "btn primary", text: "Review", onclick: async (ev) => { ev.target.disabled = true; status.textContent = ""; fb.showErrors({}); if (!fb.validate()) { ev.target.disabled = false; return; } try { await save(); st.step = 3; step3(); } catch (e) { status.textContent = e.message; fb.showErrors(e.fields); ev.target.disabled = false; } } }),
          el("button", { class: "btn", text: "Save draft and finish later", onclick: async () => { try { await save(); P.toast("Draft saved. Nothing was sent.", "ok"); go(); } catch (e) { status.textContent = e.message; fb.showErrors(e.fields); } } }))));
    }

    // ---- step 3: review
    async function step3() {
      let r;
      try { r = await P.get(`${API}/appointments/${st.id}/review?include_medical=${st.includeMedical}`); }
      catch (e) { frame(3, el("div", { class: "card err", role: "alert", text: e.message })); return; }
      const dl = el("dl", { class: "kv" });
      r.sections.forEach((s) => { if (s.value) dl.append(el("dt", { text: s.key === "availability" ? "Preferred visit time" : s.label }), el("dd", { text: s.key === "visit_type" ? (VISIT[s.value] || s.value) : s.value })); });
      const contact = el("dl", { class: "kv" }); Object.entries(r.contact_shared).forEach(([k, v]) => contact.append(el("dt", { text: P.label(k) }), el("dd", { text: String(v) })));
      const status = el("p", { class: "field-error", role: "alert" });
      const send = el("button", { class: "btn primary", text: "Send to " + (r.to || "the clinic"), disabled: !r.ready ? true : null, onclick: async () => {
        send.disabled = true; status.textContent = "";
        try { await P.post(`${API}/appointments/${st.id}/submit`, { include_medical_summary: st.includeMedical }); P.toast("Request sent. We'll tell you when the clinic replies.", "ok"); go(); }
        catch (e) { status.textContent = errText(e); send.disabled = false; }
      } });
      frame(3, el("div", {}, el("h3", { text: "Check before you send" }), el("p", { class: "muted", text: "This is exactly what " + (r.to || "the clinic") + " will receive. Nothing has been sent yet." }),
        el("div", { class: "card" }, el("h3", { text: "Your answers" }), dl), el("div", { class: "card" }, el("h3", { text: "Contact details" }), contact),
        r.medical_summary ? el("div", { class: "card" }, el("h3", { text: "Medical names you chose to include" }),
          el("p", { text: "Medications: " + (r.medical_summary.medications.join(", ") || "none") }), el("p", { text: "Allergies: " + (r.medical_summary.allergies.join(", ") || "none") }),
          el("p", { text: "Conditions: " + (r.medical_summary.conditions.join(", ") || "none") })) : null,
        r.ready ? null : el("p", { class: "field-error", text: "Still needed: " + r.missing.join(", ") + "." }), status,
        el("div", { class: "row" }, el("button", { class: "btn", text: "Back and edit", onclick: () => { st.step = 2; step2(); } }), send)));
    }

    if (st.step === 2 && st.provider) { st.provider = { ...st.provider, availability: (await providerAvail(st.provider.id)) }; step2(); } else step1();
  }
  async function providerAvail(id) {
    try { const d = await P.get(API + "/providers?q="); const p = d.items.find((x) => x.id === id); return p ? p.availability : []; } catch (e) { return []; }
  }

  // ---------------------------------------------------------------- section
  async function render(c) {
    const sub = subRoute();
    if (sub[0] === "new") return wizard(c, null);
    if (sub[0]) return renderDetail(c, sub[0]);
    return renderList(c);
  }
  P.registerSection({ id: "appointments", title: "Appointments", icon: "📅", order: 80, render });

  // nav badge: appointments waiting for the patient
  P.on("signin", async () => {
    try { const d = await P.get(API + "/dashboard-summary"); P.setBadge("appointments", d.counts.needs_confirmation || 0); } catch (e) { /* optional */ }
  });
})();



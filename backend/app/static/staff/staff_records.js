/* Staff: shared health records on the patient page (key facts strip, category tabs, clinician confirmation,
 * correction requests) + the corrections inbox page. Every tab is opened explicitly: each open is audited
 * server-side and the patient is notified, so nothing is fetched on page load except the tab list. */
(function () {
  const { el } = S;
  const SEV = { severe: "bad", moderate: "warn", mild: "", unknown: "" };
  const HIST_KIND = { condition: "Condition", surgery: "Surgery", hospitalization: "Hospital stay", family_history: "Family history" };
  const VAC_KIND = { vaccine: "Vaccine", screening: "Screening", preventive_care: "Preventive care" };
  const money = (v) => (v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString(undefined, { style: "currency", currency: "USD" }));
  const join = (parts, sep) => parts.filter((x) => x !== null && x !== undefined && x !== "").join(sep || " · ");

  function sourceLine(item) {
    const confirmed = item.source === "clinician_confirmed";
    return el("div", { class: "rec-meta small" },
      el("span", { class: "badge " + (confirmed ? "good" : ""), text: item.source_label || (confirmed ? "Clinician-confirmed" : "Patient-entered") }),
      confirmed && item.confirmed_by ? el("span", { class: "muted", text: ` by ${item.confirmed_by}` }) : null,
      el("span", { class: "muted", text: ` · Updated ${S.fmtDate(item.updated_at) || "—"}` }));
  }

  // ------------------------------------------------------------------ item renderers
  const R = {
    allergies(items, ctx) {
      const st = items.find((i) => i.record_type === "allergy_status") || { status: "unknown" };
      const list = items.filter((i) => i.record_type !== "allergy_status");
      const banner = st.status === "no_known_allergies"
        ? el("div", { class: "allergy-banner nka", role: "status" }, el("strong", { text: "No known allergies" }), " — the patient has explicitly stated they have no known allergies.")
        : st.status === "has_allergies"
          ? el("div", { class: "allergy-banner has", role: "status" }, el("strong", { text: `Allergies listed (${st.active_count || list.filter((a) => a.status === "active").length} active)` }))
          : el("div", { class: "allergy-banner unknown", role: "status" }, el("strong", { text: "Allergies: Unknown" }), " — allergy information has not been provided. This is NOT the same as “no known allergies”. Ask the patient.");
      return [banner, ...list.map((a) => card(a, ctx, a.substance,
        [el("span", { class: "badge " + (SEV[a.severity] || ""), text: `Severity: ${S.label(a.severity || "unknown")}` }),
          a.status !== "active" ? el("span", { class: "badge", text: S.label(a.status) }) : null],
        join([a.reaction ? `Reaction: ${a.reaction}` : "", a.category ? S.label(a.category) : "", a.onset ? `Since ${a.onset}` : ""])))];
    },
    medications(items, ctx) {
      if (!items.length) return [S.empty("No medications listed", "The patient shared this category but has not listed any medications.")];
      const groups = [["active", "Current"], ["past", "Past"]];
      return groups.flatMap(([k, title]) => {
        const g = items.filter((m) => m.status === k);
        return g.length ? [el("h4", { text: `${title} (${g.length})` }), ...g.map((m) => card(m, ctx, m.name,
          [m.is_supplement ? el("span", { class: "badge info", text: "Supplement" }) : null],
          join([m.dose, m.frequency, m.route, m.reason ? `For: ${m.reason}` : "", m.prescriber_name ? `Prescriber: ${m.prescriber_name}` : "",
            m.start_date ? `Started ${m.start_date}` : "", m.end_date ? `Stopped ${m.end_date}` : ""])))] : [];
      });
    },
    history(items, ctx) {
      if (!items.length) return [S.empty("No history listed")];
      return Object.keys(HIST_KIND).flatMap((k) => {
        const g = items.filter((h) => h.kind === k);
        return g.length ? [el("h4", { text: `${HIST_KIND[k]} (${g.length})` }), ...g.map((h) => card(h, ctx, h.title,
          [h.status && h.status !== "unknown" ? el("span", { class: "badge " + (h.status === "active" ? "info" : ""), text: S.label(h.status) }) : null],
          join([h.relation ? `Relative: ${h.relation}` : "", h.start_date ? `From ${h.start_date}` : "Date unknown", h.end_date ? `to ${h.end_date}` : "",
            h.facility_name])))] : [];
      });
    },
    vaccinations(items, ctx) {
      if (!items.length) return [S.empty("No vaccinations listed")];
      return items.map((v) => card(v, ctx, v.name,
        [el("span", { class: "badge info", text: VAC_KIND[v.kind] || S.label(v.kind) }), el("span", { class: "badge " + (v.status === "completed" ? "good" : ""), text: S.label(v.status) }),
          v.followup_label ? el("span", { class: "badge warn", text: v.followup_label }) : null],
        join([v.date_given ? `Given ${v.date_given}` : "", v.dose_number ? `Dose ${v.dose_number}` : "", v.administered_by,
          v.next_due_date ? `Next due ${S.fmtDate(v.next_due_date)}` : ""])));
    },
    results(items, ctx) {
      if (!items.length) return [S.empty("No results listed")];
      return [el("p", { class: "small muted", text: "Reference ranges come from the lab that ran each test. “Outside the lab's reference range” only describes where a number sits compared with that range — it is not an interpretation." }),
        ...items.map((r) => {
          const val = r.value_num === null || r.value_num === undefined ? (r.value_text || "—") : `${r.value_num}${r.unit ? " " + r.unit : ""}`;
          return card(r, ctx, r.test_name,
            [r.outside_range ? el("span", { class: "badge range", text: r.flag_text || "Outside the lab's reference range" }) : null],
            null,
            el("dl", { class: "kv compact" },
              el("dt", { text: "Result" }), el("dd", {}, el("strong", { text: val }), r.range_direction ? el("span", { class: "muted", text: ` (${r.range_direction} range)` }) : null),
              el("dt", { text: "Reference range" }), el("dd", { text: r.ref_display || "Not provided by the lab" }),
              el("dt", { text: "Date" }), el("dd", { text: r.result_date ? S.fmtDate(r.result_date) : "Unknown" }),
              r.source_name ? [el("dt", { text: "Lab / source" }), el("dd", { text: r.source_name })] : null));
        })];
    },
    billing(items) {
      const b = items[0] || {};
      const sum = b.summary || {};
      const out = [el("dl", { class: "kv" },
        el("dt", { text: "Outstanding" }), el("dd", { text: money(sum.total_outstanding) }),
        el("dt", { text: "Open bills" }), el("dd", { text: String(sum.open_bill_count ?? "—") }),
        el("dt", { text: "Overdue" }), el("dd", { text: sum.overdue_count ? `${sum.overdue_count} (${money(sum.overdue_total)})` : "None" }))];
      out.push(el("h4", { text: "Insurance plans" }), (b.plans || []).length ? el("ul", { class: "plain" }, b.plans.map((p) => el("li", {},
        el("strong", { text: join([p.insurer_name, p.plan_name]) }), " ", el("span", { class: "badge " + (p.verification_status === "verified" ? "good" : ""), text: p.source_label || S.label(p.verification_status) }),
        el("div", { class: "small muted", text: join([S.label(p.coverage_rank), p.member_id ? `Member ${p.member_id}` : "", p.group_number ? `Group ${p.group_number}` : ""]) })))) : el("p", { class: "muted", text: "No plans on file." }));
      out.push(el("h4", { text: "Bills" }), (b.bills || []).length ? el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Provider", "Service", "Balance", "Due", "Status", "Claim"].map((h) => el("th", { scope: "col", text: h })))),
        el("tbody", {}, b.bills.map((x) => el("tr", {},
          el("td", { text: x.provider_name }), el("td", { text: join([x.description, S.fmtDate(x.service_date)]) }),
          el("td", { text: money(x.balance) }), el("td", { text: S.fmtDate(x.due_date) || "—" }),
          el("td", {}, el("span", { class: "badge " + (x.overdue ? "bad" : x.status === "paid" ? "good" : ""), text: x.overdue ? "Overdue" : S.label(x.status) })),
          el("td", { text: x.claim_status ? S.label(x.claim_status) : "No claim" })))))) : el("p", { class: "muted", text: "No bills." }));
      out.push(el("p", { class: "small muted", text: "The patient's private notes and dispute messages are never shown to staff." }));
      return out;
    },
  };

  function card(item, ctx, title, badges, line, extra) {
    const node = el("article", { class: "rec" },
      el("div", { class: "row between" }, el("h5", { class: "rec-title", text: title || "Untitled" }), el("div", { class: "row" }, badges)),
      line ? el("p", { class: "rec-line", text: line }) : null, extra || null,
      item.notes ? el("p", { class: "small", text: `Note: ${item.notes}` }) : null,
      sourceLine(item));
    if (ctx.canConfirm && item.source === "patient_entered") {
      node.append(el("button", { class: "btn sm", text: "Mark clinician-confirmed", "aria-label": `Mark ${title} as clinician-confirmed`,
        onclick: async (ev) => {
          const ok = await S.confirm(`Confirm “${title}” as reviewed by you? The patient will no longer be able to edit it directly (they can still request a correction).`, "Confirm entry");
          if (!ok) return;
          ev.target.disabled = true;
          try {
            const upd = await S.post(`/api/staff/patients/${ctx.pid}/records/${ctx.category}/${item.id}/confirm`, {});
            Object.assign(item, upd);
            S.toast("Marked clinician-confirmed.", "ok");
            ctx.redraw();
          } catch (e) { S.toast(e.message, "error"); ev.target.disabled = false; }
        } }));
    }
    return node;
  }

  // ------------------------------------------------------------------ patient page mount
  S.records = {};
  S.records.mount = async function (c, pid, page) {
    const strip = el("section", { class: "keyfacts", "aria-label": "Key facts" });
    const sec = el("section", { class: "card", "aria-labelledby": "h-rec" });
    c.append(strip, sec);
    let ov;
    try { ov = await S.load(sec, () => S.get(`/api/staff/patients/${pid}/records`)); }
    catch (e) { drawStrip(); return; }
    const cache = {};
    let current = "allergies";
    const tabs = ov.tabs;
    const id = page.identity;

    function age(dob) {
      if (!dob) return null;
      const d = new Date(dob + "T00:00:00"), n = new Date();
      let a = n.getFullYear() - d.getFullYear();
      if (n < new Date(n.getFullYear(), d.getMonth(), d.getDate())) a--;
      return isNaN(a) ? null : a;
    }
    function fact(label, value, kind, onclick) {
      const v = onclick ? el("button", { class: "linklike", onclick, text: value }) : el("span", { text: value });
      return el("div", { class: "fact " + (kind || "") }, el("div", { class: "fact-label", text: label }), el("div", { class: "fact-value" }, v));
    }
    function drawStrip() {
      S.clear(strip);
      const a = age(id.dob);
      strip.append(fact("Patient", join([id.preferred_name && id.preferred_name !== id.legal_name ? `${id.legal_name} (“${id.preferred_name}”)` : id.legal_name,
        a !== null ? `${a} y` : "", id.dob ? `DOB ${S.fmtDate(id.dob)}` : ""])));
      if (!ov) return;
      const t = (cat) => tabs.find((x) => x.category === cat);
      const al = t("allergies"), alData = cache.allergies;
      if (alData) {
        const st = alData.find((i) => i.record_type === "allergy_status") || {};
        const act = alData.filter((i) => i.record_type === "allergy" && i.status === "active");
        if (st.status === "has_allergies") strip.append(fact("Allergies", act.map((x) => x.substance + (x.severity === "severe" ? " (severe)" : "")).join(", ") || "Listed", "bad"));
        else if (st.status === "no_known_allergies") strip.append(fact("Allergies", "No known allergies", "good"));
        else strip.append(fact("Allergies", "Unknown — ask the patient", "warn"));
      } else if (al.shared) strip.append(fact("Allergies", "Shared — tap to show", "warn", () => openTab("allergies", true)));
      else strip.append(fact("Allergies", al.role_allowed ? "Not shared with you" : "Not available for your role", "muted"));
      if (cache.medications) strip.append(fact("Current medications", String(cache.medications.filter((m) => m.status === "active").length)));
      if (cache.history) strip.append(fact("Active conditions", String(cache.history.filter((h) => h.kind === "condition" && h.status === "active").length)));
      if (cache.results) {
        const latest = {}; cache.results.forEach((r) => { if (!latest[r.series_key]) latest[r.series_key] = r; });
        const n = Object.values(latest).filter((r) => r.outside_range).length;
        strip.append(fact("Latest results outside lab range", String(n), n ? "range" : ""));
      }
      const shared = tabs.filter((x) => x.shared);
      const ends = shared.map((x) => x.grant_expires_at).filter(Boolean).sort()[0];
      strip.append(fact("Shared with you", shared.length ? `${shared.map((x) => x.label).join(", ")}${ends ? " · first ends " + S.fmtDate(ends) : ""}` : "No health data", shared.length ? "" : "muted"));
      if (ov.open_corrections) strip.append(fact("Open correction requests", String(ov.open_corrections), "warn", () => document.getElementById("h-corr")?.scrollIntoView({ behavior: "smooth" })));
    }

    const tablist = el("div", { class: "rtabs", role: "tablist", "aria-label": "Health record categories" });
    const panel = el("div", { class: "rpanel", role: "tabpanel", tabindex: "0", id: "rec-panel" });
    const buttons = {};
    tabs.forEach((t) => {
      const b = el("button", { role: "tab", id: `tab-${t.category}`, "aria-controls": "rec-panel", class: "rtab" + (t.category === "allergies" ? " allergy" : "") + (t.shared ? "" : " locked"),
        onclick: () => openTab(t.category) }, t.label, t.shared ? null : el("span", { class: "small", text: t.role_allowed ? " · not shared" : " · n/a" }));
      buttons[t.category] = b; tablist.append(b);
    });
    tablist.addEventListener("keydown", (ev) => {
      const order = tabs.map((t) => t.category); let i = order.indexOf(current);
      if (ev.key === "ArrowRight") i = (i + 1) % order.length; else if (ev.key === "ArrowLeft") i = (i - 1 + order.length) % order.length;
      else if (ev.key === "Home") i = 0; else if (ev.key === "End") i = order.length - 1; else return;
      ev.preventDefault(); openTab(order[i]); buttons[order[i]].focus();
    });
    const sharedCount = tabs.filter((t) => t.shared).length;
    sec.append(el("div", { class: "row between" }, el("h3", { id: "h-rec", text: "Health records" }),
      sharedCount > 1 ? el("button", { class: "btn sm", text: "Show all shared", onclick: openAll }) : null),
      el("p", { class: "small muted", text: ov.notice }), tablist, panel);

    async function openAll() {
      for (const t of tabs) if (t.shared && !cache[t.category]) { await load(t.category).catch((e) => console.warn("prefetch of " + t.category + " failed:", e.message)); }
      openTab(current);
    }
    async function load(cat) {
      const r = await S.get(`/api/staff/patients/${pid}/records/${cat}`);
      cache[cat] = r.items; tabs.find((t) => t.category === cat).grant_expires_at = r.grant_expires_at;
      drawStrip();
      return r;
    }
    function openTab(cat, fetchNow) {
      current = cat;
      Object.entries(buttons).forEach(([k, b]) => { b.setAttribute("aria-selected", String(k === cat)); b.tabIndex = k === cat ? 0 : -1; });
      panel.setAttribute("aria-labelledby", `tab-${cat}`);
      const t = tabs.find((x) => x.category === cat);
      S.clear(panel);
      if (!t.shared) {
        panel.append(t.role_allowed
          ? S.empty("Not shared with you", `${id.legal_name} has not shared ${t.label.toLowerCase()} with your organization, or the sharing has ended.`,
            el("button", { class: "btn primary", text: "Request access", onclick: () => S.requestAccess(pid, page, () => S.route(), [cat]) }))
          : S.empty("Not available for your role", `${S.ROLE[S.me.role]} accounts can't view ${t.label.toLowerCase()}.`));
        return;
      }
      if (cache[cat]) return draw(cat);
      const go = async () => {
        S.clear(panel).append(el("p", { class: "spinner", role: "status", text: "Loading…" }));
        try { await load(cat); if (current === cat) draw(cat); }
        catch (e) {
          if (e.status === 403) { t.shared = false; buttons[cat].classList.add("locked"); drawStrip(); openTab(cat); S.toast(e.message, "error"); }
          else S.clear(panel).append(S.errorBox(e, go));
        }
      };
      if (fetchNow) return go();
      panel.append(el("div", { class: "reveal" + (cat === "allergies" ? " allergy" : "") },
        el("p", {}, `Shared with your organization until ${S.fmtDate(t.grant_expires_at) || "—"}.`),
        el("button", { class: "btn primary", text: `Show ${t.label.toLowerCase()}`, onclick: go }),
        el("p", { class: "small muted", text: "The patient will see that you opened this." })));
    }
    function draw(cat) {
      S.clear(panel);
      const ctx = { pid, category: cat, canConfirm: ov.can_confirm && cat !== "billing", redraw: () => { drawStrip(); draw(cat); } };
      const t = tabs.find((x) => x.category === cat);
      panel.append(el("p", { class: "small muted", text: `Access ends ${S.fmtDate(t.grant_expires_at) || "—"}` }), ...R[cat](cache[cat] || [], ctx));
    }
    drawStrip();
    openTab("allergies");

    if (ov.can_confirm) c.append(correctionsCard(pid, ov.open_corrections || 0, () => { ov.open_corrections = Math.max(0, (ov.open_corrections || 1) - 1); drawStrip(); }));
  };

  // ------------------------------------------------------------------ corrections (patient page)
  function correctionsCard(pid, n, onResolved) {
    const box = el("section", { class: "card", "aria-labelledby": "h-corr" });
    const out = el("div", { "aria-live": "polite" });
    const intro = el("p", { class: "muted" });
    const setIntro = () => { intro.textContent = n ? `${n} open request(s) about records shared with you.` : "No open requests about records shared with you."; };
    setIntro();
    box.append(el("h3", { id: "h-corr", text: "Correction requests" }), intro,
      ...(n ? [el("button", { class: "btn", text: "Show correction requests", onclick: show })] : []), out);
    async function show() {
      let r;
      try { r = await S.load(out, () => S.get(`/api/staff/patients/${pid}/corrections`)); } catch (e) { return; }
      if (!r.items.length) { out.append(el("p", { class: "muted", text: "No open requests." })); return; }
      r.items.forEach((c) => out.append(el("article", { class: "rec" },
        el("div", { class: "row between" }, el("h5", { class: "rec-title", text: c.record_label || "Record" }), el("span", { class: "badge info", text: c.category_label })),
        el("p", { text: `“${c.message}”` }), el("p", { class: "small muted", text: `Requested ${S.fmtDate(c.created_at)}` }),
        el("div", { class: "row" },
          el("button", { class: "btn sm primary", text: "Mark resolved", onclick: () => decide(c, "resolved") }),
          el("button", { class: "btn sm danger", text: "Decline", onclick: () => decide(c, "declined") })))));
    }
    async function decide(c, status) {
      const done = await S.form({
        title: status === "resolved" ? "Resolve correction request" : "Decline correction request",
        intro: `About “${c.record_label}”. The patient is notified and sees your note.`,
        fields: [{ name: "note", label: status === "resolved" ? "Note to the patient" : "Reason (shown to the patient)", type: "textarea", required: status === "declined", maxlength: 2000 }],
        submitLabel: status === "resolved" ? "Mark resolved" : "Decline request",
        onSubmit: (v) => S.post(`/api/staff/corrections/${c.id}/resolve`, { status, note: v.note }),
      });
      if (done) {
        S.toast(status === "resolved" ? "Marked resolved. The patient was notified." : "Declined. The patient was notified.", "ok");
        n = Math.max(0, n - 1); setIntro(); onResolved(); show();
      }
    }
    return box;
  }

  // ------------------------------------------------------------------ corrections inbox page
  S.pages.corrections = async function (c) {
    c.append(el("div", { class: "row between" }, el("h2", { text: "Correction requests" }), el("button", { class: "btn", onclick: () => S.route(), text: "Refresh" })),
      el("p", { class: "muted", text: "Open requests from patients asking for a record to be corrected. Only categories the patient currently shares with your organization are listed; open the patient to read and answer the request." }));
    const list = el("div");
    c.append(list);
    let r;
    try { r = await S.load(list, () => S.get("/api/staff/corrections")); } catch (e) { return; }
    if (!r.items.length) { list.append(S.empty("No open correction requests", "When a patient asks for a correction to records they share with you, it shows up here.")); return; }
    list.append(el("div", { class: "table-wrap" }, el("table", {}, el("caption", { class: "sr", text: "Open correction requests" }),
      el("thead", {}, el("tr", {}, ["Patient", "Record type", "Requested", ""].map((h) => el("th", { scope: "col", text: h })))),
      el("tbody", {}, r.items.map((x) => el("tr", {},
        el("td", {}, el("strong", { text: x.patient_name || "Patient" })), el("td", { text: x.category_label }),
        el("td", { text: S.fmtDate(x.created_at) }),
        el("td", {}, el("a", { class: "btn sm primary", href: `#/patient/${x.patient_id}`, text: "Open patient" }))))))));
  };
})();

/* Medical history timeline (W7): conditions, surgeries, hospital stays, family history. */
(function () {
  const P = window.Portal, M = () => window.MedUI, el = P.el;
  const KIND_LABEL = { condition: "Condition", surgery: "Surgery", hospitalization: "Hospital stay", family_history: "Family history" };
  const PAGE_OF = { medications: ["med_meds", "Medications"], vaccinations: ["med_vaccines", "Vaccines"], results: ["med_results", "Lab reports"] };
  const state = { extra: new Set() };

  async function fields() {
    const provs = await M().providerOptions();
    return [
      { name: "kind", label: "Type", type: "select", required: true, default: "condition", options: M().opts(["condition", "surgery", "hospitalization", "family_history"], KIND_LABEL) },
      { name: "title", label: "What is it?", required: true, maxlength: 200, help: "For example, the name on your paperwork." },
      { name: "status", label: "Status", type: "select", default: "unknown", options: M().opts(["active", "resolved", "unknown"]) },
      { name: "start_date", label: "Started / happened", type: "date" },
      { name: "end_date", label: "Ended", type: "date" },
      { name: "relation", label: "Relative (family history only)", maxlength: 60, placeholder: "e.g. Mother" },
      { name: "facility_name", label: "Hospital / clinic", maxlength: 200 },
      { name: "provider_id", label: "Care team member", type: "select", options: provs },
      { name: "notes", label: "Notes", type: "textarea" },
    ];
  }

  async function build(host) {
    const f = await fields();
    const reload = M().reloader(host, build);
    const types = ["history", ...state.extra];
    const [tl, list] = await Promise.all([M().api("/timeline?types=" + types.join(",")), M().api("/history")]);
    const byId = Object.fromEntries(list.map((r) => [r.id, r]));

    host.append(el("div", { class: "row between" },
      el("p", { class: "muted", style: "margin:0", text: "Newest first. Entries without a date are listed at the end." }),
      el("button", { class: "btn primary", type: "button", onclick: () => M().openRecordForm("history", "history entry", f, null, reload), text: "Add history entry" })));
    host.append(el("fieldset", { class: "med-filter" }, el("legend", { text: "Also show on the timeline" }),
      [["medications", "Medications"], ["vaccinations", "Vaccines"], ["results", "Lab reports"]].map(([k, label]) => {
        const id = "tl-" + k;
        const cb = el("input", { type: "checkbox", id, checked: state.extra.has(k) ? true : null,
          onchange: (e) => { e.target.checked ? state.extra.add(k) : state.extra.delete(k); reload(); } });
        return el("label", { for: id }, cb, label);
      })));

    const item = (e) => {
      const rec = byId[e.id];
      const body = el("div", { class: "card med-item" },
        el("p", { class: "med-date", style: "margin:0", text: M().fmtPartial(e.date) }),
        el("h3", { text: e.title }),
        e.subtitle ? el("p", { style: "margin:0", text: e.subtitle }) : null,
        e.flag_text ? el("p", { style: "margin:.3rem 0 0" }, el("span", { class: "med-flag", text: e.flag_text })) : null,
        M().meta({ source: e.source, updated_at: e.updated_at }));
      if (e.type === "history" && rec) {
        if (rec.facility_name) body.append(el("p", { class: "muted", text: "Where: " + rec.facility_name }));
        if (rec.notes) body.append(el("p", { class: "muted", text: rec.notes }));
        body.append(M().actions("history", rec, f, reload, "history entry"));
      } else if (PAGE_OF[e.type]) {
        body.append(el("a", { href: "#/" + PAGE_OF[e.type][0], text: "Open " + PAGE_OF[e.type][1] }));
      }
      return el("li", {}, body);
    };

    if (!tl.events.length && !tl.undated.length) {
      host.append(el("div", { class: "card" }, el("p", { text: "No history entries yet. Add one whenever you like - or skip this; you can use the rest of the app without it." })));
      return;
    }
    host.append(el("ol", { class: "med-timeline", "aria-label": "Timeline, newest first" }, tl.events.map(item)));
    if (tl.undated.length) host.append(el("section", { "aria-labelledby": "tl-undated" }, el("h3", { id: "tl-undated", text: "Date unknown" }),
      el("ol", { class: "med-timeline" }, tl.undated.map(item))));
  }

  P.registerSection({
    id: "med_history", title: "Medical history", icon: "🕑", order: 41, hidden: true,
    render: async (c) => M().page(c, "med_history", async (host) => { const h = el("div"); host.append(h); await build(h); }),
  });
})();


/* Medications & supplements (W7). Descriptive only: no interaction checks, no advice. */
(function () {
  const P = window.Portal, M = () => window.MedUI, el = P.el;

  async function fields() {
    return [
      { name: "name", label: "Name", required: true, maxlength: 200 },
      { name: "status", label: "Active?", type: "select", required: true, default: "active", options: [{ value: "active", label: "Yes" }, { value: "past", label: "No" }] },
      { name: "is_supplement", label: "This is a supplement / vitamin / herbal product", type: "checkbox" },
      { name: "dose", label: "Dose", required: true, maxlength: 100, placeholder: "e.g. 500 mg" },
      { name: "frequency", label: "Frequency", required: true, maxlength: 100, placeholder: "e.g. twice daily" },
      { name: "start_date", label: "Started", type: "date", required: true },
      { name: "end_date", label: "Stopped", type: "date", requiredIf: { field: "status", value: "past" } },
      { name: "prescriber_name", label: "Prescribed by", maxlength: 200 },
      { name: "notes", label: "Notes", type: "textarea" },
    ];
  }

  async function build(host) {
    const f = await fields();
    const reload = M().reloader(host, build);
    const list = await M().api("/medications");
    host.append(el("div", { class: "row between" },
      el("p", { class: "muted", style: "margin:0", text: "Includes prescriptions, over-the-counter products and supplements. This list is a record only - it gives no advice." }),
      el("button", { class: "btn primary", type: "button", onclick: () => M().openRecordForm("medications", "medication", f, null, reload), text: "Add medication" })));

    const card = (m) => el("article", { class: "card med-item" },
      el("h3", { text: m.name }),
      el("p", { style: "margin:0" }, m.is_supplement ? P.badge("Supplement", "info") : null, " ", P.badge(m.status === "active" ? "Active" : "Past", m.status === "active" ? "good" : "")),
      M().dl([["Dose", m.dose], ["Frequency", m.frequency],
        ["Started", m.start_date ? M().fmtPartial(m.start_date) : null, true], m.status === "past" ? ["Stopped", M().fmtPartial(m.end_date), true] : null,
        ["Prescribed by", m.prescriber_name]]),
      m.notes ? el("p", { class: "muted", text: m.notes }) : null,
      M().meta(m), M().actions("medications", m, f, reload, "medication"));

    const section = (id, title, items, empty) => {
      const s = el("section", { "aria-labelledby": id }, el("h3", { id, text: title + " (" + items.length + ")" }));
      if (!items.length) s.append(el("p", { class: "muted", text: empty }));
      items.forEach((m) => s.append(card(m)));
      return s;
    };
    const active = list.filter((m) => m.status === "active"), past = list.filter((m) => m.status === "past");
    host.append(section("med-active", "Active", active, "Nothing listed as active. You don't have to add anything."));
    host.append(section("med-past", "Past", past, "No past medications listed."));
  }

  P.registerSection({
    id: "med_meds", title: "Medications", icon: "💊", order: 42, hidden: true,
    render: async (c) => M().page(c, "med_meds", async (host) => { const h = el("div"); host.append(h); await build(h); }),
  });
})();

/* Vaccinations & preventive care with follow-up dates (W7). Dates are reminders of what is recorded, never advice. */
(function () {
  const P = window.Portal, M = () => window.MedUI, el = P.el;
  const KIND = { vaccine: "Vaccine", screening: "Screening", preventive_care: "Preventive care" };
  const STATUS = { completed: "Completed", scheduled: "Scheduled", declined: "Declined", unknown: "Unknown" };

  async function fields() {
    return [
      { name: "name", label: "Name", required: true, maxlength: 200, placeholder: "e.g. Influenza" },
      { name: "status", label: "Status", type: "select", required: true, default: "completed", options: M().opts(["completed", "scheduled", "declined", "unknown"], STATUS) },
      { name: "date_given", label: "Date done", type: "date" },
      { name: "dose_number", label: "Dose number", type: "number" },
      { name: "administered_by", label: "Where / by whom", maxlength: 200 },
      { name: "notes", label: "Notes / follow-up", type: "textarea" },
    ];
  }

  async function build(host) {
    const f = await fields();
    const reload = M().reloader(host, build);
    const list = (await M().api("/vaccinations")).filter((v) => v.kind === "vaccine");
    host.append(el("div", { class: "row between" },
      el("p", { class: "muted", style: "margin:0", text: "Your vaccination records. Add follow-up details in Notes." }),
      el("button", { class: "btn primary", type: "button", onclick: () => M().openRecordForm("vaccinations", "vaccine", f, null, reload), text: "Add vaccine" })));
    if (!list.length) { host.append(el("div", { class: "card" }, el("p", { text: "Nothing recorded yet. Add entries whenever you like." }))); return; }
    list.forEach((v) => host.append(el("article", { class: "card med-item" },
      el("h3", { text: v.name }),
      el("p", { style: "margin:0" }, P.badge(KIND[v.kind] || v.kind, "info"), " ", P.badge(STATUS[v.status] || v.status, v.status === "completed" ? "good" : ""),
        v.followup_label ? [" ", P.badge(v.followup_label, v.followup_state === "passed" ? "warn" : "info")] : null),
      M().dl([["Done", v.status === "scheduled" ? null : M().fmtPartial(v.date_given), v.status !== "scheduled"], ["Dose", v.dose_number], ["Where / by", v.administered_by],
        ["Recorded follow-up", v.next_due_date ? P.fmtDate(v.next_due_date) : null]]),
      v.notes ? el("p", { class: "muted", text: v.notes }) : null,
      M().meta(v), M().actions("vaccinations", v, f, reload, "entry"))));
  }

  P.registerSection({
    id: "med_vaccines", title: "Vaccines", icon: "💉", order: 44, hidden: true,
    render: async (c) => M().page(c, "med_vaccines", async (host) => { const h = el("div"); host.append(h); await build(h); }),
  });
})();

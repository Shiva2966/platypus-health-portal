/* Allergies (W7): explicit "No known allergies" is different from "Unknown" and both are shown prominently. */
(function () {
  const P = window.Portal, M = () => window.MedUI, el = P.el;

  const fields = [
    { name: "substance", label: "Allergic to", required: true, maxlength: 200, placeholder: "e.g. Penicillin, peanuts, latex" },
    { name: "severity", label: "Severity", type: "select", required: true, default: "unknown", options: M0(["mild", "moderate", "severe", "unknown"]) },
    { name: "onset", label: "Since when", type: "date" },
    { name: "notes", label: "Notes", type: "textarea" },
  ];
  function M0(list) { return list.map((v) => ({ value: v, label: v === "unknown" ? "Unknown" : P.label(v) })); }

  async function setStatus(status, reload) {
    try { await P.put("/api/med/allergy-status", { status }); P.toast("Saved.", "ok"); await reload(); } catch (e) { M().fail(e); }
  }

  async function build(host) {
    const reload = M().reloader(host, build);
    const [st, list] = await Promise.all([M().api("/allergy-status"), M().api("/allergies")]);
    const active = list.filter((a) => a.status === "active"), inactive = list.filter((a) => a.status !== "active");

    const choose = el("section", { class: "card", "aria-labelledby": "al-state" }, el("h3", { id: "al-state", text: "Your allergy status" }));
    choose.append(el("div", { class: "row" },
      el("button", { class: "btn", type: "button", text: "Yes — add allergy", onclick: () => M().openRecordForm("allergies", "allergy", fields, null, reload) }),
      el("button", { class: "btn", type: "button", text: "No known allergies", disabled: active.length > 0,
        onclick: () => setStatus("no_known_allergies", reload) }),
      el("button", { class: "btn", type: "button", text: "Unknown", disabled: active.length > 0,
        onclick: () => setStatus("unknown", reload) })));
    choose.append(el("p", { role: "status", text: st.label }));
    if (active.length) choose.append(el("p", { class: "muted", text: "Remove active allergies before choosing No known allergies or Unknown." }));
    host.append(choose);
    host.append(el("div", { class: "row between" }, el("h3", { style: "margin:0", text: "Allergies" }),
      el("button", { class: "btn primary", type: "button", onclick: () => M().openRecordForm("allergies", "allergy", fields, null, reload), text: "Add allergy" })));

    const card = (a) => el("article", { class: "card med-item" + (a.status === "active" && a.severity === "severe" ? " err" : "") },
      el("h3", { text: a.substance }),
      el("p", { style: "margin:0" }, a.severity !== "unknown" ? P.badge(P.label(a.severity), a.severity === "severe" ? "bad" : "warn") : P.badge("Severity unknown", ""),
        " ", a.category !== "unknown" ? P.badge(P.label(a.category), "") : null),
      M().dl([["Reaction", a.reaction], ["Since", a.onset ? M().fmtPartial(a.onset) : null]]),
      a.notes ? el("p", { class: "muted", text: a.notes }) : null,
      M().meta(a), M().actions("allergies", a, fields, reload, "allergy"));
    if (!active.length) host.append(el("p", { class: "muted", text: "No active allergies listed." }));
    active.forEach((a) => host.append(card(a)));
    if (inactive.length) {
      host.append(el("h3", { text: "No longer applies (" + inactive.length + ")" }));
      inactive.forEach((a) => host.append(card(a)));
    }
  }

  P.registerSection({
    id: "med_allergies", title: "Allergies", icon: "⚠", order: 43, hidden: true,
    render: async (c) => { c.append(window.MedUI.tabs("med_allergies")); const h = el("div"); c.append(h); await build(h);
      c.append(el("p", { class: "muted med-note", text: window.MedUI.DISCLAIMER })); },
  });
})();

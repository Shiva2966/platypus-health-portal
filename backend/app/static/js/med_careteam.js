/* Care team & facilities directory (W7). */
(function () {
  const P = window.Portal, M = () => window.MedUI, el = P.el;
  const KIND = { clinician: "Clinician", facility: "Hospital / clinic", pharmacy: "Pharmacy", lab: "Lab", other: "Other" };

  const fields = [
    { name: "name", label: "Name", required: true, maxlength: 200 },
    { name: "kind", label: "Type", type: "select", required: true, default: "clinician", options: Object.entries(KIND).map(([value, label]) => ({ value, label })) },
    { name: "role_in_care", label: "Role in your care", maxlength: 100, placeholder: "e.g. Primary care" },
    { name: "specialty", label: "Specialty", maxlength: 100 },
    { name: "organization", label: "Organization", maxlength: 200 },
    { name: "phone", label: "Phone", type: "tel", maxlength: 50 },
    { name: "address", label: "Address", maxlength: 300 },
    { name: "last_seen", label: "Last visit", type: "date" },
    { name: "is_care_team", label: "Show in my care team", type: "checkbox", default: true },
    { name: "notes", label: "Notes", type: "textarea" },
  ];

  async function build(host) {
    const reload = M().reloader(host, build);
    const list = await M().api("/providers");
    host.append(el("div", { class: "row between" },
      el("p", { class: "muted", style: "margin:0", text: "Your own directory of clinicians, hospitals, labs and pharmacies. Adding someone here does not share anything with them." }),
      el("button", { class: "btn primary", type: "button", onclick: () => M().openRecordForm("providers", "provider", fields, null, reload), text: "Add provider" })));
    const team = list.filter((p) => p.is_care_team), other = list.filter((p) => !p.is_care_team);
    const card = (p) => el("article", { class: "card med-item" },
      el("h3", { text: p.name }),
      el("p", { style: "margin:0" }, P.badge(KIND[p.kind] || p.kind, "info"), p.role_in_care ? [" ", P.badge(p.role_in_care, "")] : null),
      M().dl([["Specialty", p.specialty], ["Organization", p.organization], ["Phone", p.phone], ["Address", p.address],
        ["Last visit", p.last_seen ? P.fmtDate(p.last_seen) : null]]),
      p.notes ? el("p", { class: "muted", text: p.notes }) : null,
      M().meta(p), M().actions("providers", p, fields, reload, "provider"));
    const section = (id, title, items, empty) => {
      const s = el("section", { "aria-labelledby": id }, el("h3", { id, text: title + " (" + items.length + ")" }));
      if (!items.length) s.append(el("p", { class: "muted", text: empty }));
      items.forEach((p) => s.append(card(p)));
      return s;
    };
    host.append(section("ct-team", "My care team", team, "No one added yet. This is optional."));
    host.append(section("ct-other", "Other facilities & pharmacies", other, "None listed."));
  }

  return; // Directory retained in storage; removed from patient navigation.
  P.registerSection({
    id: "med_careteam", title: "Care team", icon: "👥", order: 46, hidden: true,
    render: async (c) => M().page(c, "med_careteam", async (host) => { const h = el("div"); host.append(h); await build(h); }),
  });
})();

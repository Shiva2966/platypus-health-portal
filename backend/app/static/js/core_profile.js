/* W1 pages: Profile (+ identity verification) and Emergency contacts. */
(function () {
  const { el, fmtDate } = Portal;

  Portal.registerSection({
    id: "profile", title: "My profile", icon: "👤", order: 20,
    async render(c) {
      const { profile: p, fields } = await Portal.get("/api/profile");
      const kv = (k, v) => [el("dt", { text: k }), el("dd", { text: v || "Not provided" })];
      const vKind = { verified: "good", pending: "warn", unverified: "" }[p.verification_status];

      c.append(
        el("div", { class: "card" },
          el("div", { class: "row between" }, el("h3", { text: "About me" }),
            el("button", { class: "btn small", text: "Edit profile", onclick: edit })),
          el("dl", { class: "kv" },
            ...kv("Legal name", p.legal_name), ...kv("Preferred name", p.preferred_name), ...kv("Date of birth", p.dob && fmtDate(p.dob)),
            ...kv("Address", p.address), ...kv("Phone", p.phone), ...kv("Email", p.email), ...kv("Pronouns", p.pronouns)),
          el("p", { class: "muted", text: "Last updated " + fmtDate(p.updated_at) })),
        el("div", { class: "card" },
          el("h3", { text: "Your patient ID" }),
          el("p", { class: "muted", text: "A random code that clinics can use to match you to your record. It contains no personal information." }),
          el("div", { class: "row", style: "align-items:center;gap:.5rem;flex-wrap:wrap" },
            el("code", { text: p.id }),
            el("button", { class: "btn small", text: "Copy", "aria-label": "Copy patient ID to clipboard",
              onclick: async function () {
                try {
                  await navigator.clipboard.writeText(p.id);
                  this.textContent = "Copied!";
                  setTimeout(() => { this.textContent = "Copy"; }, 2000);
                } catch (_) { Portal.toast("Copy not supported in this browser. Please copy the code manually.", "warn"); }
              } }))));

      function edit() {
        const nameFields = [
          { name: "first_name", label: "First name", required: true, maxlength: 100 },
          { name: "middle_name", label: "Middle name", maxlength: 100 },
          { name: "last_name", label: "Last name", required: true, maxlength: 100 },
        ];
        Portal.openForm({ title: "Edit my profile", fields: [...nameFields, ...fields.filter((f) => f.name !== "legal_name")], values: p,
          intro: "Enter your name in separate fields. Current name: " + p.legal_name + ". First name, last name, date of birth and email are required.",
          onSubmit: (v) => {
            const { first_name, middle_name, last_name, ...profile } = v;
            return Portal.put("/api/profile", { ...profile, legal_name: [first_name, middle_name, last_name].filter(Boolean).join(" ") });
          } }).then((ok) => { if (ok) { Portal.toast("Profile saved.", "ok"); Portal.refresh(); } });
      }
    },
  });

  Portal.registerSection({
    id: "contacts", title: "Emergency contacts", icon: "☎", order: 30,
    async render(c) {
      const { items, fields: originalFields } = await Portal.get("/api/emergency-contacts");
      const fields = originalFields.map((f) => (f.type === "checkbox" ? { ...f, type: "select", required: true, default: "false", options: [{ value: "true", label: "Yes" }, { value: "false", label: "No" }] } : { ...f, required: f.name !== "notes" }));
      c.append(
        el("div", { class: "card info" },
          el("h3", { text: "Two different things" }),
          el("ul", {},
            el("li", {}, el("strong", { text: "Emergency contact: " }), "someone we call if something happens to you."),
            el("li", {}, el("strong", { text: "Authorized to see records: " }), "someone you want to be allowed to see your health information. This is a separate choice, off by default. To really give access, use Sharing & consent."))),
        el("div", { class: "row" }, el("button", { class: "btn primary", text: "Add emergency contact", onclick: () => open() })));
      if (!items.length) c.append(el("p", { class: "empty", text: "No emergency contacts yet. You can add one whenever you like." }));
      items.forEach((ct) => c.append(el("div", { class: "card" },
        el("div", { class: "row between" }, el("h3", { text: ct.name + " (" + ct.relationship + ")" }),
          el("span", { class: "row" },
            ct.is_primary ? Portal.badge("Primary contact", "info") : null,
            Portal.badge(ct.authorized_to_access_records ? "Authorized to see records" : "Not authorized to see records", ct.authorized_to_access_records ? "warn" : ""))),
        el("p", {}, "Phone: ", el("a", { href: "tel:" + ct.phone, text: ct.phone }), ct.email ? "  •  " + ct.email : ""),
        ct.notes ? el("p", { class: "muted", text: ct.notes }) : null,
        el("div", { class: "row" },
          ct.is_primary ? null : el("button", { class: "btn small", text: "Make primary", "aria-label": "Make " + ct.name + " my primary contact", onclick: async () => {
            try { await Portal.api("POST", "/api/emergency-contacts/" + ct.id + "/primary", {}); Portal.toast(ct.name + " is now your primary contact.", "ok"); Portal.refresh(); } catch (e) { Portal.toast(e.message, "error"); } } }),
          el("button", { class: "btn small", text: "Edit", "aria-label": "Edit " + ct.name, onclick: () => open(ct) }),
          el("button", { class: "btn small danger", text: "Delete", "aria-label": "Delete " + ct.name, onclick: async () => {
            if (!(await Portal.confirm("Delete " + ct.name + " from your emergency contacts?", "Delete"))) return;
            try { await Portal.api("DELETE", "/api/emergency-contacts/" + ct.id); Portal.refresh(); } catch (e) { Portal.toast(e.message, "error"); } } })))));

      function open(ct) {
        Portal.openForm({ title: ct ? "Edit contact" : "Add emergency contact", fields, values: { ...(ct || {}), is_primary: String(!!ct?.is_primary), authorized_to_access_records: String(!!ct?.authorized_to_access_records) },
          onSubmit: (v) => { v.is_primary = v.is_primary === "true"; v.authorized_to_access_records = v.authorized_to_access_records === "true"; return (ct ? Portal.put("/api/emergency-contacts/" + ct.id, v) : Portal.post("/api/emergency-contacts", v)); } })
          .then((ok) => { if (ok) { Portal.toast("Saved.", "ok"); Portal.refresh(); } });
      }
    },
  });
})();

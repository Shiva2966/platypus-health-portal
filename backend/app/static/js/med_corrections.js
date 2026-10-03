/* Correction requests (W7): the patient's own list. Staff see these later through the staff portal. */
(function () {
  const P = window.Portal, M = () => window.MedUI, el = P.el;
  const TYPE = { history: "History", medication: "Medication", allergy: "Allergy", vaccination: "Vaccine / prevention", result: "Result", provider: "Care team" };
  const KIND = { open: "info", resolved: "good", declined: "warn", withdrawn: "" };

  async function build(host) {
    const reload = M().reloader(host, build);
    const list = await M().api("/corrections");
    host.append(el("p", { class: "muted", text: "If something in your records looks wrong, use \"Request a correction\" on that entry. Requests are saved here and shown to staff who are allowed to see that record. A request does not change the entry by itself." }));
    if (!list.length) { host.append(el("div", { class: "card" }, el("p", { text: "No correction requests." }))); return; }
    list.forEach((c) => host.append(el("article", { class: "card med-item" },
      el("h3", { text: c.record_label || "Entry" }),
      el("p", { style: "margin:0" }, P.badge(TYPE[c.record_type] || c.record_type, "info"), " ", P.badge(P.label(c.status), KIND[c.status])),
      el("p", { text: c.message }),
      c.resolution_note ? el("p", { class: "muted", text: "Reply: " + c.resolution_note + (c.resolved_by ? " (" + c.resolved_by + ")" : "") }) : null,
      el("p", { class: "muted med-note", text: "Sent " + P.fmtDate(c.created_at) }),
      c.status === "open" ? el("button", { class: "btn small", type: "button", text: "Withdraw request",
        onclick: async () => { try { await P.post("/api/med/corrections/" + c.id + "/withdraw", {}); P.toast("Withdrawn.", "ok"); await reload(); } catch (e) { M().fail(e); } } }) : null)));
  }

  return; // Requests retained in storage; removed from patient navigation.
  P.registerSection({
    id: "med_corrections", title: "Correction requests", icon: "✏", order: 47, hidden: true,
    render: async (c) => M().page(c, "med_corrections", async (host) => { const h = el("div"); host.append(h); await build(h); }),
  });
})();

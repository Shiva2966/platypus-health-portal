/* W8 Bills & claims page (Portal section "billing"): bills, balances, payments + receipts, claims/EOBs, matching,
 * disputes, plain-language explanation, privacy. All text via Portal.el (no innerHTML). */
(function () {
  if (!window.Portal || !window.BillUI) return;
  const { el, money, kv, button, statusBadge } = BillUI;
  const BILL_MONEY = ["billed_amount", "amount_due"];
  const CLAIM_MONEY = ["billed_amount", "allowed_amount", "insurer_paid", "deductible", "copay", "coinsurance", "patient_responsibility"];

  const BILL_FIELDS = [
    { name: "provider_name", label: "Provider / hospital", required: true, maxlength: 200 },
    { name: "service_date", label: "Date of service", type: "date", required: true },
    { name: "billed_amount", label: "Price on the bill ($)", required: true, help: "Example: 1850.00" },
    { name: "amount_due", label: "Amount the bill asks you to pay ($)", required: true },
    { name: "due_date", label: "Due date", type: "date", help: "We use this to remind you." },
    { name: "account_number", label: "Account / statement number", maxlength: 80 },
    { name: "statement_date", label: "Statement date", type: "date" },
    { name: "description", label: "What was it for?", maxlength: 300 },
    { name: "notes", label: "Private notes", type: "textarea", maxlength: 2000, help: "Only you can see these." },
  ];
  const CLAIM_STATUS = [["processing", "Still processing"], ["submitted", "Sent to insurer"], ["paid", "Processed / paid"],
    ["partially_denied", "Partly denied"], ["denied", "Denied"], ["appealed", "Being appealed"]].map(([value, label]) => ({ value, label }));
  const CLAIM_FIELDS = [
    { name: "claim_number", label: "Claim number (on the EOB)", required: true, maxlength: 100 },
    { name: "insurer_name", label: "Insurance company", required: true, maxlength: 200 },
    { name: "provider_name", label: "Provider", required: true, maxlength: 200 },
    { name: "service_date", label: "Date of service", type: "date", required: true },
    { name: "status", label: "Claim status", type: "select", options: CLAIM_STATUS, required: true, default: "processing" },
    { name: "billed_amount", label: "Billed amount ($)", required: true },
    { name: "allowed_amount", label: "Allowed amount ($)", help: "What the plan counts for this service." },
    { name: "insurer_paid", label: "Insurer paid ($)" },
    { name: "deductible", label: "Applied to deductible ($)" },
    { name: "copay", label: "Copay ($)" },
    { name: "coinsurance", label: "Coinsurance ($)" },
    { name: "patient_responsibility", label: "Your share, per the EOB ($)" },
    { name: "denial_reason", label: "Reason denied (if any)", maxlength: 300 },
    { name: "service_description", label: "Service", maxlength: 300 },
    { name: "notes", label: "Private notes", type: "textarea", maxlength: 2000 },
  ];
  const PAY_METHODS = ["card", "check", "cash", "bank_transfer", "hsa_fsa", "other"].map((v) => ({ value: v, label: Portal.label(v) }));
  const REASONS = [["amount_wrong", "The amount looks wrong"], ["duplicate_charge", "I was charged twice"],
    ["insurance_not_applied", "Insurance was not applied"], ["service_not_received", "I did not get this service"],
    ["claim_denied", "My claim was denied"], ["other", "Something else"]].map(([value, label]) => ({ value, label }));

  // ---------- forms ----------
  function billForm(bill, done) {
    return Portal.openForm({
      title: bill ? "Edit bill" : "Add a bill", fields: BILL_FIELDS, values: bill || {},
      intro: "Copy the numbers from your paper or online bill. We check for duplicates so the same bill isn't added twice.",
      onSubmit: async (v) => {
        const body = BillUI.body(v, BILL_MONEY);
        const saved = bill ? await Portal.put("/api/billing/bills/" + bill.id, body) : await Portal.post("/api/billing/bills", body);
        Portal.toast("Bill saved.", "ok");
        return saved;
      },
    }).then((r) => { if (r) done(); });
  }
  function claimForm(claim, done) {
    return Portal.openForm({
      title: claim ? "Edit claim / EOB" : "Add a claim / EOB", fields: CLAIM_FIELDS, values: claim || {},
      intro: "An EOB (Explanation of Benefits) is a letter from your insurer. It is not a bill. If something is not on it, leave the box empty.",
      onSubmit: async (v) => {
        const body = BillUI.body(v, CLAIM_MONEY);
        const saved = claim ? await Portal.put("/api/billing/claims/" + claim.id, body) : await Portal.post("/api/billing/claims", body);
        Portal.toast("Claim saved.", "ok");
        return saved;
      },
    }).then((r) => { if (r) done(); });
  }
  function paymentForm(bill, done) {
    return Portal.openForm({
      title: "Record a payment — " + bill.provider_name, submitLabel: "Save payment",
      intro: "Balance now: " + money(bill.balance) + ". This only records a payment you made. It does not send money.",
      fields: [
        { name: "amount", label: "Amount paid ($)", required: true, default: bill.balance },
        { name: "paid_on", label: "Date paid", type: "date", required: true, default: BillUI.today() },
        { name: "method", label: "How you paid", type: "select", options: PAY_METHODS, required: true, default: "card" },
        { name: "confirmation_number", label: "Confirmation number", help: "Used to stop the same payment being saved twice." },
        { name: "note", label: "Note", maxlength: 300 },
        { name: "separate_payment", label: "This is a separate payment, not a repeat", type: "checkbox",
          help: "Only needed if you paid the same amount on the same day without a confirmation number." },
      ],
      onSubmit: async (v) => {
        const res = await Portal.post("/api/billing/bills/" + bill.id + "/payments", BillUI.body(v, ["amount"]));
        Portal.toast("Payment saved. Receipt " + res.receipt_number, "ok");
        return res;
      },
    }).then((r) => { if (r) { done(); BillUI.printReceipt(r.receipt); } });
  }
  function disputeForm(bill, done) {
    return Portal.openForm({
      title: "Dispute — " + bill.provider_name, submitLabel: "Open dispute",
      intro: "This is a private note-keeping tool. It does not contact the provider. Reminders pause while a dispute is open.",
      fields: [
        { name: "reason_code", label: "Why?", type: "select", options: REASONS, required: true },
        { name: "message", label: "What happened?", type: "textarea", required: true, maxlength: 2000 },
        { name: "disputed_amount", label: "Amount you think is wrong ($)" },
      ],
      onSubmit: async (v) => { await Portal.post("/api/billing/bills/" + bill.id + "/dispute", BillUI.body(v, ["disputed_amount"])); Portal.toast("Dispute opened.", "ok"); return true; },
    }).then((r) => { if (r) done(); });
  }

  // ---------- explanation dialog ----------
  function explainDialog(bill) {
    return Portal.get("/api/billing/bills/" + bill.id + "/explain").then((x) => Portal.dialog("Explain this bill", (close) => {
      const wrap = el("div", { class: "grid" });
      wrap.append(el("p", { class: "muted", role: "note" }, el("strong", { text: x.estimate_label + ". " }), x.disclaimer));
      const sevClass = { problem: "err", warning: "warn", info: "info", ok: "ok" }[x.worst_severity] || "";
      wrap.append(el("div", { class: "card " + sevClass }, el("p", { text: x.summary })));
      const rows = x.lines.filter((l) => l.amount !== null);
      if (rows.length) {
        const tbl = el("table", {}, el("caption", { class: "sr-only", text: "Bill and EOB numbers" }),
          el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Item" }), el("th", { scope: "col", text: "Amount" }), el("th", { scope: "col", text: "From" }))),
          el("tbody", {}, rows.map((l) => el("tr", {}, el("td", {}, el("strong", { text: l.label }), el("br"), el("span", { class: "muted", text: l.plain })),
            el("td", { text: l.display }), el("td", { class: "muted" }, el("code", { text: l.source.field }))))));
        wrap.append(el("div", { class: "scroll-x" }, tbl));
      }
      if (x.patient_owes_estimate !== null) {
        wrap.append(el("div", { class: "card info" }, el("h3", { text: "What you may still owe: " + money(x.patient_owes_estimate) }),
          el("p", { class: "muted", text: "Estimate based on " + x.patient_owes_basis + "." })));
      }
      if (x.flags.length) {
        wrap.append(el("h3", { text: "Things we noticed" }));
        x.flags.forEach((f) => wrap.append(el("div", { class: "card " + ({ problem: "err", warning: "warn", info: "info" }[f.severity] || "") },
          el("div", { class: "row" }, BillUI.severityBadge(f.severity), el("strong", { text: f.title })),
          el("p", { text: f.message }),
          f.ask ? el("p", {}, el("strong", { text: "What you can ask: " }), f.ask) : null,
          el("details", {}, el("summary", { text: "Numbers used" }),
            el("ul", {}, f.fields.map((q) => el("li", {}, el("code", { text: q.field }), " = " + (q.value === null ? "(not entered)" : q.value))))))));
      } else if (x.has_claim) {
        wrap.append(el("div", { class: "card ok" }, el("p", { text: "✔ Nothing looks off with the numbers you entered." })));
      }
      if (x.missing_info.length) {
        wrap.append(el("h3", { text: "Missing information" }));
        wrap.append(el("ul", {}, x.missing_info.map((m) => el("li", {}, el("code", { text: m.field }), " — " + m.why))));
      }
      if (x.llm_note) wrap.append(el("div", { class: "card" }, el("p", { class: "muted", text: x.llm_note.label }), el("p", { text: x.llm_note.text })));
      wrap.append(el("details", {}, el("summary", { text: "Words used on bills" }),
        el("dl", { class: "kv" }, Object.entries(x.glossary).flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]))));
      wrap.append(el("div", { class: "row" }, el("button", { class: "btn", type: "button", onclick: () => close(true), text: "Close" })));
      return wrap;
    }));
  }

  // ---------- matching dialog ----------
  function matchDialog(bill, done) {
    return Portal.get("/api/billing/bills/" + bill.id + "/match-suggestions").then((sugs) => Portal.dialog("Match to an insurance claim", (close) => {
      const wrap = el("div", { class: "grid" });
      if (!sugs.length) wrap.append(el("p", { text: "No likely claims found. Add the EOB from your insurer first (Claims tab)." }));
      sugs.forEach((s) => {
        const c = s.claim;
        const b = el("button", { class: "btn small primary", type: "button", text: "Match", disabled: s.already_matched_elsewhere ? true : null });
        b.addEventListener("click", async () => {
          b.disabled = true;
          try { await Portal.post("/api/billing/bills/" + bill.id + "/match", { claim_id: c.id }); Portal.toast("Matched.", "ok"); close(true); }
          catch (e) { Portal.toast(e.message, "error"); b.disabled = false; }
        });
        wrap.append(el("div", { class: "card" },
          el("div", { class: "row between" }, el("strong", { text: "Claim " + c.claim_number + " — " + c.provider_name }),
            Portal.badge(s.strength === "strong" ? "Strong match (" + s.score + ")" : "Possible (" + s.score + ")", s.strength === "strong" ? "good" : "warn")),
          el("p", { class: "muted", text: Portal.fmtDate(c.service_date) + " · billed " + money(c.billed_amount) + " · your share " + money(c.patient_responsibility) }),
          el("ul", {}, s.reasons.map((r) => el("li", { text: r }))),
          s.already_matched_elsewhere ? el("p", { class: "muted", text: "Already matched to another bill." }) : null, b));
      });
      wrap.append(el("button", { class: "btn", type: "button", onclick: () => close(false), text: "Close" }));
      return wrap;
    })).then((r) => { if (r) done(); });
  }

  // ---------- cards ----------
  function billCard(b, reload) {
    const owing = b.status !== "paid" && b.status !== "void" && Number(b.balance) > 0;
    const dueText = b.due_date ? ("Due " + Portal.fmtDate(b.due_date) + (b.overdue ? " — overdue" : b.days_until_due !== null && b.days_until_due <= 7 ? " — in " + b.days_until_due + " day(s)" : "")) : "No due date";
    const actions = el("div", { class: "row" },
      button("Explain this bill", () => explainDialog(b), "primary"),
      owing ? button("Record payment", () => paymentForm(b, reload)) : null,
      b.match_status === "matched" ? button("Unmatch claim", async () => { await Portal.post("/api/billing/bills/" + b.id + "/unmatch"); reload(); })
        : button("Match to claim", () => matchDialog(b, reload)),
      b.status !== "void" ? button("Dispute", () => disputeForm(b, reload)) : null,
      button("Details", () => detailDialog(b, reload)),
      button("Edit", () => billForm(b, reload)),
      button("Delete", async () => { if (await Portal.confirm("Delete this bill and its payments?", "Yes, delete")) { await Portal.delete("/api/billing/bills/" + b.id); reload(); } }, "danger"));
    return el("section", { class: "card" + (b.overdue ? " warn" : ""), "aria-label": "Bill from " + b.provider_name },
      el("div", { class: "row between" }, el("h3", { text: b.provider_name }), el("div", { class: "row" }, statusBadge(b.status),
        b.match_status === "matched" ? Portal.badge("EOB matched", "good") : Portal.badge("No EOB matched", ""),
        b.claim_status ? Portal.badge("Claim: " + Portal.label(b.claim_status).toLowerCase(), b.claim_status === "denied" ? "bad" : "") : null)),
      kv([["Service", (b.description || "") + (b.description ? " · " : "") + Portal.fmtDate(b.service_date)], ["Billed", money(b.billed_amount)],
        ["Bill asks", money(b.amount_due)], ["Paid so far", Number(b.paid_total) > 0 ? money(b.paid_total) : null],
        ["Balance", money(b.balance)], ["When", dueText], ["Account", b.account_number]]), actions);
  }

  async function detailDialog(b, reload) {
    const d = await Portal.get("/api/billing/bills/" + b.id);
    await Portal.dialog("Bill details", (close) => {
      const wrap = el("div", { class: "grid" });
      wrap.append(el("h3", { text: "Payments" }));
      if (!d.payments.length) wrap.append(el("p", { class: "muted", text: "No payments recorded." }));
      d.payments.forEach((p) => wrap.append(el("div", { class: "row between" },
        el("span", { text: Portal.fmtDate(p.paid_on) + " · " + money(p.amount) + " · " + Portal.label(p.method) + " · " + p.receipt_number }),
        el("span", { class: "row" },
          button("Receipt", async () => BillUI.printReceipt(await Portal.get("/api/billing/payments/" + p.id + "/receipt"))),
          button("Undo", async () => { if (await Portal.confirm("Remove this payment record?", "Remove")) { await Portal.delete("/api/billing/payments/" + p.id); close(true); } }, "danger")))));
      wrap.append(el("h3", { text: "Disputes" }));
      if (!d.disputes.length) wrap.append(el("p", { class: "muted", text: "No disputes." }));
      d.disputes.forEach((x) => wrap.append(el("div", { class: "card" },
        el("div", { class: "row between" }, el("strong", { text: Portal.label(x.reason_code) }), Portal.badge(Portal.label(x.status), x.status === "open" ? "bad" : "")),
        el("p", { text: x.message }),
        x.status === "open" ? el("div", { class: "row" },
          button("Mark resolved", async () => { await Portal.post("/api/billing/disputes/" + x.id + "/close", { outcome: "resolved" }); close(true); }),
          button("Withdraw", async () => { await Portal.post("/api/billing/disputes/" + x.id + "/close", { outcome: "withdrawn" }); close(true); })) : null)));
      wrap.append(el("button", { class: "btn", type: "button", onclick: () => close(false), text: "Close" }));
      return wrap;
    }).then((changed) => { if (changed) reload(); });
  }

  function claimCard(c, reload) {
    return el("section", { class: "card", "aria-label": "Claim " + c.claim_number },
      el("div", { class: "row between" }, el("h3", { text: "Claim " + c.claim_number }), el("div", { class: "row" }, statusBadge(c.status),
        c.matched_bill_id ? Portal.badge("Matched to a bill", "good") : Portal.badge("No bill matched", ""))),
      kv([["Provider", c.provider_name], ["Insurer", c.insurer_name], ["Service", Portal.fmtDate(c.service_date)], ["Billed", money(c.billed_amount)],
        ["Allowed", c.allowed_amount ? money(c.allowed_amount) : null], ["Insurer paid", c.insurer_paid ? money(c.insurer_paid) : null],
        ["Deductible", c.deductible ? money(c.deductible) : null], ["Copay", c.copay ? money(c.copay) : null],
        ["Coinsurance", c.coinsurance ? money(c.coinsurance) : null], ["Your share", c.patient_responsibility ? money(c.patient_responsibility) : null],
        ["Reason denied", c.denial_reason]]),
      el("div", { class: "row" }, button("Edit", () => claimForm(c, reload)),
        button("Delete", async () => { if (await Portal.confirm("Delete this claim?", "Yes, delete")) { await Portal.delete("/api/billing/claims/" + c.id); reload(); } }, "danger")));
  }

  // ---------- section ----------
  Portal.registerSection({
    id: "billing", title: "Bills & claims", icon: "🧾", order: 62,
    async render(container) {
      let tab = "bills";
      async function load() {
        Portal.clear(container);
        let summary, bills, claims, privacy;
        try { [summary, bills, claims, privacy] = await Promise.all([Portal.get("/api/billing/summary"), Portal.get("/api/billing/bills"),
          Portal.get("/api/billing/claims"), Portal.get("/api/billing/privacy")]); }
        catch (e) { container.append(BillUI.errorBox("Couldn't load your bills: " + e.message, load)); return; }
        Portal.setBadge("billing", summary.overdue_count + summary.due_soon_count);

        container.append(el("div", { class: "card " + (summary.overdue_count ? "warn" : "info"), role: "status" },
          el("h3", { text: "You may owe " + money(summary.total_outstanding) }),
          el("p", { text: summary.overdue_count ? summary.overdue_count + " overdue (" + money(summary.overdue_total) + "). " : "Nothing overdue. " +
            (summary.next_due ? "Next due: " + summary.next_due.provider_name + ", " + Portal.fmtDate(summary.next_due.due_date) + "." : "") }),
          el("p", { class: "muted", text: (privacy.shared ? "Shared with " + privacy.shared_with.map((s) => s.provider_name).join(", ") + "." : "Private: only you can see this page.") +
            " Estimates only — not legal or financial advice." })));

        const tabs = el("div", { class: "tabs", role: "tablist", "aria-label": "Billing sections" });
        [["bills", "Bills (" + bills.length + ")"], ["claims", "Claims & EOBs (" + claims.length + ")"], ["privacy", "Privacy"]].forEach(([id, label]) =>
          tabs.append(el("button", { class: "btn", role: "tab", type: "button", "aria-selected": String(tab === id), text: label,
            onclick: () => { tab = id; load(); } })));
        container.append(tabs);

        if (tab === "bills") {
          container.append(el("div", { class: "row" }, button("Add a bill", () => billForm(null, load), "primary"),
            button("Auto-match bills to claims", async () => { const r = await Portal.post("/api/billing/match/auto"); Portal.toast(r.matched.length + " matched, " + r.needs_review.length + " need your review."); load(); }),
            button("Check due dates now", async () => { const r = await Portal.post("/api/billing/reminders/run"); Portal.toast(r.notifications_created + " reminder(s) sent."); })));
          if (!bills.length) container.append(BillUI.emptyBox("No bills yet. Add one to see what you owe and have it explained."));
          bills.forEach((b) => container.append(billCard(b, load)));
        } else if (tab === "claims") {
          container.append(el("div", { class: "row" }, button("Add a claim / EOB", () => claimForm(null, load), "primary")));
          if (!claims.length) container.append(BillUI.emptyBox("No claims yet. Add the numbers from your insurer's EOB letter."));
          claims.forEach((c) => container.append(claimCard(c, load)));
        } else {
          container.append(el("div", { class: "card" }, el("h3", { text: "Who can see your billing?" }),
            el("p", { text: privacy.shared ? "You have shared billing with: " + privacy.shared_with.map((s) => s.provider_name + " (until " + Portal.fmtDate(s.expires_at) + ")").join("; ") + "." :
              "Nobody. Hospital staff cannot see your bills, claims or insurance unless you share the “billing” category with them." }),
            el("p", { class: "muted", text: privacy.how_to_share }), el("a", { class: "btn", href: "#/sharing", text: "Open Sharing" })));
        }
        container.append(BillUI.disclaimer());
      }
      await load();
    },
  });
})();

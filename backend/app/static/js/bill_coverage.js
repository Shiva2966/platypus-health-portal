/* W8 Insurance plans page (Portal section "coverage"). Multiple plans, primary/secondary, entered vs verified (mock). */
(function () {
  if (!window.Portal || !window.BillUI) return;
  const { el, money, kv, button } = BillUI;

  const PLAN_TYPES = ["ppo", "hmo", "epo", "pos", "hdhp", "medicare", "medicaid", "dental", "vision", "other"]
    .map((v) => ({ value: v, label: v === "other" ? "Other / supplemental" : v.toUpperCase().replace("HDHP", "High-deductible (HDHP)") }));
  const RANKS = [{ value: "primary", label: "Primary (billed first)" }, { value: "secondary", label: "Secondary (billed after primary)" },
    { value: "tertiary", label: "Third" }];
  const RELS = ["self", "spouse", "parent", "child", "other"].map((v) => ({ value: v, label: Portal.label(v) }));
  const MONEY = ["deductible_annual", "out_of_pocket_max", "copay_amount", "coinsurance_pct"];

  const FIELDS = [
    { name: "insurer_name", label: "Insurance company", required: true, maxlength: 200 },
    { name: "plan_name", label: "Plan name", maxlength: 200 },
    { name: "plan_type", label: "Plan type", type: "select", options: PLAN_TYPES, required: true, default: "ppo" },
    { name: "coverage_rank", label: "Coverage order", type: "select", options: RANKS, required: true, default: "primary",
      help: "If you have two plans, the primary pays first." },
    { name: "member_id", label: "Member ID (on your card)", required: true, maxlength: 60 },
    { name: "group_number", label: "Group number", maxlength: 60 },
    { name: "policyholder_name", label: "Policyholder name", required: true, maxlength: 200, help: "The person whose name the plan is under." },
    { name: "policyholder_relationship", label: "Policyholder is", type: "select", options: RELS, required: true, default: "self" },
    { name: "policyholder_dob", label: "Policyholder date of birth", type: "date" },
    { name: "effective_date", label: "Coverage starts", type: "date", required: true },
    { name: "end_date", label: "Coverage ends", type: "date", help: "Leave blank if it has not ended." },
    { name: "deductible_annual", label: "Yearly deductible ($)", help: "Example: 1500.00. Used for cost estimates." },
    { name: "out_of_pocket_max", label: "Out-of-pocket maximum ($)" },
    { name: "copay_amount", label: "Typical visit copay ($)" },
    { name: "coinsurance_pct", label: "Your coinsurance (%)", help: "Example: 20 means you pay 20% after the deductible." },
    { name: "insurer_phone", label: "Insurer phone", type: "tel", maxlength: 50 },
  ];

  function planForm(plan, done) {
    return Portal.openForm({
      title: plan ? "Edit insurance plan" : "Add insurance plan", fields: FIELDS, values: plan || {},
      intro: "Enter what is on your card. We never contact your insurer (this is a demo). You can run a mock check after saving.",
      onSubmit: async (v) => {
        const body = BillUI.body(v, MONEY);
        const saved = plan ? await Portal.put("/api/billing/plans/" + plan.id, body) : await Portal.post("/api/billing/plans", body);
        Portal.toast("Plan saved.", "ok");
        return saved;
      },
    }).then((r) => { if (r) done(); });
  }

  async function uploadCard(plan, side, done) {
    const input = el("input", { type: "file", id: "card-upload-" + side, accept: "image/*,application/pdf", "aria-label": "Choose a photo of the " + side + " of your card" });
    await Portal.dialog("Add card photo (" + side + ")", (close) => {
      const status = el("p", { class: "field-error", role: "alert" });
      const go = el("button", { class: "btn primary", type: "button", text: "Upload" });
      go.addEventListener("click", async () => {
        if (!input.files.length) { status.textContent = "Choose a file first."; return; }
        go.disabled = true; status.textContent = "";
        try {
          const fd = new FormData();
          fd.append("name", (plan.insurer_name + " card " + side).slice(0, 200));
          fd.append("category", "insurance");
          fd.append("file", input.files[0]);
          const doc = await Portal.post("/api/documents", fd);
          await Portal.post("/api/billing/plans/" + plan.id + "/card", { side, document_id: doc.id });
          Portal.toast("Card photo saved in your documents.", "ok");
          close(true);
        } catch (e) { status.textContent = e.message; go.disabled = false; }
      });
      return el("div", {}, el("p", { class: "muted", text: "The photo is stored with your other documents and is private unless you share it." }),
        el("label", { for: input.id, text: "Photo or PDF" }), input, status,
        el("div", { class: "row" }, go, el("button", { class: "btn", type: "button", onclick: () => close(false), text: "Cancel" })));
    }).then((r) => { if (r) done(); });
  }

  function planCard(p, reload) {
    const verified = p.verification_status === "verified";
    const failed = p.verification_status === "failed";
    const badge = Portal.badge(verified ? "✔ Verified (mock check)" : failed ? "✖ Check failed (mock)" : "Entered by you — not verified",
      verified ? "good" : failed ? "bad" : "");
    const card = el("section", { class: "card", "aria-labelledby": "plan-" + p.id },
      el("div", { class: "row between" },
        el("h3", { id: "plan-" + p.id, text: p.insurer_name + (p.plan_name ? " — " + p.plan_name : "") }),
        el("div", { class: "row" }, Portal.badge(Portal.label(p.coverage_rank), "info"), p.is_active ? Portal.badge("Active today", "good") : Portal.badge("Not active today", "warn"), badge)),
      kv([
        ["Member ID", p.member_id], ["Group", p.group_number], ["Type", (p.plan_type || "").toUpperCase()],
        ["Policyholder", p.policyholder_name + " (" + Portal.label(p.policyholder_relationship) + ")"],
        ["Covered", Portal.fmtDate(p.effective_date) + " to " + (p.end_date ? Portal.fmtDate(p.end_date) : "no end date")],
        ["Yearly deductible", p.deductible_annual ? money(p.deductible_annual) : null],
        ["Out-of-pocket max", p.out_of_pocket_max ? money(p.out_of_pocket_max) : null],
        ["Visit copay", p.copay_amount ? money(p.copay_amount) : null],
        ["Coinsurance", p.coinsurance_pct ? p.coinsurance_pct + "%" : null], ["Insurer phone", p.insurer_phone],
        ["Last check", p.verification_note],
      ]),
      el("div", { class: "row" },
        button(verified ? "Run mock check again" : "Run mock verification", async () => { await Portal.post("/api/billing/plans/" + p.id + "/verify"); reload(); }),
        button("Edit", () => planForm(p, reload)),
        button(p.card_front_document_id ? "Replace card front" : "Add card front", () => uploadCard(p, "front", reload)),
        button(p.card_back_document_id ? "Replace card back" : "Add card back", () => uploadCard(p, "back", reload)),
        button("Delete", async () => {
          if (await Portal.confirm("Delete " + p.insurer_name + "? Bills and claims stay, but they will no longer point to this plan.", "Yes, delete")) {
            await Portal.delete("/api/billing/plans/" + p.id); Portal.toast("Plan deleted."); reload();
          }
        }, "danger")),
      (p.card_front_document_id || p.card_back_document_id)
        ? el("p", { class: "muted" }, "Card photos are saved in ", el("a", { href: "#/documents", text: "Documents" }), ".") : null);
    return card;
  }

  Portal.registerSection({
    id: "coverage", title: "Insurance plans", icon: "🛡", order: 60,
    async render(container) {
      async function load() {
        Portal.clear(container);
        container.append(el("p", { class: "muted", text: "Your insurance is private to you. Nothing here is shared unless you choose to share the “billing” category." }));
        let plans;
        try { plans = await Portal.get("/api/billing/plans"); }
        catch (e) { container.append(BillUI.errorBox("Couldn't load your plans: " + e.message, load)); return; }
        container.append(el("div", { class: "row" }, button("Add insurance plan", () => planForm(null, load), "primary")));
        if (!plans.length) container.append(BillUI.emptyBox("No insurance plans yet. Add one to get better cost estimates and bill checks."));
        plans.forEach((p) => container.append(planCard(p, load)));
        container.append(BillUI.disclaimer());
      }
      await load();
    },
  });
})();

/* W8 shared helpers for the billing / insurance pages. Loaded before bill_coverage.js, bill_bills.js, bill_search.js.
 * No innerHTML anywhere: all text goes through Portal.el (textContent). Money arrives from the API as strings. */
(function () {
  if (!window.Portal) { console.error("bill_common.js: Portal missing"); return; }
  const el = Portal.el;

  const BillUI = (window.BillUI = {});
  BillUI.el = el;

  // "12.5" -> "$12.50" (display only; the API keeps exact strings)
  BillUI.money = (s) => (s === null || s === undefined || s === "" ? "—" : Number(s).toLocaleString(undefined, { style: "currency", currency: "USD" }));
  BillUI.cleanMoney = (v) => (v === null || v === undefined ? null : String(v).replace(/[$,\s]/g, "") || null);
  BillUI.today = () => new Date().toISOString().slice(0, 10);

  BillUI.errorBox = (msg, retry) =>
    el("div", { class: "card err", role: "alert" }, el("p", { text: msg }),
      retry ? el("button", { class: "btn", onclick: retry, text: "Try again" }) : null);

  BillUI.emptyBox = (msg, ...kids) => el("div", { class: "card empty" }, el("p", { text: msg }), ...kids);

  const STATUS = {
    unpaid: ["Unpaid", "warn"], partially_paid: ["Partly paid", "info"], paid: ["Paid", "good"],
    in_dispute: ["In dispute", "bad"], void: ["Void", ""],
    submitted: ["Sent to insurer", "info"], processing: ["Processing", "warn"], partially_denied: ["Partly denied", "warn"],
    denied: ["Denied", "bad"], appealed: ["Appealed", "info"],
  };
  // badges always include text (never colour alone)
  BillUI.statusBadge = (s) => { const [t, k] = STATUS[s] || [Portal.label(s), ""]; return Portal.badge(t, k); };

  BillUI.severityBadge = (sev) => {
    const map = { problem: ["Needs attention", "bad"], warning: ["Double-check", "warn"], info: ["Good to know", "info"] };
    const [t, k] = map[sev] || [sev, ""];
    return Portal.badge(t, k);
  };

  BillUI.kv = (pairs) => {
    const dl = el("dl", { class: "kv" });
    pairs.filter((p) => p && p[1] !== null && p[1] !== undefined && p[1] !== "").forEach(([k, v]) => dl.append(el("dt", { text: k }), el("dd", {}, v)));
    return dl;
  };

  // wraps an async button handler: disables while running, reports errors as toasts
  BillUI.run = (btn, fn) => async () => {
    btn.disabled = true;
    try { await fn(); } catch (e) { Portal.toast(e.message, "error"); } finally { btn.disabled = false; }
  };
  BillUI.button = (text, onclick, cls) => {
    const b = el("button", { class: "btn small " + (cls || ""), type: "button", text });
    b.addEventListener("click", BillUI.run(b, onclick));
    return b;
  };

  // Send a money-ish form object to the API: strip $ and commas, drop empty strings
  BillUI.body = (values, moneyKeys) => {
    const o = {};
    for (const [k, v] of Object.entries(values)) {
      if (v === null || v === "" || v === undefined) { o[k] = null; continue; }
      o[k] = (moneyKeys || []).includes(k) ? BillUI.cleanMoney(v) : v;
    }
    return o;
  };

  BillUI.disclaimer = () =>
    el("p", { class: "muted", role: "note", text: "Estimate and explanation only. This is not legal or financial advice. Demo data." });

  BillUI.printReceipt = (r) =>
    Portal.dialog("Payment receipt", (close) => {
      const body = el("div", { id: "receipt-print" },
        el("p", { class: "muted", text: "Receipt number " + r.receipt_number }),
        BillUI.kv([
          ["Paid on", Portal.fmtDate(r.paid_on)], ["Amount", BillUI.money(r.amount)], ["Method", Portal.label(r.method)],
          ["Confirmation", r.confirmation_number], ["Patient", r.patient_name], ["Provider", r.provider_name],
          ["Account", r.account_number], ["Service date", Portal.fmtDate(r.service_date)], ["Service", r.description],
          ["Bill amount", BillUI.money(r.bill_amount_due)], ["Balance after this payment", BillUI.money(r.balance_after_payment)],
          ["Note", r.note],
        ]),
        el("p", { class: "muted", text: r.footer }));
      return el("div", {}, body, el("div", { class: "row" },
        el("button", { class: "btn primary", type: "button", onclick: () => window.print(), text: "Print" }),
        el("button", { class: "btn", type: "button", onclick: () => close(true), text: "Close" })));
    });
})();

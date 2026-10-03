/* Sharing & consent section (W2): pending requests, active grants, share code / QR, access history. */
(function () {
  const P = window.Portal;
  if (!P || !window.DocsUI) return;
  const el = P.el;
  const U = window.DocsUI;

  const ACTION_TEXT = {
    document_viewed: "viewed a document", document_access_denied: "tried to open a document (blocked)",
    access_requested: "requested access", access_approved: "access request approved", access_denied: "access request declined",
    share_granted: "you shared records", share_revoked: "you revoked access", share_token_created: "you created a share code",
    record_viewed: "viewed your health data", record_access_denied: "tried to open health data (blocked)",
    share_token_redeemed: "used a share code", share_token_revoked: "you cancelled a share code", share_token_rejected: "tried an invalid/used share code",
  };

  function scopeText(g) {
    if (g.scope_type === "document") return g.document_name || "one document";
    if (g.is_record) return "Health data: " + (g.category_label || U.catLabel(g.category));
    return "All " + U.catLabel(g.category).toLowerCase() + " documents" + (g.include_private ? " (incl. private)" : "");
  }
  let fieldSeq = 0;
  function field(label, input, help) {
    if (!input.id) input.id = "share-f" + ++fieldSeq;
    const hint = help ? el("p", { class: "help", id: input.id + "-help", text: help }) : null;
    if (hint) input.setAttribute("aria-describedby", hint.id);
    return el("div", {}, el("label", { for: input.id, text: label }), input, hint);
  }
  function qrImg(svg) {
    return el("img", { src: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg), alt: "QR code for your share code", width: 240, height: 240,
      style: "background:#fff;padding:8px;border-radius:8px;max-width:100%;height:auto" });
  }

  async function refreshBadge() {
    try { const r = await P.get("/api/sharing/requests?status=pending", null, { noAuthRedirect: true }); P.setBadge("sharing", r.requests.length || 0); } catch (e) { /* signed out */ }
  }

  // ----- approve dialog (with exact preview) -----
  async function reviewRequest(req, reload) {
    const pv = await P.get("/api/sharing/requests/" + encodeURIComponent(req.id) + "/preview");
    return P.dialog("Access request from " + req.provider_name, (close) => {
      const cats = req.categories.map((c) => {
        // billing is private by default: never pre-ticked, the patient must opt in knowingly
        const cb = el("input", { type: "checkbox", checked: c !== "billing", value: c });
        return { c, cb, node: el("label", { class: "check" }, cb, U.catLabel(c) + (c === "billing" ? " - private by default, tick only if needed" : "")) };
      });
      const days = el("input", { type: "number", min: 1, max: 365, value: req.duration_days });
      const status = el("p", { class: "field-error", role: "alert" });
      const ul = el("ul", { class: "list" });
      if (!pv.documents.length) ul.append(el("li", { class: "muted", text: "You have no documents in these categories yet." }));
      pv.documents.forEach((d) => ul.append(el("li", { class: "card" }, el("strong", { text: d.name }), " ", P.badge(U.catLabel(d.category)), " ", d.is_private ? P.badge("private", "warn") : null)));
      return el("div", {},
        el("p", {}, el("strong", { text: req.provider_name }), req.staff_name ? " (" + req.staff_name + ")" : "", " would like to see your records."),
        req.purpose ? el("p", { text: "Reason: " + req.purpose }) : null,
        cats.length ? el("div", {}, el("p", { class: "muted", text: "Choose which categories to allow:" }), cats.map((x) => x.node)) : null,
        U.recordLines(pv.records),
        el("h4", { text: "Documents currently in this request" }), ul,
        field("Allow access for (days)", days),
        el("p", { class: "card info", text: U.RETENTION }), status,
        el("div", { class: "row" },
          el("button", { class: "btn primary", text: "Approve", onclick: async (ev) => {
            ev.currentTarget.disabled = true; status.textContent = "";
            try {
              const body = { duration_days: Number(days.value) || req.duration_days, include_private: true };
              if (cats.length) body.categories = cats.filter((x) => x.cb.checked).map((x) => x.c);
              await P.post("/api/sharing/requests/" + encodeURIComponent(req.id) + "/approve", body);
              P.toast("Access approved.", "ok"); close(true); reload();
            } catch (e) { status.textContent = e.message; ev.currentTarget.disabled = false; }
          } }),
          el("button", { class: "btn danger", text: "Decline", onclick: async () => {
            try { await P.post("/api/sharing/requests/" + encodeURIComponent(req.id) + "/deny", {}); P.toast("Request declined.", "ok"); close(true); reload(); } catch (e) { status.textContent = e.message; }
          } }),
          el("button", { class: "btn", text: "Decide later", onclick: () => close() })));
    });
  }

  // ----- choose what to share (docs/categories) -----
  async function choose(title, cta, run) {
    const docs = (await P.get("/api/documents")).documents;
    const catList = await P.get("/api/sharing/categories");
    return P.dialog(title, (close) => {
      const mk = (list) => list.map((o) => ({ c: o.value, label: o.label, cb: el("input", { type: "checkbox", value: o.value }) }));
      const recs = mk(catList.records), dcats = mk(catList.documents);
      const cats = recs.concat(dcats);
      const dcs = docs.map((d) => ({ d, cb: el("input", { type: "checkbox", value: d.id }) }));
      const status = el("p", { class: "field-error", role: "alert" });
      const box = (x) => el("label", { class: "check" }, x.cb, x.label + (x.c === "billing" ? " (private by default)" : ""));
      return el("div", {},
        el("h4", { text: "Health data" }), el("p", { class: "muted", text: "Everything in the category, including items added later, while the share lasts." }),
        el("div", { class: "grid two" }, recs.map(box)),
        el("h4", { text: "Documents by category" }), el("div", { class: "grid two" }, dcats.map(box)),
        el("h4", { text: "Individual documents" }), dcs.length ? dcs.map((x) => el("label", { class: "check" }, x.cb, x.d.name + " (" + U.catLabel(x.d.category) + ")")) : el("p", { class: "muted", text: "No documents yet." }),
        status,
        el("div", { class: "row" },
          el("button", { class: "btn primary", text: cta, onclick: () => {
            const c = cats.filter((x) => x.cb.checked).map((x) => x.c), d = dcs.filter((x) => x.cb.checked).map((x) => x.d.id);
            if (!c.length && !d.length) { status.textContent = "Choose at least one category or document."; return; }
            close(true); run(d, c);
          } }),
          el("button", { class: "btn", text: "Cancel", onclick: () => close() })));
    });
  }

  // ----- share code / QR -----
  function createCode(documentIds, categories, reload) {
    return P.dialog("Create share code", (close) => {
      const body = el("div");
      const mins = el("select"); [["15", "15 minutes"], ["60", "1 hour"], ["1440", "24 hours"], ["10080", "7 days"]].forEach(([v, l]) => mins.append(el("option", { value: v, text: l })));
      mins.value = "60";
      const uses = el("select"); [["1", "Once (recommended)"], ["3", "Up to 3 times"], ["10", "Up to 10 times"]].forEach(([v, l]) => uses.append(el("option", { value: v, text: l })));
      const gd = el("select"); [["1", "24 hours"], ["3", "3 days"], ["7", "7 days"], ["30", "30 days"]].forEach(([v, l]) => gd.append(el("option", { value: v, text: l })));
      gd.value = "3";
      const purpose = el("input", { type: "text", maxlength: 300, placeholder: "Optional reason" });
      const status = el("p", { class: "field-error", role: "alert" });
      body.append(
        el("p", { class: "muted", text: "A share code lets one clinic see exactly what you choose. The code is shown once - it can't be looked up later." }),
        field("Code can be used for", mins), field("How many times", uses), field("Access lasts after use", gd), field("Reason (optional)", purpose), status,
        el("div", { class: "row" },
          el("button", { class: "btn primary", text: "Preview what this shares", onclick: async () => {
            status.textContent = "";
            try {
              const pv = await P.post("/api/sharing/preview", { document_ids: documentIds, categories, include_private: false });
              P.clear(body);
              body.append(el("p", { text: "This code will let the clinic see:" }),
                U.recordLines(pv.records) || "",
                el("ul", { class: "list" }, pv.documents.map((d) => el("li", { class: "card", text: d.name + " (" + U.catLabel(d.category) + ")" }))),
                (pv.documents.length || pv.records.length) ? "" : el("p", { class: "card warn", text: "Nothing matches yet." }),
                pv.excluded_private_count ? el("p", { class: "muted", text: pv.excluded_private_count + " private document(s) are left out." }) : "",
                el("p", { class: "card info", text: U.RETENTION }), status,
                el("div", { class: "row" }, el("button", { class: "btn primary", text: "Create code", onclick: async (ev) => {
                  ev.currentTarget.disabled = true;
                  try {
                    const t = await P.post("/api/sharing/tokens", { document_ids: documentIds, categories, purpose: purpose.value.trim() || null,
                      expires_in_minutes: Number(mins.value), max_uses: Number(uses.value), grant_days: Number(gd.value) });
                    P.clear(body);
                    body.append(el("h4", { text: "Show this to the clinic" }), qrImg(t.qr_svg),
                      el("p", { class: "muted", text: "Or type this code:" }),
                      el("code", { style: "display:block;word-break:break-all;font-size:1.05rem;padding:.5rem;border:2px solid var(--line);border-radius:8px", text: t.token }),
                      el("p", { class: "card warn", text: "This is the only time the code is shown. Valid until " + P.fmtDate(t.expires_at) + "." }),
                      el("div", { class: "row" },
                        el("button", { class: "btn", text: "Copy code", onclick: () => navigator.clipboard?.writeText(t.token).then(() => P.toast("Copied.", "ok"), () => P.toast("Couldn't copy.", "error")) }),
                        el("button", { class: "btn primary", text: "Done", onclick: () => { close(); reload(); } })));
                  } catch (e) { status.textContent = e.message; ev.currentTarget.disabled = false; }
                } }), el("button", { class: "btn", text: "Cancel", onclick: () => close() })));
            } catch (e) { status.textContent = e.message; }
          } }),
          el("button", { class: "btn", text: "Cancel", onclick: () => close() })));
      return body;
    });
  }

  P.registerSection({
    id: "sharing", title: "Sharing & consent", icon: "🔐", order: 65,
    async render(container) {
      const reload = () => P.refresh();
      const [ov, hist] = await Promise.all([P.get("/api/sharing/overview"), P.get("/api/sharing/history")]);
      P.setBadge("sharing", ov.pending_requests.length || 0);

      container.append(el("p", { class: "card info", text: "You decide who sees your records. Nothing is shared by default, and every view by a provider is recorded below." }));

      // pending requests
      const pend = el("section", { class: "card", "aria-labelledby": "sh-pend" }, el("h3", { id: "sh-pend", text: "Access requests (" + ov.pending_requests.length + ")" }));
      if (!ov.pending_requests.length) pend.append(el("p", { class: "muted", text: "No pending requests." }));
      ov.pending_requests.forEach((r) => pend.append(el("div", { class: "card warn" },
        el("strong", { text: r.provider_name }), el("div", { text: "Wants: " + (r.categories.map(U.catLabel).join(", ") || "specific documents") + " for " + r.duration_days + " day(s)" }),
        r.purpose ? el("div", { class: "muted", text: "Reason: " + r.purpose }) : null,
        el("div", { class: "muted", text: "Requested " + P.fmtDate(r.created_at) }),
        el("div", { class: "row" }, el("button", { class: "btn primary small", text: "Review & decide", onclick: () => reviewRequest(r, reload).catch((e) => P.toast(e.message, "error")) })))));
      container.append(pend);

      // active grants
      const gs = el("section", { class: "card", "aria-labelledby": "sh-grants" }, el("h3", { id: "sh-grants", text: "Who can see your records" }));
      if (!ov.grants.length) gs.append(el("p", { class: "muted", text: "No one right now." }));
      ov.grants.forEach((g) => gs.append(el("div", { class: "card row between" },
        el("div", {}, el("strong", { text: g.provider_name }), el("div", { text: scopeText(g) }),
          el("div", { class: "muted", text: "Until " + P.fmtDate(g.expires_at) + (g.purpose ? " · " + g.purpose : "") + " · via " + P.label(g.via) })),
        el("button", { class: "btn small danger", text: "Revoke", "aria-label": "Revoke " + g.provider_name + " access to " + scopeText(g), onclick: async () => {
          if (!(await P.confirm("Revoke " + g.provider_name + "'s access to " + scopeText(g) + "? " + ov.retention_note, "Revoke access"))) return;
          try { await P.delete("/api/sharing/grants/" + encodeURIComponent(g.id)); P.toast("Access revoked.", "ok"); reload(); } catch (e) { P.toast(e.message, "error"); }
        } }))));
      gs.append(el("div", { class: "row" },
        el("button", { class: "btn", text: "Share with a provider…", onclick: () => choose("What do you want to share?", "Continue", (d, c) => U.shareFlow({ documentIds: d, categories: c, onDone: reload })).catch((e) => P.toast(e.message, "error")) })),
        el("p", { class: "muted", text: ov.retention_note }));
      container.append(gs);

      // QR / codes
      const cs = el("section", { class: "card", "aria-labelledby": "sh-codes" }, el("h3", { id: "sh-codes", text: "Share code (QR)" }),
        el("p", { class: "muted", text: "Show a one-time QR code at the clinic desk instead of searching for a provider." }),
        el("div", { class: "row" }, el("button", { class: "btn", text: "Create a share code…", onclick: () => choose("What should the code share?", "Continue", (d, c) => createCode(d, c, reload)).catch((e) => P.toast(e.message, "error")) })));
      ov.tokens.filter((t) => t.status === "active").forEach((t) => cs.append(el("div", { class: "card row between" },
        el("div", {}, P.badge("active", "good"), " ", (t.categories.map(U.catLabel).concat(t.document_ids.length ? [t.document_ids.length + " document(s)"] : [])).join(", "),
          el("div", { class: "muted", text: `Used ${t.use_count}/${t.max_uses} · expires ${P.fmtDate(t.expires_at)}` })),
        el("button", { class: "btn small danger", text: "Cancel code", onclick: async () => { try { await P.delete("/api/sharing/tokens/" + encodeURIComponent(t.id)); reload(); } catch (e) { P.toast(e.message, "error"); } } }))));
      container.append(cs);

      // history
      const hs = el("section", { class: "card", "aria-labelledby": "sh-hist" }, el("h3", { id: "sh-hist", text: "Access history" }));
      if (!hist.history.length) hs.append(el("p", { class: "muted", text: "Nothing yet." }));
      else {
        const rows = hist.history.map((h) => el("tr", {},
          el("td", { text: P.fmtDate(h.ts) }),
          el("td", { text: (h.actor_type === "staff" ? (h.provider || "A provider") + " " : "") + (ACTION_TEXT[h.action] || P.label(h.action)) }),
          el("td", { text: h.document || "" })));
        hs.append(el("div", { class: "scroll-x" }, el("table", {}, el("thead", {}, el("tr", {}, el("th", { text: "When" }), el("th", { text: "What happened" }), el("th", { text: "Item" }))), el("tbody", {}, rows))));
      }
      container.append(hs);
    },
  });

  P.on("signin", refreshBadge);
})();

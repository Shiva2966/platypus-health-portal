/* Documents section (W2). Registers via Portal.registerSection. No innerHTML with data anywhere.
 * Exposes window.DocsUI helpers used by docs_sharing.js (shareFlow, previewDialog, CATEGORIES). */
(function () {
  const P = window.Portal;
  if (!P) return;
  const el = P.el;
  const DocsUI = (window.DocsUI = {});

  const CATS = ["lab_result", "imaging", "prescription", "visit_summary", "insurance", "billing", "identification", "vaccination", "referral", "other"];
  DocsUI.CATEGORIES = CATS;
  // Structured record categories (medical data + billing data) that a share can also name.
  const RECORD_LABELS = { history: "Medical history", medications: "Medications", allergies: "Allergies", vaccinations: "Vaccinations & preventive care",
    results: "Test results", billing: "Billing (bills, claims, insurance)" };
  DocsUI.RECORD_LABELS = RECORD_LABELS;
  const catLabel = (c) => RECORD_LABELS[c] || P.label(c);
  DocsUI.catLabel = catLabel;
  // lines listing record categories + item counts (used by every "preview what will be shared" screen)
  DocsUI.recordLines = function (records) {
    if (!records || !records.length) return null;
    return el("div", {}, el("h4", { text: "Health data" }),
      el("ul", { class: "list" }, records.map((r) => el("li", { class: "card" }, el("strong", { text: r.label }), " ",
        r.count === null ? P.badge("couldn't count", "warn")
          : P.badge(r.count + (r.count === 1 ? " item" : " items"), r.count ? "info" : ""),
        r.detail ? el("div", { class: "muted", text: r.detail }) : null))),
      el("p", { class: "muted", text: "Everything in these categories, including items you add later, is visible while the share lasts." }));
  };
  const ACCEPT = "application/pdf,image/jpeg,image/png,image/webp,image/heic,image/heif,text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document,.pdf,.jpg,.jpeg,.png,.webp,.heic,.heif,.txt,.docx";
  const RETENTION = "Revoking stops all future access. Anything a provider already viewed or saved before you revoked cannot be taken back.";
  DocsUI.RETENTION = RETENTION;

  const fmtSize = (n) => (n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB");
  DocsUI.fmtSize = fmtSize;
  const typeName = (m) => ({ "application/pdf": "PDF", "image/jpeg": "JPG photo", "image/png": "PNG image", "image/webp": "WEBP image",
    "image/heic": "HEIC photo", "image/heif": "HEIF photo", "text/plain": "Text", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word (DOCX)" }[m] || m);
  DocsUI.typeName = typeName;

  let fieldSeq = 0;
  function field(label, input, help) {
    if (!input.id) input.id = "docs-f" + ++fieldSeq;
    const hint = help ? el("p", { class: "help", id: input.id + "-help", text: help }) : null;
    if (hint) input.setAttribute("aria-describedby", hint.id);
    return el("div", {}, el("label", { for: input.id, text: label }), input, hint);
  }
  function select(options, value) {
    const s = el("select");
    options.forEach((o) => s.append(el("option", { value: o.value, text: o.label })));
    if (value !== undefined) s.value = value;
    return s;
  }
  const catOptions = (withAll) => (withAll ? [{ value: "", label: "All categories" }] : []).concat(CATS.map((c) => ({ value: c, label: catLabel(c) })));

  // ---------------------------------------------------------------- preview
  DocsUI.previewDialog = function (doc) {
    return P.dialog(doc.name, (close) => {
      const url = "/api/documents/" + encodeURIComponent(doc.id) + "/content";
      const box = el("div", { class: "preview-box" });
      if (doc.mime.startsWith("image/") && doc.mime !== "image/heic" && doc.mime !== "image/heif") {
        box.append(el("img", { src: url, alt: doc.name, style: "max-width:100%;height:auto;border-radius:8px" }));
      } else if (doc.mime === "application/pdf") {
        box.append(el("iframe", { src: url, title: "Preview of " + doc.name, style: "width:100%;height:60vh;border:1px solid var(--line);border-radius:8px", referrerpolicy: "no-referrer" }));
        box.append(el("p", { class: "muted", text: "If the preview is blank on your device, use Download." }));
      } else if (doc.mime === "text/plain") {
        const pre = el("pre", { style: "white-space:pre-wrap;overflow:auto;max-height:55vh;border:1px solid var(--line);padding:.6rem;border-radius:8px", text: "Loading…" });
        fetch(url, { credentials: "same-origin", signal: AbortSignal.timeout ? AbortSignal.timeout(30000) : undefined })
          .then((r) => { if (!r.ok) throw new Error("error " + r.status); return r.text(); })
          .then((t) => (pre.textContent = t))
          .catch((e) => (pre.textContent = "Couldn't load the preview (" + e.message + ")."));
        box.append(pre);
      } else {
        box.append(el("p", { class: "card info", text: "This file type can't be previewed in the browser. Use Download to open it on your device." }));
      }
      return el("div", {}, doc.description ? el("p", { text: doc.description }) : null, box,
        el("div", { class: "row" },
          el("a", { class: "btn", href: "/api/documents/" + encodeURIComponent(doc.id) + "/download", text: "Download" }),
          el("button", { class: "btn primary", onclick: () => close(), text: "Close" })));
    });
  };

  // ---------------------------------------------------------------- upload
  function uploadDialog(file, onDone) {
    const base = (file.name || "Photo").replace(/\.[^.]+$/, "").slice(0, 200) || "Document";
    return P.dialog("Add document", (close) => {
      const name = el("input", { type: "text", maxlength: 200, required: true, "aria-required": "true", value: base });
      const desc = el("textarea", { maxlength: 2000, placeholder: "Optional - e.g. which visit it's from" });
      const cat = select(catOptions(false), "other");
      const priv = el("input", { type: "checkbox", checked: true });
      const status = el("p", { class: "field-error", role: "alert" });
      const submit = el("button", { class: "btn primary", type: "submit", text: "Upload" });
      const form = el("form", { novalidate: true },
        el("p", { class: "muted", text: `${file.name || "Camera photo"} · ${fmtSize(file.size)}. Stored privately in your account; nothing is shared until you choose.` }),
        field("Name (required)", name), field("Description (optional)", desc), field("Category", cat),
        el("label", { class: "check" }, priv, "Keep private (only included in a share when you pick it specifically)"),
        status, el("div", { class: "row" }, submit, el("button", { class: "btn", type: "button", onclick: () => close(), text: "Cancel" })));
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault(); status.textContent = "";
        if (!name.value.trim()) { status.textContent = "Please enter a name for this document."; name.focus(); return; }
        if (file.size > 15 * 1024 * 1024) { status.textContent = "This file is larger than 15 MB."; return; }
        submit.disabled = true; submit.textContent = "Uploading…";
        const fd = new FormData();
        fd.append("file", file, file.name || "photo.jpg"); fd.append("name", name.value.trim());
        if (desc.value.trim()) fd.append("description", desc.value.trim());
        fd.append("category", cat.value); fd.append("is_private", priv.checked ? "true" : "false");
        try { await P.post("/api/documents", fd); P.toast("Document uploaded.", "ok"); close(true); onDone && onDone(); }
        catch (e) { status.textContent = e.message; submit.disabled = false; submit.textContent = "Upload"; }
      });
      return form;
    });
  }

  // ---------------------------------------------------------------- share flow (2 steps with exact preview)
  DocsUI.shareFlow = async function ({ documentIds, categories, title, onDone }) {
    const provs = (await P.get("/api/sharing/providers")).providers;
    if (!provs.length) { P.toast("No providers are available to share with yet.", "error"); return; }
    return P.dialog(title || "Share with a provider", (close) => {
      const body = el("div");
      const state = {};
      const step1 = () => {
        P.clear(body);
        const prov = select(provs.map((p) => ({ value: p.id, label: p.name + (p.specialty ? " – " + p.specialty : "") })));
        const days = select([{ value: "1", label: "24 hours" }, { value: "3", label: "3 days" }, { value: "7", label: "7 days" }, { value: "30", label: "30 days" }, { value: "90", label: "90 days" }, { value: "365", label: "1 year" }], state.days || "7");
        const until = el("input", { type: "date", min: new Date(Date.now() + 864e5).toISOString().slice(0, 10) });
        const purpose = el("input", { type: "text", maxlength: 300, placeholder: "e.g. Referral for knee pain", value: state.purpose || "" });
        const priv = el("input", { type: "checkbox", checked: state.priv !== false });
      const docCats = (categories || []).filter((c) => !RECORD_LABELS[c] || c === "billing");
        if (state.provider) prov.value = state.provider;
        const status = el("p", { class: "field-error", role: "alert" });
        body.append(field("Provider", prov), field("Reason (optional)", purpose),
          field("Share for", days, "Access ends automatically after this time. You can revoke earlier."),
          field("...or until a specific date (optional)", until),
          docCats.length ? el("label", { class: "check" }, priv, "Include documents I marked private in the document categories") : "",
          status,
          el("div", { class: "row" },
            el("button", { class: "btn primary", text: "Review what will be shared", onclick: async () => {
              status.textContent = "";
              Object.assign(state, { provider: prov.value, days: days.value, purpose: purpose.value.trim(), priv: priv.checked, until: until.value });
              try {
                state.preview = await P.post("/api/sharing/preview", { document_ids: documentIds || [], categories: categories || [], include_private: !!(categories && categories.length && priv.checked) });
                step2();
              } catch (e) { status.textContent = e.message; }
            } }),
            el("button", { class: "btn", onclick: () => close(), text: "Cancel" })));
      };
      const step2 = () => {
        P.clear(body);
        const pv = state.preview; const pname = provs.find((p) => p.id === state.provider)?.name || "";
        const status = el("p", { class: "field-error", role: "alert" });
        const until = state.until ? new Date(state.until + "T23:59:00") : null;
        body.append(el("p", {}, "You are about to share the following with ", el("strong", { text: pname }), " until ",
          el("strong", { text: until ? until.toLocaleDateString() : new Date(Date.now() + state.days * 864e5).toLocaleDateString() }), ":"));
        const hasDocScope = (documentIds && documentIds.length) || pv.categories.some((c) => !RECORD_LABELS[c] || c === "billing");
        if (!pv.documents.length && hasDocScope) body.append(el("p", { class: "card warn", text: "No documents match right now. Documents you add later to these categories would be shared automatically." }));
        body.append(DocsUI.recordLines(pv.records) || "");
        const ul = el("ul", { class: "list" });
        pv.documents.forEach((d) => ul.append(el("li", { class: "card" }, el("strong", { text: d.name }), " ", P.badge(catLabel(d.category)), " ", d.is_private ? P.badge("private", "warn") : null,
          el("div", { class: "muted", text: typeName(d.mime) + " · " + fmtSize(d.size) }))));
        if (pv.documents.length) body.append(el("h4", { text: "Documents" }));
        body.append(ul);
        if (pv.categories.length) body.append(el("p", { class: "muted", text: "Category sharing covers: " + pv.categories.map(catLabel).join(", ") + (pv.excluded_private_count ? ` (${pv.excluded_private_count} private document(s) left out)` : "") + "." }));
        body.append(el("p", { class: "card info", text: RETENTION }), status,
          el("div", { class: "row" },
            el("button", { class: "btn primary", text: "Confirm and share", onclick: async (ev) => {
              ev.currentTarget.disabled = true;
              try {
                const payload = { provider_id: state.provider, document_ids: documentIds || [], categories: categories || [], include_private: !!(categories && categories.length && state.priv), purpose: state.purpose || null };
                if (until) payload.expires_at = until.toISOString(); else payload.expires_in_days = Number(state.days);
                await P.post("/api/sharing/grants", payload);
                P.toast("Shared with " + pname + ".", "ok"); close(true); onDone && onDone();
              } catch (e) { status.textContent = e.message; ev.currentTarget.disabled = false; }
            } }),
            el("button", { class: "btn", onclick: step1, text: "Back" }),
            el("button", { class: "btn", onclick: () => close(), text: "Cancel" })));
      };
      step1();
      return body;
    });
  };

  // ---------------------------------------------------------------- per-record options
  async function optionsDialog(doc, reload) {
    return P.dialog("Options: " + doc.name, (close) => {
      const body = el("div");
      const draw = async () => {
        let d;
        try { d = await P.get("/api/documents/" + encodeURIComponent(doc.id)); } catch (e) { close(); reload(); return; }
        P.clear(body);
        const name = el("input", { type: "text", maxlength: 200, value: d.name });
        const desc = el("textarea", { maxlength: 2000 }); desc.value = d.description || "";
        const cat = select(catOptions(false), d.category);
        const priv = el("input", { type: "checkbox", checked: d.is_private });
        const status = el("p", { class: "field-error", role: "alert" });
        body.append(
          el("h4", { text: "Details" }), field("Name", name), field("Description (optional)", desc), field("Category", cat),
          el("label", { class: "check" }, priv, "Private (left out of category-wide shares)"), status,
          el("div", { class: "row" }, el("button", { class: "btn primary", text: "Save changes", onclick: async () => {
            status.textContent = "";
            try { await P.api("PATCH", "/api/documents/" + encodeURIComponent(d.id), { name: name.value, description: desc.value, category: cat.value, is_private: priv.checked }); P.toast("Saved.", "ok"); reload(); draw(); }
            catch (e) { status.textContent = e.message; }
          } })),
          el("h4", { text: "Sharing" }));
        if (!d.shared_with.length) body.append(el("p", { class: "muted", text: "Not shared with anyone." }));
        d.shared_with.forEach((s) => body.append(el("div", { class: "card row between" },
          el("span", {}, el("strong", { text: s.provider_name }), " ", P.badge(s.scope_type === "category" ? "via category" : "this document"), el("br"),
            el("span", { class: "muted", text: "Until " + P.fmtDate(s.expires_at) })),
          el("button", { class: "btn small danger", text: "Stop sharing", onclick: async () => {
            if (!(await P.confirm(`Stop sharing with ${s.provider_name}? ${RETENTION}`, "Stop sharing"))) return;
            try { await P.delete("/api/sharing/grants/" + encodeURIComponent(s.grant_id)); P.toast("Sharing stopped.", "ok"); reload(); draw(); } catch (e) { P.toast(e.message, "error"); }
          } }))));
        body.append(el("div", { class: "row" },
          el("button", { class: "btn", text: "Share with a provider…", onclick: () => DocsUI.shareFlow({ documentIds: [d.id], onDone: () => { reload(); draw(); } }) })),
          el("h4", { text: "File" }),
          el("div", { class: "row" },
            el("button", { class: "btn", text: "Replace file…", onclick: () => {
              const inp = el("input", { type: "file", accept: ACCEPT, hidden: true });
              inp.addEventListener("change", async () => {
                if (!inp.files[0]) return;
                const fd = new FormData(); fd.append("file", inp.files[0]);
                try { await P.api("PUT", "/api/documents/" + encodeURIComponent(d.id) + "/file", fd); P.toast("File replaced.", "ok"); reload(); draw(); } catch (e) { P.toast(e.message, "error"); }
              });
              document.body.append(inp); inp.click(); setTimeout(() => inp.remove(), 60000);
            } }),
            el("button", { class: "btn danger", text: "Delete…", onclick: async () => {
              if (!(await P.confirm(`Delete "${d.name}"? It will be removed from your account and any sharing will stop.`, "Delete"))) return;
              try { await P.delete("/api/documents/" + encodeURIComponent(d.id)); P.toast("Document deleted.", "ok"); close(); reload(); } catch (e) { P.toast(e.message, "error"); }
            } })),
          el("div", { class: "row" }, el("button", { class: "btn", onclick: () => close(), text: "Close" })));
      };
      draw();
      return body;
    });
  }

  // ---------------------------------------------------------------- section
  function fileIcon(mime) {
    const icon = mime === "application/pdf" ? "📄" : (mime || "").startsWith("image/") ? "🖼️" : mime === "text/plain" ? "📝" : "📎";
    return el("div", { "aria-hidden": "true", class: "file-icon", text: icon,
      style: "width:64px;height:64px;flex:0 0 64px;display:flex;align-items:center;justify-content:center;font-size:2rem;border-radius:8px;border:1px solid var(--line);background:var(--bg-soft, #f3f6fa)" });
  }
  function docCard(d, reload) {
    const thumb = d.has_thumbnail
      ? el("img", { src: "/api/documents/" + encodeURIComponent(d.id) + "/thumbnail", alt: "", loading: "lazy", width: 64, height: 64,
          style: "object-fit:cover;border-radius:8px;border:1px solid var(--line);flex:0 0 64px", onerror: (e) => e.target.replaceWith(fileIcon(d.mime)) })
      : fileIcon(d.mime);
    return el("article", { class: "card", "aria-label": d.name },
      el("div", { class: "row", style: "align-items:flex-start;flex-wrap:nowrap" }, thumb,
        el("div", { style: "flex:1;min-width:0" },
          el("h3", { text: d.name, style: "overflow-wrap:anywhere" }),
          el("div", { class: "row" }, P.badge(catLabel(d.category), "info"), P.badge(typeName(d.mime)), el("span", { class: "muted", text: fmtSize(d.size) + " · " + P.fmtDate(d.created_at) }),
            d.is_private ? P.badge("Private", "warn") : null,
            d.shared_with.length ? P.badge("Shared with " + [...new Set(d.shared_with.map((s) => s.provider_name))].join(", "), "good") : P.badge("Not shared")),
          d.description ? el("p", { text: d.description }) : null)),
      el("div", { class: "row" },
        el("button", { class: "btn small", text: "View", onclick: () => DocsUI.previewDialog(d) }),
        el("a", { class: "btn small", href: "/api/documents/" + encodeURIComponent(d.id) + "/download", text: "Download" }),
        el("button", { class: "btn small primary", text: "Options & sharing", onclick: () => optionsDialog(d, reload) })));
  }

  P.registerSection({
    id: "documents", title: "Documents", icon: "📄", order: 60,
    async render(container) {
      const f = { q: "", category: "", mime: "", date_from: "", date_to: "", sort: "newest" };
      const listBox = el("div", { "aria-live": "polite" });
      const q = el("input", { type: "search", placeholder: "Search name or description", "aria-label": "Search documents" });
      const cat = select(catOptions(true), "");
      const mime = select([{ value: "", label: "All types" }, { value: "application/pdf", label: "PDF" }, { value: "image", label: "Photos / images" }, { value: "text/plain", label: "Text" }, { value: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", label: "Word" }], "");
      const from = el("input", { type: "date", "aria-label": "From date" });
      const to = el("input", { type: "date", "aria-label": "To date" });
      const sort = select([{ value: "newest", label: "Newest first" }, { value: "oldest", label: "Oldest first" }, { value: "name", label: "Name A–Z" }, { value: "size", label: "Largest first" }], "newest");
      let timer;
      const load = async () => {
        const qs = new URLSearchParams();
        if (q.value.trim()) qs.set("q", q.value.trim());
        if (cat.value) qs.set("category", cat.value);
        if (mime.value) qs.set("mime", mime.value);
        if (from.value) qs.set("date_from", from.value);
        if (to.value) qs.set("date_to", to.value);
        qs.set("sort", sort.value);
        try {
          const r = await P.get("/api/documents?" + qs);
          P.clear(listBox);
          if (!r.documents.length) listBox.append(el("div", { class: "card empty" },
            el("p", { text: (q.value || cat.value || mime.value || from.value || to.value) ? "No documents match your search." : "You haven't added any documents yet." }),
            el("p", { class: "muted", text: "Add a PDF, a photo of a paper record, or a text/Word file. They stay private until you share them." })));
          r.documents.forEach((d) => listBox.append(docCard(d, load)));
        } catch (e) { P.clear(listBox); listBox.append(el("div", { class: "card err", role: "alert", text: e.message })); }
      };
      [cat, mime, from, to, sort].forEach((x) => x.addEventListener("change", load));
      q.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });

      const pick = (capture) => {
        const inp = el("input", { type: "file", accept: capture ? "image/*" : ACCEPT, capture: capture ? "environment" : null, hidden: true });
        inp.addEventListener("change", () => { const fl = inp.files[0]; inp.remove(); if (fl) uploadDialog(fl, load); });
        document.body.append(inp); inp.click();
      };
      container.append(
        el("p", { class: "card info", text: "Your documents are kept private. Nothing is shared with a hospital or clinic unless you choose to share it." }),
        el("div", { class: "row" },
          el("button", { class: "btn primary", text: "Upload a file", onclick: () => pick(false) }),
          el("button", { class: "btn", text: "Take a photo", onclick: () => pick(true) })),
        el("p", { class: "muted", text: "PDF, JPG, PNG, HEIC/HEIF, WEBP, TXT or DOCX · up to 15 MB each." }),
        el("details", { class: "card" }, el("summary", { text: "Search and filter" }),
          field("Search", q), field("Category", cat), field("File type", mime), field("Added from", from), field("Added to", to), field("Sort by", sort)),
        listBox);
      await load();
    },
  });
})();

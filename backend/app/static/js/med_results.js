/* Patient lab reports use existing owner-only document links. */
(function () {
  const P = window.Portal, M = () => window.MedUI, el = P.el;
  function card(d) {
    const base = "/api/documents/" + encodeURIComponent(d.id);
    return el("article", { class: "card med-item" },
      el("h3", { text: d.name }),
      el("p", { class: "muted", text: "Uploaded " + P.fmtDate(d.created_at) }),
      el("div", { class: "row" },
        el("a", { class: "btn small", href: base + "/content", target: "_blank", rel: "noopener", text: "Open report" }),
        el("a", { class: "btn small", href: base + "/download", text: "Download report" })));
  }
  async function build(host) {
    const list = el("section", { "aria-label": "My uploaded lab reports" });
    let saved = null;
    async function refresh() {
      list.replaceChildren(el("p", { role: "status", text: "Loading your lab reports…" }));
      try {
        const response = await P.get("/api/documents?category=lab_result&sort=newest");
        const docs = response.documents || [];
        if (saved && !docs.some((d) => d.id === saved.id)) docs.unshift(saved);
        list.replaceChildren(el("h3", { text: "My lab reports (" + docs.length + ")" }));
        if (!docs.length) list.append(el("p", { class: "muted", text: "No documents filed as lab reports yet. Reports uploaded in Documents under another category can be found in All documents." }));
        docs.forEach((d) => list.append(card(d)));
      } catch (e) {
        list.replaceChildren(el("p", { class: "field-error", role: "alert", text: "Could not refresh your reports: " + e.message }),
          el("button", { class: "btn small", type: "button", text: "Try again", onclick: refresh }));
        if (saved) list.append(card(saved));
      }
    }
    const input = el("input", { id: "lab-pdf-upload", type: "file", accept: "application/pdf,.pdf" });
    const status = el("p", { role: "status" });
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0]; if (!file) return;
      input.value = "";
      if (file.size > 15 * 1024 * 1024) { status.textContent = "Choose a PDF smaller than 15 MB."; return; }
      input.disabled = true; status.textContent = "Uploading your report…";
      try {
        const data = new FormData();
        data.append("file", file, file.name);
        data.append("name", (file.name || "Lab report").replace(/\.pdf$/i, "").slice(0, 200) || "Lab report");
        data.append("category", "lab_result"); data.append("is_private", "true");
        saved = await P.post("/api/documents", data);
        list.replaceChildren(card(saved));
        status.textContent = "Report uploaded. You can open or download it below.";
        await refresh();
      } catch (e) { status.textContent = e.message || "Upload failed. Please try again."; }
      finally { input.disabled = false; }
    });
    host.append(el("section", { class: "card" },
      el("label", { for: "lab-pdf-upload", text: "Upload lab report (PDF)" }), input, status,
      el("div", { class: "row" },
        el("button", { class: "btn small", type: "button", text: "Refresh reports", onclick: refresh }),
        el("a", { href: "#/documents", class: "btn small", text: "All documents" }))), list,
      el("p", { class: "muted med-note", text: "Your reports stay private until you approve hospital access in Sharing & consent." }));
    await refresh();
  }
  P.registerSection({ id: "med_results", title: "Lab reports", icon: "📄", order: 45, hidden: true,
    render: async (c) => M().page(c, "med_results", async (host) => { const h = el("div"); host.append(h); await build(h); }) });
})();

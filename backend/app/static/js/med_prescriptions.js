/* Prescriptions use the established document upload and sharing APIs. */
(function () {
  const P = window.Portal, el = P.el, M = () => window.MedUI;
  const ui = window.MedUI;
  if (ui && Array.isArray(ui.TABS) && !ui.TABS.some(([id]) => id === "med_prescriptions")) {
    ui.TABS.push(["med_prescriptions", "Prescriptions"]);
  }

  function picker(host, listHost, label, accept, id) {
    const input = el("input", { id, type: "file", accept });
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      input.value = "";
      if (file.size > 15 * 1024 * 1024) { P.toast("Choose a file smaller than 15 MB.", "error"); return; }
      input.disabled = true;
      const status = el("div", { class: "card", role: "status" }, el("p", { text: "Uploading prescription…" }));
      host.prepend(status);
      try {
        const data = new FormData();
        data.append("file", file, file.name);
        data.append("name", (file.name || "Prescription").replace(/\.[^.]+$/, "").slice(0, 200) || "Prescription");
        data.append("category", "prescription");
        data.append("is_private", "true");
        await P.post("/api/documents", data);
        status.remove();
        P.toast("Prescription uploaded. It stays private until you share it.", "ok");
        await loadDocuments(listHost);
      } catch (e) {
        status.setAttribute("role", "alert");
        status.replaceChildren(el("p", { class: "field-error", text: e.message || "Upload failed. Please try again." }),
          el("button", { type: "button", class: "btn small", text: "Dismiss", onclick: () => status.remove() }));
      } finally { input.disabled = false; }
    });
    return el("div", {}, el("label", { for: id, text: label }), input);
  }

  async function loadDocuments(host) {
    host.replaceChildren(el("p", { role: "status", text: "Loading prescriptions…" }));
    try {
      const response = await P.get("/api/documents?category=prescription");
      const docs = response.documents || [];
      host.replaceChildren(el("h3", { text: "My prescriptions (" + docs.length + ")" }));
      if (!docs.length) host.append(el("p", { class: "muted", text: "No prescriptions uploaded yet. Choose an image or PDF above." }));
      docs.forEach((doc) => host.append(el("article", { class: "card med-item" },
        el("h3", { text: doc.name }),
        doc.description ? el("p", { class: "muted", text: doc.description }) : null,
        el("p", { class: "muted med-note", text: "Uploaded " + P.fmtDate(doc.created_at) }),
        el("a", { class: "btn small", href: "/api/documents/" + encodeURIComponent(doc.id) + "/content",
          target: "_blank", rel: "noopener", text: "Open prescription" }))));
    } catch (e) {
      host.replaceChildren(el("p", { role: "alert", class: "field-error", text: "Could not load prescriptions: " + e.message }),
        el("button", { class: "btn small", type: "button", text: "Try again", onclick: () => loadDocuments(host) }));
    }
  }

  async function build(host) {
    const list = el("section", { "aria-label": "Uploaded prescriptions" });
    host.append(el("section", { class: "card", "aria-labelledby": "rx-upload" },
      el("h3", { id: "rx-upload", text: "Upload a prescription" }),
      el("p", { class: "muted", text: "Choose a photo from your device or upload a PDF. JPG, PNG, WEBP, HEIC and HEIF images are supported. Maximum 15 MB per file." }),
      picker(host, list, "Upload image", "image/jpeg,image/png,image/webp,image/heic,image/heif,.jpg,.jpeg,.jpe,.jfif,.png,.webp,.heic,.heif", "rx-image"),
      picker(host, list, "Upload PDF", "application/pdf,.pdf", "rx-pdf"),
      el("p", { class: "muted med-note", text: "Prescriptions stay private until you approve hospital access in Sharing & consent." })), list);
    await loadDocuments(list);
  }

  P.registerSection({ id: "med_prescriptions", title: "Prescriptions", icon: "📋", order: 46, hidden: true,
    render: async (c) => M().page(c, "med_prescriptions", async (host) => { const h = el("div"); host.append(h); await build(h); }) });
})();

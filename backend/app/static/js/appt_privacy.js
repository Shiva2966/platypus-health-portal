/* W9 - Privacy & settings (patient). Section id "privacy" (order 900): sharing defaults, communication permissions,
 * accessibility (text size / high contrast / reduced motion - no language setting), connected apps, export, deletion request.
 * Also applies the saved accessibility prefs right after sign-in. Uses textContent only. */
(function () {
  const P = window.Portal;
  if (!P) return;
  const el = P.el;
  const API = "/api/settings";

  // ---- apply accessibility prefs (Portal.prefs handles text size + contrast; reduced motion is ours) ----
  function applyMotion(on) {
    document.documentElement.classList.toggle("reduce-motion", !!on);
    let s = document.getElementById("reduce-motion-style");
    if (on && !s) {
      s = document.createElement("style"); s.id = "reduce-motion-style";
      s.textContent = "html.reduce-motion *, html.reduce-motion *::before, html.reduce-motion *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }";
      document.head.append(s);
    }
    if (!on && s) s.remove();
  }
  function applyPrefs(a) {
    const size = a.text_size || "normal";
    if (P.prefs && P.prefs.set) P.prefs.set({ ...P.prefs.get(), size, contrast: !!a.high_contrast });
    else { document.documentElement.dataset.size = size; document.documentElement.classList.toggle("hc", !!a.high_contrast); }
    try { localStorage.setItem("hp_motion", a.reduced_motion ? "1" : "0"); } catch (e) { /* ignore */ }
    applyMotion(a.reduced_motion);
  }
  try { applyMotion(localStorage.getItem("hp_motion") === "1"); } catch (e) { /* ignore */ }
  P.on("signin", async () => { try { applyPrefs(await P.get(API + "/accessibility")); } catch (e) { /* use local prefs */ } });

  function toggleRow(label, help, checked, onChange) {
    const id = "tg-" + Math.random().toString(36).slice(2, 8);
    const input = el("input", { type: "checkbox", id }); input.checked = !!checked;
    input.addEventListener("change", async () => { input.disabled = true; try { await onChange(input.checked); } catch (e) { input.checked = !input.checked; P.toast(e.message, "error"); } input.disabled = false; });
    return el("div", {}, el("label", { class: "check", for: id }, input, " " + label), help ? el("p", { class: "help", text: help }) : null);
  }
  const put = (path, body) => P.put(API + path, body);

  async function render(c) {
    const [pv, acc, apps, del] = await Promise.all([P.get(API + "/privacy"), P.get(API + "/accessibility"), P.get(API + "/connected-apps"), P.get(API + "/deletion-request")]);
    const s = pv.settings;

    // ---- accessibility
    const size = el("select", { id: "acc-size", "aria-label": "Text size" },
      [["normal", "Normal"], ["large", "Large"], ["xlarge", "Extra large"]].map(([v, t]) => el("option", { value: v, text: t })));
    size.value = acc.text_size;
    size.addEventListener("change", async () => { try { const r = await put("/accessibility", { text_size: size.value }); applyPrefs(r); } catch (e) { P.toast(e.message, "error"); } });
    c.append(el("div", { class: "card" }, el("h3", { text: "Accessibility" }), el("label", { for: "acc-size", text: "Text size" }), size,
      toggleRow("High contrast", "Stronger colours and borders.", acc.high_contrast, async (v) => applyPrefs(await put("/accessibility", { high_contrast: v }))),
      toggleRow("Reduce motion", "Turns off animations and transitions.", acc.reduced_motion, async (v) => applyPrefs(await put("/accessibility", { reduced_motion: v }))),
      el("p", { class: "help", text: "These are saved to your account and follow you to other devices." })));

    // ---- sharing defaults + communication
    const days = el("input", { type: "number", min: "1", max: "365", id: "share-days", value: String(s.default_share_days), "aria-describedby": "share-days-help" });
    days.addEventListener("change", async () => { try { await put("/privacy", { default_share_days: Number(days.value) }); P.toast("Saved.", "ok"); } catch (e) { P.toast(e.message, "error"); } });
    const bool = (key, label, help) => toggleRow(label, help, s[key], (v) => put("/privacy", { [key]: v }));
    c.append(el("div", { class: "card" }, el("h3", { text: "Sharing defaults" }), el("p", { class: "muted", text: pv.note }),
      el("label", { for: "share-days", text: "Suggested sharing length (days)" }), days, el("p", { class: "help", id: "share-days-help", text: "Used as the starting suggestion when you share. You can always change it." }),
      bool("share_requires_my_approval", "A clinic must ask and I must approve before they can see anything", "Recommended."),
      bool("notify_on_view", "Tell me when someone views one of my documents", null)));
    c.append(el("div", { class: "card" }, el("h3", { text: "How you want to be contacted" }),
      bool("allow_email", "Email", null), bool("allow_sms", "Text messages", null), bool("allow_phone_calls", "Phone calls", null),
      bool("allow_appointment_reminders", "Appointment reminders", null), bool("allow_marketing", "News and offers (off by default)", null)));

    // ---- connected apps
    const appsBox = el("div", { class: "card" }, el("h3", { text: "Connected apps" }), el("p", { class: "muted", text: apps.note }));
    if (!apps.items.length) appsBox.append(el("p", { class: "empty", text: "No apps are connected." }));
    apps.items.forEach((a) => appsBox.append(el("div", { class: "row between" }, el("span", {}, el("strong", { text: a.name }), " - " + a.scopes.join(", ")),
      el("button", { class: "btn small danger", text: "Disconnect", onclick: async () => { try { await P.delete(`${API}/connected-apps/${a.id}`); P.refresh(); } catch (e) { P.toast(e.message, "error"); } } }))));
    if (apps.available.length) appsBox.append(el("h4", { text: "Available (demo)" }), ...apps.available.map((a) => el("div", { class: "row between" }, el("span", {}, el("strong", { text: a.name }), " - would read: " + a.scopes.join(", ")),
      el("button", { class: "btn small", text: "Connect", onclick: async () => { try { await P.post(API + "/connected-apps", { app_key: a.key }); P.refresh(); } catch (e) { P.toast(e.message, "error"); } } }))));
    c.append(appsBox);

    // ---- export
    c.append(el("div", { class: "card" }, el("h3", { text: "Export all my data" }), el("p", { class: "muted", text: "Download everything in your account: profile, records, appointments, who you shared with, notifications and your activity log. Passwords are never included." }),
      el("div", { class: "row" }, el("a", { class: "btn", href: API + "/export.json", download: "my-health-data.json", text: "Download as JSON" }),
        el("a", { class: "btn", href: API + "/export.zip", download: "my-health-data.zip", text: "Download ZIP (data + my documents)" }))));

    // ---- deletion
    const delBox = el("div", { class: "card" }, el("h3", { text: "Delete my account" }), el("p", { text: del.retention_explanation }));
    if (del.request) {
      delBox.append(el("p", { class: "field-error", text: "Deletion requested on " + P.fmtDate(del.request.requested_at) + ". Earliest completion: " + P.fmtDate(del.request.earliest_completion_at) + "." }),
        el("button", { class: "btn", text: "Cancel my deletion request", onclick: async () => { try { await P.delete(API + "/deletion-request"); P.toast("Deletion request cancelled.", "ok"); P.refresh(); } catch (e) { P.toast(e.message, "error"); } } }));
    } else {
      delBox.append(el("button", { class: "btn danger", text: "Request deletion", onclick: () => P.openForm({
        title: "Request account deletion", submitLabel: "Request deletion", intro: "You will have " + del.cooling_off_days + " days to change your mind. Nothing is erased right away.",
        fields: [{ name: "reason", label: "Why are you leaving?", type: "textarea", maxlength: 1000 }, { name: "confirm", label: "I have read what happens to my data and I want to request deletion", type: "checkbox", required: true }],
        onSubmit: async (v) => { if (!v.confirm) { const e = new Error("Please tick the box to confirm."); e.fields = { confirm: "Please tick the box to confirm." }; throw e; } await P.post(API + "/deletion-request", { reason: v.reason, confirm: true }); P.refresh(); } }) }));
    }
    c.append(delBox);
  }
  P.registerSection({ id: "privacy", title: "Privacy & settings", icon: "🔒", order: 900, render });
})();

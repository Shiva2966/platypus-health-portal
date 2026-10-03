/* Patient notification inbox + bell badge. Registers the "notifications" section via Portal.registerSection.
 * Unread count: polling every 15 s is the reliable baseline (and detects an expired session -> sign-in prompt).
 * The SSE stream is an optional speed-up; after repeated errors it is closed and retried with backoff. */
(function () {
  const P = window.Portal;
  if (!P || typeof P.registerSection !== "function") throw new Error("notif_inbox.js needs shell.js loaded first");
  const el = P.el;
  const AS = "as=patient";
  const POLL_MS = 15000;
  let unread = 0, timer = null, es = null, sseFails = 0, sseRetry = null, bell = null, badgeEl = null;

  const fixLink = (l) => (!l ? null : l.startsWith("#/") ? l : l.startsWith("#") ? "#/" + l.slice(1) : null);

  function setUnread(n) {
    unread = n;
    P.setBadge("notifications", n);
    if (badgeEl) { badgeEl.textContent = n > 99 ? "99+" : String(n); badgeEl.hidden = !n; }
    if (bell) bell.setAttribute("aria-label", n ? `Notifications, ${n} unread` : "Notifications");
  }
  async function refresh() {
    if (!P.user || document.hidden) return;
    // A 401 here shows the "session expired" sign-in screen; network errors show the shell's Retry banner.
    try { const r = await P.get(`/api/notifications/unread-count?${AS}`); setUnread(r.unread); }
    catch (e) { console.warn("unread count not refreshed:", e.message); }
  }
  function openSse() {
    sseRetry = null;
    if (!window.EventSource || !P.user || es) return;
    es = new EventSource(`/api/notifications/stream?${AS}`);
    es.addEventListener("unread", (ev) => {
      sseFails = 0;
      try { setUnread(JSON.parse(ev.data).unread); } catch (e) { console.warn("bad SSE payload", e); }
    });
    es.onerror = () => {
      // The server ends each stream after ~60 s and the browser reconnects; only repeated errors mean trouble.
      sseFails += 1;
      if (sseFails < 3 && es.readyState !== EventSource.CLOSED) return;
      es.close(); es = null;
      const wait = Math.min(600000, 30000 * 2 ** Math.min(sseFails - 1, 5));
      sseRetry = setTimeout(openSse, wait);
    };
  }
  function ensureBell() {
    if (bell && document.body.contains(bell)) return;
    const header = document.querySelector("header.top");
    if (!header) return;
    badgeEl = el("span", { class: "nav-badge", hidden: true, "aria-hidden": "true" });
    bell = el("a", { href: "#/notifications", class: "btn small", "aria-label": "Notifications", style: "position:relative" },
      el("span", { "aria-hidden": "true", text: "🔔" }), " ", badgeEl);
    const who = document.getElementById("who");
    header.insertBefore(bell, who || document.getElementById("logout-btn"));
  }
  function start() {
    stop(); ensureBell(); refresh();
    timer = setInterval(refresh, POLL_MS);
    sseFails = 0; openSse();
  }
  function stop() {
    clearInterval(timer); clearTimeout(sseRetry); sseRetry = null;
    if (es) { es.close(); es = null; }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  P.on("signin", start);
  if (P.user) start();

  async function render(c) {
    let kind = "", only = false, data;
    const list = el("div", { "aria-live": "polite" });
    const kindSel = el("select", { id: "pn-kind", "aria-label": "Show notification type" }, el("option", { value: "", text: "All types" }));
    const onlyCb = el("input", { type: "checkbox", id: "pn-unread" });
    const markAll = el("button", { class: "btn", text: "Mark all as read" });
    const settings = el("button", { class: "btn", text: "Notification settings" });
    c.append(el("div", { class: "row" }, el("label", { class: "check", for: "pn-unread" }, onlyCb, " Unread only"), kindSel, markAll, settings), list);

    async function draw() {
      P.clear(list).append(el("p", { class: "spinner", text: "Loading…" }));
      const q = new URLSearchParams({ as: "patient", limit: "50" });
      if (only) q.set("unread", "true");
      if (kind) q.set("kind", kind);
      try { data = await P.get("/api/notifications?" + q); }
      catch (e) { P.clear(list).append(el("div", { class: "card err", role: "alert" }, el("p", { text: e.message }), el("button", { class: "btn", onclick: draw, text: "Try again" }))); return; }
      const kl = (k) => (data.kind_labels && data.kind_labels[k]) || P.label(k);
      if (kindSel.children.length === 1) data.kinds.forEach((k) => kindSel.append(el("option", { value: k, text: kl(k) })));
      setUnread(data.unread);
      P.clear(list);
      if (!data.items.length) { list.append(el("div", { class: "card" }, el("p", { text: only ? "You're all caught up." : "No notifications yet." }), el("p", { class: "muted", text: "We'll tell you here when a hospital asks for access, views a document, or an appointment or bill needs attention." }))); return; }
      data.items.forEach((n) => {
        const href = fixLink(n.link);
        list.append(el("div", { class: "card", style: n.read ? "" : "border-left:5px solid #0b5cad" },
          el("strong", { text: n.title }), n.read ? null : el("span", { class: "badge", style: "margin-left:.5rem", text: "New" }),
          n.body ? el("p", { text: n.body }) : null,
          el("p", { class: "muted", text: `${kl(n.kind)} · ${P.fmtDate(n.ts)}` }),
          el("div", { class: "row" },
            href ? el("a", { class: "btn small primary", href, text: "Open", onclick: () => mark(n) }) : null,
            n.read ? null : el("button", { class: "btn small", text: "Mark as read", onclick: async () => { await mark(n); draw(); } }))));
      });
    }
    async function mark(n) { try { const r = await P.post(`/api/notifications/${n.id}/read?${AS}`, {}); setUnread(r.unread); } catch (e) { P.toast("Couldn't mark it as read: " + e.message, "error"); } }
    onlyCb.addEventListener("change", () => { only = onlyCb.checked; draw(); });
    kindSel.addEventListener("change", () => { kind = kindSel.value; draw(); });
    markAll.addEventListener("click", async () => { try { await P.post(`/api/notifications/read-all?${AS}`, {}); setUnread(0); P.toast("All notifications marked as read."); draw(); } catch (e) { P.toast(e.message, "error"); } });
    settings.addEventListener("click", prefs);
    await draw();
  }

  async function prefs() {
    let items;
    try { items = (await P.get(`/api/notifications/prefs?${AS}`)).prefs; } catch (e) { P.toast(e.message, "error"); return; }
    P.dialog("Notification settings", (close) => {
      const rows = items.map((p) => { const i = el("input", { type: "checkbox", id: "pp-" + p.kind }); i.checked = p.enabled; return [p, i]; });
      const status = el("p", { class: "field-error", role: "alert" });
      return el("div", {}, el("p", { class: "muted", text: "Choose what you want to hear about. Turned-off types are hidden and no longer counted." }),
        rows.map(([p, i]) => el("label", { class: "check", for: i.id }, i, " " + p.label)), status,
        el("div", { class: "row" },
          el("button", { class: "btn primary", text: "Save", onclick: async () => {
            try { await P.put(`/api/notifications/prefs?${AS}`, { prefs: Object.fromEntries(rows.map(([p, i]) => [p.kind, i.checked])) }); P.toast("Settings saved."); close(true); refresh(); }
            catch (e) { status.textContent = e.message; } } }),
          el("button", { class: "btn", text: "Cancel", onclick: () => close(false) })));
    });
  }

  P.registerSection({ id: "notifications", title: "Notifications", icon: "🔔", order: 90, render });
})();

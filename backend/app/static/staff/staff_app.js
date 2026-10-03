/* Staff portal shell: hash router, role-based navigation, notification bell (SSE + 15s polling), boot. */
(function () {
  const { el } = S;
  const NAV = {
    clinical: [["dashboard", "Dashboard"], ["patients", "Patients & requests"], ["appointments", "Appointments"],
      ["documents", "Shared documents"], ["redeem", "Share code"], ["notifications", "Notifications"]],
    admin: [["audit", "Audit log"], ["users", "Staff accounts"], ["notifications", "Notifications"]],
  };
  const TITLES = { dashboard: "Dashboard", patients: "Find a patient", patient: "Patient", appointments: "Appointments", requests: "Access requests",
    documents: "Shared documents", redeem: "Share code", notifications: "Notifications", audit: "Audit log", users: "Staff accounts",
    corrections: "Correction requests" };
  const isClinician = () => S.me && (S.me.role === "nurse" || S.me.role === "physician");
  const navFor = () => (S.me.role === "admin" ? NAV.admin
    : isClinician() ? NAV.clinical.slice(0, 3).concat([["corrections", "Corrections"]], NAV.clinical.slice(3)) : NAV.clinical);

  function parse() {
    const parts = location.hash.replace(/^#\/?/, "").split("?")[0].split("/").filter(Boolean);
    return { name: parts[0] || (S.me && S.me.role === "admin" ? "audit" : "dashboard"), args: parts.slice(1).map(decodeURIComponent) };
  }
  const allowed = (name) => (S.me.role === "admin" ? ["audit", "users", "notifications"] : ["dashboard", "patients", "patient", "appointments", "requests", "documents", "redeem", "notifications"].concat(isClinician() ? ["corrections"] : [])).includes(name);

  // Browser tabs share the staff cookie; never trust a stale account header.
  let identityCheck = null;
  S.syncIdentity = async function () {
    if (!S.me) return false;
    if (!identityCheck) identityCheck = S.get("/api/staff/me").finally(() => { identityCheck = null; });
    const me = await identityCheck;
    const changed = !S.me || me.id !== S.me.id || me.role !== S.me.role || me.provider_id !== S.me.provider_id;
    S.me = me;
    document.getElementById("who").textContent = `${me.name} · ${S.ROLE[me.role]}`;
    return changed;
  };
  let token = 0;
  S.route = async function () {
    if (!S.me) return;
    try { await S.syncIdentity(); } catch (e) { if (!e.sessionExpired) S.toast(e.message, "error"); return; }
    if (!S.me) return;
    S.clearTimers();
    let { name, args } = parse();
    if (!allowed(name)) { location.hash = S.me.role === "admin" ? "#/audit" : "#/dashboard"; return; }
    buildNav(name);
    const main = S.clear(document.getElementById("main"));
    const my = ++token;
    main.append(el("h2", { class: "sr", text: TITLES[name] || "" }));
    document.title = `${TITLES[name] || "Staff"} – Platypus Hospital Portal${document.documentElement.dataset.demo === "1" ? " (Demo)" : ""}`;
    const body = el("div"); main.append(body);
    try { await S.pages[name](body, args); }
    catch (e) { if (my === token && !body.children.length) body.append(S.errorBox(e, S.route)); console.warn(e); }
    if (my === token) { main.focus({ preventScroll: true }); S.announce(TITLES[name] || ""); }
  };

  function buildNav(cur) {
    const list = S.clear(document.getElementById("nav-list"));
    navFor().forEach(([id, label]) => {
      const a = el("a", { href: "#/" + id, "aria-current": (id === cur || (id === "patients" && cur === "patient")) ? "page" : null }, label,
        id === "notifications" && S.unread ? el("span", { class: "nav-badge", "aria-label": `${S.unread} unread`, text: String(S.unread) }) : null);
      list.append(el("li", {}, a));
    });
  }

  // ---------- bell ----------
  S.unread = 0;
  S.setBell = function (n) {
    const b = document.getElementById("bell-count");
    if (n > S.unread && S.me && S.unread !== null && S._bellReady) { S.toast(`You have ${n - S.unread} new notification(s).`); }
    S.unread = n; S._bellReady = true;
    b.textContent = n > 99 ? "99+" : String(n); b.hidden = !n;
    document.getElementById("bell").setAttribute("aria-label", n ? `Notifications, ${n} unread` : "Notifications");
    if (S.me) buildNav(parse().name);
  };
  // Polling every 15 s is the reliable baseline (and notices an expired session); SSE is an optional speed-up
  // that is dropped after repeated errors and retried with backoff.
  S.refreshBell = async function () {
    if (!S.me || document.hidden) return;
    try { S.setBell((await S.get("/api/notifications/unread-count?as=staff")).unread); }
    catch (e) { console.warn("unread count not refreshed:", e.message); } // banner / sign-in screen already shown by S.api
  };
  let es = null, pollTimer = null, sseFails = 0, sseRetry = null;
  function openSse() {
    sseRetry = null;
    if (!window.EventSource || !S.me || es) return;
    es = new EventSource("/api/notifications/stream?as=staff");
    es.addEventListener("unread", (ev) => {
      sseFails = 0;
      try { S.setBell(JSON.parse(ev.data).unread); } catch (e) { console.warn("bad SSE payload", e); }
    });
    es.onerror = () => {
      sseFails += 1;
      if (sseFails < 3 && es.readyState !== EventSource.CLOSED) return; // normal reconnect after each ~60 s stream
      es.close(); es = null;
      sseRetry = setTimeout(openSse, Math.min(600000, 30000 * 2 ** Math.min(sseFails - 1, 5)));
    };
  }
  function startBell() {
    stopBell(); S._bellReady = false; S.refreshBell();
    pollTimer = setInterval(S.refreshBell, 15000);
    sseFails = 0; openSse();
  }
  function stopBell() { clearInterval(pollTimer); clearTimeout(sseRetry); sseRetry = null; if (es) { es.close(); es = null; } }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { S.syncIdentity().then((changed) => { if (changed) S.route(); }).catch(() => {}); S.refreshBell(); } });

  // ---------- boot ----------
  function enter(me) {
    S.me = me;
    S.clear(document.getElementById("login-view")).hidden = true;
    document.getElementById("app").hidden = false;
    document.getElementById("who").textContent = `${me.name} · ${S.ROLE[me.role]}`;
    startBell();
    if (!location.hash || location.hash === "#") location.hash = me.role === "admin" ? "#/audit" : "#/dashboard";
    S.route();
  }
  S.onSignedOut = (notice) => { if (!S.me && !document.getElementById("app").hidden) leave(notice); };
  function leave(notice) {
    stopBell(); S.clearTimers(); S.me = null; S.unread = 0;
    document.getElementById("app").hidden = true;
    S.showLogin(async () => { try { enter(await S.get("/api/staff/me", null, { noAuthRedirect: true })); } catch (e) { S.toast(e.message, "error"); } }, notice);
  }

  document.getElementById("logout").addEventListener("click", async () => {
    try { await S.post("/api/staff/logout", {}, { noAuthRedirect: true }); }
    catch (e) { S.toast("Signed out on this computer. The server could not be told (" + e.message + ").", "error"); }
    location.hash = ""; leave();
  });
  document.getElementById("global-search").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const v = document.getElementById("gs-input").value.trim();
    if (v.length < 2) { S.toast("Type at least 2 letters to search.", "error"); return; }
    S.pendingSearch = v;
    if (parse().name === "documents") S.route(); else location.hash = "#/documents";
    document.getElementById("gs-input").value = "";
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && S.me) { ev.preventDefault(); document.getElementById("gs-input").focus(); }
  });
  window.addEventListener("hashchange", S.route);

  (async function boot() {
    try { enter(await S.get("/api/staff/me", null, { noAuthRedirect: true })); }
    catch (e) {
      if (e.status === 401) { leave(); return; }
      // Network down or server error: say so with a Retry (never a blank page).
      const v = S.clear(document.getElementById("login-view")); v.hidden = false;
      v.append(S.errorBox(e, boot));
    }
  })();
})();

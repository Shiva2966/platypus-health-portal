/* W1: Dashboard (home). Every widget loads its ONE endpoint independently (tests/test_dashboard_wiring.py checks
 * they exist). A failing widget says so (with the server's message) and offers Try again; the others still load. */
(function () {
  const { el, fmtDate, fmtMoney } = Portal;

  const arr = (r) => (Array.isArray(r) ? r : r && (r.items || r.grants || r.requests || r.appointments || r.results || r.shares)) || [];
  const pick = (o, ...keys) => { for (const k of keys) if (o && o[k] !== undefined && o[k] !== null && o[k] !== "") return o[k]; return ""; };
  const nameOf = (o) => pick(o, "provider_name", "recipient_name", "requester_name", "organization", "provider", "name", "staff_name");
  const strName = (v) => (v && typeof v === "object" ? v.name || "" : v);

  function widget(title, link, linkLabel) {
    const body = el("div", {}, el("p", { class: "spinner", text: "Loading…" }));
    const card = el("section", { class: "card", "aria-label": title }, el("div", { class: "row between" },
      el("h3", { text: title }), link ? el("a", { href: link, text: linkLabel || "See all" }) : null), body);
    return { card, body };
  }

  async function load(w, url, build) {
    const run = async () => {
      Portal.clear(w.body).append(el("p", { class: "spinner", text: "Loading…" }));
      let data;
      try { data = await Portal.get(url); }
      catch (e) {
        if (e.sessionExpired) return; // the shell already shows the sign-in screen
        Portal.clear(w.body).append(el("p", { class: "field-error", role: "alert", text: "Couldn't load this section: " + e.message }),
          el("button", { class: "btn small", text: "Try again", onclick: run }));
        return;
      }
      Portal.clear(w.body).append(build(data));
    };
    return run();
  }

  const empty = (t) => el("p", { class: "muted", text: t });
  const list = (items) => el("ul", { class: "list" }, items.map((i) => el("li", {}, i)));

  Portal.registerSection({
    id: "dashboard", title: "Home", icon: "🏠", order: 10,
    async render(c) {
      const hello = el("h3", { text: "Hello, " + (Portal.user.display_name || "") });
      c.append(hello);

      // ---- widgets ----
      const wGaps = widget("Profile notice");
      const wAppt = widget("Upcoming appointments", "#/appointments", "Schedule an appointment");
      const wBills = widget("Bills to pay", "#/billing");
      const wShare = widget("Who can see my records", "#/sharing");
      const wReq = widget("Access requests waiting for you", "#/sharing");
      const wNote = widget("Notifications", "#/notifications");

      const grid = el("div", { class: "grid two" }, wAppt.card, wShare.card, wNote.card, wBills.card);
      c.append(wGaps.card, grid);
      wShare.card.append(wReq.card);

      load(wGaps, "/api/profile/gaps", (d) => {
        const g = arr(d);
        if (!g.length) return el("p", {}, Portal.badge("All good", "good"), " Your profile looks complete.");
        return list(g.map((x) => el("span", { text: x.message })));
      });

      load(wNote, "/api/notifications/unread-count", (d) => {
        const n = d.count ?? d.unread ?? 0; Portal.setBadge("notifications", n);
        return n ? el("p", {}, Portal.badge(String(n), "bad"), " unread. ", el("a", { href: "#/notifications", text: "Read them" })) : empty("You're all caught up.");
      });

      load(wReq, "/api/access-requests?status=pending", (d) => {
        const r = arr(d).filter((x) => !x.status || x.status === "pending");
        if (!r.length) return empty("No one is asking for access right now.");
        return list(r.map((x) => el("span", {}, el("strong", { text: strName(nameOf(x)) || "A clinic" }), " asked for access",
          x.purpose ? " (" + x.purpose + ")" : "", ". ", el("a", { href: "#/sharing", text: "Review" }))));
      });

      load(wShare, "/api/shares/active", (d) => {
        const s = arr(d);
        if (!s.length) return empty("You are not sharing anything right now. Nothing is shared by default.");
        return list(s.slice(0, 5).map((x) => el("span", {}, el("strong", { text: strName(nameOf(x)) || "Shared" }),
          pick(x, "expires_at", "grant_expires_at") ? " — until " + fmtDate(pick(x, "expires_at", "grant_expires_at")) : " — no end date")));
      });

      load(wAppt, "/api/appt/dashboard-summary", (d) => {
        const a = d.upcoming;
        if (!a.length) return empty("No upcoming appointments.");
        return list(a.slice(0, 5).map((x) => el("span", {}, el("strong", { text: strName(pick(x, "provider_name", "provider")) || "Appointment" }),
          x.for_name ? " (for " + x.for_name + ")" : "", " ",
          Portal.badge(x.status === "rescheduled" ? "New time - please confirm" : Portal.label(x.status), x.status === "booked" ? "good" : x.status === "rescheduled" ? "warn" : "info"), " ",
          pick(x, "scheduled_for", "scheduled_at", "start") ? fmtDate(pick(x, "scheduled_for", "scheduled_at", "start")) : "waiting for the clinic",
          " ", el("a", { href: "#/appointments/" + encodeURIComponent(x.id), text: "Open" }))));
      });

      load(wBills, "/api/billing/summary", (d) => {
        const total = Number(d.total_outstanding || 0), n = d.next_due || (d.items || [])[0];
        if (!total && !n) return empty("No unpaid bills.");
        return el("div", {}, el("p", {}, el("strong", { text: fmtMoney(total) }), " outstanding",
          d.overdue_count ? [" — ", Portal.badge(d.overdue_count + " overdue" + (d.overdue_total ? " (" + fmtMoney(d.overdue_total) + ")" : ""), "bad")] : ""),
          n ? el("p", {}, "Next due: ", el("strong", { text: n.provider_name }), " ", fmtMoney(n.balance ?? n.amount_due), n.due_date ? " by " + fmtDate(n.due_date) : "",
            n.claim_status ? " • insurance claim: " + Portal.label(n.claim_status) : " • no insurance claim matched yet") : null);
      });    },
  });
})();

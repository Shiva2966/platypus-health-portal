/* Patient access history: "who accessed what and when" (GET /api/audit/mine). Section id: access-history. */
(function () {
  const P = window.Portal;
  if (!P || typeof P.registerSection !== "function") return;
  const el = P.el;

  async function render(c) {
    let who = "all", offset = 0;
    const limit = 25;
    const sel = el("select", { id: "ah-who", "aria-label": "Whose activity" },
      [["all", "Everyone"], ["staff", "Hospital staff and the system"], ["me", "Only me"]].map(([v, t]) => el("option", { value: v, text: t })));
    const list = el("div", { "aria-live": "polite" });
    c.append(el("p", { class: "muted", text: "Every time someone looks at your records or you change who can, it's written here. Times are shown in your local time zone." }),
      el("div", { class: "row" }, el("label", { for: "ah-who", text: "Show activity by" }), sel), list);

    async function draw() {
      P.clear(list).append(el("p", { class: "spinner", text: "Loading…" }));
      let r;
      try { r = await P.get(`/api/audit/mine?who=${who}&limit=${limit}&offset=${offset}`); }
      catch (e) { P.clear(list).append(el("div", { class: "card err", role: "alert" }, el("p", { text: e.message }), el("button", { class: "btn", onclick: draw, text: "Try again" }))); return; }
      P.clear(list);
      if (!r.items.length) { list.append(el("div", { class: "card" }, el("p", { text: "Nothing to show yet." }), el("p", { class: "muted", text: "When a hospital opens your records, you'll see who, what and when." }))); return; }
      r.items.forEach((i) => {
        const what = i.document ? ` “${i.document}”` : "";
        list.append(el("div", { class: "card" },
          el("strong", { text: `${i.actor} — ${i.action_label}${what}` }),
          el("p", { class: "muted", text: P.fmtDate(i.ts) + (i.detail && i.detail.expires_at ? ` · until ${P.fmtDate(i.detail.expires_at)}` : "") + (i.detail && i.detail.purpose ? ` · purpose: ${i.detail.purpose}` : "") })));
      });
      list.append(el("div", { class: "row" },
        el("button", { class: "btn", text: "← Newer", disabled: offset === 0 ? true : null, onclick: () => { offset = Math.max(0, offset - limit); draw(); } }),
        el("span", { class: "muted", text: `${offset + 1}–${offset + r.items.length} of ${r.total}` }),
        el("button", { class: "btn", text: "Older →", disabled: offset + limit >= r.total ? true : null, onclick: () => { offset += limit; draw(); } })));
    }
    sel.addEventListener("change", () => { who = sel.value; offset = 0; draw(); });
    await draw();
  }

  P.registerSection({ id: "access-history", title: "Who accessed my records", icon: "👁", order: 92, render });
})();

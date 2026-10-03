/* W8 Cost & provider search (Portal section "care-costs"). All providers, openings and prices are MOCK data and the
 * prices are estimate RANGES with stated uncertainty. */
(function () {
  if (!window.Portal || !window.BillUI) return;
  const { el, money, kv, button } = BillUI;

  function estimateBlock(e) {
    if (!e) return el("p", { class: "muted", text: "Choose a service to see an estimate." });
    const basisText = { self_pay: "No plan chosen: full price range", in_network_plan: "In your plan's network (estimated)", out_of_network_plan: "Not in your plan's network (estimated)" }[e.basis] || "";
    return el("div", { class: "card info", role: "group", "aria-label": "Cost estimate" },
      el("p", {}, el("strong", { text: "ESTIMATE: " }), "you might pay about ", el("strong", { text: money(e.you_pay_low) + " to " + money(e.you_pay_high) }),
        " (could differ by about " + e.uncertainty_pct + "%)."),
      el("p", { class: "muted", text: basisText + ". Total price range " + money(e.total_low) + " to " + money(e.total_high) + ". Confidence: " + e.confidence + "." }),
      el("details", {}, el("summary", { text: "How we estimated this" }), el("ul", {}, e.assumptions.map((a) => el("li", { text: a })))));
  }

  function providerCard(r) {
    const net = r.network ? Portal.badge(r.network === "in_network" ? "In your network" : "Out of network", r.network === "in_network" ? "good" : "warn") : null;
    return el("section", { class: "card", "aria-label": r.name },
      el("div", { class: "row between" }, el("h3", { text: r.name }), el("div", { class: "row" }, net, Portal.badge("Mock listing", ""))),
      kv([["Type", r.kind + " · " + r.specialty], ["Address", r.address],
        ["Distance", r.distance_miles === null ? "Video / online" : r.distance_miles + " miles from " + (document.getElementById("cc-area-label")?.textContent || "you")]]),
      el("div", { class: "row", "aria-label": "Accessibility" }, r.accessibility.map((a) => Portal.badge("♿ " + a.label, "info"))),
      estimateBlock(r.estimate),
      el("p", { class: "muted", text: "Next openings (mock — not real appointments):" }),
      el("ul", {}, r.next_openings.slice(0, 4).map((o) => el("li", { text: o.label }))),
      el("a", { class: "btn small", href: "#/appointments", text: "Go to appointments" }));
  }

  Portal.registerSection({
    id: "care-costs", title: "Find care & costs", icon: "🔎", order: 63,
    async render(container) {
      let meta, plans;
      try { [meta, plans] = await Promise.all([Portal.get("/api/billing/providers/meta"), Portal.get("/api/billing/plans")]); }
      catch (e) { container.append(BillUI.errorBox("Couldn't load search: " + e.message)); return; }

      container.append(el("div", { class: "card warn", role: "note" }, el("strong", { text: "Demo data. " }), meta.label));

      const sel = (name, label, options, value) => {
        const id = "cc-" + name;
        const s = el("select", { id, name }, options.map((o) => el("option", { value: o.value, text: o.label, selected: o.value === value ? true : null })));
        return [el("label", { for: id, text: label }), s, s];
      };
      const [l1, serviceEl] = sel("service", "What do you need?", [{ value: "", label: "Any service" }].concat(meta.services.map((s) => ({ value: s.key, label: s.label }))), "");
      const [l2, areaEl] = sel("area", "Where are you? (mock areas)", meta.areas.map((a) => ({ value: a.key, label: a.label })), meta.default_area);
      const [l3, planEl] = sel("plan_id", "Insurance plan for estimates", [{ value: "", label: "No plan (full price)" }].concat(plans.map((p) => ({ value: p.id, label: p.insurer_name + " (" + p.coverage_rank + ")" }))), plans[0] ? plans[0].id : "");
      const [l4, sortEl] = sel("sort", "Sort by", [{ value: "distance", label: "Closest" }, { value: "cost", label: "Lowest estimated cost" }, { value: "soonest", label: "Soonest opening" }], "distance");
      const dist = el("input", { id: "cc-dist", type: "number", min: "1", max: "200", inputmode: "numeric", placeholder: "Any distance" });
      const from = el("input", { id: "cc-from", type: "date" });
      const to = el("input", { id: "cc-to", type: "date" });
      const inNet = el("input", { id: "cc-innet", type: "checkbox" });
      const access = {};
      const fs = el("fieldset", {}, el("legend", { text: "Accessibility needs (all must be met)" }));
      meta.accessibility.forEach((a) => {
        const cb = el("input", { type: "checkbox", id: "cc-a-" + a.key });
        access[a.key] = cb;
        fs.append(el("label", { class: "check", for: "cc-a-" + a.key }, cb, a.label));
      });
      const results = el("div", { "aria-live": "polite" });
      const areaLabel = el("span", { id: "cc-area-label", class: "sr-only" });

      async function search(ev) {
        if (ev) ev.preventDefault();
        Portal.clear(results);
        results.append(el("p", { class: "spinner", text: "Searching…" }));
        const q = new URLSearchParams();
        if (serviceEl.value) q.set("service", serviceEl.value);
        q.set("area", areaEl.value); q.set("sort", sortEl.value);
        areaLabel.textContent = areaEl.options[areaEl.selectedIndex].text;
        if (planEl.value) q.set("plan_id", planEl.value);
        if (dist.value) q.set("max_distance_miles", dist.value);
        if (from.value) q.set("date_from", from.value);
        if (to.value) q.set("date_to", to.value);
        if (inNet.checked && planEl.value) q.set("in_network_only", "true");
        Object.entries(access).forEach(([k, cb]) => cb.checked && q.append("accessibility", k));
        try {
          const res = await Portal.get("/api/billing/providers/search?" + q.toString());
          Portal.clear(results);
          results.append(el("p", { role: "status", text: res.count + " result(s) near " + res.origin + ", openings " + Portal.fmtDate(res.window.from) + " to " + Portal.fmtDate(res.window.to) + "." }));
          results.append(el("p", { class: "muted", text: res.uncertainty_note }));
          if (!res.count) results.append(BillUI.emptyBox("Nothing matches. Try a wider distance, fewer accessibility filters, or more dates."));
          res.results.forEach((r) => results.append(providerCard(r)));
        } catch (e) { Portal.clear(results); results.append(BillUI.errorBox(e.message, search)); }
      }

      const form = el("form", { class: "card", novalidate: true, onsubmit: search, "aria-label": "Search filters" },
        l1, serviceEl, l2, areaEl, l3, planEl,
        el("label", { for: "cc-dist", text: "Farthest distance (miles)" }), dist,
        el("label", { for: "cc-from", text: "Openings from" }), from, el("label", { for: "cc-to", text: "Openings until" }), to,
        el("label", { class: "check", for: "cc-innet" }, inNet, "Only providers in my plan's network"), fs, l4, sortEl,
        el("div", { class: "row" }, el("button", { class: "btn primary", type: "submit", text: "Search" })), areaLabel);
      container.append(form, results, BillUI.disclaimer());
      search();
    },
  });
})();

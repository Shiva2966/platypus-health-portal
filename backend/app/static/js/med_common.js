/* Medical records UI helpers (W7). Exposes window.MedUI. Loaded before the med_* pages register (sections only use it at render time).
 * No innerHTML with data anywhere (textContent via Portal.el). Synthetic demo data only. No advice, no diagnosis.
 */
(function () {
  const P = window.Portal;
  if (!P) { console.error("med_common.js: Portal missing"); return; }
  const el = P.el;
  const M = (window.MedUI = {});

  // ---------- scoped styles (no changes to shared CSS) ----------
  const css = `
  .med-tabs{display:flex;flex-wrap:wrap;gap:.4rem;margin:0 0 1rem;padding:0;list-style:none}
  .med-tabs a{font:inherit;font-weight:600;min-height:48px;padding:.5rem .9rem;border-radius:var(--radius);border:2px solid var(--primary);color:var(--primary);background:var(--surface);text-decoration:none;display:inline-flex;align-items:center}
  .med-tabs a[aria-current=page]{background:var(--primary);color:var(--primary-text)}
  .med-allergy{display:flex;gap:.75rem;align-items:flex-start;flex-wrap:wrap;border-width:3px}
  .med-allergy h3{margin:0;font-size:1.15rem}
  .med-allergy .big{font-size:1.6rem;line-height:1}
  .med-chips{display:flex;flex-wrap:wrap;gap:.4rem;margin:.4rem 0 0;padding:0;list-style:none}
  .med-chip{border:2px solid currentColor;border-radius:999px;padding:.15rem .7rem;font-weight:700}
  .med-item h3{margin:0 0 .2rem}
  .med-item .actions{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.6rem}
  .med-dl{display:grid;grid-template-columns:max-content 1fr;gap:.15rem .8rem;margin:.3rem 0}
  .med-dl dt{font-weight:600}.med-dl dd{margin:0}
  .med-timeline{list-style:none;margin:0;padding:0 0 0 1rem;border-left:4px solid var(--line)}
  .med-timeline>li{position:relative;margin:0 0 .9rem;padding-left:.4rem}
  .med-timeline>li::before{content:"";position:absolute;left:-1.5rem;top:.7rem;width:.9rem;height:.9rem;border-radius:50%;background:var(--primary);border:2px solid var(--surface)}
  .med-date{font-weight:700}
  .med-flag{display:inline-block;border:2px solid var(--warn);color:var(--warn);background:var(--warn-bg);border-radius:var(--radius);padding:.1rem .5rem;font-weight:700;font-size:.85rem}
  .med-fig{margin:.5rem 0 1rem}.med-fig svg{width:100%;height:auto;display:block;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius)}
  .med-fig .axis{stroke:var(--line);stroke-width:1}.med-fig .gridline{stroke:var(--line);stroke-width:.5;stroke-dasharray:3 3}
  .med-fig text{fill:var(--text);font-size:11px;font-family:inherit}
  .med-fig .band{fill:var(--info-bg);stroke:var(--primary);stroke-width:1;stroke-dasharray:4 3}
  .med-fig .line{fill:none;stroke:var(--primary);stroke-width:2.5}
  .med-fig .pt{fill:var(--primary);stroke:var(--surface);stroke-width:2}
  .med-fig .pt-out{fill:var(--surface);stroke:var(--warn);stroke-width:3}
  .med-fig :focus{outline:3px solid var(--focus);outline-offset:2px}
  .med-legend{font-size:.85rem}
  .med-filter{display:flex;flex-wrap:wrap;gap:.9rem;margin:.4rem 0 1rem}
  .med-filter label{display:inline-flex;align-items:center;gap:.4rem;min-height:44px}
  .med-filter input{width:1.4rem;height:1.4rem}
  .med-note{font-size:.9rem}
  `;
  const st = document.createElement("style"); st.id = "med-css"; st.textContent = css; document.head.append(st);

  // ---------- navigation between the medical pages ----------
  M.TABS = [
    ["med_overview", "Summary"], ["med_history", "History"], ["med_meds", "Medications"],
    ["med_allergies", "Allergies"], ["med_vaccines", "Vaccines & prevention"], ["med_results", "Results"],
    ["med_careteam", "Care team"], ["med_corrections", "Corrections"],
  ];
  M.tabs = (active) => el("nav", { "aria-label": "Medical records sections" },
    el("ul", { class: "med-tabs" }, M.TABS.map(([id, label]) =>
      el("li", {}, el("a", { href: "#/" + id, "aria-current": id === active ? "page" : null, text: label })))));

  M.DISCLAIMER = "This page shows information you or your care team entered. It is not a diagnosis and not medical advice.";

  // ---------- formatting ----------
  M.MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  M.fmtPartial = (s) => {
    if (!s) return "Unknown";
    if (s.length === 4) return s;
    if (s.length === 7) return M.MONTHS[Number(s.slice(5)) - 1] + " " + s.slice(0, 4);
    return P.fmtDate(s);
  };
  M.orUnknown = (v) => (v === null || v === undefined || v === "" ? "Unknown" : v);
  M.num = (v) => (v === null || v === undefined ? "" : String(+Number(v).toFixed(4)));
  M.fmtValue = (r) => (r.value_num === null || r.value_num === undefined
    ? (r.value_text || "—") : (M.num(r.value_num) + (r.unit ? " " + r.unit : "")));

  /* A <dl> of label/value rows; rows whose value is null are skipped unless unknownIfEmpty is set for that row. */
  M.dl = (rows) => el("dl", { class: "med-dl" }, rows.filter(Boolean).map(([k, v, showUnknown]) =>
    (v === null || v === undefined || v === "") && !showUnknown ? null
      : [el("dt", { text: k }), el("dd", { text: v === null || v === undefined || v === "" ? "Unknown" : v })]));

  M.meta = (rec) => el("p", { class: "muted med-note" }, P.sourceBadge(rec), " ",
    rec.source === "clinician_confirmed" && rec.confirmed_at ? "Confirmed " + P.fmtDate(rec.confirmed_at) + " · " : "",
    "Updated " + P.fmtDate(rec.updated_at));

  // ---------- api / errors ----------
  M.api = (path) => P.get("/api/med" + path);
  M.fail = (e) => P.toast(e.message || "Something went wrong.", "error");

  // ---------- option lists ----------
  M.opts = (list, extra) => list.map((v) => ({ value: v, label: (extra && extra[v]) || P.label(v) }));
  M.providerOptions = async () => {
    try {
      const list = await M.api("/providers");
      return [{ value: "", label: "— none —" }].concat(list.map((p) => ({ value: p.id, label: p.name })));
    } catch (e) { return [{ value: "", label: "— none —" }]; }
  };

  // ---------- generic add / edit form ----------
  M.openRecordForm = async function (res, label, fields, rec, reload) {
    const done = await P.openForm({
      title: (rec ? "Edit " : "Add ") + label,
      intro: "Only the starred essentials are needed. Fields marked (optional) can stay blank or be set to Unknown - you can fill them in later.",
      fields, values: rec || undefined, submitLabel: "Save",
      onSubmit: (v) => (rec ? P.put(`/api/med/${res}/${rec.id}`, v) : P.post(`/api/med/${res}`, v)),
    });
    if (done) { P.toast("Saved.", "ok"); await reload(); }
  };

  M.requestCorrection = async function (rec, reload) {
    const done = await P.openForm({
      title: "Request a correction",
      intro: "Tell your care team what looks wrong in \"" + (rec.title || rec.name || rec.test_name || "this entry") +
        "\". Your request is saved and shown to staff who are allowed to see this record. It does not change the entry by itself.",
      fields: [{ name: "message", label: "What should be corrected?", type: "textarea", required: true, maxlength: 2000 }],
      submitLabel: "Send request",
      onSubmit: (v) => P.post("/api/med/corrections", { record_type: rec.record_type, record_id: rec.id, message: v.message }),
    });
    if (done) { P.toast("Correction request saved.", "ok"); if (reload) await reload(); }
  };

  M.deleteRecord = async function (res, rec, reload) {
    const name = rec.title || rec.name || rec.test_name || "this entry";
    if (!(await P.confirm("Remove \"" + name + "\" from your records? This can't be undone.", "Yes, remove"))) return;
    try { await P.delete(`/api/med/${res}/${rec.id}`); P.toast("Removed.", "ok"); await reload(); } catch (e) { M.fail(e); }
  };

  /* Standard action row for a record card. */
  M.actions = (res, rec, fields, reload, label) => el("div", { class: "actions" },
    rec.can_edit
      ? [el("button", { class: "btn small", type: "button", onclick: () => M.openRecordForm(res, label, fields, rec, reload), "aria-label": "Edit " + (rec.title || label), text: "Edit" }),
         el("button", { class: "btn small danger", type: "button", onclick: () => M.deleteRecord(res, rec, reload), "aria-label": "Remove " + (rec.title || label), text: "Remove" })]
      : el("span", { class: "muted med-note", text: "Confirmed by a clinician - to change it, request a correction." }),
    el("button", { class: "btn small", type: "button", onclick: () => M.requestCorrection(rec, reload), "aria-label": "Request a correction for " + (rec.title || label), text: "Request a correction" }));

  // ---------- allergy banner (always visible at the top of every medical page) ----------
  M.allergyBanner = function (summary) {
    const a = summary.allergy;
    let cls = "card info", icon = "❔", head, body;
    if (a.status === "has_allergies") {
      cls = "card err"; icon = "⚠"; head = "Allergies";
      body = el("ul", { class: "med-chips" }, a.items.map((i) =>
        el("li", { class: "med-chip", text: i.substance + (i.severity && i.severity !== "unknown" ? " (" + i.severity + ")" : "") })));
    } else if (a.status === "no_known_allergies") {
      cls = "card ok"; icon = "✔"; head = "No known allergies";
      body = el("p", { class: "muted", text: "You stated that you have no known allergies. Update this any time." });
    } else {
      cls = "card warn"; head = "Allergies: Unknown";
      body = el("p", { class: "muted", text: "Allergy information has not been provided yet. \"Unknown\" is not the same as \"No known allergies\"." });
    }
    return el("section", { class: cls + " med-allergy", "aria-label": "Allergy status" },
      el("span", { class: "big", "aria-hidden": "true", text: icon }),
      el("div", {}, el("h3", { text: head }), body,
        el("a", { class: "btn small", href: "#/med_allergies", text: a.status === "unknown" ? "Add or confirm allergy information" : "View allergies" })));
  };

  /* page(activeTab, build(container, summary)) -> render function body for registerSection */
  M.page = async function (container, active, build) {
    let summary = null;
    try { summary = await M.api("/summary"); } catch (e) { if (e.status === 401) throw e; }
    container.append(M.tabs(active));
    if (summary) container.append(M.allergyBanner(summary));
    await build(container, summary);
    container.append(el("p", { class: "muted med-note", text: M.DISCLAIMER }));
  };

  /* Re-render helper: build(container) is re-run into the same container after a change. */
  M.reloader = (host, build) => async function reload() {
    P.clear(host);
    await build(host);
  };

  // ---------- SVG trend chart ----------
  const NS = "http://www.w3.org/2000/svg";
  const S = (tag, attrs, ...kids) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) n.setAttribute(k, String(v));
    kids.flat().forEach((k) => n.append(k.nodeType ? k : document.createTextNode(String(k))));
    return n;
  };
  const shortDate = (t) => new Date(t).toLocaleDateString(undefined, { month: "short", year: "2-digit" });
  const longDate = (s) => P.fmtDate(s);

  /* series: one entry of GET /api/med/results/trends -> <figure> with an inline SVG line chart.
   * Outside-range points are drawn as hollow diamonds with a heavy outline (shape + outline, not colour alone). */
  M.trendChart = function (s) {
    const W = 640, H = 250, pl = 54, pr = 18, pt = 14, pb = 36;
    const pts = s.points.map((p) => ({ ...p, t: new Date(p.date + "T00:00:00").getTime() }));
    const latest = s.latest;
    const rl = latest.ref_low, rh = latest.ref_high;
    let lo = Math.min(...pts.map((p) => p.value)), hi = Math.max(...pts.map((p) => p.value));
    if (rl !== null && rl !== undefined) lo = Math.min(lo, rl);
    if (rh !== null && rh !== undefined) hi = Math.max(hi, rh);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
    let t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    if (t0 === t1) { t0 -= 86400000 * 15; t1 += 86400000 * 15; }
    const x = (t) => pl + ((t - t0) / (t1 - t0)) * (W - pl - pr);
    const y = (v) => pt + ((hi - v) / (hi - lo)) * (H - pt - pb);
    const fmt = (v) => String(+v.toFixed(2));
    const uid = "trend-" + Math.random().toString(36).slice(2, 8);
    const outside = pts.filter((p) => p.outside_range).length;
    const title = `${s.test_name}${s.unit ? " (" + s.unit + ")" : ""}`;
    const desc = `${pts.length} results from ${longDate(pts[0].date)} to ${longDate(pts[pts.length - 1].date)}. ` +
      `Latest ${fmt(latest.value)}${s.unit ? " " + s.unit : ""}. ` +
      (outside ? `${outside} of ${pts.length} outside the lab's reference range.` : `None outside the lab's reference range.`);

    const svg = S("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-labelledby": uid + "-t " + uid + "-d", preserveAspectRatio: "xMidYMid meet" },
      S("title", { id: uid + "-t" }, title), S("desc", { id: uid + "-d" }, desc));
    // y grid + labels
    for (let i = 0; i <= 4; i++) {
      const v = lo + ((hi - lo) * i) / 4;
      svg.append(S("line", { class: "gridline", x1: pl, x2: W - pr, y1: y(v), y2: y(v) }));
      svg.append(S("text", { x: pl - 6, y: y(v) + 4, "text-anchor": "end" }, fmt(v)));
    }
    // reference range band (the lab's own range for the latest result)
    if ((rl !== null && rl !== undefined) || (rh !== null && rh !== undefined)) {
      const top = y(rh !== null && rh !== undefined ? rh : hi), bot = y(rl !== null && rl !== undefined ? rl : lo);
      svg.append(S("rect", { class: "band", x: pl, y: Math.min(top, bot), width: W - pl - pr, height: Math.abs(bot - top), "aria-hidden": "true" }));
    }
    svg.append(S("line", { class: "axis", x1: pl, x2: pl, y1: pt, y2: H - pb }), S("line", { class: "axis", x1: pl, x2: W - pr, y1: H - pb, y2: H - pb }));
    // x labels
    const nT = Math.min(4, pts.length === 1 ? 1 : 4);
    for (let i = 0; i < nT; i++) {
      const t = nT === 1 ? pts[0].t : t0 + ((t1 - t0) * i) / (nT - 1);
      svg.append(S("text", { x: x(t), y: H - pb + 18, "text-anchor": i === 0 && nT > 1 ? "start" : i === nT - 1 && nT > 1 ? "end" : "middle" }, shortDate(t)));
    }
    if (pts.length > 1) svg.append(S("polyline", { class: "line", points: pts.map((p) => x(p.t) + "," + y(p.value)).join(" ") }));
    pts.forEach((p) => {
      const cx = x(p.t), cy = y(p.value);
      const label = `${longDate(p.date)}: ${fmt(p.value)}${s.unit ? " " + s.unit : ""}` + (p.outside_range ? ", outside the lab's reference range" : "");
      const shape = p.outside_range
        ? S("rect", { class: "pt-out", x: cx - 6, y: cy - 6, width: 12, height: 12, transform: `rotate(45 ${cx} ${cy})`, tabindex: 0, role: "img", "aria-label": label }, S("title", {}, label))
        : S("circle", { class: "pt", cx, cy, r: 5, tabindex: 0, role: "img", "aria-label": label }, S("title", {}, label));
      svg.append(shape);
    });

    const range = (rl !== null && rl !== undefined) || (rh !== null && rh !== undefined)
      ? `Shaded band: the lab's reference range for the latest result (${M.num(rl) || "…"}–${M.num(rh) || "…"}${s.unit ? " " + s.unit : ""}). ` : "No reference range was entered for these results. ";
    return el("figure", { class: "med-fig" }, svg,
      el("figcaption", { class: "muted med-legend" }, "● inside the lab's range   ◆ outside the lab's reference range. ", range,
        "Individual points are listed below the chart."));
  };

  M.clearHost = P.clear;
})();

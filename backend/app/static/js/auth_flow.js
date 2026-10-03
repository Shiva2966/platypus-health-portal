/* Email + password + one-time-code (OTP) sign-in screens, shared by the patient app and the staff portal.
 *
 *   AuthFlow.mount(hostElement, {
 *     api: "/api/auth",              // or "/api/staff/auth"
 *     kind: "patient" | "staff",
 *     title, subtitle,               // heading texts
 *     onDone(user),                  // called after a session exists (cookie already set)
 *     signupFields: ["name","dob"],  // extra sign-up inputs ("dob", "invite_code")
 *     footer: Node | null,           // extra links shown under the card
 *   })
 *
 * Self-contained (no dependency on Portal) and XSS-safe (textContent only). Never stores passwords or codes
 * anywhere except the input boxes. The OTP box uses autocomplete="one-time-code" + inputmode="numeric" so
 * phones can offer the code from the email/notification.
 */
(function () {
  "use strict";

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, "");
      else n.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
  function maskEmail(addr) {
    const s = String(addr || ""); const at = s.lastIndexOf("@");
    if (at < 1) return s.includes("*") ? s : "your email";
    return s.slice(0, 1) + "***" + s.slice(at);
  }
  let uid = 0;
  const nextId = (p) => "ao-" + p + "-" + ++uid;

  // Sending a code can take ~25 s when the mail server is slow (one SMTP retry), so allow 45 s.
  const TIMEOUT_MS = 45000;
  async function request(method, url, body) {
    let res;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
    try {
      res = await fetch(url, { method, credentials: "same-origin", signal: ctl.signal,
        headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      const timedOut = e && e.name === "AbortError";
      throw { network: true, fields: {}, message: timedOut ? "The server took too long to answer. Please try again."
        : "Can't reach the server. Check your connection and try again." };
    } finally { clearTimeout(timer); }
    let data = {};
    try { data = await res.json(); }
    catch (e) { if (res.ok) throw { status: res.status, fields: {}, message: "The server sent an unreadable answer. Please try again." }; }
    if (!res.ok) {
      throw { status: res.status, fields: data.fields || {}, retryAfter: parseInt(res.headers.get("Retry-After") || "0", 10) || 0,
        message: typeof data.detail === "string" ? data.detail : "Something went wrong (error " + res.status + ")." };
    }
    return data;
  }

  // ---------- form building blocks ----------
  function field(label, o) {
    o = o || {};
    const id = nextId("f");
    const input = el("input", Object.assign({ id, name: o.name, type: o.type || "text", "aria-describedby": id + "-hint " + id + "-err",
      "aria-required": o.required === false ? null : "true", autocomplete: o.autocomplete, inputmode: o.inputmode,
      maxlength: o.maxlength || 254, autocapitalize: "off", spellcheck: "false", placeholder: o.placeholder }, o.attrs || {}));
    const hint = el("p", { class: "ao-hint", id: id + "-hint", text: o.hint || "" });
    const err = el("p", { class: "ao-field-error", id: id + "-err", role: "alert" });
    let control = input;
    if (o.type === "password") {
      const toggle = el("button", { type: "button", class: "ao-toggle", "aria-pressed": "false", "aria-controls": id, text: "Show",
        onclick: () => { const show = input.type === "password"; input.type = show ? "text" : "password";
          toggle.textContent = show ? "Hide" : "Show"; toggle.setAttribute("aria-pressed", String(show)); toggle.setAttribute("aria-label", (show ? "Hide " : "Show ") + label.toLowerCase()); } });
      toggle.setAttribute("aria-label", "Show " + label.toLowerCase());
      control = el("div", { class: "ao-pw" }, input, toggle);
    }
    const wrap = el("div", { class: "ao-field" }, el("label", { for: id, text: label }), control, hint, err);
    return { wrap, input, setError(m) { err.textContent = m || ""; input.setAttribute("aria-invalid", m ? "true" : "false"); } };
  }

  function mount(host, opts) {
    const api = opts.api;
    const kind = opts.kind || "patient";
    const signupFields = opts.signupFields || [];
    let cfg = { resend_after: 45, expires_in: 600, trust_device_days: 30, min_password_length: 10 };
    let timer = null;
    // Config only tunes timers/lengths; on failure the defaults above are used and the screens still work.
    const cfgReady = request("GET", api + "/config").then((c) => { cfg = Object.assign(cfg, c); })
      .catch((e) => console.warn("Sign-in config unavailable, using defaults:", e.message));

    function shell(heading, intro, ...body) {
      if (timer) { clearInterval(timer); timer = null; }
      clear(host); host.hidden = false;
      const h = el(opts.headingTag || "h1", { id: "ao-title", tabindex: "-1", text: heading });
      const card = el("div", { class: "ao-card", role: "group", "aria-labelledby": "ao-title" }, h, intro ? el("p", { class: "ao-intro", text: intro }) : null, ...body);
      host.append(el("div", { class: "ao-wrap" + (opts.embedded ? " ao-embedded" : "") },
        opts.embedded ? null : el("p", { class: "ao-brand", text: opts.title || "Sign in" }), card, opts.embedded ? null : (opts.footer || null)));
      return { h, card };
    }
    const banner = (text, kind2) => el("p", { class: "ao-banner " + (kind2 || "info"), role: kind2 === "error" ? "alert" : "status", text });
    const formError = () => el("p", { class: "ao-form-error", role: "alert" });
    function setBusy(btn, busy, label) { btn.disabled = busy; btn.setAttribute("aria-busy", String(busy)); if (label) btn.textContent = busy ? "Please wait…" : label; }
    function showErrors(e, fields, status) {
      const used = new Set();
      for (const [name, f] of Object.entries(fields || {})) if (e.fields && e.fields[name]) { f.setError(e.fields[name]); used.add(name); }
      const unmatched = Object.keys(e.fields || {}).filter((k) => !used.has(k));
      status.textContent = used.size && !unmatched.length ? "Please fix the highlighted fields." : e.message;
      const first = Object.values(fields || {}).find((f) => f.input.getAttribute("aria-invalid") === "true");
      if (first) first.input.focus();
    }
    const clearErrors = (fields, status) => { Object.values(fields).forEach((f) => f.setError("")); status.textContent = ""; };
    function link(text, fn) { return el("button", { type: "button", class: "ao-link", text, onclick: fn }); }

    function devBox(code, input) {
      if (!code) return null;
      return el("div", { class: "ao-dev", role: "note" },
        el("strong", { text: "Development mode: " }), "no email is sent, so here is your code: ",
        el("span", { class: "ao-dev-code", text: code }), " ",
        el("button", { type: "button", class: "ao-link", text: "Fill it in", onclick: () => { if (input) { input.value = code; input.dispatchEvent(new Event("input")); input.focus(); } } }));
    }

    // ---------- screens ----------
    function loginScreen(notice, noticeKind) {
      const f = { email: field("Email address", { name: "email", type: "email", autocomplete: "username", inputmode: "email" }),
        password: field("Password", { name: "password", type: "password", autocomplete: "current-password", maxlength: 128 }) };
      const status = formError();
      const btn = el("button", { class: "ao-btn primary", type: "submit", text: "Sign in" });
      const form = el("form", { novalidate: true }, f.email.wrap, f.password.wrap, status, btn,
        el("div", { class: "ao-links" }, link("Forgot your password?", () => forgotScreen(f.email.input.value)),
          link(kind === "staff" ? "Request a staff account" : "Create an account", () => signupScreen())));
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault(); clearErrors(f, status);
        const email = f.email.input.value.trim();
        if (!email) { f.email.setError("Enter your email address."); f.email.input.focus(); return; }
        if (!f.password.input.value) { f.password.setError("Enter your password."); f.password.input.focus(); return; }
        setBusy(btn, true, "Sign in");
        try {
          const d = await request("POST", api + "/login", { email, password: f.password.input.value });
          f.password.input.value = "";
          if (d.otp_required) otpScreen({ mode: "login", email, challenge: d.otp_challenge_id, hint: d.email_hint, resendAfter: d.resend_after, dev: d.dev_otp });
          else opts.onDone(d);
        } catch (e) { showErrors(e, f, status); setBusy(btn, false, "Sign in"); if (e.status === 429 && e.retryAfter) startLockCountdown(btn, e.retryAfter, "Sign in"); }
      });
      const { h } = shell("Sign in", opts.subtitle, notice ? banner(notice, noticeKind || "ok") : null, form);
      f.email.input.focus();
      return h;
    }

    function startLockCountdown(btn, secs, label) {
      let left = secs; btn.disabled = true;
      if (timer) clearInterval(timer);
      const tick = () => { if (!btn.isConnected || left <= 0) { clearInterval(timer); timer = null; btn.disabled = false; btn.textContent = label; return; }
        btn.textContent = "Try again in " + Math.ceil(left / 60) + " min"; left -= 1; };
      tick(); timer = setInterval(tick, 1000);
    }

    function signupScreen() {
      const f = { name: field("Full name", { name: "name", autocomplete: "name", maxlength: 200 }) };
      if (signupFields.includes("dob")) f.dob = field("Date of birth", { name: "dob", type: "date", autocomplete: "bday", hint: "Used to make sure your records match you." });
      f.email = field("Email address", { name: "email", type: "email", autocomplete: "email", inputmode: "email", hint: "We'll send a 6-digit code to confirm it." });
      f.password = field("Create a password", { name: "password", type: "password", autocomplete: "new-password", maxlength: 128,
        hint: "At least " + cfg.min_password_length + " characters. A few unrelated words works well. Common passwords are not allowed." });
      if (signupFields.includes("invite_code")) f.invite_code = field("Invite code", { name: "invite_code", autocomplete: "off", required: false, hint: "From your hospital administrator." });
      const status = formError();
      const btn = el("button", { class: "ao-btn primary", type: "submit", text: "Create account and send code" });
      const form = el("form", { novalidate: true }, ...Object.values(f).map((x) => x.wrap), status, btn,
        el("div", { class: "ao-links" }, link("I already have an account", () => loginScreen())));
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault(); clearErrors(f, status);
        const body = {}; for (const [k, v] of Object.entries(f)) body[k] = v.input.value.trim();
        body.password = f.password.input.value;
        const missing = { name: "Enter your full name.", email: "Enter your email address.", dob: "Enter your date of birth.", password: "Choose a password." };
        for (const k of Object.keys(missing)) if (f[k] && !body[k]) { f[k].setError(missing[k]); f[k].input.focus(); return; }
        setBusy(btn, true, "Create account and send code");
        try {
          const d = await request("POST", api + "/signup", body);
          if (d.verification_required === false || (d.id && !d.verification_required)) { opts.onDone(d); return; }
          otpScreen({ mode: "signup", email: body.email, resendAfter: d.resend_after, dev: d.dev_otp });
        } catch (e) { showErrors(e, f, status); setBusy(btn, false, "Create account and send code"); }
      });
      shell(kind === "staff" ? "Request a staff account" : "Create your account", "Sign up with your email. You'll confirm it with a 6-digit code.", form);
      f.name.input.focus();
    }

    function notArrivedHint() {
      return el("p", { class: "ao-hint ao-not-arrived", text: "Didn't get it? Emails can take a minute. Check your spam or junk folder, make sure the address is right, then use \"Send a new code\"." });
    }

    function otpScreen(s) {
      const isLogin = s.mode === "login";
      const code = field("6-digit code", { name: "code", autocomplete: "one-time-code", inputmode: "numeric", maxlength: 7,
        attrs: { pattern: "[0-9 ]*", enterkeyhint: "go" }, hint: "Check your inbox (and spam folder) for the newest email." });
      code.input.classList.add("ao-code");
      const trust = el("input", { type: "checkbox", id: nextId("trust") });
      const trustRow = el("label", { class: "ao-check", for: trust.id }, trust, "Don't ask for a code on this device for " + cfg.trust_device_days + " days. Only use this on your own phone or computer.");
      const status = formError();
      const btn = el("button", { class: "ao-btn primary", type: "submit", text: isLogin ? "Sign in" : "Confirm email" });
      const resend = el("button", { type: "button", class: "ao-link ao-resend" });
      const msg = el("p", { class: "ao-sent", role: "status", text: "We sent a 6-digit code to " + maskEmail(s.hint || s.email) + ". It expires in " + Math.round(cfg.expires_in / 60) + " minutes." });
      let challenge = s.challenge; let busy = false;
      const form = el("form", { novalidate: true }, msg, devBox(s.dev, code.input), code.wrap, trustRow, status, btn,
        el("div", { class: "ao-links" }, resend, link("Use a different email", () => (isLogin ? loginScreen() : signupScreen()))),
        notArrivedHint());

      async function submit() {
        if (busy) return;
        const c = code.input.value.replace(/\s+/g, "");
        clearErrors({ code }, status);
        if (!/^\d{6}$/.test(c)) { code.setError("Enter the 6 digits from the email."); code.input.focus(); return; }
        busy = true; setBusy(btn, true, isLogin ? "Sign in" : "Confirm email");
        try {
          const d = isLogin
            ? await request("POST", api + "/verify-login-otp", { otp_challenge_id: challenge, code: c, trust_device: trust.checked })
            : await request("POST", api + "/verify-email", { email: s.email, code: c, trust_device: trust.checked });
          opts.onDone(d);
        } catch (e) {
          busy = false; setBusy(btn, false, isLogin ? "Sign in" : "Confirm email");
          code.setError(e.network ? "" : e.message); if (e.network) status.textContent = e.message;
          code.input.select(); code.input.focus();
        }
      }
      form.addEventListener("submit", (ev) => { ev.preventDefault(); submit(); });
      code.input.addEventListener("input", () => { if (code.input.value.replace(/\s+/g, "").length === 6) submit(); });
      code.input.addEventListener("paste", (ev) => {
        const text = (ev.clipboardData || window.clipboardData).getData("text") || "";
        const m = text.match(/\d[\d\s-]{4,}\d/); const digits = m ? m[0].replace(/\D/g, "") : "";
        if (digits.length === 6) { ev.preventDefault(); code.input.value = digits; submit(); }
      });

      function countdown(secs) {
        let left = secs; if (timer) clearInterval(timer);
        const tick = () => {
          if (!resend.isConnected) { clearInterval(timer); timer = null; return; }
          if (left > 0) { resend.disabled = true; resend.textContent = "Send a new code in " + left + "s"; left -= 1; }
          else { clearInterval(timer); timer = null; resend.disabled = false; resend.textContent = "Send a new code"; }
        };
        tick(); timer = setInterval(tick, 1000);
      }
      resend.addEventListener("click", async () => {
        resend.disabled = true; status.textContent = "";
        try {
          const d = isLogin ? await request("POST", api + "/resend-login-otp", { otp_challenge_id: challenge })
            : await request("POST", api + "/resend-verification", { email: s.email });
          msg.textContent = "New code sent. Use the newest email - older codes no longer work.";
          const old = form.querySelector(".ao-dev"); if (old) old.remove();
          if (d.dev_otp) msg.after(devBox(d.dev_otp, code.input));
          code.input.value = ""; code.input.focus(); countdown(d.resend_after || cfg.resend_after);
        } catch (e) {
          status.textContent = e.message;
          if (isLogin && e.status === 400) { loginScreen(); return; }
          countdown(e.retryAfter || cfg.resend_after);
        }
      });
      shell(isLogin ? "Enter your sign-in code" : "Confirm your email", null, form);
      countdown(s.resendAfter || cfg.resend_after);
      code.input.focus();
    }

    function forgotScreen(prefill) {
      const f = { email: field("Email address", { name: "email", type: "email", autocomplete: "username", inputmode: "email" }) };
      f.email.input.value = prefill || "";
      const status = formError();
      const btn = el("button", { class: "ao-btn primary", type: "submit", text: "Send me a code" });
      const form = el("form", { novalidate: true }, f.email.wrap, status, btn, el("div", { class: "ao-links" }, link("Back to sign in", () => loginScreen())));
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault(); clearErrors(f, status);
        const email = f.email.input.value.trim();
        if (!email) { f.email.setError("Enter your email address."); f.email.input.focus(); return; }
        setBusy(btn, true, "Send me a code");
        try { const d = await request("POST", api + "/forgot-password", { email }); resetScreen({ email, dev: d.dev_otp, resendAfter: d.resend_after }); }
        catch (e) { showErrors(e, f, status); setBusy(btn, false, "Send me a code"); }
      });
      shell("Reset your password", "Enter your email and we'll send a 6-digit code.", form);
      f.email.input.focus();
    }

    function resetScreen(s) {
      const code = field("6-digit code", { name: "code", autocomplete: "one-time-code", inputmode: "numeric", maxlength: 7, attrs: { pattern: "[0-9 ]*" } });
      code.input.classList.add("ao-code");
      const pw = field("New password", { name: "new_password", type: "password", autocomplete: "new-password", maxlength: 128, hint: "At least " + cfg.min_password_length + " characters." });
      const status = formError();
      const btn = el("button", { class: "ao-btn primary", type: "submit", text: "Change password" });
      const resend = el("button", { type: "button", class: "ao-link" });
      const form = el("form", { novalidate: true },
        el("p", { class: "ao-sent", role: "status", text: "If " + maskEmail(s.email) + " has an account, we've sent it a 6-digit code. It expires in " + Math.round(cfg.expires_in / 60) + " minutes." }),
        devBox(s.dev, code.input), code.wrap, pw.wrap, status, btn,
        el("div", { class: "ao-links" }, resend, link("Back to sign in", () => loginScreen())),
        notArrivedHint());
      const fields = { code, new_password: pw };
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault(); clearErrors(fields, status);
        const c = code.input.value.replace(/\s+/g, "");
        if (!/^\d{6}$/.test(c)) { code.setError("Enter the 6 digits from the email."); code.input.focus(); return; }
        if (!pw.input.value) { pw.setError("Choose a new password."); pw.input.focus(); return; }
        setBusy(btn, true, "Change password");
        try { await request("POST", api + "/reset-password", { email: s.email, code: c, new_password: pw.input.value }); loginScreen("Password changed. Please sign in with your new password."); }
        catch (e) { showErrors(e, fields, status); setBusy(btn, false, "Change password"); }
      });
      function countdown(secs) {
        let left = secs; if (timer) clearInterval(timer);
        const tick = () => { if (!resend.isConnected) { clearInterval(timer); timer = null; return; }
          if (left > 0) { resend.disabled = true; resend.textContent = "Send a new code in " + left + "s"; left -= 1; }
          else { clearInterval(timer); timer = null; resend.disabled = false; resend.textContent = "Send a new code"; } };
        tick(); timer = setInterval(tick, 1000);
      }
      resend.addEventListener("click", async () => {
        resend.disabled = true;
        try { const d = await request("POST", api + "/forgot-password", { email: s.email }); status.textContent = "A new code is on its way."; countdown(d.resend_after || cfg.resend_after);
          if (d.dev_otp) { const old = form.querySelector(".ao-dev"); if (old) old.remove(); form.firstChild.after(devBox(d.dev_otp, code.input)); } }
        catch (e) { status.textContent = e.message; countdown(e.retryAfter || cfg.resend_after); }
      });
      shell("Choose a new password", null, form);
      countdown(s.resendAfter || cfg.resend_after);
      code.input.focus();
    }

    cfgReady.then(() => (opts.start === "signup" ? signupScreen() : loginScreen(opts.notice, opts.notice ? "error" : "ok")));
    return { showLogin: loginScreen, showSignup: signupScreen };
  }

  window.AuthFlow = { mount, request };
})();

/* Patient sign-in / sign-up / OTP / password-reset screens.
 * Plugs into shell.js through its documented hook:  Portal.authRenderer(host, {mode, onSuccess}).
 * Load order in index.html: shell.js, then auth_flow.js, then this file. If any of this fails to load,
 * shell.js shows an error banner and a "sign-in screen failed to load" message.
 */
(function () {
  "use strict";
  if (!window.Portal || !window.AuthFlow) throw new Error("auth_patient.js needs shell.js and auth_flow.js loaded first");
  window.Portal.authRenderer = function (host, o) {
    const footer = document.createElement("p");
    footer.className = "ao-footer";
    footer.append("Hospital staff? ");
    const a = document.createElement("a");
    a.href = "/staff/"; a.textContent = "Go to the staff portal";
    footer.append(a, ".");
    window.AuthFlow.mount(host, {
      api: "/api/auth", kind: "patient", title: "Platypus",
      subtitle: "Sign in with your email and password. We'll also email you a 6-digit code to keep your records safe.",
      signupFields: ["dob"], footer, start: o && o.mode === "register" ? "signup" : "login", notice: o && o.notice,
      onDone: () => o.onSuccess(),
    });
  };
})();

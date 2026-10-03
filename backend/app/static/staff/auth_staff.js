/* Staff sign-in / sign-up / OTP / password-reset screens (email + password + emailed code).
 *
 *   StaffAuth.render(hostElement, { onDone(user), notice })
 *
 * Needs /static/js/auth_flow.js and /static/css/auth_otp.css, both loaded by /staff/index.html before this file.
 * After onDone the staff cookie is set; call your usual /api/staff/me to load the signed-in user.
 */
(function () {
  "use strict";
  if (!window.AuthFlow) throw new Error("auth_staff.js needs /static/js/auth_flow.js loaded first");
  window.StaffAuth = {
    render(host, opts) {
      const footer = document.createElement("p");
      footer.className = "ao-footer";
      footer.append("Patient? ");
      const a = document.createElement("a"); a.href = "/"; a.textContent = "Go to Platypus"; footer.append(a, ".");
      return window.AuthFlow.mount(host, {
        api: "/api/staff/auth", kind: "staff", title: "Platypus Hospital Portal",
        subtitle: "Sign in with your work email and password. We'll also email you a 6-digit code.",
        signupFields: ["invite_code"], footer, onDone: (d) => opts.onDone(d), notice: opts.notice,
        embedded: !!(opts && opts.embedded !== false), headingTag: "h2",
      });
    },
  };
})();


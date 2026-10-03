/* Staff sign-in page frame. The screens themselves come from /staff/auth_staff.js (StaffAuth.render). */
(function () {
  const { el } = S;
  if (!window.StaffAuth) throw new Error("staff_login.js needs /staff/auth_staff.js loaded first");

  S.showLogin = function (onDone, notice) {
    document.getElementById("app").hidden = true;
    const host = S.clear(document.getElementById("login-view")); host.hidden = false;
    host.append(el("h1", { text: "Hospital staff sign-in" }),
      el("p", { class: "muted", text: "For clinic and hospital staff only. Patients sign in on the patient app." }));
    const card = el("div", { class: "card" });
    host.append(card, el("p", { class: "small muted" }, "Patient? ", el("a", { href: "/", text: "Go to the patient app" }), "."));
    window.StaffAuth.render(card, { onDone, notice });
  };
})();

/* Patient medical navigation. Existing records and APIs remain unchanged. */
(function () {
  const P = window.Portal, M = window.MedUI;
  M.TABS = [["med_history", "History"], ["med_meds", "Medications"],
    ["med_allergies", "Allergies"], ["med_vaccines", "Vaccines"], ["med_results", "Lab reports"]];
  const history = P.sections.find((s) => s.id === "med_history");
  if (history) P.registerSection({ ...history, hidden: false, title: "Medical records" });
})();

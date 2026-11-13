"use strict";

const errorSummary = document.querySelector("[data-error-summary]");
if (errorSummary instanceof HTMLElement) errorSummary.focus();

// Native validation must reveal every ancestor before focusing an invalid control.
document.addEventListener(
  "invalid",
  (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const group = target.closest("[data-account-group]");
    if (group instanceof HTMLElement) group.hidden = false;
    let disclosure = target.closest("details");
    while (disclosure) {
      disclosure.open = true;
      disclosure = disclosure.parentElement?.closest("details");
    }
  },
  true,
);

const setupForm = document.querySelector("[data-setup-form]");
if (setupForm instanceof HTMLFormElement) {
  const control = setupForm.elements.namedItem(setupForm.dataset.accountModeName);
  if (control instanceof HTMLSelectElement) {
    const syncAccountGroups = () => {
      const mode = control.value;
      setupForm.querySelectorAll("[data-account-group]").forEach((group) => {
        const relevant = !mode || group.dataset.accountGroup === mode;
        const hasError = group.querySelector(".errorlist, [aria-invalid='true']");
        group.hidden = !relevant && !hasError;
        group.querySelectorAll("input, select, textarea").forEach((field) => {
          field.disabled = group.hidden;
        });
      });
      setupForm.querySelectorAll("[data-new-account-required] input").forEach((field) => {
        field.required = mode === "new";
      });
      const apiKey = setupForm.elements.namedItem(setupForm.dataset.apiKeyName);
      if (apiKey instanceof HTMLInputElement) apiKey.required = mode === "new";
    };
    setupForm.addEventListener("change", syncAccountGroups);
    syncAccountGroups();
  }
}

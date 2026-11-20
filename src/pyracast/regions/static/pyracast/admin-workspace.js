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

// A form is sent once. The pressed button stays enabled: disabling it would drop its
// name and value (for example action=validate) from the submitted data.
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || event.defaultPrevented) return;
  if ("submitting" in form.dataset) {
    event.preventDefault();
    return;
  }
  form.dataset.submitting = "";
});
window.addEventListener("pageshow", () => {
  document.querySelectorAll("form[data-submitting]").forEach((form) => {
    delete form.dataset.submitting;
  });
});

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

const coordinateNumber = /^-?\d+(?:\.\d+)?$/;
function parseCoordinatePair(text) {
  const input = text.trim();
  const separated = input.split(/[,;]/);
  const pair = separated.length === 1 ? input.split(/\s+/) : separated.map((value) => value.trim());
  return pair.length === 2 && pair.every((value) => coordinateNumber.test(value)) ? pair : null;
}

document.querySelectorAll("[data-coordinate-pair]").forEach((latitude) => {
  if (!(latitude instanceof HTMLInputElement) || !latitude.form) return;
  const longitude = latitude.form.elements.namedItem(latitude.dataset.coordinatePair);
  if (!(longitude instanceof HTMLInputElement)) return;
  const hint = latitude.form.querySelector("[data-coordinate-hint]");
  if (hint instanceof HTMLElement) {
    hint.hidden = false;
    const described = latitude.getAttribute("aria-describedby");
    latitude.setAttribute("aria-describedby", described ? `${described} ${hint.id}` : hint.id);
  }
  latitude.addEventListener("paste", (event) => {
    const pair = parseCoordinatePair(event.clipboardData?.getData("text") ?? "");
    if (!pair) return;
    event.preventDefault();
    latitude.value = pair[0];
    longitude.value = pair[1];
  });
});

document.querySelectorAll("[data-mounting]").forEach((mounting) => {
  if (!(mounting instanceof HTMLSelectElement) || !mounting.form) return;
  const groups = mounting.form.querySelectorAll("[data-fixed-mounting]");
  const syncAngles = () => {
    groups.forEach((group) => {
      group.hidden = mounting.value !== "fixed" && !group.querySelector(".errorlist");
      group.querySelectorAll("input").forEach((field) => {
        field.disabled = group.hidden;
      });
    });
  };
  mounting.addEventListener("change", syncAngles);
  syncAngles();
});

"use strict";

const uploadForm = document.querySelector("[data-historical-upload]");
if (uploadForm instanceof HTMLFormElement) {
  const fileInput = uploadForm.querySelector("[data-upload-file]");
  const drop = uploadForm.querySelector("[data-upload-drop]");
  const result = uploadForm.querySelector("[data-preview-result]");
  const controls = uploadForm.querySelector("[data-save-controls]");
  const token = uploadForm.querySelector("[data-preview-token]");
  const status = uploadForm.querySelector("[data-upload-status]");
  let busy = false;
  const invalidate = () => {
    controls.hidden = true;
    token.value = "";
    result.replaceChildren();
    status.textContent = "";
    uploadForm.elements.namedItem("settings_confirmed").checked = false;
  };
  uploadForm.addEventListener("change", (event) => {
    if (event.target.name !== "settings_confirmed") invalidate();
  });
  for (const name of ["dragover", "dragenter"]) drop.addEventListener(name, (event) => {
    event.preventDefault(); drop.classList.add("is-dragging");
  });
  for (const name of ["dragleave", "drop"]) drop.addEventListener(name, (event) => {
    event.preventDefault(); drop.classList.remove("is-dragging");
  });
  drop.addEventListener("drop", (event) => {
    if (busy) return;
    if (event.dataTransfer.files.length === 1) {
      fileInput.files = event.dataTransfer.files;
      invalidate();
    } else {
      invalidate();
      status.textContent = uploadForm.dataset.singleFile;
    }
  });
  uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const action = event.submitter?.value || "preview";
    const data = new FormData(uploadForm);
    data.set("action", action);
    const buttons = uploadForm.querySelectorAll("button");
    const inputs = uploadForm.querySelectorAll("input, select");
    busy = true;
    buttons.forEach((button) => { button.disabled = true; });
    inputs.forEach((input) => { input.disabled = true; });
    uploadForm.setAttribute("aria-busy", "true");
    status.textContent = action === "save" ? uploadForm.dataset.saving : uploadForm.dataset.checking;
    try {
      const response = await fetch(uploadForm.getAttribute("action") || window.location.href, {
        method: "POST", body: data, headers: { Accept: "application/json" }, credentials: "same-origin",
      });
      const payload = await response.json();
      if (payload.saved) {
        result.replaceChildren(); controls.hidden = true; token.value = "";
        status.textContent = `${uploadForm.dataset.saved} ${uploadForm.dataset.intervals}: ${payload.interval_count}.`;
        status.tabIndex = -1;
        status.focus();
      } else if (typeof payload.message === "string") {
        status.textContent = payload.message;
      } else {
        result.innerHTML = payload.html;
        token.value = payload.token;
        controls.hidden = !payload.can_save;
        status.textContent = "";
        result.querySelector("[data-error-summary]")?.focus();
      }
    } catch {
      status.textContent = uploadForm.dataset.networkError;
    } finally {
      buttons.forEach((button) => { button.disabled = false; });
      inputs.forEach((input) => { input.disabled = false; });
      busy = false;
      uploadForm.removeAttribute("aria-busy");
    }
  });
}

// Minimal behaviour for the admin interface. No framework, no build step.
"use strict";

function csrfToken() {
  return document.querySelector('meta[name="csrf-token"]').content;
}

// Forms with data-confirm ask before submitting.
document.addEventListener("submit", (event) => {
  const form = event.target;
  const question = form.dataset.confirm;
  if (question && !window.confirm(question)) {
    event.preventDefault();
  }
});

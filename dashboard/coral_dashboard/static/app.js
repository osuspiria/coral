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

// Streaming upload: the raw file is the request body, one request per file.
async function uploadFile(file, logEl) {
  const line = document.createElement("li");
  line.textContent = `${file.name}: uploading…`;
  logEl.appendChild(line);
  try {
    const response = await fetch(`/media/upload?filename=${encodeURIComponent(file.name)}`, {
      method: "PUT",
      headers: { "X-CSRF-Token": csrfToken(), "Content-Type": "application/octet-stream" },
      body: file,
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok) {
      line.textContent = `${file.name}: ${data.duplicate ? "already in the library" : "uploaded"}`;
      line.className = "notice";
      return true;
    }
    line.textContent = `${file.name}: ${data.detail || `failed (HTTP ${response.status})`}`;
    line.className = "error";
  } catch (err) {
    line.textContent = `${file.name}: network error`;
    line.className = "error";
  }
  return false;
}

const uploadButton = document.getElementById("upload-start");
if (uploadButton) {
  uploadButton.addEventListener("click", async () => {
    const input = document.getElementById("upload-files");
    const logEl = document.getElementById("upload-log");
    if (!input.files.length) return;
    uploadButton.disabled = true;
    let anyOk = false;
    for (const file of input.files) {
      anyOk = (await uploadFile(file, logEl)) || anyOk;
    }
    uploadButton.disabled = false;
    if (anyOk) {
      setTimeout(() => window.location.reload(), 1200);
    }
  });
}

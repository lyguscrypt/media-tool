const messages = {
  cut: "Cutting audio…",
  download: "Downloading YouTube audio…",
  thumbnail: "Downloading YouTube thumbnail…",
};

let runningOperations = 0;
window.addEventListener("beforeunload", (event) => {
  if (runningOperations > 0) {
    event.preventDefault();
    event.returnValue = "";
  }
});

document.querySelectorAll(".card form").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    if (button.disabled) return;

    const action = form.querySelector('input[name="action"]').value;
    const processing = form.querySelector(".processing");
    const result = form.querySelector(".operation-result");
    const originalButton = button.innerHTML;
    const data = new FormData(form);
    form.querySelectorAll(".errorlist").forEach((error) => error.remove());
    result.replaceChildren();
    result.hidden = true;
    processing.querySelector(".processing-message").textContent = messages[action];
    processing.hidden = false;
    button.disabled = true;
    button.textContent = "Processing…";
    runningOperations += 1;

    try {
      const response = await fetch(form.getAttribute("action") || window.location.href, {
        method: "POST",
        body: data,
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("The server returned an unexpected response. Check the server logs.");
      }
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "The operation failed. Check the server logs.");
      }
      const heading = document.createElement("strong");
      heading.textContent = "Processing complete";
      const description = document.createElement("p");
      description.textContent = "Output saved to:";
      result.append(heading, description);
      payload.output_paths.forEach((path) => {
        const element = document.createElement("code");
        element.className = "output-path";
        element.textContent = path;
        result.append(element);
      });
      result.className = "operation-result notice success";
    } catch (error) {
      result.className = "operation-result notice error";
      result.textContent = error instanceof Error
        ? error.message
        : "The request failed. The operation may still be running; check the output folder before retrying.";
      if (error instanceof TypeError) {
        result.textContent = "The connection failed. The operation may still be running; check the output folder before retrying.";
      }
    } finally {
      runningOperations -= 1;
      processing.hidden = true;
      result.hidden = false;
      button.innerHTML = originalButton;
      button.disabled = false;
    }
  });
});

// Parse-trigger for the TDoc detail page.
// POSTs the parse job to /jobs/parse/tdocs, then injects a div that
// hx-get's the job status partial (which polls every 2s until terminal).
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.id !== "parse-form") {
      return;
    }
    event.preventDefault();

    var tdocId = form.getAttribute("data-tdoc-id");
    var force = form.querySelector('input[name="force"]').checked;
    var full = form.querySelector('input[name="full"]').checked;
    var queued = form.querySelector(".parse-queued");
    var target = document.getElementById("parse-job-target");

    fetch("/jobs/parse/tdocs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filter: { tdoc_id: tdocId },
        force: force,
        full: full,
      }),
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("parse enqueue failed: HTTP " + response.status);
        }
        return response.json();
      })
      .then(function (body) {
        if (queued) {
          queued.style.display = "inline";
        }
        var div = document.createElement("div");
        div.setAttribute("hx-get", "/jobs/" + body.job_id + "?format=html");
        div.setAttribute("hx-trigger", "load");
        div.setAttribute("hx-swap", "outerHTML");
        target.appendChild(div);
        if (window.htmx) {
          window.htmx.process(div);
        }
      })
      .catch(function (err) {
        if (queued) {
          queued.textContent = "Failed to enqueue parse job";
          queued.style.display = "inline";
        }
        console.error(err);
      });
  });
})();

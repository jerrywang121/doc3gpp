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
        installTerminalObserver(target, queued);
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

  // The job-status partial renders a polling span only when status is
  // non-terminal (templates/partials/job_status.html:18). When HTMX
  // swaps to a terminal state the polling span disappears — hide the
  // "Parse job queued" hint so it doesn't linger forever, and reload
  // the page so the server-rendered cover page / TTCN / extracted-at
  // sections pick up the freshly-written DB rows.
  function installTerminalObserver(target, queued) {
    if (!queued || !target || !window.MutationObserver) {
      return;
    }
    var observer = new MutationObserver(function () {
      var node = target.querySelector(
        "[hx-get*='/jobs/'][hx-trigger='every 2s']"
      );
      if (!node) {
        queued.style.display = "none";
        observer.disconnect();
        window.location.reload();
      }
    });
    observer.observe(target, { childList: true, subtree: true });
  }
})();

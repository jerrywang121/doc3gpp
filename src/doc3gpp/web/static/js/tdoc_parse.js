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
  //
  // Important: the freshly-appended wrapper div carries
  // ``hx-trigger="load"`` (NOT ``every 2s``), and a mutation fires the
  // moment it is attached — before HTMX has had any chance to issue the
  // initial GET. A naive selector that just checks for the polling
  // span's presence fires on that first mutation and reloads the page
  // before the job is even enqueued. The fix is to remember whether we
  // have ever seen the polling span: only when it disappears AFTER it
  // has appeared do we conclude the job reached a terminal state.
  //
  // Edge case: a job that is already terminal when the first HTMX GET
  // returns (instant failure, cache purge) never renders the polling
  // span, so ``pollSeen`` stays false and the observer would never
  // fire. A timeout fallback hides the hint and reloads the page once
  // the job has had ample time to reach a terminal state.
  function installTerminalObserver(target, queued) {
    if (!queued || !target || !window.MutationObserver) {
      return;
    }
    var pollSeen = false;
    var done = false;
    function finish() {
      if (done) {
        return;
      }
      done = true;
      queued.style.display = "none";
      observer.disconnect();
      window.location.reload();
    }
    var observer = new MutationObserver(function () {
      var node = target.querySelector(
        "[hx-get*='/jobs/'][hx-trigger='every 2s']"
      );
      if (node) {
        pollSeen = true;
        return;
      }
      if (pollSeen) {
        finish();
      }
    });
    observer.observe(target, { childList: true, subtree: true });
    // 30s is far beyond the 2s poll cadence. If the polling span never
    // appeared the job is terminal (or the stream is wedged) — either
    // way the hint must not linger and the page should refresh. If the
    // span WAS seen, the observer handles the terminal transition and
    // this timeout is a no-op.
    window.setTimeout(function () {
      if (!pollSeen) {
        finish();
      }
    }, 30000);
  }
})();

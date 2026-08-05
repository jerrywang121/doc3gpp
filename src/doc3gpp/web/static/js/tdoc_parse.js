// Parse-trigger for the TDoc detail page.
//
// Thin wrapper over the shared ``bindJobPolling`` helper from
// ``job_poller.js``. The tdoc parse route
// (``POST /jobs/parse/tdocs``) expects a JSON envelope (filter +
// force + full), unlike the flat ``/jobs/sync_tdocs`` alias that
// reads form-encoded bodies — so we override the body via
// ``buildBody`` and the Content-Type via ``contentType``, and let
// the poller own the polling + reload lifecycle.
(function () {
  "use strict";

  function init() {
    var form = document.getElementById("parse-form");
    if (!form || !window.bindJobPolling) {
      return;
    }
    var tdocId = form.getAttribute("data-tdoc-id");
    var queued = form.querySelector(".parse-queued");
    if (queued) {
      // Preserve the original label so the poller can restore it on
      // re-submit (e.g. a user queues a second parse after the
      // terminal-state reload path has hidden the hint).
      queued.dataset.label = queued.textContent;
    }
    window.bindJobPolling(form, {
      queuedSelector: ".parse-queued",
      targetSelector: "#parse-job-target",
      contentType: "application/json",
      buildBody: function (form) {
        var force = form.querySelector('input[name="force"]').checked;
        var full = form.querySelector('input[name="full"]').checked;
        return JSON.stringify({
          filter: { tdoc_id: tdocId },
          force: force,
          full: full,
        });
      },
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

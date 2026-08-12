// Sync-trigger for the spec detail page.
//
// Thin wrapper over the shared ``bindJobPolling`` helper from
// ``job_poller.js``. The spec sync route (``POST /jobs/sync/specs``)
// expects a JSON envelope (spec_id + force), so we override the body
// via ``buildBody`` and the Content-Type via ``contentType``, and let
// the poller own the polling + reload lifecycle.
(function () {
  "use strict";

  function init() {
    var form = document.getElementById("spec-sync-form");
    if (!form || !window.bindJobPolling) {
      return;
    }
    var specId = form.getAttribute("data-spec-id");
    var queued = form.querySelector(".spec-sync-queued");
    if (queued) {
      queued.dataset.label = queued.textContent;
    }
    window.bindJobPolling(form, {
      queuedSelector: ".spec-sync-queued",
      targetSelector: "#spec-sync-job-target",
      contentType: "application/json",
      buildBody: function (form) {
        var force = form.querySelector('input[name="force"]').checked;
        return JSON.stringify({ spec_id: specId, force: force });
      },
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

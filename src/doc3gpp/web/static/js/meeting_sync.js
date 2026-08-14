// Sync-trigger for the meeting show + meetings list pages.
//
// Every meeting-sync form on the web surface (``meeting_show.html``,
// ``partials/meeting_results.html``) is driven by the shared
// ``bindJobPolling`` helper from ``job_poller.js``. The flat
// ``/jobs/sync_tdocs`` alias reads form-encoded bodies, so we use
// the helper's default serialiser — no JSON bridge required.
//
// We bind forms on ``DOMContentLoaded`` (and run inline if the
// document is already ready) and then watch the document for new
// forms: the meetings list swaps its results partial in via HTMX
// when the user applies a filter, so fresh ``#sync-form`` rows
// appear after the script has already loaded. The observer ignores
// forms that already carry a data-bound marker so we don't double-
// bind (the helpers listen on ``submit`` and a double-binding
// would double-enqueue).
(function () {
  "use strict";

  var FORM_ID = "sync-form";
  var BOUND_ATTR = "data-doc3gpp-sync-bound";

  function bindForm(form) {
    if (!form || form.getAttribute(BOUND_ATTR) === "1") {
      return;
    }
    if (!window.bindJobPolling) {
      return;
    }
    form.setAttribute(BOUND_ATTR, "1");
    window.bindJobPolling(form, {
      queuedSelector: ".sync-queued",
      targetSelector: "#" + FORM_ID + "-job-target",
      onTerminal: function () {
        window.location.reload();
      },
    });
  }

  function bindAll() {
    var forms = document.querySelectorAll("#" + FORM_ID);
    for (var i = 0; i < forms.length; i++) {
      bindForm(forms[i]);
    }
  }

  function init() {
    bindAll();
    if (!window.MutationObserver || !document.body) {
      return;
    }
    var observer = new MutationObserver(function () {
      bindAll();
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();


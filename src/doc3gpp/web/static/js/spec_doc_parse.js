(function () {
  "use strict";

  function init() {
    var form = document.getElementById("spec-doc-parse-form");
    if (!form || !window.bindJobPolling) return;
    var specId = form.getAttribute("data-spec-id");
    var version = form.getAttribute("data-version");
    var sourceParsed = form.getAttribute("data-source-parsed") === "true";
    var queued = form.querySelector(".spec-doc-parse-queued");
    if (queued) queued.dataset.label = queued.textContent;
    window.bindJobPolling(form, {
      queuedSelector: ".spec-doc-parse-queued",
      targetSelector: "#spec-doc-parse-job-target",
      contentType: "application/json",
      buildBody: function (form) {
        var forceEl = form.querySelector('input[name="force"]');
        return JSON.stringify({
          "spec_ids": [specId],
          "version": version,
          "force": sourceParsed && !!forceEl && forceEl.checked,
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

(function () {
  "use strict";

  function checked(form, name) {
    var input = form.querySelector('input[name="' + name + '"]');
    return Boolean(input && input.checked);
  }

  function dateExpressions(form) {
    var since = form.elements.since ? form.elements.since.value.trim() : "";
    var until = form.elements.until ? form.elements.until.value.trim() : "";
    return [
      since ? ">= " + since : "",
      until ? "<= " + until : "",
    ].filter(Boolean);
  }

  function bind(form, buildBody) {
    if (!window.bindJobPolling) {
      return;
    }
    window.bindJobPolling(form, {
      contentType: "application/json",
      buildBody: buildBody,
    });
  }

  function init() {
    bind(document.getElementById("listserv-sync-form"), function (form) {
      return JSON.stringify({
        list_id: form.elements.list_id.value,
        dates: dateExpressions(form),
        force: checked(form, "force"),
      });
    });
    bind(document.getElementById("listserv-parse-form"), function (form) {
      return JSON.stringify({
        list_id: form.elements.list_id.value,
        dates: dateExpressions(form),
        force: checked(form, "force"),
      });
    });
    bind(document.getElementById("listserv-parse-item-form"), function (form) {
      return JSON.stringify({
        url: form.elements.url.value,
        force: checked(form, "force"),
      });
    });
    bind(document.getElementById("listserv-index-form"), function (form) {
      var body = {
        rebuild: checked(form, "rebuild"),
        rebuild_embeddings: checked(form, "rebuild_embeddings"),
        rebuild_all: checked(form, "rebuild_all"),
        resume: checked(form, "resume"),
        stale_only: checked(form, "stale_only"),
      };
      var batch = form.elements.batch.value.trim();
      if (batch) {
        body.batch = Number(batch);
      }
      return JSON.stringify(body);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

// Page-local wrapper for the /sync hub.
//
// Binds every form with id ending in "-form" on the hub page to the shared
// ``bindJobPolling`` helper, providing a ``buildBody`` per form that
// transforms the user-facing inputs into the JSON shape the matching
// ``/jobs/...`` route expects. The terminal action is overridden to
// refresh the bottom ``#recent-jobs`` div via HTMX instead of doing a
// full ``location.reload()`` (which would lose scroll + lose the user's
// place in the page).
(function () {
  "use strict";

  function refreshRecentJobs() {
    if (window.htmx && window.htmx.ajax) {
      window.htmx.ajax("GET", "/sync?format=fragment",
                       {target: "#recent-jobs", swap: "outerHTML"});
    } else {
      window.location.reload();
    }
  }

  function readCheckbox(form, name) {
    var el = form.querySelector('input[name="' + name + '"]');
    return !!(el && el.checked);
  }

  function readText(form, name) {
    var el = form.querySelector('input[name="' + name + '"]');
    return el ? el.value.trim() : "";
  }

  function readSelectedRadio(form, name) {
    var els = form.querySelectorAll('input[name="' + name + '"]');
    for (var i = 0; i < els.length; i++) {
      if (els[i].checked) {
        return els[i].value;
      }
    }
    return null;
  }

  var BODY_BUILDERS = {
    "sync-meetings-form": function (form) {
      return JSON.stringify({
        tsg: readText(form, "tsg"),
        force: readCheckbox(form, "force"),
      });
    },
    "sync-tdocs-form": function (form) {
      var selector = readSelectedRadio(form, "selector") || "meeting_id";
      var value = readText(form, "value");
      var body = {force: readCheckbox(form, "force")};
      if (selector === "meeting_id") {
        body.meeting_id = parseInt(value, 10);
      } else {
        // The route's pydantic body field is ``meeting`` (not
        // ``meeting_name``); the handler stores it under ``meeting_name``.
        body.meeting = value;
      }
      return JSON.stringify(body);
    },
    "sync-tdocs-all-form": function (form) {
      return JSON.stringify({force: readCheckbox(form, "force")});
    },
    "sync-specs-tsg-form": function (form) {
      return JSON.stringify({
        tsg: readText(form, "tsg"),
        force: readCheckbox(form, "force"),
        per_version_details: readCheckbox(form, "per_version_details"),
      });
    },
    "sync-specs-id-form": function (form) {
      return JSON.stringify({
        spec_id: readText(form, "spec_id"),
        force: readCheckbox(form, "force"),
        per_version_details: readCheckbox(form, "per_version_details"),
      });
    },
    "parse-tdocs-form": function (form) {
      var filter = {};
      var filterKeys = [
        "tdoc_id", "meeting", "status", "spec", "wi",
        "release", "version", "source",
      ];
      for (var i = 0; i < filterKeys.length; i++) {
        var k = filterKeys[i];
        var v = readText(form, "filter_" + k);
        if (v) {
          filter[k] = v;
        }
      }
      var body = {
        filter: filter,
        force: readCheckbox(form, "force"),
        full: readCheckbox(form, "full"),
      };
      var maxBatch = readText(form, "max_batch");
      if (maxBatch) {
        body.max_batch = parseInt(maxBatch, 10);
      }
      return JSON.stringify(body);
    },
    "parse-tdoc-url-form": function (form) {
      var selector = readSelectedRadio(form, "selector") || "recursive";
      var recursive = (selector === "recursive");
      var body = {
        url: readText(form, "url"),
        recursive: recursive,
        force: readCheckbox(form, "force"),
        full: readCheckbox(form, "full"),
      };
      if (!recursive) {
        var d = parseInt(readText(form, "max_depth"), 10);
        body.max_depth = isNaN(d) ? 2 : d;
      }
      return JSON.stringify(body);
    },
    "rebuild-search-form": function (form) {
      return JSON.stringify({
        stale_only: readCheckbox(form, "stale_only"),
        resume: readCheckbox(form, "resume"),
      });
    },
    "purge-cache-form": function (form) {
      var select = form.querySelector('select[name="scope"]');
      return JSON.stringify({
        scope: select ? select.value : "markdown",
        yes: readCheckbox(form, "yes"),
      });
    },
  };

  function bindForm(form) {
    if (!form || !form.id || !BODY_BUILDERS[form.id]) {
      return;
    }
    if (!window.bindJobPolling) {
      return;
    }
    window.bindJobPolling(form, {
      contentType: "application/json",
      buildBody: BODY_BUILDERS[form.id],
      onTerminal: refreshRecentJobs,
    });
  }

  function init() {
    var forms = document.querySelectorAll('main.content form[id$="-form"]');
    for (var i = 0; i < forms.length; i++) {
      bindForm(forms[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

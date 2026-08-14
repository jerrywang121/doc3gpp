// Shared job-polling helper for the web UI.
//
// Used by every form that enqueues a background job and wants the page
// to refresh when the job reaches a terminal state:
//
//   1. The user submits the form (e.g. "Sync this meeting's TDocs",
//      "Parse this TDoc").
//   2. We POST the form's body to the form's action URL and read
//      ``job_id`` from the JSON envelope (202 with
//      ``{"job_id": ..., "status": "queued", "links": {...}}``).
//   3. We inject a wrapper div with ``hx-get="/jobs/{id}?format=html"``
//      and ``hx-trigger="load"`` into the form's target element. HTMX
//      swaps in the job-status partial, which itself polls every 2s
//      while the job is non-terminal and renders a static block once
//      it reaches a terminal state (see partials/job_status.html).
//   4. A MutationObserver watches the target. Once the polling span
//      (the ``hx-trigger="every 2s"`` block in the partial) has
//      appeared AND subsequently disappeared, we conclude the job is
//      terminal, hide the queued hint, and reload the page so the
//      server-rendered sections pick up the freshly-written DB rows.
//   5. A timeout fallback handles jobs that were already terminal
//      before the first poll rendered (e.g. instant failure) — in
//      that case the polling span never appears and the observer
//      alone would never fire.
//
// Why a dedicated helper: the original ``tdoc_parse.js`` worked
// correctly by hand-rolling this cycle, but the meeting-sync forms on
// ``meeting_show.html`` and ``partials/meeting_results.html`` shipped
// a broken variant that used ``hx-swap="none"`` and never picked up
// the job_id from the response. Centralising the contract here means
// every form gets the same UX and the same regressions are locked in
// by the static-endpoint tests in ``test_web_routes.py``.
(function (global) {
  "use strict";

  // The timeout is far beyond the 2s poll cadence. If the polling
  // span never appeared, the job is either already terminal or the
  // stream is wedged — either way the hint must not linger and the
  // page should refresh. If the span WAS seen, the observer handles
  // the terminal transition and this timeout is a no-op.
  var TERMINAL_FALLBACK_MS = 30000;

  /**
   * Bind the poller to a single form. Safe to call multiple times —
   * each binding installs its own listener and its own target
   * observer, so a page with several forms (e.g. the meetings list)
   * polls them independently.
   *
   * @param {HTMLFormElement} form The form to instrument.
   * @param {object} [options]
   * @param {string} [options.queuedSelector=".sync-queued"] CSS
   *   selector for the "queued" hint span inside the form. Defaults
   *   to ``.sync-queued``; the tdoc parse form passes
   *   ``.parse-queued``.
   * @param {string} [options.targetSelector] CSS selector for the
   *   div the wrapper element is appended to. Defaults to the form's
   *   sibling ``#<form.id>-job-target`` (matches
   *   ``#parse-job-target`` / ``#sync-job-target`` conventions).
   * @param {string} [options.body] Override the request body. When
   *   unset, the form's own fields are sent as
   *   ``application/x-www-form-urlencoded`` — the same shape the
   *   ``/jobs/sync_tdocs`` flat alias accepts. The tdoc parse form
   *   passes a JSON body instead because
   *   ``/jobs/parse/tdocs`` expects a JSON envelope.
    * @param {string} [options.contentType] Content-Type for the POST.
    *   Defaults to ``application/x-www-form-urlencoded``; the tdoc
    *   parse form passes ``application/json``.
    * @param {function} [options.onTerminal] Called once when the job
    *   reaches a terminal state (or the fallback window elapses).
    *   Defaults to ``function () { window.location.reload(); }``; pass
    *   a custom callback when the page needs a different refresh
    *   behaviour (e.g. an HTMX partial swap on the hub page).
    */
  function bindJobPolling(form, options) {
    if (!form || !form.tagName || form.tagName !== "FORM") {
      return;
    }
    var opts = options || {};
    var queuedSelector = opts.queuedSelector || ".sync-queued";
    var targetSelector =
      opts.targetSelector ||
      (form.id ? "#" + form.id + "-job-target" : null);
    var target = targetSelector ? document.querySelector(targetSelector) : null;
    var onTerminal =
      typeof opts.onTerminal === "function"
        ? opts.onTerminal
        : function () {
            window.location.reload();
          };

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var queued = form.querySelector(queuedSelector);
      if (queued) {
        queued.textContent = queued.dataset.label || queued.textContent;
        queued.style.display = "inline";
      }

      // ``buildBody`` lets callers compute the body at submit time so
      // checkbox state captured at the moment of click is honoured.
      // When unset, fall back to the form-encoded serialiser.
      var init = {
        method: "POST",
        headers: {},
      };
      if (typeof opts.buildBody === "function") {
        init.body = opts.buildBody(form);
      } else if (opts.body !== undefined) {
        init.body = opts.body;
      }
      if (opts.contentType) {
        init.headers["Content-Type"] = opts.contentType;
      }
      if (!("body" in init)) {
        // Default: send the form's own fields. The flat alias
        // ``/jobs/sync_tdocs`` reads the form-encoded body, so
        // serialising the form here keeps that route working without
        // a JSON bridge.
        var params = new URLSearchParams();
        var fields = form.elements;
        for (var i = 0; i < fields.length; i++) {
          var el = fields[i];
          if (!el.name || el.disabled) {
            continue;
          }
          if (el.type === "checkbox" || el.type === "radio") {
            if (el.checked) {
              params.append(el.name, el.value || "on");
            }
          } else {
            params.append(el.name, el.value);
          }
        }
        init.body = params.toString();
        init.headers["Content-Type"] = "application/x-www-form-urlencoded";
      }

      var action = form.getAttribute("action") || form.action;
      fetch(action, init)
        .then(function (response) {
          if (!response.ok) {
            throw new Error(
              "enqueue failed: HTTP " + response.status
            );
          }
          return response.json();
        })
        .then(function (body) {
          if (!body || !body.job_id) {
            throw new Error("enqueue response missing job_id");
          }
          attachPolling(form, target, body.job_id, queued, onTerminal);
        })
        .catch(function (err) {
          if (queued) {
            queued.textContent = "Failed to enqueue job";
            queued.style.display = "inline";
          }
          console.error(err);
        });
    });
  }

  function attachPolling(form, target, jobId, queued, onTerminal) {
    if (!target) {
      // Without a target we can't observe the polling span. Fall back
      // to a blind refresh after the fallback window so the page
      // eventually reflects whatever the worker wrote. This honours
      // ``onTerminal`` too so a hub panel without a job target never
      // does an uncoordinated hard reload.
      if (queued) {
        queued.style.display = "inline";
      }
      window.setTimeout(function () {
        onTerminal();
      }, TERMINAL_FALLBACK_MS);
      return;
    }
    var div = document.createElement("div");
    div.setAttribute("hx-get", "/jobs/" + jobId + "?format=html");
    div.setAttribute("hx-trigger", "load");
    div.setAttribute("hx-swap", "outerHTML");
    target.appendChild(div);
    installTerminalObserver(target, queued, onTerminal);
    if (global.htmx) {
      global.htmx.process(div);
    }
  }

  // The job-status partial renders a polling span only when status is
  // non-terminal (templates/partials/job_status.html). When HTMX
  // swaps to a terminal state the polling span disappears — hide the
  // "queued" hint so it doesn't linger forever, and reload the page
  // so the server-rendered sections pick up the freshly-written DB
  // rows.
  //
  // Important: the freshly-appended wrapper div carries
  // ``hx-trigger="load"`` (NOT ``every 2s``), and a mutation fires
  // the moment it is attached — before HTMX has had any chance to
  // issue the initial GET. A naive selector that just checks for the
  // polling span's presence fires on that first mutation and reloads
  // the page before the job is even enqueued. The fix is to remember
  // whether we have ever seen the polling span: only when it
  // disappears AFTER it has appeared do we conclude the job reached a
  // terminal state.
  //
  // Edge case: a job that is already terminal when the first HTMX GET
  // returns (instant failure, cache purge) never renders the polling
  // span, so ``pollSeen`` stays false and the observer would never
  // fire. A timeout fallback hides the hint and reloads the page once
  // the job has had ample time to reach a terminal state.
  function installTerminalObserver(target, queued, onTerminal) {
    if (!target || !global.MutationObserver) {
      return;
    }
    var pollSeen = false;
    var done = false;
    function finish() {
      if (done) {
        return;
      }
      done = true;
      if (queued) {
        queued.style.display = "none";
      }
      observer.disconnect();
      onTerminal();
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
    global.setTimeout(function () {
      if (!pollSeen) {
        finish();
      }
    }, TERMINAL_FALLBACK_MS);
  }

  global.bindJobPolling = bindJobPolling;
})(typeof window !== "undefined" ? window : globalThis);

"use strict";

// Standalone smoke test for job_poller.js (run via `node` from pytest).
//
// Playwright is not available in this environment, so this hand-rolled
// script mocks just enough of the DOM / browser globals that
// job_poller.js touches and drives the full bind->submit->poll->finish
// cycle deterministically. It asserts the two acceptance criteria for
// the ``onTerminal`` option:
//
//   1. When ``onTerminal`` is passed, the callback fires and
//      ``window.location.reload`` is NOT called.
//   2. When ``onTerminal`` is omitted, ``window.location.reload`` IS
//      called.
//
// Exit 0 = all assertions passed; non-zero = failure.

const assert = require("assert");
const path = require("path");
const fs = require("fs");

const POLLER_PATH = path.resolve(
  __dirname,
  "../../src/doc3gpp/web/static/js/job_poller.js"
);

// ---- global / browser mocks (must exist before the IIFE runs) ----

let reloadCalled = false;
let onTerminalCalled = false;
let observerCallback = null;
let pollVisible = false;

global.window = globalThis; // IIFE footer passes `window` when defined
global.location = {
  reload: function () {
    reloadCalled = true;
  },
};

// Keep the terminal fallback timer inert so it never races the observer
// path we are exercising.
global.setTimeout = function () {};
global.window.setTimeout = global.setTimeout;

global.fetch = function () {
  return Promise.resolve({
    ok: true,
    json: async () => ({ job_id: "job-1", status: "queued" }),
  });
};

global.htmx = { process: function () {} };

class FakeMutationObserver {
  constructor(callback) {
    observerCallback = callback;
  }
  observe() {}
  disconnect() {}
}
global.MutationObserver = FakeMutationObserver;

// ---- minimal fake DOM nodes ----

function makeQueued() {
  return {
    textContent: "",
    dataset: { label: "Syncing..." },
    style: { display: "none" },
  };
}

function makeForm() {
  const handlers = {};
  return {
    tagName: "FORM",
    id: "test-form",
    action: "/jobs/sync_tdocs",
    elements: [
      {
        name: "tsg",
        value: "SA2",
        type: "text",
        disabled: false,
        checked: false,
      },
    ],
    addEventListener: function (event, cb) {
      handlers[event] = cb;
    },
    getAttribute: function (attr) {
      return attr === "action" ? "/jobs/sync_tdocs" : null;
    },
    querySelector: function () {
      return makeQueued();
    },
    _handlers: handlers,
  };
}

function makeTarget() {
  return {
    style: {},
    appendChild: function () {},
    querySelector: function () {
      return pollVisible ? { nodeType: 1 } : null;
    },
  };
}

// ---- drive one full scenario ----

function runScenario(useOnTerminal) {
  reloadCalled = false;
  onTerminalCalled = false;
  observerCallback = null;
  pollVisible = false;

  const form = makeForm();
  const target = makeTarget();

  global.document = {
    querySelector: function (sel) {
      return sel === "#test-form-job-target" ? target : null;
    },
    createElement: function () {
      return { setAttribute: function () {}, style: {} };
    },
  };

  const opts = {
    queuedSelector: ".sync-queued",
    targetSelector: "#test-form-job-target",
  };
  if (useOnTerminal) {
    opts.onTerminal = function () {
      onTerminalCalled = true;
    };
  }

  global.bindJobPolling(form, opts);
  form._handlers.submit({ preventDefault: function () {} });

  return new Promise(function (resolve) {
    // Let the fetch promise chain (attachPolling ->
    // installTerminalObserver) settle.
    setImmediate(function () {
      // First observer fire: polling span present -> pollSeen = true.
      pollVisible = true;
      observerCallback();
      // Second observer fire: polling span gone -> finish() -> onTerminal.
      pollVisible = false;
      observerCallback();
      resolve({ reloadCalled, onTerminalCalled });
    });
  });
}

async function main() {
  const source = fs.readFileSync(POLLER_PATH, "utf8");
  // Evaluate job_poller.js against the mocks we set up above.
  // eslint-disable-next-line no-eval
  (0, eval)(source);

  // Scenario 1: onTerminal provided.
  const provided = await runScenario(true);
  assert.strictEqual(
    provided.onTerminalCalled,
    true,
    "onTerminal callback should have fired"
  );
  assert.strictEqual(
    provided.reloadCalled,
    false,
    "window.location.reload should NOT be called when onTerminal is given"
  );

  // Scenario 2: onTerminal omitted -> default reload.
  const omitted = await runScenario(false);
  assert.strictEqual(
    omitted.reloadCalled,
    true,
    "window.location.reload should be called when onTerminal is omitted"
  );

  console.log(
    "PASS: onTerminal callback fires (reload skipped) and default reload fires when omitted"
  );
  process.exit(0);
}

main().catch(function (err) {
  console.error("FAIL:", err && err.message ? err.message : err);
  process.exit(1);
});

"use strict";

// Exercise the real LISTSERV form builders and job poller without a browser.
// Run with Node from the pytest wrapper; this uses only Node built-ins.

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const LISTSERV_PATH = path.resolve(
  __dirname,
  "../../src/doc3gpp/web/static/js/listserv.js"
);
const POLLER_PATH = path.resolve(
  __dirname,
  "../../src/doc3gpp/web/static/js/job_poller.js"
);

function makeTarget() {
  return {
    appended: [],
    appendChild: function (node) {
      this.appended.push(node);
    },
    querySelector: function () {
      return null;
    },
  };
}

function makeForm({ id, action, listId, since, until, checked, batch }) {
  const handlers = {};
  const classes = new Set();
  const target = makeTarget();
  const queued = {
    textContent: "Sync job queued",
    dataset: { label: "Sync job queued" },
    style: { display: "none" },
  };
  const controls = {
    list_id: { value: listId },
    since: { value: since },
    until: { value: until },
    batch: { value: batch },
  };
  for (const [name, value] of Object.entries(checked)) {
    controls[name] = { checked: value };
  }

  return {
    id,
    tagName: "FORM",
    action,
    target,
    targetSelector: "#" + id + "-job-target",
    elements: controls,
    classList: {
      add: function (name) { classes.add(name); },
      remove: function (name) { classes.delete(name); },
      contains: function (name) { return classes.has(name); },
    },
    addEventListener: function (event, callback) {
      handlers[event] = callback;
    },
    getAttribute: function (name) {
      return name === "action" ? action : null;
    },
    querySelector: function (selector) {
      if (selector === this.targetSelector) {
        return target;
      }
      if (selector === ".sync-queued") {
        return queued;
      }
      const inputMatch = selector.match(/^input\[name="([^"]+)"\]$/);
      return inputMatch ? controls[inputMatch[1]] || null : null;
    },
    _handlers: handlers,
  };
}

function makeScenario(readyState) {
  const forms = {
    sync: makeForm({
      id: "listserv-sync-form",
      action: "/api/listserv/sync",
      listId: "TEAM_A",
      since: " 2026-10-01 ",
      until: " 2026-10-31 ",
      checked: { force: true },
      batch: "",
    }),
    parse: makeForm({
      id: "listserv-parse-form",
      action: "/api/listserv/parse",
      listId: "TEAM_X",
      since: "  ",
      until: " 2026-10-20 \t",
      checked: { force: false },
      batch: "",
    }),
    index: makeForm({
      id: "listserv-index-form",
      action: "/api/listserv/index",
      listId: "",
      since: "",
      until: "",
      checked: {
        rebuild: true,
        rebuild_embeddings: false,
        rebuild_all: true,
        resume: false,
        stale_only: true,
      },
      batch: " 12 ",
    }),
  };
  const formsById = Object.fromEntries(
    Object.values(forms).map(function (form) { return [form.id, form]; })
  );
  const decoys = new Map(
    Object.values(forms).map(function (form) {
      return [form.targetSelector, makeTarget()];
    })
  );
  const requests = [];
  let domContentLoaded = null;

  global.window = globalThis;
  global.location = { reload: function () {} };
  global.setTimeout = function () {};
  global.window.setTimeout = global.setTimeout;
  global.fetch = function (url, init) {
    requests.push({ url, init });
    const jobId = "job-" + requests.length;
    return Promise.resolve({
      ok: true,
      json: async function () { return { job_id: jobId, status: "queued" }; },
    });
  };
  global.window.fetch = global.fetch;
  global.htmx = { process: function () {} };
  global.window.htmx = global.htmx;
  global.MutationObserver = class {
    constructor(callback) { this.callback = callback; }
    observe() {}
    disconnect() {}
  };
  global.window.MutationObserver = global.MutationObserver;
  global.document = {
    readyState,
    addEventListener: function (event, callback) {
      if (event === "DOMContentLoaded") {
        domContentLoaded = callback;
      }
    },
    getElementById: function (id) { return formsById[id] || null; },
    querySelector: function (selector) { return decoys.get(selector) || null; },
    createElement: function () {
      return {
        attrs: {},
        setAttribute: function (name, value) { this.attrs[name] = value; },
      };
    },
  };

  return {
    forms,
    requests,
    decoys,
    getDOMContentLoaded: function () { return domContentLoaded; },
  };
}

function evaluateScript(scriptPath) {
  // The files are browser IIFEs; evaluate them against the mocked globals.
  // eslint-disable-next-line no-eval
  (0, eval)(fs.readFileSync(scriptPath, "utf8"));
}

function submit(form) {
  let prevented = false;
  assert.strictEqual(typeof form._handlers.submit, "function");
  form._handlers.submit({
    preventDefault: function () { prevented = true; },
  });
  assert.strictEqual(prevented, true, "submission should be handled by the poller");
}

function bodyOf(request) {
  assert.strictEqual(request.init.method, "POST");
  assert.strictEqual(request.init.headers["Content-Type"], "application/json");
  return JSON.parse(request.init.body);
}

async function runScenario(readyState) {
  const scenario = makeScenario(readyState);
  evaluateScript(POLLER_PATH);
  evaluateScript(LISTSERV_PATH);

  if (readyState === "loading") {
    for (const form of Object.values(scenario.forms)) {
      assert.strictEqual(form._handlers.submit, undefined);
    }
    const callback = scenario.getDOMContentLoaded();
    assert.strictEqual(typeof callback, "function");
    callback();
  } else {
    assert.strictEqual(scenario.getDOMContentLoaded(), null);
  }

  const { sync, parse, index } = scenario.forms;
  submit(sync);
  submit(parse);
  submit(index);
  await new Promise(function (resolve) { setImmediate(resolve); });

  assert.strictEqual(scenario.requests.length, 3);
  assert.strictEqual(scenario.requests[0].url, "/api/listserv/sync");
  assert.deepStrictEqual(bodyOf(scenario.requests[0]), {
    list_id: "TEAM_A",
    dates: [">= 2026-10-01", "<= 2026-10-31"],
    force: true,
  });
  assert.strictEqual(scenario.requests[1].url, "/api/listserv/parse");
  assert.deepStrictEqual(bodyOf(scenario.requests[1]), {
    list_id: "TEAM_X",
    dates: ["<= 2026-10-20"],
    force: false,
  });
  assert.strictEqual(scenario.requests[2].url, "/api/listserv/index");
  assert.deepStrictEqual(bodyOf(scenario.requests[2]), {
    rebuild: true,
    rebuild_embeddings: false,
    rebuild_all: true,
    resume: false,
    stale_only: true,
    batch: 12,
  });

  // Empty date bounds remain an unrestricted date selection.
  sync.elements.since.value = " \n\t";
  sync.elements.until.value = "  ";
  submit(sync);
  await new Promise(function (resolve) { setImmediate(resolve); });
  assert.strictEqual(scenario.requests.length, 4);
  assert.strictEqual(scenario.requests[3].url, "/api/listserv/sync");
  assert.deepStrictEqual(bodyOf(scenario.requests[3]), {
    list_id: "TEAM_A",
    dates: [],
    force: true,
  });

  // A second index submission verifies that an empty batch is omitted rather
  // than sent as an empty string or zero.
  index.elements.batch.value = "  ";
  submit(index);
  await new Promise(function (resolve) { setImmediate(resolve); });
  assert.strictEqual(scenario.requests.length, 5);
  assert.strictEqual(scenario.requests[4].url, "/api/listserv/index");
  assert.deepStrictEqual(bodyOf(scenario.requests[4]), {
    rebuild: true,
    rebuild_embeddings: false,
    rebuild_all: true,
    resume: false,
    stale_only: true,
  });
  assert.strictEqual(
    Object.prototype.hasOwnProperty.call(bodyOf(scenario.requests[4]), "batch"),
    false
  );

  assert.deepStrictEqual(
    sync.target.appended.map(function (node) { return node.attrs["hx-get"]; }),
    ["/jobs/job-1?format=html", "/jobs/job-4?format=html"]
  );
  assert.deepStrictEqual(
    parse.target.appended.map(function (node) { return node.attrs["hx-get"]; }),
    ["/jobs/job-2?format=html"]
  );
  for (const form of [sync, parse]) {
    assert.strictEqual(scenario.decoys.get(form.targetSelector).appended.length, 0);
  }
  assert.strictEqual(index.target.appended.length, 2);
  assert.deepStrictEqual(
    index.target.appended.map(function (node) { return node.attrs["hx-get"]; }),
    ["/jobs/job-3?format=html", "/jobs/job-5?format=html"]
  );
  assert.strictEqual(scenario.decoys.get(index.targetSelector).appended.length, 0);
}

async function main() {
  await runScenario("loading");
  await runScenario("complete");
  console.log(
    "PASS: LISTSERV sync/parse/index forms submit JSON and use their own job targets"
  );
}

main().catch(function (error) {
  console.error("FAIL:", error && error.stack ? error.stack : error);
  process.exitCode = 1;
});

"use strict";

// Smoke-test the /sync page's form-to-JSON builders without a browser.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const SYNC_HUB_PATH = path.resolve(
  __dirname,
  "../../src/doc3gpp/web/static/js/sync_hub.js"
);

const builders = {};
global.window = globalThis;
global.window.bindJobPolling = function (form, options) {
  builders[form.id] = options.buildBody;
};

function makeForm(id, values) {
  return {
    id,
    querySelector: function (selector) {
      const match = /^input\[name="([^"]+)"\]$/.exec(selector);
      if (!match || !(match[1] in values)) return null;
      return { value: values[match[1]], checked: false };
    },
  };
}

const forms = [
  makeForm("sync-wis-form", {tsg: "r5"}),
  makeForm("parse-tdocs-form", {filter_meeting_id: "156"}),
];
global.document = {
  readyState: "loading",
  addEventListener: function (_event, callback) {
    callback();
  },
  querySelectorAll: function () {
    return forms;
  },
};

(0, eval)(fs.readFileSync(SYNC_HUB_PATH, "utf8"));

assert.deepStrictEqual(
  JSON.parse(builders["sync-wis-form"](forms[0])),
  {tsg: "r5"},
  "WI sync should submit only its TSG"
);
assert.strictEqual(
  JSON.parse(builders["parse-tdocs-form"](forms[1])).filter.meeting_id,
  156,
  "Meeting ID should be serialized as a number"
);

console.log("PASS: sync hub WI and Meeting ID body builders");

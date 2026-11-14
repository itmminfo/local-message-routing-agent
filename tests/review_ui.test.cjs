// Executes the shipped browser script with a minimal DOM and fake HTTP boundary.
// These behavioral checks complement, rather than replace, real-browser smoke.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const markup = fs.readFileSync(path.join(root, "app/web/static/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "app/web/static/review.js"), "utf8");
const storageKey = "local-review:/api/v1/review:preview";
const referenceKey = `${storageKey}:unresolved`;
const intentKey = `${storageKey}:send-intent`;
const departments = ["help-desk", "kadry", "it", "human-resources", "other"]
  .map((name) => ({ recipient: `${name}@example.com`, label: name }));
const record = (state = "pending_review", extra = {}) => ({
  id: "preview-1", version: 1, payload_digest: "a".repeat(64),
  email: "test@example.com", message: "<img src=x onerror=alert(1)> Original",
  state, created_at: "2026-11-14T13:00:00Z", expires_at: "2026-11-14T13:30:00Z",
  recipient: state === "pending_review" ? null : departments[0].recipient,
  suggestion: { status: "not_requested" }, message_id: null,
  attempts: ["sending", "sent", "failed_before_submit", "delivery_unknown"].includes(state)
    ? [{ id: "attempt-1", state }] : [],
  retry_allowed: state === "failed_before_submit", ...extra,
});
const http = (status, data = {}) => ({ responseStatus: status, responseData: data });
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Element {
  constructor() {
    this.value = ""; this.checked = false; this.hidden = false; this.disabled = false;
    this.dataset = {}; this.maxLength = 5000; this.children = []; this.listeners = new Map();
    this.textContent = "";
  }
  set innerHTML(_) { throw new Error("Unsafe HTML write"); }
  get childElementCount() { return this.children.length; }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  removeAttribute(name) { if (name === "data-state") delete this.dataset.state; }
  reportValidity() { return true; }
  focus() {}
  reset() {}
  emit(name) { return this.listeners.get(name)?.({ preventDefault() {} }); }
}

async function page(handler, storedId, referenceId, sessionHandler, initialStorage = [], storageFaults = {}) {
  const elements = Object.fromEntries([...markup.matchAll(/id="([^"]+)"/g)]
    .map((match) => [match[1], new Element()]));
  const storage = new Map(initialStorage);
  if (storedId) storage.set(storageKey, storedId);
  if (referenceId) storage.set(referenceKey, referenceId);
  const calls = [];
  const context = vm.createContext({
    document: {
      querySelector: () => ({ content: "/api/v1" }),
      getElementById: (id) => elements[id], createElement: () => new Element(),
    },
    sessionStorage: {
      setItem: (key, value) => {
        if (key === intentKey && storageFaults.write) throw new Error("storage write failed");
        storage.set(key, value);
      },
      getItem: (key) => {
        if (key === intentKey && storageFaults.read) throw new Error("storage read failed");
        return storage.get(key) ?? null;
      },
      removeItem: (key) => storage.delete(key),
    },
    fetch: async (url, options) => {
      const call = { url, ...options, body: options.body ? JSON.parse(options.body) : undefined };
      calls.push(call);
      const data = url.endsWith("/session")
        ? sessionHandler ? await sessionHandler(call) : { csrf_token: "test-csrf", departments }
        : await handler(call);
      return data?.responseStatus
        ? { ok: false, status: data.responseStatus, json: async () => data.responseData }
        : { ok: true, status: 200, json: async () => data };
    },
    setTimeout: () => 1, clearTimeout() {},
  });
  vm.runInContext(script, context);
  await tick();
  return { elements, storage, calls };
}

async function create(ui) {
  ui.elements["sender-email"].value = "test@example.com";
  ui.elements.message.value = record().message;
  await ui.elements["compose-form"].emit("submit");
}
async function confirm(ui) {
  ui.elements.department.value = departments[0].recipient;
  await ui.elements.department.emit("change");
  ui.elements.confirmed.checked = true;
  await ui.elements.confirmed.emit("change");
}
const posts = (ui) => ui.calls.filter((call) => call.method === "POST");

test("preview, explicit approval, literal text and high confidence do not auto-send", async () => {
  const ui = await page(async (call) => {
    if (call.url.endsWith("/suggest")) return record("pending_review", { suggestion: {
      status: "ready", recipient: departments[0].recipient, reason: "<script>bad()</script>",
      issue_quotes: ["<b>literal</b>"], routing_confidence: "high",
    } });
    if (call.url.endsWith("/approve")) return record("sent");
    return record();
  });
  await create(ui);
  assert.equal(posts(ui).length, 1);
  assert.equal(ui.elements["original-message"].textContent, record().message);
  assert.equal(ui.elements["original-message"].childElementCount, 0);
  assert.equal(ui.elements.department.value, "");
  assert.equal(ui.elements.confirmed.checked, false);
  await ui.elements["approval-form"].emit("submit");
  assert.equal(posts(ui).length, 1);
  await ui.elements["request-suggestion"].emit("click");
  assert.equal(ui.elements.department.value, "");
  assert.equal(ui.elements["suggestion-reason"].textContent, "<script>bad()</script>");
  await confirm(ui);
  await ui.elements["approval-form"].emit("submit");
  const approval = posts(ui).at(-1);
  assert.deepEqual(approval.body, { version: 1, payload_digest: "a".repeat(64),
    recipient: departments[0].recipient, confirmed: true });
  assert.equal(approval.headers["X-CSRF-Token"], "test-csrf");
  assert.deepEqual([...ui.storage.values()], ["preview-1"]);
});

test("manual approval can finish while model waits and late model cannot regress it", async () => {
  let finish;
  const waiting = new Promise((resolve) => { finish = resolve; });
  const ui = await page(async (call) => call.url.endsWith("/suggest") ? waiting
    : call.url.endsWith("/approve") ? record("sent") : record());
  await create(ui);
  const suggestion = ui.elements["request-suggestion"].emit("click");
  await confirm(ui);
  await ui.elements["approval-form"].emit("submit");
  finish(record("pending_review", { suggestion: { status: "ready" } }));
  await suggestion;
  assert.equal(ui.elements["state-label"].textContent, "Wysłano");
});

test("editing original invalidates preview and clears explicit confirmation", async () => {
  const ui = await page(async () => record());
  await create(ui); await confirm(ui);
  ui.elements.message.value = "Changed";
  await ui.elements.message.emit("input");
  await ui.elements["approval-form"].emit("submit");
  assert.equal(posts(ui).length, 1);
  assert.equal(ui.storage.has(storageKey), false);
  assert.equal(ui.elements.confirmed.checked, false);
});

test("reload delivery_unknown never permits retry even if retry flag is malformed", async () => {
  const ui = await page(async () => record("delivery_unknown", { retry_allowed: true }), "preview-1");
  assert.equal(ui.elements["retry-send"].hidden, true);
  await ui.elements["retry-send"].emit("click");
  assert.equal(posts(ui).length, 0);
});

test("bootstrap cannot race edits with an unfinished restore", async () => {
  let finish;
  const waiting = new Promise((resolve) => { finish = resolve; });
  const ui = await page(async () => waiting, "preview-1");
  assert.equal(ui.elements["create-preview"].disabled, true);
  assert.equal(ui.elements.message.disabled, true);
  finish(record()); await tick();
  assert.equal(ui.elements.message.disabled, false);
});

for (const failure of ["network", 500, 503, 403, 404]) {
  test(`restore ${failure} preserves the saved operation and blocks new creation`, async () => {
    const ui = await page(async () => {
      if (failure === "network") throw new Error("network unavailable");
      return http(failure);
    }, "preview-1");
    assert.equal(ui.storage.get(storageKey), "preview-1");
    assert.equal(ui.elements["create-preview"].disabled, true);
    assert.equal(ui.elements.message.disabled, true);
    await create(ui);
    assert.equal(posts(ui).length, 0);
    assert.equal(ui.elements["recovery-id"].textContent, "preview-1");
    assert.equal(ui.elements["abandon-context"].hidden, ![403, 404].includes(failure));
  });
}

test("transient restore can recover the same pending operation without creating a new one", async () => {
  let unavailable = true;
  const ui = await page(async () => {
    if (unavailable) throw new Error("restart outage");
    return record();
  }, "preview-1");
  assert.equal(ui.storage.get(storageKey), "preview-1");
  unavailable = false;
  await ui.elements["resolve-status"].emit("click");
  assert.equal(ui.elements["recovery-panel"].hidden, true);
  assert.equal(ui.elements["original-message"].textContent, record().message);
  assert.equal(ui.elements.message.disabled, false);
  assert.equal(ui.elements.confirmed.checked, false);
  assert.equal(posts(ui).length, 0);
});

for (const operation of ["approve", "retry"]) {
  test(`${operation} lost response and failed status read cannot unlock another preview`, async () => {
    let unavailable = false;
    const initial = operation === "approve" ? record() : record("failed_before_submit", {
      attempts: [{ id: "attempt-1", state: "failed_before_submit" }],
    });
    const ui = await page(async (call) => {
      if (call.url.endsWith(`/${operation}`)) {
        unavailable = true;
        throw new Error("HTTP response lost after server work");
      }
      if (unavailable) throw new Error("status read unavailable");
      return initial;
    }, operation === "retry" ? "preview-1" : undefined);
    if (operation === "approve") { await create(ui); await confirm(ui); }
    await ui.elements[operation === "approve" ? "approval-form" : "retry-send"]
      .emit(operation === "approve" ? "submit" : "click");
    const before = posts(ui).length;
    assert.equal(ui.storage.get(storageKey), "preview-1");
    assert.equal(ui.elements.message.disabled, true);
    assert.equal(ui.elements["create-preview"].disabled, true);
    assert.equal(ui.elements.confirmed.checked, false);
    await create(ui);
    await ui.elements["new-message"].emit("click");
    await ui.elements["approval-form"].emit("submit");
    await ui.elements["retry-send"].emit("click");
    assert.equal(posts(ui).length, before);
  });
}

for (const failure of ["read", "write"]) {
  test(`intent storage ${failure} failure prevents the SMTP mutation`, async () => {
    const faults = {};
    const ui = await page(async () => record(), undefined, undefined, undefined, [], faults);
    await create(ui); await confirm(ui);
    faults[failure] = true;
    await ui.elements["approval-form"].emit("submit");
    assert.equal(posts(ui).filter((call) => call.url.endsWith("/approve")).length, 0);
    assert.equal(ui.elements["approve-send"].disabled, true);
  });
}

test("fencing an unresolved approval closes the old ID before a fresh form is enabled", async () => {
  const ui = await page(async (call) => {
    if (call.url.endsWith("/approve")) throw new Error("queued approval");
    return call.url.endsWith("/reject") ? record("rejected") : record();
  });
  await create(ui); await confirm(ui);
  await ui.elements["approval-form"].emit("submit");
  assert.equal(ui.elements["withdraw-preview"].hidden, false);
  assert.equal(ui.storage.has(intentKey), true);
  await ui.elements["withdraw-preview"].emit("click");
  assert.equal(ui.elements["state-label"].textContent, "Odrzucono");
  assert.equal(ui.storage.has(intentKey), false);
  const cancel = posts(ui).at(-1);
  assert.equal(cancel.url, "/api/v1/review/previews/preview-1/reject");
  assert.deepEqual(cancel.body, { version: 1 });
  await ui.elements["new-message"].emit("click");
  assert.equal(ui.elements["create-preview"].disabled, false);
});

test("a virtual expired snapshot without an attempt requires a persisted fence too", async () => {
  const intent = JSON.stringify({ preview_id: "preview-1", operation: "approve", expected_attempt_id: null });
  const ui = await page(async (call) => call.url.endsWith("/reject")
    ? record("rejected") : record("expired"), "preview-1", undefined, undefined, [[intentKey, intent]]);
  assert.equal(ui.elements["create-preview"].disabled, true);
  assert.equal(ui.storage.get(intentKey), intent);
  await ui.elements["withdraw-preview"].emit("click");
  assert.equal(ui.elements["state-label"].textContent, "Odrzucono");
  assert.equal(ui.storage.has(intentKey), false);
});

test("a fence that loses to sending resolves the intent without enabling a new preview", async () => {
  let accepted = false;
  const ui = await page(async (call) => {
    if (call.url.endsWith("/approve")) throw new Error("lost approval");
    if (call.url.endsWith("/reject")) { accepted = true; return http(409); }
    return accepted ? record("sending") : record();
  });
  await create(ui); await confirm(ui);
  await ui.elements["approval-form"].emit("submit");
  await ui.elements["withdraw-preview"].emit("click");
  assert.equal(ui.elements["state-label"].textContent, "Wysyłanie");
  assert.equal(ui.storage.has(intentKey), false);
  assert.equal(ui.elements["new-message"].hidden, true);
  const count = posts(ui).length;
  await create(ui);
  assert.equal(posts(ui).length, count);
});

test("explicitly leaving an inaccessible intent archives its marker and keeps the warning reference", async () => {
  const intent = JSON.stringify({ preview_id: "preview-1", operation: "retry", expected_attempt_id: "attempt-1" });
  const ui = await page(async () => http(404), "preview-1", undefined, undefined, [[intentKey, intent]]);
  ui.elements["accept-context-loss"].checked = true;
  await ui.elements["accept-context-loss"].emit("change");
  await ui.elements["abandon-context"].emit("click");
  assert.equal(ui.storage.has(intentKey), false);
  assert.equal(ui.storage.get(`${intentKey}:archived:preview-1`), intent);
  assert.equal(ui.storage.get(referenceKey), "preview-1");
  assert.equal(ui.elements["create-preview"].disabled, false);
  assert.equal(posts(ui).length, 0);
});

test("a marker unreadable during bootstrap stays blocked after storage recovers until fenced", async () => {
  const intent = JSON.stringify({ preview_id: "preview-1", operation: "approve", expected_attempt_id: null });
  const faults = { read: true };
  const ui = await page(async () => record(), "preview-1", undefined, undefined,
    [[intentKey, intent]], faults);
  assert.equal(ui.elements["create-preview"].disabled, true);
  assert.equal(posts(ui).length, 0);
  faults.read = false;
  await ui.elements["resolve-status"].emit("click");
  assert.equal(ui.elements["recovery-panel"].hidden, false);
  assert.equal(ui.elements["create-preview"].disabled, true);
  assert.equal(ui.elements["withdraw-preview"].hidden, false);
  assert.equal(ui.storage.get(intentKey), intent);
});

test("inaccessible session requires a separate warning gesture and keeps the old ID reference", async () => {
  const ui = await page(async () => http(403), "preview-1");
  assert.equal(ui.storage.get(storageKey), "preview-1");
  await ui.elements["abandon-context"].emit("click");
  assert.equal(ui.elements["create-preview"].disabled, true);
  ui.elements["accept-context-loss"].checked = true;
  await ui.elements["accept-context-loss"].emit("change");
  await ui.elements["abandon-context"].emit("click");
  assert.equal(ui.storage.has(storageKey), false);
  assert.equal(ui.storage.get(referenceKey), "preview-1");
  assert.equal(ui.elements["previous-operation-id"].textContent, "preview-1");
  assert.equal(ui.elements["create-preview"].disabled, false);
  assert.equal(ui.elements.confirmed.checked, false);
  assert.equal(posts(ui).length, 0);
});

test("the previous inaccessible operation reference survives reload", async () => {
  const ui = await page(async () => record(), undefined, "preview-old");
  assert.equal(ui.elements["previous-operation-id"].textContent, "preview-old");
  assert.equal(ui.elements["previous-operation"].hidden, false);
});

test("each new manual retry binds to the last failed attempt visible to the operator", async () => {
  let retries = 0;
  const failed = (count) => record("failed_before_submit", {
    attempts: Array.from({ length: count }, (_, i) => ({ id: `attempt-${i + 1}`, state: "failed_before_submit" })),
  });
  const ui = await page(async (call) => call.url.endsWith("/retry")
    ? failed(++retries + 1) : failed(1), "preview-1");
  await ui.elements["retry-send"].emit("click");
  await ui.elements["retry-send"].emit("click");
  const requests = posts(ui);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].body.expected_attempt_id, "attempt-1");
  assert.equal(requests[1].body.expected_attempt_id, "attempt-2");
});

test("bootstrap session outage also preserves the operation and can recover without editing storage", async () => {
  let sessions = 0;
  const ui = await page(async () => record("sent"), "preview-1", undefined, async () =>
    ++sessions === 1 ? http(503) : { csrf_token: "fresh-csrf", departments });
  assert.equal(ui.storage.get(storageKey), "preview-1");
  assert.equal(ui.elements["create-preview"].disabled, true);
  assert.equal(ui.elements["resolve-status"].disabled, false);
  await ui.elements["resolve-status"].emit("click");
  assert.equal(ui.elements["recovery-panel"].hidden, true);
  assert.equal(ui.elements["state-label"].textContent, "Wysłano");
  assert.equal(ui.elements["new-message"].hidden, false);
  assert.equal(posts(ui).length, 0);
});

test("failed session refresh cannot silently abandon an inaccessible operation", async () => {
  let sessions = 0;
  const ui = await page(async () => http(404), "preview-1", undefined, async () =>
    ++sessions === 1 ? { csrf_token: "test-csrf", departments } : http(503));
  ui.elements["accept-context-loss"].checked = true;
  await ui.elements["accept-context-loss"].emit("change");
  await ui.elements["abandon-context"].emit("click");
  assert.equal(ui.storage.get(storageKey), "preview-1");
  assert.equal(ui.storage.has(referenceKey), false);
  assert.equal(ui.elements["create-preview"].disabled, true);
  assert.equal(posts(ui).length, 0);
});

test("successful state read resolves an approve response loss without another mutation", async () => {
  let delivered = false;
  const ui = await page(async (call) => {
    if (call.url.endsWith("/approve")) {
      delivered = true;
      throw new Error("lost successful response");
    }
    return delivered ? record("sent") : record();
  });
  await create(ui); await confirm(ui);
  await ui.elements["approval-form"].emit("submit");
  assert.equal(ui.elements["recovery-panel"].hidden, true);
  assert.equal(ui.elements["state-label"].textContent, "Wysłano");
  assert.equal(posts(ui).length, 2); // create + one approval, no resend.
});

test("a late proposal cannot hide the unresolved state from a failed status read", async () => {
  let finish;
  const waiting = new Promise((resolve) => { finish = resolve; });
  const ui = await page(async (call) => {
    if (call.url.endsWith("/suggest")) return waiting;
    if (call.method === "GET") throw new Error("state unavailable");
    return record();
  });
  await create(ui);
  const suggestion = ui.elements["request-suggestion"].emit("click");
  await ui.elements["refresh-status"].emit("click");
  finish(record("pending_review", { suggestion: { status: "ready" } }));
  await suggestion;
  assert.equal(ui.elements["recovery-panel"].hidden, false);
  assert.equal(ui.elements["state-label"].textContent, "Stan wymaga sprawdzenia");
  assert.equal(ui.elements["approve-send"].disabled, true);
});

test("a late failed read of an edited preview cannot replace the new preview ID", async () => {
  let failRead;
  let created = 0;
  const waiting = new Promise((_, reject) => { failRead = reject; });
  const ui = await page(async (call) => {
    if (call.method === "GET") return waiting;
    return record("pending_review", { id: `preview-${++created}` });
  });
  await create(ui);
  const oldRead = ui.elements["refresh-status"].emit("click");
  ui.elements.message.value = "Changed original";
  await ui.elements.message.emit("input");
  await create(ui);
  assert.equal(ui.storage.get(storageKey), "preview-2");
  failRead(new Error("old preview read failed late"));
  await oldRead;
  assert.equal(ui.storage.get(storageKey), "preview-2");
  assert.equal(ui.elements["recovery-panel"].hidden, true);
  assert.equal(ui.elements["state-label"].textContent, "Czeka na akceptację");
});

test("a pending snapshot cannot resolve a lost approval that may still enter the server", async () => {
  const ui = await page(async (call) => {
    if (call.url.endsWith("/approve")) throw new Error("response lost before queued approval starts");
    return record(); // The queued POST may enter begin_approval after this GET.
  });
  await create(ui); await confirm(ui);
  await ui.elements["approval-form"].emit("submit");
  assert.equal(ui.storage.get(storageKey), "preview-1");
  assert.equal(ui.elements["create-preview"].disabled, true);
  assert.equal(ui.elements.message.disabled, true);
  assert.equal(ui.elements["recovery-panel"].hidden, false);
});

test("the same failed-attempt snapshot cannot resolve a lost queued retry", async () => {
  const initial = record("failed_before_submit", {
    attempts: [{ id: "attempt-1", state: "failed_before_submit" }],
  });
  const ui = await page(async (call) => {
    if (call.url.endsWith("/retry")) throw new Error("response lost before queued retry starts");
    return initial;
  }, "preview-1");
  await ui.elements["retry-send"].emit("click");
  assert.equal(ui.storage.get(storageKey), "preview-1");
  assert.equal(ui.elements["retry-send"].disabled, true);
  assert.equal(ui.elements["recovery-panel"].hidden, false);
});

for (const operation of ["approve", "retry"]) {
  test(`reload preserves the unresolved ${operation} intent even when GET returns the old state`, async () => {
    const initial = operation === "approve" ? record() : record("failed_before_submit", {
      attempts: [{ id: "attempt-1", state: "failed_before_submit" }],
    });
    const ui = await page(async (call) => {
      if (call.url.endsWith(`/${operation}`)) throw new Error("queued request response lost");
      return initial;
    }, operation === "retry" ? "preview-1" : undefined);
    if (operation === "approve") { await create(ui); await confirm(ui); }
    await ui.elements[operation === "approve" ? "approval-form" : "retry-send"]
      .emit(operation === "approve" ? "submit" : "click");
    const reloaded = await page(async () => initial, undefined, undefined, undefined, ui.storage);
    assert.equal(reloaded.storage.get(storageKey), "preview-1");
    assert.equal(reloaded.elements["create-preview"].disabled, true);
    assert.equal(reloaded.elements["retry-send"].disabled, true);
    assert.equal(reloaded.elements["recovery-panel"].hidden, false);
  });
}

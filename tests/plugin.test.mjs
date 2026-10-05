import test from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, statSync, symlinkSync, chmodSync } from "node:fs"
import { execFileSync } from "node:child_process"
import { resolve } from "node:path"
import { ChildCore } from "../plugin/child.mjs"
import { createPlugin } from "../plugin/plugin.mjs"
import { V2_HOST_VERSIONS, transcriptWatermark } from "../plugin/native.mjs"

const hostVersion = V2_HOST_VERSIONS.at(-1)
const scope = { registryFingerprint: "registry", toolFingerprint: "tools", permissionFingerprint: "permissions", variant: "medium" }

const model = { providerID: "openai", id: "gpt-5.5", variants: { medium: {} }, limit: { context: 400000 } }
const tokens = { input: 2000, output: 10, reasoning: 3, cache: { read: 8000, write: 0 } }

async function eventually(condition, message = "expected state") {
  const until = Date.now() + 5000
  while (Date.now() < until) {
    const value = await condition()
    if (value) return value
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  throw Error(`Timed out: ${message}`)
}

function timerQueue() {
  const timers = new Set()
  return {
    setTimer(callback, delay) { const timer = { callback, delay, unref() {} }; timers.add(timer); return timer },
    clearTimer(timer) { timers.delete(timer) },
    fire(delay) { for (const timer of [...timers]) if (timer.delay === delay) { timers.delete(timer); timer.callback() } },
    timers,
  }
}

function fakeHost() {
  const sessions = new Map(), histories = new Map(), statuses = {}, requests = [], pending = new Map()
  let hooks, sequence = 0, held = false, callback = async () => {}, nextText = "Nothing to save.", callTokens = tokens, afterStep = async () => {}
  const host = {
    sessions, histories, statuses, requests, pending,
    set hooks(value) { hooks = value },
    set held(value) { held = value },
    set duringCall(value) { callback = value },
    set text(value) { nextText = value },
    set tokens(value) { callTokens = value },
    set afterStep(value) { afterStep = value },
    parent(id = "parent", parentID) {
      sessions.set(id, { id, title: id + " investigation", permission: [], model: { variant: "medium" }, ...scope, ...(parentID ? { parentID } : {}) })
      histories.set(id, [
        { info: { id: id + "_u", sessionID: id, role: "user", agent: "build", model: { providerID: "openai", modelID: "gpt-5.5", variant: "medium" } }, parts: [{ id: id + "_text", type: "text", text: "Fix the reusable ordering rule" }] },
        { info: { id: id + "_a", sessionID: id, role: "assistant", finish: "stop", tokens }, parts: [{ id: id + "_phase", type: "text", text: "Fixed", metadata: { openai: { phase: "final_answer" } } }, { id: id + "_old", type: "step-finish", tokens }] },
      ])
      statuses[id] = { type: "idle" }
    },
    client: { session: {
      get: async request => ({ data: sessions.get(request.path.id) }),
      messages: async request => ({ data: structuredClone(histories.get(request.path.id) || []) }),
      status: async () => ({ data: structuredClone(statuses) }),
      create: async request => {
        const id = "native_" + ++sequence
        sessions.set(id, { id, title: request.body.title, metadata: request.body.metadata, version: hostVersion }); histories.set(id, [])
        requests.push(["create", request])
        await hooks?.event({ event: { type: "session.created", properties: { info: sessions.get(id) } } })
        return { data: sessions.get(id) }
      },
      delete: async request => { sessions.delete(request.path.id); histories.delete(request.path.id); return { data: true } },
      fork: async request => {
        const id = "native_" + ++sequence
        requests.push(["fork", request])
        const history = structuredClone(histories.get(request.path.id))
        for (const message of history) {
          message.info.id = id + "_" + message.info.id; message.info.sessionID = id
          for (const part of message.parts) part.id = id + "_" + part.id
        }
        sessions.set(id, { id, title: "Fork", version: hostVersion }); histories.set(id, history)
        for (const message of history) await hooks.event({ event: { type: "message.updated", properties: { info: message.info } } })
        await hooks.event({ event: { type: "session.idle", properties: { sessionID: id } } })
        return { data: sessions.get(id) }
      },
      update: async request => { Object.assign(sessions.get(request.path.id), request.body); return { data: sessions.get(request.path.id) } },
      prompt: async request => {
        const id = request.path.id
        requests.push(["prompt", request]); statuses[id] = { type: "busy" }
        const history = histories.get(id)
        const user = { info: { id: id + "_user_" + history.length, sessionID: id, role: "user", agent: request.body.agent, model: { ...request.body.model, variant: request.body.variant } }, parts: request.body.parts.map((part, index) => ({ ...part, id: id + "_prompt_" + index })) }
        history.push(user)
        const assistant = { info: { id: id + "_assistant_" + history.length, sessionID: id, role: "assistant" }, parts: [] }
        history.push(assistant)
        const input = { sessionID: id, agent: request.body.agent, model, message: user.info, ...scope }
        try {
          const system = { system: ["host system", "skill descriptions"] }
          await hooks["experimental.chat.system.transform"](input, system)
          await hooks["chat.params"](input, { options: { instructions: system.system.join("\n"), promptCacheKey: id, reasoningEffort: "medium" } })
          await hooks["chat.headers"](input, { headers: { Authorization: "host-secret", "session-id": id } })
          await callback(id, input)
          if (held) await new Promise(resolve => pending.set(id, resolve))
          assistant.info.finish = "stop"; assistant.info.tokens = callTokens
          assistant.parts.push({ id: id + "_answer_" + history.length, type: "text", text: nextText }, { id: id + "_step_" + history.length, type: "step-finish", tokens: callTokens })
          await hooks.event({ event: { type: "message.part.updated", properties: { part: { ...assistant.parts.at(-1), sessionID: id, messageID: assistant.info.id } } } })
          await afterStep(id, input)
          return { data: structuredClone(assistant) }
        } finally { statuses[id] = { type: "idle" }; pending.delete(id) }
      },
      abort: async request => {
        requests.push(["abort", request]); statuses[request.path.id] = { type: "idle" }
        pending.get(request.path.id)?.()
        return { data: true }
      },
    } },
  }
  host.parent()
  return host
}

async function fixture(t, settings = "", host = fakeHost()) {
  const home = mkdtempSync("/tmp/opencode/skill-plugin-")
  writeFileSync(resolve(home, "settings.yaml"), "library:\n  root: skills\nnotifications:\n  enabled: false\ntriggers:\n  idle:\n    seconds: 0\n" + settings)
  const diagnostics = [], operations = [], timers = timerQueue()
  const core = new ChildCore({ home, diagnostic: message => diagnostics.push(message) })
  const recordingCore = {
    get closed() { return core.closed },
    async request(op, payload, identity) { const answer = await core.request(op, payload, identity); operations.push({ op, payload, answer }); return answer },
    dispose: () => core.dispose(),
  }
  const hooks = await createPlugin({ client: host.client, directory: home, serverUrl: "http://fake-host" }, { home }, { core: recordingCore, ...timers, diagnostic: message => diagnostics.push(message), hostVersion })
  host.hooks = hooks
  t.after(async () => { await hooks.dispose?.(); await core.dispose(); rmSync(home, { recursive: true, force: true }) })
  assert.equal(typeof hooks.event, "function", diagnostics.join("\n"))
  await hooks.config({})
  await eventually(() => operations.some(entry => entry.op === "claim"), "initial pump")
  return { home, core, hooks, host, diagnostics, operations, timers }
}

async function settle(fixture, id = "parent", observe = true) {
  const { hooks, host, timers } = fixture
  const info = host.histories.get(id).findLast(message => message.info.role === "user").info
  if (observe) {
    const input = { sessionID: id, agent: "build", model, message: info, ...scope }
    const system = { system: ["parent system", "parent skill descriptions"] }
    const options = { options: { instructions: system.system.join("\n"), promptCacheKey: id, reasoningEffort: "medium" } }
    const headers = { headers: { "session-id": id, Authorization: "host-secret" } }
    const before = structuredClone({ system, options, headers })
    await hooks["experimental.chat.system.transform"](input, system)
    await hooks["chat.params"](input, options)
    await hooks["chat.headers"](input, headers)
    assert.deepEqual({ system, options, headers }, before)
  }
  await hooks.event({ event: { type: "message.updated", properties: { info } } })
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: id } } })
  timers.fire(0)
}

test("wired native fork, inherited event exclusion, host evidence and restart idempotence", async t => {
  const f = await fixture(t)
  await settle(f)
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"), "native finish")
  assert.equal(finished.answer.outcome, "unchanged")
  assert.equal(f.host.requests.filter(([op]) => op === "fork").length, 1)
  assert.equal(f.operations.filter(entry => entry.op === "enqueue").length, 1)
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls.length, 1)
  assert.equal(review.calls[0].response.usage.input_tokens, 10000)
  assert.equal(review.calls[0].response.content, "Nothing to save.")
  assert.equal(review.calls[0].response_id, null)
  assert.equal(JSON.stringify(review).includes("host-secret"), false)
  assert.equal(review.evidence.filter(entry => entry.payload.part?.type === "step-finish").length, 1)
  assert.equal(review.calls[0].response.usage.provider_counter_availability, "unavailable")
  const reviewer = f.operations.find(entry => entry.op === "bind").payload.reviewerID
  await f.hooks.event({ event: { type: "session.idle", properties: { sessionID: reviewer } } })
  f.timers.fire(0)
  assert.equal(f.operations.filter(entry => entry.op === "enqueue").length, 1)
  await f.hooks.dispose()
  f.host.sessions.get(reviewer).title = "Renamed background session"
  f.host.sessions.get(reviewer).metadata = { operator: "retained" }
  const core = new ChildCore({ home: f.home })
  const timers = timerQueue()
  const restarted = await createPlugin({ client: f.host.client, directory: f.home, serverUrl: "http://fake-host" }, { home: f.home }, { core, ...timers, hostVersion })
  assert.deepEqual(f.host.sessions.get(reviewer).metadata, { operator: "retained", automation: {
    owner: "skill-learn", kind: "skill-review", reviewID: review.id, suppressAudio: true, suppressMemoryCollection: true,
  } })
  assert.equal(f.host.sessions.get(reviewer).title, "Renamed background session")
  t.after(async () => { await restarted.dispose(); await core.dispose() })
  await restarted.event({ event: { type: "session.idle", properties: { sessionID: reviewer } } })
  timers.fire(0)
  assert.equal((await core.request("state")).reviewers.filter(row => row.id === review.id).length, 1)
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, 1)
})

test("same-parent supersession aborts reviewer only and keeps independent queued work", async t => {
  const f = await fixture(t)
  f.host.held = true
  f.host.text = '{"changes":[{"name":"cancelled-write","action":"create","content":"Use for cancelled work."}]}'
  await settle(f)
  const first = await eventually(() => f.operations.find(entry => entry.op === "admit"), "first dispatch")
  f.host.parent("other")
  await settle(f, "other")
  await eventually(() => f.operations.filter(entry => entry.op === "enqueue").length === 2)
  f.host.histories.get("parent").push({ info: { id: "new_user", sessionID: "parent", role: "user", agent: "build", model: { providerID: "openai", modelID: "gpt-5.5", variant: "medium" } }, parts: [{ id: "new_text", type: "text", text: "Correct the rule" }] })
  await settle(f)
  await eventually(() => f.operations.some(entry => entry.op === "cancel" || entry.op === "finish"))
  assert.equal((await f.core.request("show", { id: first.payload.reviewID })).outcome, "cancelled")
  assert.equal(existsSync(resolve(f.home, "skills/cancelled-write")), false)
  assert.ok(f.host.requests.filter(([op]) => op === "abort").every(([, request]) => request.path.id.startsWith("native_")))
  f.host.held = false; f.host.text = "Nothing to save."
  f.timers.fire(5000)
  await eventually(() => f.operations.filter(entry => entry.op === "finish").length >= 2 || f.operations.some(entry => entry.op === "finish" && entry.payload.reviewID !== first.payload.reviewID), "independent queued dispatch")
})

test("progressive snapshot skill loading, root mismatch and reviewer-only execution guards", async t => {
  const f = await fixture(t)
  mkdirSync(resolve(f.home, "skills/procedure"), { recursive: true })
  writeFileSync(resolve(f.home, "skills/procedure/SKILL.md"), "Current disk body")
  f.host.duringCall = async id => {
    for (const tool of ["bash", "read", "write", "user_memory", "project_memory", "mcp_side_effect"]) {
      await assert.rejects(f.hooks["tool.execute.before"]({ sessionID: id, tool }), /only load skills/)
      await f.hooks["tool.execute.before"]({ sessionID: "parent", tool })
    }
    await f.hooks["tool.execute.before"]({ sessionID: id, tool: "skill" })
    await f.hooks["tool.execute.after"]({ sessionID: id, tool: "skill", args: { name: "procedure" } }, { metadata: { dir: resolve(f.home, "skills/procedure") }, output: "Old snapshot body" })
    await assert.rejects(f.hooks["tool.execute.after"]({ sessionID: id, tool: "skill", args: { name: "procedure" } }, { metadata: { dir: f.home }, output: "Wrong root body" }), /different library root/)
  }
  await settle(f)
  const completed = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(completed.answer.outcome, "unchanged")
  assert.equal(f.operations.filter(entry => entry.op === "skill-loaded").length, 2)
  const load = f.operations.find(entry => entry.op === "skill-loaded")
  assert.equal(load.payload.body, "Old snapshot body")
  assert.equal(readFileSync(resolve(f.home, "skills/procedure/SKILL.md"), "utf8"), "Current disk body")
  assert.equal(f.host.requests.find(([op]) => op === "prompt")[1].body.parts[0].text.includes("Current disk body"), false)
})

test("forced digest and delegate records do not execute historical tools", async t => {
  const f = await fixture(t, "review:\n  contextMode: digest\n")
  f.host.parent("delegate", "parent")
  await settle(f, "delegate")
  await eventually(() => f.operations.some(entry => entry.op === "enqueue" && entry.answer.disposition === "not-reviewed"))
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, 0)
  await settle(f)
  const completed = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(completed.answer.outcome, "unchanged")
  assert.equal(f.host.requests.filter(([op]) => op === "fork").length, 0)
  const prompt = f.host.requests.find(([op]) => op === "prompt")[1]
  assert.deepEqual(prompt.body.parts.map(part => part.type), ["text"])
  assert.match(prompt.body.parts[0].text, /\[user\]/)
})

test("the call reaching its input budget completes before reviewer-only next-call denial", async t => {
  const f = await fixture(t, "review:\n  maxInputTokens: 10000\n")
  f.host.afterStep = async (id, input) => {
    await assert.rejects(f.hooks["chat.params"](input, { options: { instructions: "review system", promptCacheKey: id } }), /input-budget/)
    await f.hooks["chat.params"]({ ...input, sessionID: "parent" }, { options: {} })
  }
  await settle(f)
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls.length, 1)
  assert.equal(review.calls[0].call_status, "completed")
  assert.equal(review.calls[0].response.usage.input_tokens, 10000)
  assert.match(review.error, /input-budget/)
})

async function observeSmallProbeParent(f) {
  const info = f.host.histories.get("parent")[0].info
  f.host.histories.get("parent")[1].info.tokens = { input: 500, output: 10, reasoning: 3, cache: { read: 0, write: 0 } }
  f.host.tokens = f.host.histories.get("parent")[1].info.tokens
  const input = { sessionID: "parent", agent: "build", model, message: info, ...scope }
  await f.hooks["experimental.chat.system.transform"](input, { system: ["parent system"] })
  await f.hooks["chat.params"](input, { options: { instructions: "parent system", promptCacheKey: "parent", reasoningEffort: "medium" } })
  await f.hooks["chat.headers"](input, { headers: { "session-id": "parent" } })
}

test("explicit native diagnostic admits at most two small calls and cannot publish or use tools", async t => {
  const f = await fixture(t)
  await observeSmallProbeParent(f)
  f.host.text = '{"changes":[{"name":"probe-write","action":"create","content":"Use for work."}]}'
  f.host.duringCall = async id => {
    await assert.rejects(f.hooks["tool.execute.before"]({ sessionID: id, tool: "skill" }), /only load skills/)
  }
  await f.core.request("request-probe", { parentID: "parent", mode: "fork" })
  f.timers.fire(5000)
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls.length, 2)
  assert.ok(review.calls.every(call => call.call_status === "completed"))
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, 2)
  assert.equal(existsSync(resolve(f.home, "skills/probe-write")), false)
  assert.equal(review.evidence.find(entry => entry.kind === "cache-diagnostic").payload.publication, false)
})

test("a host retry is vetoed before another diagnostic provider dispatch", async t => {
  const f = await fixture(t)
  await observeSmallProbeParent(f)
  let executions = 0
  f.host.duringCall = async (id, input) => {
    executions++
    await f.hooks.event({ event: { type: "session.status", properties: { sessionID: id, status: { type: "retry" } } } })
    await assert.rejects(f.hooks["chat.params"](input, { options: {} }), /retries are disabled/)
  }
  await f.core.request("request-probe", { parentID: "parent", mode: "fork" })
  f.timers.fire(5000)
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(executions, 1)
  assert.equal(review.calls.length, 1)
  assert.match(review.error, /retries are disabled/)
})

test("turn trigger waits for settlement and repeated user delivery does not count twice", async t => {
  const f = await fixture(t, "")
  // Exercise the configured independent switches through a separate instance.
  await f.hooks.dispose()
  writeFileSync(resolve(f.home, "settings.yaml"), "library:\n  root: skills\nnotifications:\n  enabled: false\ntriggers:\n  idle:\n    enabled: false\n  turns:\n    count: 2\n")
  const core = new ChildCore({ home: f.home }), timers = timerQueue()
  const hooks = await createPlugin({ client: f.host.client, directory: f.home }, { home: f.home }, { core, ...timers, hostVersion })
  f.host.hooks = hooks
  t.after(async () => { await hooks.dispose(); await core.dispose() })
  const info = f.host.histories.get("parent")[0].info
  for (let i = 0; i < 2; i++) await hooks.event({ event: { type: "message.updated", properties: { info } } })
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "parent" } } })
  assert.equal([...timers.timers].some(timer => timer.delay === 0), false)
  await hooks.event({ event: { type: "message.updated", properties: { info: { ...info, id: "second-user" } } } })
  assert.equal([...timers.timers].some(timer => timer.delay === 0), false)
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "parent" } } })
  assert.equal([...timers.timers].some(timer => timer.delay === 0), true)
})

test("child transports large stdin, isolates errors and terminates on disposal or failure", async t => {
  const home = mkdtempSync("/tmp/opencode/skill-child-")
  writeFileSync(resolve(home, "settings.yaml"), "library:\n  root: skills\nnotifications:\n  enabled: false\n")
  const core = new ChildCore({ home })
  t.after(async () => { await core.dispose(); rmSync(home, { recursive: true, force: true }) })
  const evidence = "large stdin evidence".repeat(100000)
  const answer = await core.request("enqueue", { harness: "opencode", hostID: "host", sessionID: "parent", watermark: "large", messages: [{ info: { id: "u", role: "user" }, parts: [{ type: "text", text: evidence }] }] })
  assert.equal((await core.request("show", { id: answer.reviewId })).context.messages[0].parts[0].text, evidence)
  assert.equal(core.child.spawnargs.some(value => value.includes(evidence.slice(0, 200))), false)
  await assert.rejects(core.request("unknown-op"), /Unknown operation/)
  assert.equal((await core.request("hello")).version, 1)
  await core.dispose()
  assert.notEqual(core.child.exitCode, null)
  const broken = new ChildCore({ home, python: "/nonexistent/python" })
  t.after(() => broken.dispose())
  await assert.rejects(broken.request("hello"), /ENOENT|unavailable/)
  const crashed = new ChildCore({ home })
  await crashed.request("hello")
  const request = crashed.request("show", { id: "missing" })
  crashed.child.kill("SIGKILL")
  await assert.rejects(request)
  await crashed.dispose()
})

test("unexpected stdout and unsupported response versions disable and terminate the child", async t => {
  const home = mkdtempSync("/tmp/opencode/skill-framing-")
  t.after(() => rmSync(home, { recursive: true, force: true }))
  for (const value of ["noise", "version"]) {
    const runtime = resolve(home, value), packageDirectory = resolve(runtime, "skill_learn")
    mkdirSync(packageDirectory, { recursive: true })
    writeFileSync(resolve(packageDirectory, "__init__.py"), "")
    writeFileSync(resolve(packageDirectory, "__main__.py"), "import json,sys,time\nrequest=json.loads(sys.stdin.readline())\nprint(" + (value === "noise" ? "'unexpected diagnostic stdout'" : "json.dumps({**request,'version':2,'ok':True,'result':{}})") + ",flush=True)\ntime.sleep(60)\n")
    const core = new ChildCore({ home, runtime })
    t.after(() => core.dispose())
    await assert.rejects(core.request("hello"))
    assert.equal(core.closed, true)
    await eventually(() => core.child.signalCode || core.child.exitCode !== null, "protocol child termination")
  }
})

test("bad settings or missing Python disables learning without installing parent hooks", async t => {
  const home = mkdtempSync("/tmp/opencode/skill-disabled-")
  t.after(() => rmSync(home, { recursive: true, force: true }))
  writeFileSync(resolve(home, "settings.yaml"), "llm:\n  apiKey: removed\n")
  const diagnostics = []
  assert.deepEqual(await createPlugin({ directory: home, client: fakeHost().client }, { home }, { hostVersion, diagnostic: message => diagnostics.push(message) }), {})
  assert.deepEqual(await createPlugin({ directory: home, client: fakeHost().client }, { home, python: "/nonexistent/python" }, { hostVersion, diagnostic: message => diagnostics.push(message) }), {})
  assert.ok(diagnostics.some(message => message.includes("learning disabled")))
})

function expireLease(home, reviewID) {
  execFileSync("python3", ["-c", "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('UPDATE review_state SET lease_until=0 WHERE id=?',(sys.argv[2],)); c.commit()", resolve(home, "runtime.sqlite"), reviewID])
}

test("restart collects a finished expired native reviewer once without prompting", async t => {
  const f = await fixture(t)
  const claim = f.operations.find(entry => entry.op === "claim").payload
  const queued = await f.core.request("enqueue", { harness: "opencode", hostID: claim.hostID, hostPID: process.pid, sessionID: "parent", watermark: "saved", messages: [] })
  await f.core.request("claim", { hostID: claim.hostID, owner: "lost-owner" })
  await f.core.request("bind", { reviewID: queued.reviewId, reviewerID: "saved-native", owner: "lost-owner", inheritedIDs: ["inherited"] })
  await f.core.request("admit", { reviewID: queued.reviewId, reviewerID: "saved-native", owner: "lost-owner", assistantID: "saved-assistant" })
  f.host.sessions.set("saved-native", { id: "saved-native", title: "Saved reviewer" })
  f.host.histories.set("saved-native", [
    { info: { id: "inherited", role: "assistant", finish: "stop" }, parts: [{ id: "inherited-step", type: "step-finish", tokens: { ...tokens, input: 999999 } }] },
    { info: { id: "saved-assistant", role: "assistant", finish: "stop" }, parts: [{ id: "saved-text", type: "text", text: "Nothing to save." }, { id: "saved-step", type: "step-finish", tokens }] },
  ])
  f.host.statuses["saved-native"] = { type: "idle" }
  expireLease(f.home, queued.reviewId)
  f.timers.fire(5000)
  await eventually(() => f.operations.find(entry => entry.op === "reconcile" && entry.payload.state === "finished"))
  const review = await f.core.request("show", { id: queued.reviewId })
  assert.equal(review.outcome, "unchanged")
  assert.equal(review.calls.length, 1)
  assert.equal(review.calls[0].response.usage.input_tokens, 10000)
  assert.equal(f.host.requests.some(([op]) => op === "prompt"), false)
  assert.equal(f.operations.filter(entry => entry.op === "reconcile" && entry.payload.state === "finished").length, 1)
})

test("a live unbound claim is retained, ambiguous recovery is surfaced, missing work releases the queue", async t => {
  const f = await fixture(t)
  const claim = f.operations.find(entry => entry.op === "claim").payload
  const queued = await f.core.request("enqueue", { harness: "opencode", hostID: claim.hostID, hostPID: process.pid, sessionID: "parent", watermark: "saved", messages: [] })
  await f.core.request("claim", { hostID: claim.hostID, owner: "lost-owner" })
  const secondCore = new ChildCore({ home: f.home }), timers = timerQueue()
  const second = await createPlugin({ client: f.host.client, directory: f.home, serverUrl: "http://fake-host" }, { home: f.home }, { core: secondCore, ...timers, hostVersion })
  assert.equal((await f.core.request("show", { id: queued.reviewId })).status, "running")
  await second.dispose(); await secondCore.dispose()
  await f.core.request("bind", { reviewID: queued.reviewId, reviewerID: "saved-native", owner: "lost-owner", inheritedIDs: [] })
  f.host.sessions.set("saved-native", { id: "saved-native" }); f.host.histories.set("saved-native", [])
  const status = f.host.client.session.status
  f.host.client.session.status = async () => ({ error: { message: "host state unavailable" } })
  expireLease(f.home, queued.reviewId)
  f.timers.fire(5000)
  await eventually(() => f.operations.find(entry => entry.op === "reconcile" && entry.payload.state === "ambiguous"))
  assert.equal((await f.core.request("show", { id: queued.reviewId })).recovery_state, "recovery-needed")
  // Both preload and claim reconcile during a poll; finish it before changing the host fixture.
  await eventually(() => [...f.timers.timers].some(timer => timer.delay === 5000))
  f.host.client.session.status = status; f.host.sessions.delete("saved-native")
  f.timers.fire(5000)
  await eventually(() => f.operations.find(entry => entry.op === "reconcile" && entry.payload.state === "missing"))
  assert.equal((await f.core.request("show", { id: queued.reviewId })).outcome, "failed")
  f.host.parent("other")
  await settle(f, "other")
  await eventually(() => f.operations.find(entry => entry.op === "finish"), "queue after recovery")
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, 1)
})

test("child crash aborts only internal execution and ordinary parent hooks remain usable", async t => {
  const home = mkdtempSync("/tmp/opencode/skill-crash-")
  writeFileSync(resolve(home, "settings.yaml"), "library:\n  root: skills\nnotifications:\n  enabled: false\ntriggers:\n  idle:\n    seconds: 0\n")
  const host = fakeHost(), timers = timerQueue(), diagnostics = []
  let core
  const hooks = await createPlugin({ directory: home, client: host.client }, { home }, { ...timers, hostVersion,
    diagnostic: message => diagnostics.push(message), startCore: options => (core = new ChildCore(options)) })
  host.hooks = hooks; host.held = true
  t.after(async () => { await hooks.dispose(); await core.dispose(); rmSync(home, { recursive: true, force: true }) })
  await settle({ hooks, host, timers })
  const id = await eventually(() => [...host.pending.keys()][0], "running reviewer")
  core.child.kill("SIGKILL")
  await eventually(() => host.requests.some(([op]) => op === "abort"))
  assert.ok(host.requests.filter(([op]) => op === "abort").every(([, request]) => request.path.id === id))
  await hooks["chat.params"]({ sessionID: "parent", agent: "build", model, message: { id: "ordinary" } }, { options: {} })
  await hooks["tool.execute.before"]({ sessionID: "parent", tool: "bash" })
  assert.ok(diagnostics.some(message => /child exited|unavailable/.test(message)))
})

test("isolated copy install bundles runtime and preserves settings and unrelated entries", async t => {
  const directory = mkdtempSync("/tmp/opencode/skill install-\"")
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const destination = resolve(directory, "plugins"), home = resolve(directory, "home")
  mkdirSync(destination); mkdirSync(home)
  const settings = "library:\n  root: skills\nnotifications:\n  enabled: false\n"
  writeFileSync(resolve(home, "settings.yaml"), settings)
  writeFileSync(resolve(destination, "unrelated.js"), "export default () => ({})")
  const installer = resolve(import.meta.dirname, "../install.sh")
  for (let i = 0; i < 2; i++) execFileSync(installer, ["--dest", destination, "--home", home, "--python", "python3"], { cwd: directory })
  assert.equal(readFileSync(resolve(home, "settings.yaml"), "utf8"), settings)
  assert.equal(readFileSync(resolve(destination, "unrelated.js"), "utf8"), "export default () => ({})")
  const entry = await import(resolve(destination, "skill-learn/index.js"))
  assert.equal(entry.default.id, "skill-learn")
  assert.equal(typeof entry.default.setup, "function")
  const helpers = resolve(destination, "skill-learn")
  for (const cue of ["initiated", "started", "finished", "unchanged", "failed", "cancelled"]) {
    assert.equal(readFileSync(resolve(helpers, "runtime/skill_learn/sounds", `${cue}.wav`)).subarray(0, 4).toString(), "RIFF")
  }
  const { ChildCore: InstalledCore, pythonRuntime } = await import(resolve(helpers, "child.mjs"))
  assert.equal(pythonRuntime(), resolve(helpers, "runtime"))
  assert.equal(existsSync(resolve(destination, "skill-learn.js")), false)
  assert.equal(existsSync(resolve(directory, "skill-learn-runtime")), false)
  assert.equal(JSON.parse(readFileSync(resolve(helpers, "package.json"))).exports, "./index.js")
  const { OpenCode } = await import(resolve(helpers, "node_modules/@opencode/client/dist/promise/index.js"))
  assert.equal(typeof OpenCode.make({ baseUrl: "http://offline-host" }).session.fork, "function")
  const child = new InstalledCore({ home })
  assert.equal((await child.request("hello")).version, 1)
  await child.dispose()
})

test("Bash replacement keeps settings and databases while removing other old files through a symlinked discovery root", async t => {
  const directory = mkdtempSync("/tmp/opencode/skill-relocate-")
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const config = resolve(directory, "config"), harness = resolve(directory, "harness"), destination = resolve(config, "plugins")
  mkdirSync(config); mkdirSync(harness); mkdirSync(resolve(harness, "plugins"))
  symlinkSync(resolve(harness, "plugins"), destination)
  const plugin = resolve(harness, "plugins/skill-learn")
  mkdirSync(plugin)
  const settings = "library:\n  root: skills\nnotifications:\n  enabled: false\nreports:\n  root: reports\n"
  writeFileSync(resolve(plugin, "settings.yaml"), settings)
  mkdirSync(resolve(plugin, "reports")); mkdirSync(resolve(plugin, "skills/kept"), { recursive: true })
  writeFileSync(resolve(plugin, "reports/operator-note.txt"), "Obsolete report notes")
  writeFileSync(resolve(plugin, "skills/kept/SKILL.md"), "Obsolete plugin-local skill")
  writeFileSync(resolve(plugin, "operator-note.txt"), "Obsolete operator file")
  writeFileSync(resolve(plugin, "stale-helper.mjs"), "Obsolete source")
  writeFileSync(resolve(destination, "unrelated.js"), "Unrelated plugin")
  const reviewID = execFileSync("python3", ["-c", "from skill_learn.native_store import NativeStore; import sys; s=NativeStore(sys.argv[1]); submission=s.insert_submission({'harness':'opencode','session_id':'parent','watermark':'one','delegate_depth':0,'messages':[]}); review=s.add_review(submission,'opencode','parent','model'); s.add_evidence(review,'kept',{'text':'original evidence'}); s.begin_operation('kept-operation','hello',{}); s.close(); print(review)", plugin], { cwd: resolve(import.meta.dirname, "../plugin/server") }).toString().trim()
  const savedFiles = {
    "state.backup.sqlite": readFileSync(resolve(plugin, "state.sqlite")),
    "offline.sqlite-wal": Buffer.from("Retain WAL bytes"),
    "offline.sqlite-shm": Buffer.from("Retain SHM bytes"),
    "operator.db": readFileSync(resolve(plugin, "state.sqlite")),
    "operator.db-journal": Buffer.from("Retain journal bytes"),
  }
  for (const [name, content] of Object.entries(savedFiles)) writeFileSync(resolve(plugin, name), content)
  const databaseNames = ["state.sqlite", "runtime.sqlite", ...Object.keys(savedFiles)]
  const inode = Object.fromEntries([...databaseNames, "settings.yaml"].map(name => [name, statSync(resolve(plugin, name)).ino]))
  const installer = resolve(import.meta.dirname, "../install.sh")
  const env = { ...process.env, OPENCODE_CONFIG_DIR: config }
  delete env.SKILL_LEARN_HOME
  for (let i = 0; i < 2; i++) execFileSync(installer, ["--dest", destination], { env })
  assert.equal(existsSync(resolve(destination, "skill-learn.js")), false)
  assert.equal(readFileSync(resolve(plugin, "settings.yaml"), "utf8"), settings)
  for (const name of [...databaseNames, "settings.yaml"]) assert.equal(statSync(resolve(plugin, name)).ino, inode[name])
  for (const [name, content] of Object.entries(savedFiles)) assert.deepEqual(readFileSync(resolve(plugin, name)), content)
  for (const name of ["reports", "skills", "operator-note.txt", "stale-helper.mjs"]) assert.equal(existsSync(resolve(plugin, name)), false)
  assert.equal(readFileSync(resolve(destination, "unrelated.js"), "utf8"), "Unrelated plugin")
  assert.ok(readFileSync(resolve(plugin, "index.js"), "utf8").includes(JSON.stringify(plugin)))
  const output = execFileSync("python3", ["-c", "from skill_learn.settings import DEFAULT_HOME; from skill_learn.native_store import NativeStore; import sys; print(DEFAULT_HOME); s=NativeStore(DEFAULT_HOME); assert s.evidence(sys.argv[1])[0]['payload']['text']=='original evidence'; assert s._connection.execute(\"SELECT count(*) FROM runtime.operations WHERE id='kept-operation'\").fetchone()[0]==1; s.close()", reviewID], { cwd: resolve(plugin, "runtime"), env }).toString()
  assert.equal(output.trim(), plugin)
  const defaultHome = execFileSync(process.execPath, ["--input-type=module", "-e", `import { createPlugin } from ${JSON.stringify(resolve(plugin, "plugin.mjs"))}; await createPlugin({directory: '.', client: {}}, {}, {hostVersion: ${JSON.stringify(hostVersion)}, startCore: options => { console.log(options.home); throw Error('stop'); }, diagnostic: () => {}})`], { env }).toString()
  assert.equal(defaultHome.trim(), plugin)
  const report = JSON.parse(execFileSync("python3", ["-m", "skill_learn", "report"], { cwd: resolve(plugin, "runtime"), env }).toString())
  assert.equal(report.index, resolve(plugin, "reports/index.html"))
})

test("dependency preparation failure preserves the existing plugin before replacement", async t => {
  const directory = mkdtempSync("/tmp/opencode/skill-install-failed-")
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const destination = resolve(directory, "plugins"), plugin = resolve(destination, "skill-learn")
  mkdirSync(plugin, { recursive: true })
  writeFileSync(resolve(plugin, "settings.yaml"), "Existing destination settings")
  writeFileSync(resolve(plugin, "state.sqlite"), "Existing database bytes")
  writeFileSync(resolve(plugin, "old-helper.mjs"), "Existing source bytes")
  const installer = resolve(import.meta.dirname, "../install.sh")
  const binaries = resolve(directory, "bin")
  mkdirSync(binaries)
  writeFileSync(resolve(binaries, "npm"), "#!/bin/sh\nexit 42\n")
  chmodSync(resolve(binaries, "npm"), 0o755)
  assert.throws(() => execFileSync(installer, ["--dest", destination], { env: { ...process.env, PATH: binaries + ":" + process.env.PATH }, stdio: "pipe" }))
  assert.equal(readFileSync(resolve(plugin, "settings.yaml"), "utf8"), "Existing destination settings")
  assert.equal(readFileSync(resolve(plugin, "state.sqlite"), "utf8"), "Existing database bytes")
  assert.equal(readFileSync(resolve(plugin, "old-helper.mjs"), "utf8"), "Existing source bytes")
})

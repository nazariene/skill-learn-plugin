import test from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from "node:fs"
import { resolve } from "node:path"
import { ChildCore } from "../plugin/child.mjs"
import { setupV2, connectV2Host, projectV2Message } from "../plugin/v2.mjs"
import { V2_HOST_VERSIONS } from "../plugin/native.mjs"

const model = { id: "gpt-5.5", providerID: "openai", variants: [{ id: "medium", settings: { reasoningEffort: "medium" } }], limit: { context: 400000 }, settings: { baseURL: "http://native-route" } }
const selection = { id: model.id, providerID: model.providerID, variant: "medium" }
const tokens = { input: 500, output: 10, reasoning: 3, cache: { read: 0, write: 0 } }
async function eventually(condition) {
  const until = Date.now() + 5000
  while (Date.now() < until) {
    const answer = await condition()
    if (answer) return answer
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  throw new Error("Expected V2 state did not settle")
}

function hostFixture(directory, version) {
  const sessions = new Map(), history = new Map(), active = {}, requests = [], hooks = new Map(), skills = new Map()
  const events = [], pending = new Map()
  let wake, sequence = 0, held = false, steps = 1, duringCall = async () => {}
  const emit = (type, data, location = { directory }) => {
    events.push({ type, data, location }); wake?.(); wake = undefined
  }
  const callHook = async (domain, name, event) => { for (const hook of hooks.get(`${domain}:${name}`) || []) await hook(event) }
  const hook = domain => async (name, callback) => {
    const key = `${domain}:${name}`, list = hooks.get(key) || []
    list.push(callback); hooks.set(key, list)
    return { dispose: async () => { hooks.set(key, list.filter(value => value !== callback)) } }
  }
  const parent = (id = "parent", parentID) => {
    sessions.set(id, { id, title: "Ordering investigation", agent: "build", model: selection, permissions: [], location: { directory }, ...(parentID ? { parentID } : {}) })
    history.set(id, [
      { id: `${id}_user`, type: "user", text: "Fix the reusable ordering rule", time: { created: 1 } },
      { id: `${id}_assistant`, type: "assistant", agent: "build", model: selection, time: { created: 2, completed: 3 }, finish: "stop", tokens,
        content: [{ type: "reasoning", text: "", state: { encryptedContent: "opaque" } }, { type: "text", text: "Fixed", state: { phase: "final_answer" } }] },
    ])
  }
  const session = {
    hook: hook("session"),
    get: async ({ sessionID }) => { if (!sessions.has(sessionID)) throw Error("not found"); return structuredClone(sessions.get(sessionID)) },
    context: async ({ sessionID }) => {
      requests.push(["context", { sessionID }])
      const messages = history.get(sessionID)
      const boundary = messages.findLastIndex(message => message.type === "compaction" && message.status === "completed")
      return structuredClone(messages.slice(Math.max(0, boundary)))
    },
    create: async input => {
      const id = `review_${++sequence}`
      sessions.set(id, { id, title: input.title, metadata: input.metadata, agent: "build", model: selection, location: input.location, permissions: [] }); history.set(id, [])
      requests.push(["create", input]); return structuredClone(sessions.get(id))
    },
    update: async input => {
      const allowed = version === "2.0.24" ? ["title", "metadata", "permissions"] : ["title", "permissions"]
      Object.assign(sessions.get(input.sessionID), Object.fromEntries(Object.entries(input).filter(([key, value]) => allowed.includes(key) && value !== undefined)))
      requests.push(["context-update", input])
    },
    switchAgent: async input => { sessions.get(input.sessionID).agent = input.agent; requests.push(["agent", input]) },
    switchModel: async input => { sessions.get(input.sessionID).model = input.model; requests.push(["model", input]) },
    prompt: async input => {
      const metadata = sessions.get(input.sessionID).metadata.automation
      assert.equal(metadata.owner, "skill-learn")
      assert.equal(metadata.kind, "skill-review")
      assert.equal(metadata.suppressAudio, true)
      assert.equal(metadata.suppressMemoryCollection, true)
      requests.push(["prompt", input]); active[input.sessionID] = { type: "running" }
      history.get(input.sessionID).push({ id: `user_${++sequence}`, type: "user", text: input.text, time: { created: sequence } })
      return { id: `inbox_${sequence}`, type: "user" }
    },
    wait: async ({ sessionID }) => {
      requests.push(["wait", { sessionID }])
      if (!active[sessionID]) return
      const state = sessions.get(sessionID), transcript = history.get(sessionID)
      try {
        for (let step = 0; step < steps; step++) {
          const context = { sessionID, agent: state.agent, model: state.model,
            system: [{ type: "text", text: "Native host system", providerMetadata: { test: "retained" } }],
            messages: [], tools: { skill: { description: "Load a skill", input: { type: "object", properties: { id: { type: "string" } } } }, shell: { description: "Shell", input: {} } }, options: { maxTokens: 1000 } }
          await callHook("session", "context", context)
          const assistant = { id: `assistant_${++sequence}`, type: "assistant", agent: state.agent, model: state.model, content: [], time: { created: sequence } }
          transcript.push(assistant)
          emit("session.step.started", { sessionID, assistantMessageID: assistant.id })
          const headers = { sessionID, agent: state.agent, model: state.model, kind: "primary", headers: { "x-session-affinity": version === "2.0.6" ? sessionID : state.fork?.sessionID || sessionID, Authorization: "host-secret" } }
          await callHook("session", "model.request", headers)
          if (state.fork) assert.equal(headers.headers["x-session-affinity"], state.fork.sessionID)
          await duringCall(sessionID, context)
          if (held) await new Promise(resolve => pending.set(sessionID, resolve))
          if (step + 1 < steps) {
            await callHook("tool", "execute.before", { sessionID, tool: "skill" })
            const result = { content: [{ type: "text", text: "Procedure body" }], metadata: { directory: resolve(directory, "skills/procedure") } }
            await callHook("tool", "execute.after", { sessionID, tool: "skill", input: { id: "procedure" }, status: "completed", result })
            assistant.content = [{ type: "tool", id: `call_${sequence}`, name: "skill", state: { status: "completed", input: { id: "procedure" }, content: result.content } }]
            assistant.finish = "tool-calls"
          } else {
            assistant.content = [{ type: "text", text: "Nothing to save." }]; assistant.finish = "stop"
          }
          assistant.tokens = tokens; assistant.time.completed = ++sequence
          emit("session.step.ended", { sessionID, assistantMessageID: assistant.id })
        }
        transcript.push({ id: `idle_${++sequence}`, type: "idle", outcome: "succeeded" })
      } catch (error) {
        // Released Session.wait awaits idle; execution failures arrive through events/history.
        transcript.push({ id: `idle_${++sequence}`, type: "idle", outcome: "failed" })
        emit("session.execution.failed", { sessionID, error: { type: "unknown", message: error.message } })
      } finally { delete active[sessionID]; pending.delete(sessionID) }
    },
    interrupt: async input => { requests.push(["interrupt", input]); delete active[input.sessionID]; pending.get(input.sessionID)?.() },
  }
  const client = { server: { info: async () => ({ version, pid: process.pid }) }, session: {
    active: async () => structuredClone(active),
    update: async input => {
      if (version === "2.0.6" && input.metadata !== undefined) throw Error("Metadata updates are not supported")
      Object.assign(sessions.get(input.sessionID), Object.fromEntries(Object.entries(input).filter(([key, value]) => ["title", "metadata", "permissions"].includes(key) && value !== undefined)))
      requests.push(["update", input])
    },
    fork: async input => {
      const id = `review_${++sequence}`
      requests.push(["fork", input])
      sessions.set(id, { ...structuredClone(sessions.get(input.sessionID)), id, fork: { sessionID: input.sessionID } })
      history.set(id, structuredClone(history.get(input.sessionID)))
      emit("session.forked", { sessionID: id, parentID: input.sessionID })
      return structuredClone(sessions.get(id))
    },
  }, message: { list: async input => {
    if (input.cursor) { assert.equal(input.order, undefined); assert.equal(input.limit, undefined) }
    requests.push(["messages", input])
    const all = history.get(input.sessionID), offset = Number(input.cursor || 0), page = all.slice(offset, offset + 2)
    return { data: structuredClone(page), cursor: { next: offset + 2 < all.length ? String(offset + 2) : null } }
  } } }
  const ctx = { app: { version }, location: { directory }, options: {}, session,
    model: { list: async () => ({ location: { directory }, data: [structuredClone(model)] }) }, agent: { get: async () => ({ location: { directory }, data: { id: "build", request: { body: {} } } }) },
    skill: { transform: async callback => { callback({ get: id => skills.get(id), add: skill => skills.set(skill.id, skill) }); return { dispose: async () => skills.clear() } } },
    tool: { hook: hook("tool") }, event: { subscribe: async function* ({ signal }) {
      signal.addEventListener("abort", () => wake?.(), { once: true })
      while (!signal.aborted) {
        if (!events.length) await new Promise(resolve => { wake = resolve })
        if (signal.aborted) return
        yield events.shift()
      }
    } },
  }
  parent()
  return { ctx, client, sessions, history, skills, hooks, requests, emit, parent, callHook, pending,
    set held(value) { held = value }, set steps(value) { steps = value }, set duringCall(value) { duringCall = value } }
}

async function fixture(t, version = "2.0.24", settings = "") {
  const home = mkdtempSync("/tmp/opencode/skill-v2-")
  writeFileSync(resolve(home, "settings.yaml"), "library:\n  root: skills\nnotifications:\n  enabled: false\ntriggers:\n  idle:\n    seconds: 0\n" + settings)
  const host = hostFixture(home, version), operations = [], timers = new Set(), diagnostics = []
  let core, stop
  const start = async () => {
    core = new ChildCore({ home })
    const current = core, claims = operations.filter(entry => entry.op === "claim").length
    const wrapped = { get closed() { return current.closed }, dispose: () => current.dispose(), request: async (...args) => {
      const answer = await current.request(...args); operations.push({ op: args[0], payload: args[1], answer }); return answer
    } }
    stop = await setupV2(host.ctx, { home }, { core: wrapped, diagnostic: value => diagnostics.push(value),
      connection: { discover: async () => ({ url: "http://own-host" }), makeClient: () => host.client },
      setTimer: (callback, delay) => { const timer = { callback, delay, unref() {} }; timers.add(timer); return timer }, clearTimer: timer => timers.delete(timer) })
    assert.equal(typeof stop, "function", diagnostics.join("\n"))
    await eventually(() => operations.filter(entry => entry.op === "claim").length > claims)
  }
  t.after(async () => { await stop?.(); await core.dispose(); rmSync(home, { recursive: true, force: true }) })
  await start()
  const fire = delay => { for (const timer of [...timers]) if (timer.delay === delay) { timers.delete(timer); timer.callback() } }
  const observe = async (id = "parent", options = {}) => {
    await host.callHook("session", "context", { sessionID: id, agent: "build", model: selection,
      system: [{ type: "text", text: "Native host system", providerMetadata: { test: "retained" } }], messages: [],
      tools: { skill: { description: "Load a skill", input: { type: "object", properties: { id: { type: "string" } } } }, shell: { description: "Shell", input: {} } }, options: { maxTokens: 1000, ...options } })
    await host.callHook("session", "model.request", { sessionID: id, kind: "primary", headers: { "x-session-affinity": id, Authorization: "host-secret" } })
  }
  const settle = async (id = "parent") => {
    host.emit("session.inbox.enqueued", { sessionID: id, inboxID: `${id}_user`, item: { type: "user" } })
    host.emit("session.execution.succeeded", { sessionID: id })
    await eventually(() => [...timers].some(timer => timer.delay === 0)); fire(0)
  }
  return { home, host, get core() { return core }, operations, diagnostics, fire, observe, settle,
    stop: () => stop(), restart: async () => { await stop(); await start() } }
}

for (const version of V2_HOST_VERSIONS.filter(version => version !== "2.0.6")) test(`V2 ${version} fork preserves native history, waits for completion and maps host calls`, async t => {
  const f = await fixture(t, version)
  f.host.sessions.get("parent").metadata = { operator: "retained", automation: { customPolicy: "retained" } }
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged", JSON.stringify(finished))
  const fork = f.host.requests.find(([op]) => op === "fork")
  assert.deepEqual(fork[1], { sessionID: "parent" })
  const bound = f.operations.find(entry => entry.op === "bind").payload
  assert.deepEqual(f.host.sessions.get(bound.reviewerID).metadata, { operator: "retained", automation: {
    customPolicy: "retained", owner: "skill-learn", kind: "skill-review", reviewID: bound.reviewID,
    suppressAudio: true, suppressMemoryCollection: true,
  } })
  assert.deepEqual(f.host.sessions.get("parent").metadata, { operator: "retained", automation: { customPolicy: "retained" } })
  assert.deepEqual(f.host.history.get(bound.reviewerID).slice(0, 2), f.host.history.get("parent"))
  assert.equal(f.host.requests.some(([op]) => op === "context-update"), false)
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls.length, 1)
  assert.equal(review.calls[0].call_status, "completed")
  assert.equal(review.calls[0].response.content, "Nothing to save.")
  assert.match(review.calls[0].payload.assistantID, /^assistant_/)
  assert.equal(review.calls[0].response.usage.input_tokens, 500)
  assert.equal(JSON.stringify(review).includes("host-secret"), false)
  assert.ok(f.host.requests.some(([op]) => op === "wait"))
  f.host.emit("session.idle", { sessionID: bound.reviewerID })
  await f.observe()
  assert.equal(f.operations.filter(entry => entry.op === "enqueue").length, 1)
})

for (const version of V2_HOST_VERSIONS) test(`V2 ${version} post-compaction continuation uses a freshly observed checkpoint profile`, async t => {
  const f = await fixture(t, version)
  await f.observe()
  const history = f.host.history.get("parent")
  history[1].tokens = { ...tokens, input: 250000 }
  const checkpoint = { id: "checkpoint", type: "compaction", status: "completed", summary: "Current checkpoint summary",
    recent: "Retained recent tool evidence", providerContext: { opaque: "host-owned checkpoint" } }
  const continued = { id: "continued", type: "assistant", agent: "build", model: selection,
    time: { created: 4, completed: 5 }, finish: "stop", tokens, content: [{ type: "text", text: "Continued after compaction" }] }
  history.push(checkpoint, continued)
  await f.observe()
  f.host.duringCall = async id => {
    if (version !== "2.0.6") {
      const active = await f.host.ctx.session.context({ sessionID: id })
      assert.deepEqual(active[0], checkpoint)
      assert.equal(active.some(message => message.id === "parent_user"), false)
    } else {
      const prompt = f.host.requests.find(([op]) => op === "prompt")[1].text
      assert.match(prompt, /Current checkpoint summary[\s\S]*Retained recent tool evidence[\s\S]*Continued after compaction/)
      assert.equal(prompt.includes("Fix the reusable ordering rule"), false)
    }
  }
  await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged", JSON.stringify(finished))
  const queued = f.operations.find(entry => entry.op === "enqueue").payload
  assert.deepEqual(queued.messages.map(message => message.info.id), ["checkpoint", "continued"])
  assert.deepEqual(queued.messages[0].native, checkpoint)
  assert.equal(queued.profile.turnID, "checkpoint")
  assert.equal(queued.compatibility.compatible, version !== "2.0.6")
  assert.equal(queued.parentInputTokens, tokens.input)
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.context_mode, version === "2.0.6" ? "digest" : "fork")
  assert.equal(f.host.requests.filter(([op]) => op === "fork").length, version === "2.0.6" ? 0 : 1)
  assert.equal(f.host.requests.filter(([op]) => op === "messages").length, 0)
})

test("V2 2.0.6 uses marked digest sessions without attempting unsupported fork metadata updates", async t => {
  const f = await fixture(t, "2.0.6")
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged", JSON.stringify(finished))
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.context_mode, "digest")
  assert.equal(JSON.parse(review.decision_json).reason, "reviewer-metadata-update-unavailable")
  assert.equal(f.host.requests.some(([op]) => op === "fork"), false)
  assert.equal(f.host.requests.some(([op, input]) => op === "update" && input.metadata), false)
  assert.equal(f.host.requests.find(([op]) => op === "create")[1].metadata.automation.suppressAudio, true)
})

for (const version of V2_HOST_VERSIONS) test(`V2 ${version} unmarked retained reviewers cannot disable startup or trigger recursive learning`, async t => {
  const f = await fixture(t, version)
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  const bound = f.operations.find(entry => entry.op === "bind").payload
  const session = f.host.sessions.get(bound.reviewerID)
  session.title = "Renamed old reviewer"
  session.metadata = { operator: "retained", automation: { unrelated: "retained" } }
  const promptCount = f.host.requests.filter(([op]) => op === "prompt").length
  const updateCount = f.host.requests.filter(([op]) => op === "update").length
  await f.restart()
  assert.equal(f.diagnostics.some(value => value.includes("learning disabled")), false, f.diagnostics.join("\n"))
  assert.equal(session.title, "Renamed old reviewer")
  if (version === "2.0.6") {
    assert.deepEqual(session.metadata, { operator: "retained", automation: { unrelated: "retained" } })
    assert.equal(f.host.requests.filter(([op]) => op === "update").length, updateCount)
  } else {
    assert.deepEqual(session.metadata, { operator: "retained", automation: { unrelated: "retained",
      owner: "skill-learn", kind: "skill-review", reviewID: finished.payload.reviewID, suppressAudio: true, suppressMemoryCollection: true } })
  }
  f.host.emit("session.execution.succeeded", { sessionID: session.id })
  f.host.parent("after-restart")
  await f.observe("after-restart"); await f.settle("after-restart")
  await eventually(() => f.operations.filter(entry => entry.op === "finish").length === 2)
  assert.equal(f.operations.filter(entry => entry.op === "enqueue").length, 2)
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, promptCount + 1)
  assert.equal(f.host.requests.some(([op]) => op === "context-update"), false)
})

test("V2 HTTP metadata persistence is still required before binding or prompting a fork", async t => {
  const f = await fixture(t)
  const update = f.host.client.session.update
  f.host.client.session.update = async input => {
    if (input.metadata) return
    return update(input)
  }
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.match(finished.payload.error, /persist reviewer suppression metadata/)
  assert.equal(f.operations.some(entry => entry.op === "bind"), false)
  assert.equal(f.host.requests.some(([op]) => op === "prompt"), false)
})

for (const phase of ["preload", "registration"]) test(`V2 ${phase} failure rejects activation and cleans up its worker and registrations`, async t => {
  const home = mkdtempSync("/tmp/opencode/skill-v2-startup-")
  t.after(() => rmSync(home, { recursive: true, force: true }))
  const host = hostFixture(home, "2.0.24"), diagnostics = [], timers = new Set()
  let closed = false
  const core = { get closed() { return closed }, dispose: async () => { closed = true }, request: async op => {
    if (op === "hello") return { libraryRoot: resolve(home, "library"), leaseSeconds: 30 }
    if (op === "host-skills") return []
    assert.equal(op, "state")
    return { reviewers: phase === "preload" ? [{ id: "rv_old", reviewer_id: "old-reviewer", status: "finished" }] : [] }
  } }
  if (phase === "preload") {
    host.parent("old-reviewer")
    host.client.session.update = async () => {}
  } else {
    const hook = host.ctx.session.hook
    host.ctx.session.hook = (name, callback) => {
      if (name === "retry") throw new Error("Registration unavailable")
      return hook(name, callback)
    }
  }
  await assert.rejects(setupV2(host.ctx, { home }, { core, diagnostic: value => diagnostics.push(value),
    connection: { discover: async () => ({ url: "http://own-host" }), makeClient: () => host.client },
    setTimer: callback => { timers.add(callback); return callback }, clearTimer: callback => timers.delete(callback) }),
  phase === "preload" ? /persist reviewer suppression metadata/ : /Registration unavailable/)
  assert.equal(closed, true)
  assert.equal(timers.size, 0)
  assert.equal(host.skills.size, 0)
  assert.ok([...host.hooks.values()].every(callbacks => callbacks.length === 0))
  assert.equal(host.requests.some(([op]) => op === "prompt" || op === "fork"), false)
  assert.equal(diagnostics.filter(value => value.startsWith("learning disabled:")).length, 1)
  assert.equal(host.sessions.get("parent").title, "Ordering investigation")
})

for (const version of V2_HOST_VERSIONS) test(`V2 ${version} digest captures only the latest completed checkpoint and subsequent evidence`, async t => {
  const f = await fixture(t, version, "review:\n  contextMode: digest\n")
  f.host.history.get("parent").push(
    { id: "old_checkpoint", type: "compaction", status: "completed", summary: "EXCLUDED_OLD_CHECKPOINT" },
    { id: "old_user", type: "user", text: "EXCLUDED_OLD_USER", time: { created: 4 } },
    { id: "checkpoint", type: "compaction", status: "completed", summary: "CURRENT_SUMMARY", recent: "RETAINED_RECENT_CONTEXT" },
    { id: "current_user", type: "user", text: "CURRENT_USER_REQUEST", time: { created: 5 } },
    { id: "current_assistant", type: "assistant", agent: "build", model: selection, finish: "stop", tokens,
      time: { created: 6, completed: 7 }, content: [{ type: "tool", id: "lookup", name: "read", state: {
        status: "completed", input: { path: "current-file" }, content: [{ type: "text", text: "CURRENT_TOOL_RESULT" }] } }] },
    { id: "incomplete_checkpoint", type: "compaction", status: "failed" },
  )
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged")
  const queued = f.operations.find(entry => entry.op === "enqueue").payload
  assert.deepEqual(queued.messages.map(message => message.info.id), ["checkpoint", "current_user", "current_assistant", "incomplete_checkpoint"])
  assert.equal(queued.profile.turnID, "current_user")
  const prompt = f.host.requests.find(([op]) => op === "prompt")[1].text
  for (const text of ["CURRENT_SUMMARY", "RETAINED_RECENT_CONTEXT", "CURRENT_USER_REQUEST", "CURRENT_TOOL_RESULT"]) assert.ok(prompt.includes(text), text)
  for (const text of ["EXCLUDED_OLD_CHECKPOINT", "EXCLUDED_OLD_USER", "Fix the reusable ordering rule"]) assert.equal(prompt.includes(text), false, text)
  assert.equal(f.host.requests.filter(([op]) => op === "messages").length, 0)
})

test("V2 an unrefreshed pre-compaction profile cannot override the current model selection", async t => {
  const f = await fixture(t)
  await f.observe()
  const current = { ...selection, id: "current-model", variant: "high" }
  f.host.sessions.get("parent").model = current
  f.host.ctx.model.list = async () => ({ data: [model, { ...model, id: current.id, variants: [{ id: "high", settings: { reasoningEffort: "high" } }] }] })
  f.host.history.get("parent").push(
    { id: "checkpoint", type: "compaction", status: "completed", summary: "Current context" },
    { id: "continued", type: "assistant", agent: "build", model: current, finish: "stop", tokens,
      time: { created: 4, completed: 5 }, content: [{ type: "text", text: "Completed current context" }] },
  )
  await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged", JSON.stringify(finished))
  const queued = f.operations.find(entry => entry.op === "enqueue").payload
  assert.equal(queued.compatibility.reason, "parent-profile-stale")
  assert.deepEqual(queued.profile.model, { providerID: current.providerID, modelID: current.id })
  assert.equal(queued.profile.variant, "high")
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.context_mode, "digest")
  assert.equal(review.called_model, "openai/current-model")
  assert.equal(f.host.requests.filter(([op]) => op === "fork").length, 0)
})

test("V2 discovery rejects another server and unsupported service versions before startup", async () => {
  const ctx = { app: { version: "2.0.21" } }
  await assert.rejects(connectV2Host(ctx, { discover: async () => null, makeClient: () => ({}) }), /not registered/)
  await assert.rejects(connectV2Host(ctx, { discover: async () => ({ url: "http://other" }), makeClient: () => ({ server: { info: async () => ({ pid: process.pid + 1, version: ctx.app.version }) } }) }), /not this plugin's host/)
  await assert.rejects(connectV2Host({ app: { version: "2.0.99" } }), /Unsupported/)
})

test("V2 diagnostics use the retry veto, block auxiliary requests and admit at most two calls", async t => {
  const f = await fixture(t)
  await f.observe()
  f.host.duringCall = async id => {
    const retry = { sessionID: id, attempt: 2, decision: { retry: true, delay: 0 } }
    await f.host.callHook("session", "retry", retry)
    assert.deepEqual(retry.decision, { retry: false })
    await assert.rejects(f.host.callHook("session", "generate", { sessionID: id }), /Auxiliary/)
    await assert.rejects(f.host.callHook("tool", "execute.before", { sessionID: id, tool: "skill" }), /only load skills/)
  }
  await f.core.request("request-probe", { parentID: "parent", mode: "fork" }); f.fire(5000)
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls.length, 1)
  assert.match(review.error, /retries are disabled/)
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, 1)
})

test("V2 missing profiles digest, delegate/global events are excluded, and parent failures remain isolated", async t => {
  const f = await fixture(t)
  f.host.parent("delegate", "parent")
  await f.observe("delegate"); await f.settle("delegate")
  await eventually(() => f.operations.some(entry => entry.op === "enqueue" && entry.answer.disposition === "not-reviewed"))
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, 0)
  f.host.emit("session.idle", { sessionID: "foreign" }, { directory: "/other/location" })
  await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged")
  assert.equal(f.host.requests.filter(([op]) => op === "fork").length, 0)
  assert.equal(f.host.requests.find(([op]) => op === "create")[1].metadata.automation.suppressMemoryCollection, true)
  assert.match(f.host.requests.find(([op]) => op === "prompt")[1].text, /\[user\]/)
  f.host.ctx.model.list = async () => { throw Error("catalogue unavailable") }
  await f.observe()
  assert.ok(f.diagnostics.some(value => value.includes("catalogue unavailable")))
})

test("V2 context projection retains checkpoint, attachment, provider-state and tool evidence", () => {
  const session = { id: "parent", model: selection }
  const checkpoint = { id: "checkpoint", type: "compaction", status: "completed", summary: "Summary", recent: "Serialized recent context", providerContext: { opaque: "native" } }
  const projected = projectV2Message(checkpoint, session)
  assert.deepEqual(projected.native, checkpoint)
  assert.match(projected.parts[0].text, /Serialized recent context/)
  const tool = { type: "tool", id: "call", name: "lookup", providerState: { phase: "commentary" }, state: { status: "completed", input: { q: "x" }, content: [{ type: "text", text: "Evidence" }] }, time: { created: 1 } }
  const assistant = projectV2Message({ id: "assistant", type: "assistant", model: selection, content: [tool], finish: "tool-calls", tokens }, session)
  assert.equal(assistant.parts[0].state.output, "Evidence")
  assert.equal(assistant.native.content[0].providerState.phase, "commentary")
})

test("V2 native reviewer guards preserve parent execution and record only the requested skill snapshot", async t => {
  const f = await fixture(t)
  mkdirSync(resolve(f.home, "skills/procedure"), { recursive: true })
  writeFileSync(resolve(f.home, "skills/procedure/SKILL.md"), "Current disk body")
  f.host.duringCall = async id => {
    await assert.rejects(f.host.callHook("tool", "execute.before", { sessionID: id, tool: "shell" }), /only load skills/)
    await f.host.callHook("tool", "execute.before", { sessionID: "parent", tool: "shell" })
    await f.host.callHook("tool", "execute.after", { sessionID: id, tool: "skill", input: { id: "procedure" }, status: "completed",
      result: { content: "Old snapshot body", metadata: { directory: resolve(f.home, "skills/procedure") } } })
  }
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged")
  const load = f.operations.find(entry => entry.op === "skill-loaded").payload
  assert.equal(load.body, "Old snapshot body")
  assert.equal(load.discoveryCompatible, true)
})

test("V2 skill-loading continuation admits and records each model call before collecting a final answer", async t => {
  const f = await fixture(t)
  mkdirSync(resolve(f.home, "skills/procedure"), { recursive: true })
  f.host.steps = 2
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged", JSON.stringify(finished))
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls.length, 2)
  assert.ok(review.calls.every(call => call.call_status === "completed"))
  assert.equal(review.calls[0].response.tool_calls[0].tool, "skill")
  assert.equal(review.calls[1].response.content, "Nothing to save.")
  assert.equal(f.operations.filter(entry => entry.op === "admit" && entry.answer.allowed).length, 2)
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, 1)
})

test("V2 marked but unbound reviewers fail closed before model admission", async t => {
  const f = await fixture(t)
  f.host.parent("unbound-reviewer")
  f.host.sessions.get("unbound-reviewer").metadata = { automation: { owner: "skill-learn", kind: "skill-review", reviewID: "rv_missing" } }
  await assert.rejects(f.observe("unbound-reviewer"), /no durable review binding/)
  await assert.rejects(f.host.callHook("session", "generate", { sessionID: "unbound-reviewer" }), /no durable review binding/)
  await assert.rejects(f.host.callHook("tool", "execute.before", { sessionID: "unbound-reviewer", tool: "shell" }), /no durable review binding/)
  const title = { sessionID: "unbound-reviewer" }
  await f.host.callHook("session", "title", title)
  assert.equal(title.result, "Skill review")
  assert.equal(f.operations.some(entry => entry.op === "admit"), false)
  await f.observe()
})

test("V2 refresh discovers a retained reviewer but cannot admit another activation's paid call", async t => {
  const f = await fixture(t)
  const claim = f.operations.find(entry => entry.op === "claim").payload
  const queued = await f.core.request("enqueue", { harness: "opencode", hostID: claim.hostID, hostPID: process.pid,
    sessionID: "parent", watermark: "foreign-owner", messages: [] })
  await f.core.request("claim", { hostID: claim.hostID, owner: "foreign-owner" })
  await f.core.request("bind", { reviewID: queued.reviewId, reviewerID: "foreign-reviewer", owner: "foreign-owner", inheritedIDs: [] })
  f.host.parent("foreign-reviewer")
  f.host.sessions.get("foreign-reviewer").metadata = { automation: { owner: "skill-learn", kind: "skill-review", reviewID: queued.reviewId } }
  await assert.rejects(f.observe("foreign-reviewer"), /another plugin activation/)
  assert.equal(f.operations.some(entry => entry.op === "admit"), false)
  assert.equal((await f.core.request("show", { id: queued.reviewId })).status, "running")
  await f.core.request("cancel", { reviewID: queued.reviewId, hostSettled: true })
})

test("V2 execution failure after a skill step retains the real error rather than parsing empty feedback", async t => {
  const f = await fixture(t)
  mkdirSync(resolve(f.home, "skills/procedure"), { recursive: true })
  f.host.steps = 2
  let calls = 0
  f.host.duringCall = async () => { if (++calls === 2) throw new Error("Synthetic continuation failed") }
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "failed")
  assert.match(finished.answer.error, /Synthetic continuation failed|execution failed before a final answer/)
  assert.equal(finished.answer.error.includes("Expecting value"), false)
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls[0].response.tool_calls[0].tool, "skill")
  assert.equal(review.calls[1].call_status, "interrupted")
})

test("V2 diagnostic continuation records two separate calls and then stops", async t => {
  const f = await fixture(t)
  await f.observe()
  await f.core.request("request-probe", { parentID: "parent", mode: "fork" }); f.fire(5000)
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls.length, 2)
  assert.ok(review.calls.every(call => call.call_status === "completed" && call.response.usage.input_tokens === 500))
  assert.equal(new Set(review.calls.map(call => call.payload.assistantID)).size, 2)
  assert.equal(f.host.requests.filter(([op]) => op === "prompt").length, 2)
})

test("V2 next-call budget waits for the allowed call and blocks continuation only for the reviewer", async t => {
  const f = await fixture(t, "2.0.21", "review:\n  maxInputTokens: 500\n")
  await f.observe()
  f.host.duringCall = async (id, context) => {
    f.host.history.get(id).at(-1).finish = "stop"
    f.host.history.get(id).at(-1).tokens = tokens
    await assert.rejects(f.host.callHook("session", "context", context), /input-budget/)
    await f.observe()
  }
  await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  const review = await f.core.request("show", { id: finished.payload.reviewID })
  assert.equal(review.calls.length, 1)
  assert.equal(review.calls[0].call_status, "completed")
  assert.match(review.error, /input-budget/)
})

test("V2 follow selection uses the completed assistant when session defaults were not persisted", async t => {
  const f = await fixture(t)
  delete f.host.sessions.get("parent").model
  delete f.host.sessions.get("parent").agent
  await f.observe(); await f.settle()
  const finished = await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(finished.answer.outcome, "unchanged")
  assert.deepEqual(f.host.requests.find(([op]) => op === "model")[1].model, selection)
})

test("V2 changed registry and unrepresentable nested-fork affinity select digest before dispatch", async t => {
  const f = await fixture(t)
  await f.observe()
  f.host.ctx.agent.get = async () => ({ location: f.host.ctx.location, data: { id: "build", request: { body: { temperature: 0.4 } } } })
  await f.settle()
  await eventually(() => f.operations.find(entry => entry.op === "finish"))
  assert.equal(f.host.requests.filter(([op]) => op === "fork").length, 0)
  assert.equal(f.operations.find(entry => entry.op === "claim" && entry.answer.plan).answer.plan.decision.reason, "parent-registry-changed")
  f.host.ctx.agent.get = async () => ({ location: f.host.ctx.location, data: { id: "build", request: { body: {} } } })
  f.host.parent("nested")
  f.host.sessions.get("nested").fork = { sessionID: "parent" }
  await f.observe("nested"); await f.settle("nested")
  await eventually(() => f.operations.filter(entry => entry.op === "finish").length === 2)
  assert.equal(f.host.requests.filter(([op]) => op === "fork").length, 0)
  assert.equal(f.operations.findLast(entry => entry.op === "claim" && entry.answer.plan).answer.plan.decision.reason, "fork-affinity-unpreservable")
})

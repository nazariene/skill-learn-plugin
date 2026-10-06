import test from "node:test"
import assert from "node:assert/strict"
import { V2_HOST_VERSIONS, NativeReviews, ParentProfiles, transcriptWatermark } from "../plugin/native.mjs"

const hostVersion = V2_HOST_VERSIONS.at(-1)
const scope = { registryFingerprint: "registry", toolFingerprint: "tools", permissionFingerprint: "permissions", variant: "medium" }
const parent = { id: "parent", version: hostVersion, permission: [], model: { variant: "medium" }, ...scope }

const raw = [
  { info: { id: "u1", sessionID: "parent", role: "user" }, parts: [{ type: "text", text: "Fix the ordering" }] },
  { info: { id: "a1", sessionID: "parent", parentID: "u1", role: "assistant" }, parts: [
    { type: "step-start" },
    { type: "reasoning", text: "", metadata: { openai: { itemId: "rs1", reasoningEncryptedContent: "opaque" } } },
    { type: "text", text: "Checking", metadata: { openai: { phase: "commentary", itemId: "m1" } } },
    { type: "tool", callID: "c1", tool: "read", state: { status: "completed", input: { filePath: "x" }, output: "contents", time: {} } },
    { type: "step-finish", tokens: { input: 20, cache: { read: 80, write: 0 } } },
    { type: "step-start" },
    { type: "text", text: "Fixed", metadata: { openai: { phase: "final_answer", itemId: "m2" } } },
  ] },
  { info: { id: "u2", sessionID: "parent", role: "user" }, parts: [{ type: "compaction", tail_start_id: "u1" }] },
]

function fakeHost() {
  const history = new Map([["parent", structuredClone(raw)]]), requests = []
  const sessions = new Map([["parent", structuredClone(parent)]])
  const client = { session: {
    get: async request => ({ data: structuredClone(sessions.get(request.path.id)) }),
    status: async () => ({ data: { parent: { type: "idle" } } }),
    messages: async request => ({ data: history.get(request.path.id) }),
    fork: async request => {
      requests.push(["fork", request])
      const ids = new Map(history.get(request.path.id).map(message => [message.info.id, `fork_${message.info.id}`]))
      const copied = structuredClone(history.get(request.path.id))
      for (const message of copied) {
        message.info.id = ids.get(message.info.id)
        message.info.sessionID = "reviewer"
        if (message.info.parentID) message.info.parentID = ids.get(message.info.parentID)
        for (const part of message.parts) if (part.tail_start_id) part.tail_start_id = ids.get(part.tail_start_id)
      }
      history.set("reviewer", copied)
      sessions.set("reviewer", { ...structuredClone(sessions.get(request.path.id)), id: "reviewer" })
      return { data: sessions.get("reviewer") }
    },
    create: async request => {
      requests.push(["create", request]); history.set("reviewer", [])
      sessions.set("reviewer", { id: "reviewer", ...request.body })
      return { data: structuredClone(sessions.get("reviewer")) }
    },
    update: async request => {
      requests.push(["update", request]); Object.assign(sessions.get(request.path.id), request.body)
      return { data: structuredClone(sessions.get(request.path.id)) }
    },
    prompt: async request => { requests.push(["prompt", request]); return { data: { info: { role: "assistant" }, parts: [{ type: "text", text: "Nothing to save." }] } } },
    abort: async request => { requests.push(["abort", request]); return { data: true } },
  } }
  return { client, history, requests, sessions }
}

function plan() {
  return { reviewID: "rv1", parentID: "parent", mode: "fork", watermark: transcriptWatermark(raw), instruction: "Review skills only.",
    model: { providerID: "openai", modelID: "gpt-5.5", variant: "medium" }, profile: { agent: "build" }, permission: [] }
}

test("native fork leaves phases, reasoning, steps and compaction remapping to the host", async () => {
  const host = fakeHost(), bindings = []
  host.sessions.get("parent").metadata = { operator: { ticket: "keep" }, automation: { customPolicy: "keep" } }
  const adapter = new NativeReviews({ ...host, directory: "/workspace", bind: async binding => {
    bindings.push(binding)
    assert.equal(host.requests.some(([op]) => op === "prompt"), false)
    assert.deepEqual(host.sessions.get(binding.reviewerID).metadata, { operator: { ticket: "keep" }, automation: {
      customPolicy: "keep", owner: "skill-learn", kind: "skill-review", reviewID: "rv1", suppressAudio: true, suppressMemoryCollection: true,
    } })
  } })
  const reviewPlan = plan()
  const id = await adapter.prepare(reviewPlan)
  await adapter.prompt(id, reviewPlan)
  const inherited = host.history.get(id)
  assert.deepEqual(inherited[1].parts, raw[1].parts)
  assert.equal(inherited[2].parts[0].tail_start_id, "fork_u1")
  assert.deepEqual(bindings[0].inheritedIDs, ["fork_u1", "fork_a1", "fork_u2"])
  const prompt = host.requests.find(([op]) => op === "prompt")[1]
  assert.deepEqual(prompt.body.parts, [{ type: "text", text: reviewPlan.instruction }])
  assert.equal(prompt.body.system, undefined)
  assert.equal(prompt.body.variant, "medium")
  assert.deepEqual(host.requests.find(([op]) => op === "fork")[1].body, {})
  assert.deepEqual(host.history.get("parent"), raw)
  assert.deepEqual(host.sessions.get("parent").metadata, { operator: { ticket: "keep" }, automation: { customPolicy: "keep" } })
  await adapter.abort(id)
  await assert.rejects(adapter.abort("parent"), /unregistered/)
})

test("fresh digest submits evidence as text, never historical native tool actions", async () => {
  const host = fakeHost(), adapter = new NativeReviews({ ...host, directory: "/workspace" })
  const reviewPlan = { ...plan(), mode: "digest", agent: "build", instruction: '[assistant tool] read({"filePath":"x"})\n[tool] contents\nReview skills only.' }
  const id = await adapter.prepare(reviewPlan)
  await adapter.prompt(id, reviewPlan)
  assert.equal(host.requests.some(([op]) => op === "fork"), false)
  assert.deepEqual(host.requests.find(([op]) => op === "create")[1].body.metadata, { automation: {
    owner: "skill-learn", kind: "skill-review", reviewID: "rv1", suppressAudio: true, suppressMemoryCollection: true,
  } })
  assert.deepEqual(host.history.get(id), [])
  assert.deepEqual(host.requests.find(([op]) => op === "prompt")[1].body.parts, [{ type: "text", text: reviewPlan.instruction }])
})

test("a creation-only metadata host rejects forks without creating an unmarked reviewer", async () => {
  const host = fakeHost(), adapter = new NativeReviews({ ...host, directory: "/workspace", metadataUpdates: false })
  await assert.rejects(adapter.prepare(plan()), /digest is required/)
  assert.deepEqual(host.requests, [])
  const digest = { ...plan(), mode: "digest", agent: "build" }
  const id = await adapter.prepare(digest)
  await adapter.prompt(id, digest)
  assert.equal(host.requests.some(([op, request]) => op === "update" && request.body.metadata), false)
  host.sessions.get(id).metadata.automation.suppressAudio = false
  await assert.rejects(adapter.prompt(id, digest), /cannot update reviewer suppression metadata/)
  assert.equal(host.requests.filter(([op]) => op === "prompt").length, 1)
})

test("changed parent, failed binding and SDK errors cannot dispatch", async () => {
  const host = fakeHost(), adapter = new NativeReviews({ ...host, directory: "/workspace", bind: async () => { throw new Error("binding failed") } })
  await assert.rejects(adapter.prepare({ ...plan(), watermark: "old" }), /transcript changed/)
  await assert.rejects(adapter.prepare(plan()), /binding failed/)
  await assert.rejects(adapter.prompt("reviewer", plan()), /bound before dispatch/)
  assert.equal(host.requests.some(([op]) => op === "prompt"), false)
  host.client.session.get = async () => ({ error: { message: "missing" } })
  await assert.rejects(adapter.read("parent"), /request failed/)
})

for (const mode of ["fork", "digest"]) test(`${mode} refuses dispatch when suppression metadata is not persisted`, async () => {
  const host = fakeHost(), bindings = []
  const create = host.client.session.create
  host.client.session.create = async request => {
    const response = await create(request)
    delete host.sessions.get("reviewer").metadata
    delete response.data.metadata
    return response
  }
  host.client.session.update = async () => ({ data: {} })
  const adapter = new NativeReviews({ ...host, directory: "/workspace", bind: async binding => bindings.push(binding) })
  await assert.rejects(adapter.prepare({ ...plan(), mode }), /persist reviewer suppression metadata/)
  await assert.rejects(adapter.prompt("reviewer", plan()), /bound before dispatch/)
  assert.deepEqual(bindings, [])
  assert.equal(host.requests.some(([op]) => op === "prompt"), false)
})

test("a metadata write failure blocks a fresh fork and a later diagnostic prompt", async () => {
  const host = fakeHost(), adapter = new NativeReviews({ ...host, directory: "/workspace" })
  const update = host.client.session.update
  host.client.session.update = async () => { throw new Error("metadata write failed") }
  await assert.rejects(adapter.prepare(plan()), /metadata write failed/)
  host.client.session.update = update
  const id = await adapter.prepare(plan())
  host.sessions.get(id).metadata.automation.suppressAudio = false
  host.client.session.update = async () => { throw new Error("metadata write failed") }
  await assert.rejects(adapter.prompt(id, plan()), /metadata write failed/)
  assert.equal(host.requests.some(([op]) => op === "prompt"), false)
})

async function observe(profiles, extraOptions = {}) {
  const hooks = profiles.hooks()
  const input = { sessionID: "parent", agent: "build", model: { id: "gpt-5.5", providerID: "openai", limit: { context: 400000 } }, message: { id: "u2" }, ...scope }
  const system = { system: ["original system", "original skill descriptions"] }
  const params = { temperature: undefined, topP: undefined, options: { instructions: system.system.join("\n"), reasoningEffort: "medium", promptCacheKey: "parent", store: false, ...extraOptions } }
  const headers = { headers: { "session-id": "parent", "x-session-affinity": "parent", originator: "opencode", Authorization: "secret", "ChatGPT-Account-Id": "secret-account" } }
  const before = { system: { system: [...system.system] }, params: { ...params, options: { ...params.options } }, headers: { headers: { ...headers.headers } } }
  await hooks["experimental.chat.system.transform"](input, system)
  await hooks["chat.params"](input, params)
  await hooks["chat.headers"](input, headers)
  assert.deepEqual({ system, params, headers }, before)
  return { hooks, input }
}

test("profile hooks preserve ordinary calls and apply only portable reviewer controls", async () => {
  const profiles = new ParentProfiles({ hostVersion }), { hooks, input } = await observe(profiles)
  const selected = { providerID: "openai", modelID: "gpt-5.5", variant: "medium" }
  const admitted = profiles.compatibility(parent, raw, selected)
  assert.equal(admitted.compatible, true)
  assert.equal(JSON.stringify(admitted.profile).includes("secret"), false)
  profiles.reviewers.set("reviewer", { mode: "fork", profile: admitted.profile })
  const reviewer = { ...input, sessionID: "reviewer" }
  const output = { options: { instructions: "new system", promptCacheKey: "reviewer" }, temperature: 0.7 }
  await hooks["chat.params"](reviewer, output)
  assert.deepEqual(output.options, admitted.profile.options)
  assert.equal(output.temperature, undefined)
  const system = { system: ["reviewer system"] }, headers = { headers: {} }
  await hooks["experimental.chat.system.transform"](reviewer, system)
  await hooks["chat.headers"](reviewer, headers)
  assert.deepEqual(system.system, admitted.profile.system)
  assert.equal(headers.headers["session-id"], "parent")
  assert.equal(headers.headers["x-session-affinity"], "parent")
  await assert.rejects(hooks["chat.params"]({ ...reviewer, agent: "different" }, output), /differs/)
})

test("unpreservable scopes, stale profiles, changed agent/model and unknown options digest", async () => {
  const profiles = new ParentProfiles({ hostVersion })
  await observe(profiles)
  const session = parent, selected = { providerID: "openai", modelID: "gpt-5.5", variant: "medium" }
  assert.equal(profiles.compatibility(session, raw, selected, { agent: "reviewer" }).reason, "reviewer-agent-differs")
  assert.equal(profiles.compatibility(session, raw, { ...selected, variant: "high" }).reason, "selected-model-variant-differs")
  assert.equal(profiles.compatibility(session, raw.slice(0, 2), selected).reason, "parent-profile-stale")
  assert.equal(profiles.compatibility({ ...session, permission: [{ permission: "bash", action: "deny", pattern: "*" }] }, raw, selected, { restorePermission: false }).reason, "session-permission-unpreservable")
  assert.equal(profiles.compatibility({ ...session, permission: {} }, raw, selected).reason, "invalid-session-permission")
  await observe(profiles, { apiKey: "credential", schema: { parse: () => {} } })
  assert.equal(profiles.compatibility(session, raw, selected).reason, "unpreservable-provider-options")
  assert.equal(JSON.stringify(profiles.parents.get("parent")).includes("credential"), false)
})

test("a completed checkpoint establishes a new profile boundary and requires fresh request headers", async () => {
  const profiles = new ParentProfiles({ hostVersion }), { hooks, input } = await observe(profiles)
  const selected = { providerID: "openai", modelID: "gpt-5.5", variant: "medium" }
  const checkpoint = { info: { id: "checkpoint", role: "assistant", summary: true }, parts: [], native: { type: "compaction", status: "completed" } }
  const messages = [...raw, checkpoint, { info: { id: "reminder", role: "user", synthetic: true }, parts: [] }]
  assert.equal(profiles.compatibility(parent, messages, selected).reason, "parent-profile-stale")
  const current = { ...input, message: checkpoint.info }
  await hooks["experimental.chat.system.transform"](current, { system: ["Current context instructions"] })
  await hooks["chat.params"](current, { options: {} })
  assert.equal(profiles.compatibility(parent, messages, selected).reason, "parent-profile-unavailable")
  await hooks["chat.headers"](current, { headers: { "x-session-affinity": "parent" } })
  assert.equal(profiles.compatibility(parent, messages, selected).compatible, true)
  assert.deepEqual(profiles.parents.get("parent").system, ["Current context instructions"])
  const pending = { info: { id: "incomplete", role: "compaction" }, parts: [], native: { type: "compaction", status: "pending" } }
  assert.equal(profiles.compatibility(parent, [...messages, pending], selected).compatible, true)
  const user = { info: { id: "new-user", role: "user" }, parts: [] }
  assert.equal(profiles.compatibility(parent, [...messages, user], selected).reason, "parent-profile-stale")
})

test("a session creation version cannot establish the running host version", async () => {
  const profiles = new ParentProfiles()
  await observe(profiles)
  assert.equal(profiles.compatibility(parent, raw, {
    providerID: "openai", modelID: "gpt-5.5", variant: "medium",
  }).reason, "unsupported-host-version")
})

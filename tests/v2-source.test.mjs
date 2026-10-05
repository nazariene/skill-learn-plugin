import test from "node:test"
import assert from "node:assert/strict"
import { readFileSync, readdirSync, existsSync } from "node:fs"
import { resolve } from "node:path"
import { V2_HOST_VERSIONS } from "../plugin/native.mjs"

const root = process.env.OPENCODE_V2_FIXTURES || "/tmp/opencode/skill-learn-v2-fixtures"
for (const version of V2_HOST_VERSIONS) test(`released V2 ${version} prepares native phases, tools, affinity and normalized usage`, { skip: !existsSync(resolve(root, version)) }, async () => {
  const path = resolve(root, version, "package")
  assert.equal(JSON.parse(readFileSync(resolve(path, "package.json"))).version, version)
  const chunks = readdirSync(resolve(path, "dist/chunks")).filter(name => name.endsWith(".js")).map(name => readFileSync(resolve(path, "dist/chunks", name), "utf8"))
  const source = label => { const match = chunks.find(text => text.includes(label)); assert.ok(match, label); return match }
  const load = text => import("data:text/javascript;base64," + Buffer.from(text).toString("base64"))
  const { Effect } = await import("effect")
  const aiPackage = version === "2.0.6" ? "opencode-ai-v2-0-6" : "@opencode/ai"
  const { LLM, Message } = await import(aiPackage)
  const OpenAI = await import(aiPackage + "/providers/openai")
  const Responses = await import(aiPackage + "/protocols/openai-responses")
  const conversionSource = source("// src/session/runner/to-llm-message.ts")
  const conversion = await load(`import { ${version === "2.0.6" ? "" : "Media,"} Message, ReasoningEfforts, ToolCallPart, ToolResultPart } from ${JSON.stringify(import.meta.resolve(aiPackage))};
    import { Option, Schema } from ${JSON.stringify(import.meta.resolve("effect"))};
    import { fileURLToPath } from "node:url";
    const isCheckpoint2 = message => { assertNoCheckpoint(message); return false };
    const assertNoCheckpoint = message => { if (message.providerContext) throw new Error("Fixture does not support provider checkpoints") };
    const decode2 = () => { throw new Error("Unexpected provider checkpoint") };
    ${conversionSource.slice(conversionSource.indexOf("var imageMimes"))}`)
  const selection = { providerID: "openai", id: "gpt-5.5" }
  const native = [
    { id: "old", type: "compaction", status: "completed", summary: "Checkpoint summary", recent: "Serialized retained context" },
    { id: "user", type: "user", text: "Fix the reusable rule" },
    { id: "step1", type: "assistant", model: selection, content: [
      { type: "reasoning", text: "", state: { itemId: "rs_fixture", reasoningEncryptedContent: "opaque" } },
      { type: "text", text: "Looking up", state: { itemId: "msg_comment", phase: "commentary" } },
      { type: "tool", id: "call_lookup", name: "docs_lookup", state: { status: "completed", input: { q: "rule" }, content: [{ type: "text", text: "Lookup result" }] } },
    ] },
    { id: "step2", type: "assistant", model: selection, content: [{ type: "text", text: "Fixed", state: { itemId: "msg_final", phase: "final_answer" } }] },
  ]
  const history = conversion.toLLMMessages2(native, selection)
  history.push(Message.make({ role: "user", content: "Review skills only." }))
  const request = LLM.request({ model: OpenAI.responses("gpt-5.5"), system: [{ type: "text", text: "Native system" }], messages: history,
    promptCacheKey: "parent", providerOptions: { reasoningEffort: "medium" },
    tools: [{ name: "docs_lookup", description: "Effective MCP lookup", inputSchema: { type: "object", properties: { q: { type: "string" } }, required: ["q"] } }] })
  const wire = await Effect.runPromise(Responses.protocol.body.from(request))
  const commentary = wire.input.findIndex(part => part.phase === "commentary")
  const call = wire.input.findIndex(part => part.type === "function_call")
  const output = wire.input.findIndex(part => part.type === "function_call_output")
  const final = wire.input.findIndex(part => part.phase === "final_answer")
  assert.ok(commentary >= 0 && commentary < call && call < output && output < final)
  assert.ok(wire.input.some(part => part.type === "reasoning" && part.encrypted_content === "opaque"))
  assert.match(wire.input[0].content[0].text, /Checkpoint summary[\s\S]*Serialized retained context/)
  assert.equal(wire.input.at(-1).content[0].text, "Review skills only.")
  assert.equal(wire.tools[0].name, "docs_lookup")
  assert.equal(wire.prompt_cache_key, "parent")
  const preparation = source("// src/session/model-request.ts")
  let affinity
  if (version === "2.0.6") {
    const statement = preparation.match(/const root = session\.fork\?\.sessionID \?\? session\.id;/)[0]
    affinity = await load(`export function get(session) { ${statement} return root }`)
    assert.match(preparation, /"x-session-affinity": session.id/)
  } else {
    const affinitySource = source("// src/session/affinity.ts")
    const getName = affinitySource.match(/var (get\d*) = \(session\)/)[1]
    affinity = await load(affinitySource.slice(affinitySource.indexOf(`var ${getName} =`), affinitySource.indexOf("export {")) + `export { ${getName} as get };`)
    assert.match(preparation, /"x-session-affinity": affinity/)
  }
  assert.equal(affinity.get({ id: "review", fork: { sessionID: "parent" } }), affinity.get({ id: "parent" }))
  const usageSource = source("// src/session/usage.ts")
  const normalizedName = usageSource.match(/var (tokens\d*) = \(usage\)/)[1]
  const usage = await load(usageSource.slice(usageSource.indexOf("var finite"), usageSource.indexOf("function calculateCost")) + `export { ${normalizedName} as tokens };`)
  assert.deepEqual(usage.tokens({ nonCachedInputTokens: 2000, cacheReadInputTokens: 8000 }), { input: 2000, output: 0, reasoning: 0, cache: { read: 8000, write: 0 } })
  assert.deepEqual(usage.tokens({ nonCachedInputTokens: 2000 }), usage.tokens({ nonCachedInputTokens: 2000, cacheReadInputTokens: 0, cacheWriteInputTokens: 0 }))
  assert.match(preparation, /promptCacheKey:.*(?:affinity|root)/)
  assert.match(preparation, /headers: \{[\s\S]*x-session-affinity/)
  assert.match(source("// src/session/projector.ts"), /agent: parent.agent,[\s\S]*model: parent.model,[\s\S]*permission: parent.permission/)
})

test("released V2 client lowers authenticated public session requests through a fake transport", async () => {
  const { OpenCode } = await import("@opencode/client")
  const requests = []
  const client = OpenCode.make({ baseUrl: "http://offline-host", headers: { Authorization: "fixture-auth" }, fetch: async (request, init) => {
    const input = request instanceof Request ? request : new Request(request, init)
    requests.push({ path: new URL(input.url).pathname, method: input.method, auth: input.headers.get("authorization"), body: await input.text() })
    return Response.json(requests.length === 1 ? { data: { id: "review", location: { directory: "/project" } } } : { data: { id: "inbox", type: "user" } })
  } })
  const fork = await client.session.fork({ sessionID: "parent" })
  assert.equal(fork.id, "review")
  await client.session.prompt({ sessionID: "review", text: "Review", delivery: "queue" })
  assert.deepEqual(requests.map(request => [request.method, request.path]), [["POST", "/api/session/parent/fork"], ["POST", "/api/session/review/prompt"]])
  assert.ok(requests.every(request => request.auth === "fixture-auth"))
  assert.deepEqual(JSON.parse(requests[1].body), { text: "Review", delivery: "queue" })
})

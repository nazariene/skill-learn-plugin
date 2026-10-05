import { createHash } from "node:crypto"
import { resolve } from "node:path"
import { createPlugin } from "./plugin.mjs"
import { portableOptions, responseData, V2_HOST_VERSIONS } from "./native.mjs"

const fingerprint = value => createHash("sha256").update(JSON.stringify(value)).digest("hex")
const generationKeys = ["temperature", "topP", "topK", "maxTokens"]
const nativeDefaultKeys = new Set([...generationKeys, "baseURL", "apiKey", "transport"])
const textContent = content => typeof content === "string" ? content : (content || []).filter(part => part.type === "text").map(part => part.text).join("\n")

// This projection is core/report evidence only. Native fork and prompt never
// send it back to OpenCode as inherited model history.
export function projectV2Message(message, session) {
  const info = { id: message.id, sessionID: session.id, role: message.type, time: message.time,
    agent: message.agent || session.agent, model: { providerID: (message.model || session.model)?.providerID,
      modelID: (message.model || session.model)?.id, variant: (message.model || session.model)?.variant },
    finish: message.finish, tokens: message.tokens, error: message.error }
  let parts = []
  if (message.type === "assistant") {
    parts = message.content.map((part, index) => ({ ...part, id: `${message.id}:${index}`,
      ...(part.type === "tool" ? { tool: part.name, callID: part.id,
        state: { ...part.state, output: textContent(part.state.content), time: part.time } } : {}) }))
    if (message.finish || message.error) parts.push({ id: `${message.id}:finish`, type: "step-finish", tokens: message.tokens })
  } else if (["user", "synthetic", "system", "skill"].includes(message.type)) {
    parts = [{ id: `${message.id}:text`, type: "text", text: message.text }]
    for (const [index, skill] of (message.skills || []).entries()) if (skill.text) parts.push({ id: `${message.id}:skill:${index}`, type: "text", text: skill.text })
    for (const file of message.files || []) parts.push({ type: "file", filename: file.name, mime: file.mime, uri: file.uri })
    if (message.type !== "user") { info.role = "user"; info.synthetic = true }
  } else if (message.type === "compaction" && message.status === "completed") {
    info.role = "assistant"; info.summary = true; info.finish = "stop"
    parts = [{ id: `${message.id}:summary`, type: "text", text: message.summary + (message.recent ? "\n[Retained checkpoint context]\n" + message.recent : "") }]
  } else if (message.type === "shell") {
    info.role = "user"; info.synthetic = true
    parts = [{ id: `${message.id}:shell`, type: "text", text: `[Historical shell command] ${message.command}\n${message.output?.output || ""}` }]
  }
  return { info, parts, native: structuredClone(message) }
}

export async function connectV2Host(ctx, { discover, makeClient, pid = process.pid } = {}) {
  if (!V2_HOST_VERSIONS.includes(ctx.app.version)) throw new Error(`Unsupported OpenCode service version: ${ctx.app.version}`)
  if (!discover || !makeClient) {
    const [{ Service }, { OpenCode }] = await Promise.all([import("@opencode/client/service"), import("@opencode/client")])
    discover = () => Service.discover()
    makeClient = endpoint => OpenCode.make({ baseUrl: endpoint.url, headers: Service.headers(endpoint) })
  }
  const endpoint = await discover()
  if (!endpoint) throw new Error("The owning OpenCode service is not registered")
  const client = makeClient(endpoint)
  const info = await client.server.info()
  if (info.pid !== pid || info.version !== ctx.app.version) throw new Error("Discovered OpenCode service is not this plugin's host")
  return { client, url: endpoint.url }
}

export async function setupV2(ctx, options = {}, dependencies = {}) {
  const diagnostic = dependencies.diagnostic || (message => console.error(`skill-learn: ${message}`))
  let hooks, controller, subscription
  const registrations = []
  try {
    const connection = await connectV2Host(ctx, dependencies.connection)
    const host = connection.client
    const directory = ctx.location.directory
    const readSession = async id => {
      const session = await ctx.session.get({ sessionID: id })
      if (!session.model || !session.agent) {
        const context = await ctx.session.context({ sessionID: id })
        const assistant = context.findLast(message => message.type === "assistant")
        session.model ||= assistant?.model
        session.agent ||= assistant?.agent
      }
      return session
    }
    const registry = async session => {
      const [models, agent] = (await Promise.all([ctx.model.list(), ctx.agent.get({ agentID: session.agent || "build" })])).map(responseData)
      const model = models.find(model => model.providerID === session.model?.providerID && model.id === session.model?.id)
      if (!model) throw new Error("Selected host model is unavailable")
      const variant = model.variants.find(variant => variant.id === session.model?.variant)
      const defaults = { ...model.settings, ...variant?.settings }
      const unavailable = portableOptions(Object.fromEntries(Object.entries(defaults).filter(([key]) => !nativeDefaultKeys.has(key)))).unavailable
      if (Object.keys(model.body || {}).length || Object.keys(variant?.body || {}).length || Object.keys(agent.request?.body || {}).length) unavailable.push("raw-body-overlay")
      return { model, fingerprint: fingerprint([model, agent]), unavailable }
    }
    const get = async id => {
      const session = await readSession(id)
      const observed = await registry(session).catch(() => null)
      return { ...session, permission: session.permissions || [], permissionFingerprint: fingerprint(session.permissions || []), registryFingerprint: observed?.fingerprint }
    }
    const messages = async id => {
      const session = await readSession(id)
      const pages = [], seen = new Set()
      let cursor
      do {
        const page = await host.message.list({ sessionID: id, ...(cursor ? { cursor } : { order: "asc", limit: 200 }) })
        pages.push(...page.data)
        cursor = page.cursor.next || undefined
        if (cursor && seen.has(cursor)) throw new Error("OpenCode repeated a message pagination cursor")
        if (cursor) seen.add(cursor)
      } while (cursor)
      return pages.map(message => projectV2Message(message, session))
    }
    // The queue engine's internal host seam retains its existing call shapes.
    // All operations below use the released V2 public APIs.
    const client = { session: {
      get: request => get(request.path.id),
      messages: request => messages(request.path.id),
      status: () => host.session.active(),
      fork: request => host.session.fork({ sessionID: request.path.id }),
      create: request => ctx.session.create({ location: { directory }, title: request.body.title, metadata: request.body.metadata }),
      update: request => ctx.session.update({ sessionID: request.path.id, title: request.body.title,
        ...(request.body.metadata ? { metadata: request.body.metadata } : {}),
        ...(request.body.permission ? { permissions: request.body.permission } : {}) }),
      prompt: async request => {
        const sessionID = request.path.id
        const selection = request.body
        await ctx.session.switchAgent({ sessionID, agent: selection.agent })
        await ctx.session.switchModel({ sessionID, model: { providerID: selection.model.providerID, id: selection.model.modelID,
          ...(selection.variant ? { variant: selection.variant } : {}) } })
        await ctx.session.prompt({ sessionID, text: selection.parts[0].text, delivery: "queue" })
        await ctx.session.wait({ sessionID })
      },
      abort: async request => {
        await ctx.session.interrupt({ sessionID: request.path.id, continue: false })
        await ctx.session.wait({ sessionID: request.path.id })
      },
    } }
    hooks = await createPlugin({ client, directory, serverUrl: connection.url, hostVersion: ctx.app.version },
      { ...ctx.options, ...options }, { ...dependencies, hostVersion: ctx.app.version, diagnostic })
    if (!hooks.dispose) return
    const skills = await hooks["native.skills"]()
    registrations.push(await ctx.skill.transform(editor => {
      for (const skill of skills) if (!editor.get(skill.id)) editor.add(skill)
    }))
    registrations.push(await ctx.session.hook("context", async event => {
      const internal = hooks["native.internal"](event.sessionID)
      try {
        const session = await readSession(event.sessionID)
        const observed = await registry({ ...session, agent: event.agent, model: event.model })
        const history = await messages(event.sessionID)
        const latest = history.findLast(message => message.info.role === "user" && !message.info.synthetic)?.info
        const input = { sessionID: event.sessionID, agent: event.agent, model: { ...observed.model,
          variants: { default: {}, ...Object.fromEntries(observed.model.variants.map(variant => [variant.id, variant.settings || {}])) } },
          message: latest, registryFingerprint: observed.fingerprint, toolFingerprint: fingerprint(event.tools),
          variant: event.model.variant ?? null,
          unavailableDefaults: observed.unavailable, permissionFingerprint: fingerprint(session.permissions || []), affinity: session.parentID || session.fork?.sessionID || session.id }
        const system = { system: event.system }
        await hooks["experimental.chat.system.transform"](input, system)
        event.system = system.system
        const output = { options: Object.fromEntries(Object.entries(event.options).filter(([key]) => !generationKeys.includes(key))),
          temperature: event.options.temperature, topP: event.options.topP, topK: event.options.topK, maxOutputTokens: event.options.maxTokens }
        // V2's native cache key is not a semantic provider option. A plugin's
        // custom key override cannot be represented as observed native parity.
        if ("promptCacheKey" in output.options) input.unavailableDefaults.push("cache-key-override")
        await hooks["chat.params"](input, output)
        for (const key of Object.keys(event.options)) delete event.options[key]
        Object.assign(event.options, output.options)
        for (const key of generationKeys) {
          const value = output[key === "maxTokens" ? "maxOutputTokens" : key]
          if (value !== undefined) event.options[key] = value
        }
      } catch (error) {
        if (internal) throw error
        hooks["native.invalidate"](event.sessionID)
        diagnostic(`parent profile unavailable: ${error.message}`)
      }
    }))
    registrations.push(await ctx.session.hook("model.request", async event => {
      if (event.kind !== "primary") return
      await hooks["chat.headers"](event, event)
    }))
    registrations.push(await ctx.session.hook("retry", event => hooks["native.retry"](event)))
    for (const kind of ["compaction", "generate"]) registrations.push(await ctx.session.hook(kind, event => hooks["native.auxiliary"](event)))
    registrations.push(await ctx.session.hook("title", event => {
      try { hooks["native.auxiliary"](event) } catch { event.result = "Skill review" }
    }))
    registrations.push(await ctx.tool.hook("execute.before", event => hooks["tool.execute.before"](event)))
    registrations.push(await ctx.tool.hook("execute.after", async event => {
      if (event.status !== "completed" || event.tool !== "skill") return
      await hooks["tool.execute.after"]({ ...event, args: { name: event.input?.id } },
        { metadata: { ...event.result.metadata, dir: event.result.metadata?.directory }, output: textContent(event.result.content) })
    }))
    controller = new AbortController()
    subscription = (async () => {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        if (event.location?.directory && resolve(event.location.directory) !== resolve(directory)) continue
        const properties = { ...event.data }, id = properties.sessionID
        if (id && !event.location?.directory) {
          const session = await ctx.session.get({ sessionID: id }).catch(() => null)
          if (!session || resolve(session.location.directory) !== resolve(directory)) continue
        }
        let type = event.type
        if (type === "session.inbox.enqueued" && properties.item?.type === "user") {
          await hooks.event({ event: { type: "message.updated", properties: { info: { id: properties.inboxID, sessionID: id, role: "user" } } } })
          continue
        }
        if (type === "session.execution.started") { type = "session.status"; properties.status = { type: "busy" } }
        if (["session.execution.succeeded", "session.execution.failed", "session.execution.interrupted"].includes(type)) type = "session.idle"
        if (type === "session.renamed") { type = "session.updated"; properties.info = { id, title: properties.title } }
        if (["session.step.started", "session.step.ended", "session.step.failed", "session.tool.success", "session.tool.failed"].includes(type)) type = "message.part.updated"
        await hooks.event({ event: { type, properties } })
      }
    })().catch(async error => {
      if (!controller.signal.aborted) {
        diagnostic(`event subscription stopped: ${error.message}`)
        await hooks.dispose()
      }
    })
    await hooks.config({})
    return async () => {
      controller.abort()
      await hooks.dispose()
      await Promise.all(registrations.map(registration => registration.dispose()))
      await subscription
    }
  } catch (error) {
    diagnostic(`learning disabled: ${error.message}`)
    controller?.abort()
    await hooks?.dispose?.()
    await Promise.all(registrations.map(registration => registration.dispose()))
  }
}

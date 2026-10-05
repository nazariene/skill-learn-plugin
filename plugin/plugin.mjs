import { createHash, randomUUID } from "node:crypto"
import { homedir } from "node:os"
import { resolve } from "node:path"
import { realpathSync } from "node:fs"
import { ChildCore } from "./child.mjs"
import { NativeReviews, ParentProfiles, supportedHostVersion, responseData, readMessages, transcriptWatermark, portableOptions, portableHeaders } from "./native.mjs"

const AUXILIARY = new Set(["title", "summary", "compaction", "user-memory-collector", "project-memory-collector", "skill-learner", "skill-learning-review", "skill-evaluator", "skill-grader"])
const hash = value => createHash("sha256").update(JSON.stringify(value)).digest("hex")
const sessionIDOf = event => event.properties?.sessionID || event.properties?.info?.sessionID || event.properties?.part?.sessionID || (event.type?.startsWith("session.") ? event.properties?.info?.id : undefined)
const alive = pid => { try { if (!Number.isInteger(pid)) return null; process.kill(pid, 0); return true } catch (error) { return error.code === "ESRCH" ? false : null } }

export async function createPlugin(input, options = {}, dependencies = {}) {
  const diagnostic = dependencies.diagnostic || (message => console.error(`skill-learn: ${message}`))
  const config = process.env.OPENCODE_CONFIG_DIR || resolve(process.env.XDG_CONFIG_HOME || resolve(homedir(), ".config"), "opencode")
  let pluginDirectory = resolve(config, "plugins")
  try { pluginDirectory = realpathSync(pluginDirectory) } catch {}
  let defaultHome = resolve(pluginDirectory, "skill-learn")
  try { defaultHome = realpathSync(defaultHome) } catch {}
  const home = resolve((options.home || process.env.SKILL_LEARN_HOME || defaultHome).replace(/^~(?=\/|$)/, homedir()))
  const owner = randomUUID()
  // In-process OpenCode clients have separate execution maps even when their
  // session DB is shared. A PID distinguishes owners of a paid native call.
  const hostID = hash([String(input.serverUrl || "in-process"), input.directory, dependencies.pid ?? process.pid])
  const hostScope = hash([String(input.serverUrl || "in-process"), input.directory])
  const hostPID = dependencies.pid ?? process.pid
  let child, settings, disposed = false, disabled = false, pumping = false, pumpAgain = false
  const reviewers = new Map(), states = new Map(), buffered = []
  const setTimer = dependencies.setTimer || setTimeout
  const clearTimer = dependencies.clearTimer || clearTimeout
  const startCore = dependencies.startCore || (options => new ChildCore(options))
  const background = promise => { promise.catch(error => diagnostic(error.message)) }
  const profiles = new ParentProfiles({ reviewers, diagnostic, hostVersion: input.hostVersion || dependencies.hostVersion || null })
  const native = new NativeReviews({ client: input.client, directory: input.directory, reviewers,
    bind: payload => child.request("bind", { ...payload, owner }) })
  const profileHooks = profiles.hooks()

  async function abortOwned() {
    const settled = new Set()
    for (const [id, review] of reviewers) {
      if (review.hostID !== hostID || review.terminal) continue
      try { await abortSettled(id); settled.add(id) } catch (error) { diagnostic(error.message) }
    }
    return settled
  }

  async function abortSettled(id) {
    await native.abort(id)
    const statuses = responseData(await input.client.session.status({ query: { directory: input.directory }, throwOnError: true }))
    if (statuses[id]?.type && statuses[id].type !== "idle") throw new Error("Native abort has not settled; claim retained for reconciliation")
  }

  try {
    if (!supportedHostVersion(profiles.hostVersion)) throw new Error("Unsupported OpenCode V2 service version")
    if (Object.keys(options).some(key => !["home", "python"].includes(key))) throw new Error("Unknown skill-learn plugin option")
    child = dependencies.core || startCore({ home, python: options.python || "python3", diagnostic,
      onFailure: () => { disabled = true; background(abortOwned()) } })
    settings = await child.request("hello")
    if (!dependencies.core && !options.python && settings.python !== "python3") {
      await child.dispose()
      child = startCore({ home, python: settings.python, diagnostic, onFailure: () => { disabled = true; background(abortOwned()) } })
      settings = await child.request("hello")
    }
    await preload()
  } catch (error) {
    diagnostic(`learning disabled: ${error.message}`)
    await child?.dispose?.()
    return {}
  }

  function stateFor(id) {
    if (!states.has(id)) states.set(id, { idle: false, users: new Set(), turns: 0, timer: null, submitted: null, name: null, submitting: false })
    return states.get(id)
  }

  function disarm(state) {
    if (state.timer !== null) clearTimer(state.timer)
    state.timer = null
  }

  function arm(id) {
    const state = stateFor(id)
    if (disposed || disabled || !state.idle || reviewers.has(id)) return
    const turnsDue = settings.triggers.turns.enabled && state.turns >= settings.triggers.turns.count
    if (!turnsDue && !settings.triggers.idle.enabled) return
    disarm(state)
    state.timer = setTimer(() => background(submit(id, turnsDue ? "turns" : "idle")), turnsDue ? 0 : settings.triggers.idle.seconds * 1000)
    state.timer?.unref?.()
  }

  async function preload() {
    const saved = await child.request("state", { hostID })
    for (const row of saved.reviewers) {
      if (reviewers.has(row.reviewer_id)) continue
      const plan = JSON.parse(row.plan_json || "{}")
      reviewers.set(row.reviewer_id, { ...plan, reviewID: row.id, hostID: row.host_id, hostPID: row.host_pid,
        state: "preparing", terminal: row.status !== "running", inherited: new Set(JSON.parse(row.inherited_json || "[]")),
        attempts: new Map(), pendingCalls: [], seenSteps: new Set(), seenParts: new Map() })
      try {
        await native.mark(row.reviewer_id, row.id)
        reviewers.get(row.reviewer_id).state = "ready"
      } catch (error) {
        if (!error.message.includes("404") && !error.message.includes("NotFound") && !error.message.includes("not found")) throw error
      }
    }
    if (saved.active && (saved.active.lease_until || 0) <= Date.now() / 1000) await reconcile(saved.active)
  }

  async function submission(id, trigger) {
    const { session, messages } = await native.read(id)
    if (!session?.id || !messages.length || AUXILIARY.has(session.agent)) return null
    const latestUser = messages.findLast(message => message.info?.role === "user")?.info
    const observed = profiles.parents.get(id)
    const profile = observed || { model: latestUser?.model ? { providerID: latestUser.model.providerID, modelID: latestUser.model.modelID } : undefined,
      variant: latestUser?.model?.variant ?? latestUser?.variant ?? null, agent: latestUser?.agent || session.agent || "build", limitations: ["Parent public-hook observations unavailable"] }
    const selected = settings.selection === "follow" && profile.model?.providerID ? { ...profile.model, variant: profile.variant } : {
      providerID: settings.model.split("/")[0], modelID: settings.model.split("/").slice(1).join("/"), variant: settings.variant }
    const compatibility = profiles.compatibility(session, messages, selected)
    const last = messages.findLast(message => message.info?.role === "assistant" && message.info.finish && !message.info.summary)
    const counters = last?.info?.tokens
    const values = [counters?.input, counters?.cache?.read, counters?.cache?.write]
    const parentInputTokens = values.every(value => Number.isFinite(value) && value >= 0) ? values.reduce((a,b) => a + b, 0) : null
    const output = [counters?.output, counters?.reasoning]
    const parentOutputTokens = output.every(value => Number.isFinite(value) && value >= 0) ? output.reduce((a,b) => a + b, 0) : null
    let delegateDepth = 0, parentID = session.parentID
    const seen = new Set([id])
    while (parentID && !seen.has(parentID)) {
      seen.add(parentID); delegateDepth++
      const parent = responseData(await input.client.session.get({ path: { id: parentID }, query: { directory: input.directory }, throwOnError: true }))
      parentID = parent?.parentID
    }
    return { harness: "opencode", hostID, hostPID, hostScope, directory: input.directory, sessionID: id, sessionName: session.title,
      watermark: transcriptWatermark(messages), messages, trigger, delegateDepth, profile, compatibility,
      permission: session.permission || [], parentInputTokens, parentOutputTokens }
  }

  async function submit(id, trigger) {
    const state = stateFor(id)
    state.timer = null
    if (!state.idle || disposed || disabled || state.submitting || reviewers.has(id)) return
    state.submitting = true
    try {
      const body = await submission(id, trigger)
      if (!body || !state.idle) return
      if (body.watermark === state.submitted && body.sessionName === state.name) return
      const answer = await child.request("enqueue", body)
      state.submitted = body.watermark; state.name = body.sessionName; state.turns = 0
      for (const target of answer.abort || []) {
        if (target.hostID === hostID && target.reviewerID) {
          await abortSettled(target.reviewerID)
          await collect(target.reviewerID, true)
        }
      }
      background(pump())
    } finally { state.submitting = false }
  }

  async function recordSnapshot(id) {
    const review = reviewers.get(id)
    if (!review || review.terminal || review.hostID !== hostID) return []
    const messages = await readMessages(input.client, input.directory, id)
    for (const message of messages) {
      if (review.inherited?.has(message.info.id)) continue
      if (message.info.role === "assistant" && !review.attempts.has(message.info.id) && review.pendingCalls?.length) {
        const attempt = review.pendingCalls.shift()
        review.attempts.set(message.info.id, attempt)
        await child.request("record", { reviewID: review.reviewID, eventID: `call-start:${message.info.id}`, callID: attempt.callID,
          event: { kind: "host_call_started", messageID: message.info.id } })
      }
      for (const part of message.parts || []) {
        const eventID = part.type === "step-finish" ? `step:${part.id}` : `part:${part.id}:${hash(part)}`
        if (part.type === "step-finish" && review.seenSteps.has(part.id)) continue
        if (part.type !== "step-finish" && review.seenParts.get(part.id) === hash(part)) continue
        const attempt = review.attempts.get(message.info.id)
        await child.request("record", { reviewID: review.reviewID, eventID,
          event: { messageID: message.info.id, part, assistant: message.info, ...(part.type === "step-finish" ? { message } : {}) }, callID: attempt?.callID,
          durationSeconds: attempt ? (Date.now() - attempt.started) / 1000 : null })
        if (part.type === "step-finish") review.seenSteps.add(part.id)
        else review.seenParts.set(part.id, hash(part))
      }
    }
    return messages.filter(message => !review.inherited?.has(message.info.id))
  }

  async function collect(id, cancelled = false, reconcileState = false) {
    const review = reviewers.get(id)
    if (!review || review.terminal || review.collecting) return
    review.collecting = true
    try {
      const messages = await recordSnapshot(id)
      const last = messages.findLast(message => message.info.role === "assistant")
      const text = (last?.parts || []).filter(part => part.type === "text").map(part => part.text).join("\n")
      const error = review.stopReason || (last?.info?.error ? JSON.stringify(last.info.error) : !last?.info?.finish ? "Native reviewer interrupted before a terminal result" : undefined)
      const payload = { reviewID: review.reviewID, text, messages, error }
      if (cancelled) await child.request("cancel", { reviewID: review.reviewID, hostSettled: true })
      else await child.request(reconcileState ? "reconcile" : "finish", { ...payload, ...(reconcileState ? { state: "finished" } : {}) })
      review.terminal = true
    } finally { review.collecting = false }
  }

  async function reconcile(row) {
    if (row.host_id !== hostID && alive(row.host_pid) !== false) return
    if (!row.reviewer_id) {
      await child.request("reconcile", { reviewID: row.id, state: "abandoned" })
      return
    }
    try {
      const read = await native.read(row.reviewer_id)
      if (!read.session?.id) { await child.request("reconcile", { reviewID: row.id, state: "missing" }); return }
      const review = reviewers.get(row.reviewer_id)
      const context = await child.request("show", { id: row.id })
      for (const call of context.calls) {
        if (call.payload?.assistantID) review.attempts.set(call.payload.assistantID, { callID: call.id, started: Date.parse(call.created_at) })
        else if (call.call_status === "attempted" && !review.pendingCalls.some(attempt => attempt.callID === call.id)) review.pendingCalls.push({ callID: call.id, started: Date.parse(call.created_at) })
        if (call.response?.host_step?.id) review.seenSteps.add(call.response.host_step.id)
      }
      const statuses = responseData(await input.client.session.status({ query: { directory: input.directory }, throwOnError: true }))
      if (statuses[row.reviewer_id]?.type && statuses[row.reviewer_id].type !== "idle") {
        if (row.host_id === hostID) await child.request("reconcile", { reviewID: row.id, state: "active", owner })
        return
      }
      // Reconnect to a finished native session without prompting it again.
      review.hostID = hostID
      await collect(row.reviewer_id, Boolean(row.cancel_requested), true)
    } catch (error) {
      if (error.message.includes("404") || error.message.includes("NotFound") || error.message.includes("not found")) await child.request("reconcile", { reviewID: row.id, state: "missing" })
      else { diagnostic(`reconciliation requires attention: ${error.message}`); await child.request("reconcile", { reviewID: row.id, state: "ambiguous" }) }
    }
  }

  async function pump() {
    if (disposed || disabled) return
    if (pumping) { pumpAgain = true; return }
    pumping = true
    try {
      await preload()
      let answer = await child.request("claim", { owner, hostID, hostScope, hostPID })
      if (answer.active) {
        if (answer.active.cancel_requested && answer.active.host_id === hostID && answer.active.reviewer_id) {
          await abortSettled(answer.active.reviewer_id)
          await collect(answer.active.reviewer_id, true)
        } else if (answer.needsReconcile) await reconcile(answer.active)
        return
      }
      if (!answer.plan) {
        const probe = await child.request("next-probe")
        if (!probe) return
        try {
          if (!supportedHostVersion(profiles.hostVersion)) throw new Error("Diagnostic retry bounds require the supported host version")
          const body = await submission(probe.parent_id, "diagnostic")
          if (!body || body.delegateDepth) throw new Error("Probe requires a settled coding parent")
          const status = responseData(await input.client.session.status({ query: { directory: input.directory }, throwOnError: true }))
          if (status[probe.parent_id]?.type && status[probe.parent_id].type !== "idle") throw new Error("Probe parent is busy")
          answer = await child.request("prepare-probe", { probeID: probe.id, owner, submission: body })
        } catch (error) {
          await child.request("probe-failed", { probeID: probe.id, error: error.message })
          diagnostic(error.message)
          return
        }
      }
      const plan = answer.plan
      let id
      try {
        id = await native.prepare(plan)
        const registered = reviewers.get(id)
        Object.assign(registered, { hostID, hostPID, inherited: new Set(), attempts: new Map(), pendingCalls: [], seenSteps: new Set(), seenParts: new Map() })
        const saved = await child.request("state", { hostID })
        registered.inherited = new Set(JSON.parse(saved.reviewers.find(row => row.reviewer_id === id)?.inherited_json || "[]"))
        while (buffered.length) await processEvent(buffered.shift())
        const heartbeat = setInterval(() => {
          background(child.request("heartbeat", { reviewID: plan.reviewID, owner }).then(answer => {
            if (!answer.active || answer.cancelRequested) background(abortSettled(id))
          }))
        }, Math.max(250, Math.min(5000, settings.leaseSeconds * 500)))
        heartbeat.unref?.()
        try {
          await native.prompt(id, plan)
          if (plan.diagnostic && !registered.stopReason) {
            await recordSnapshot(id)
            await native.prompt(id, { ...plan, instruction: "Reply exactly: Nothing to save." })
          }
        } finally { clearInterval(heartbeat) }
        await collect(id)
      } catch (error) {
        if (id) {
          const registered = reviewers.get(id)
          registered.stopReason ||= error.message
          await abortSettled(id)
          await collect(id)
        } else await child.request("finish", { reviewID: plan.reviewID, error: error.message })
      }
    } finally {
      pumping = false
      if (pumpAgain) {
        pumpAgain = false
        background(pump())
      }
    }
  }

  async function processEvent(event) {
    if (event.type === "server.instance.disposed") { await dispose(); return }
    const id = sessionIDOf(event)
    if (!id || disposed) return
    const review = reviewers.get(id)
    if (review) {
      if (event.type === "session.status" && event.properties?.status?.type === "retry" && review.diagnostic) review.retryBlocked = true
      // The prompt promise owns normal completion. Restart reconciliation owns
      // abandoned completion; idle events never create another submission.
      if (event.type === "message.part.updated" && review.hostID === hostID && !review.terminal) background(recordSnapshot(id))
      return
    }
    const state = stateFor(id)
    const info = event.properties?.info || {}
    if (event.type === "session.deleted") { disarm(state); states.delete(id); profiles.parents.delete(id); return }
    if (event.type === "message.updated" && info.role === "user" && info.id && !state.users.has(info.id) && !info.synthetic && !info.summary) {
      state.users.add(info.id); state.turns++; state.idle = false; disarm(state)
    }
    if (event.type === "session.status" || event.type === "session.idle") {
      state.idle = event.type === "session.idle" || event.properties?.status?.type === "idle"
      if (state.idle) arm(id)
      else disarm(state)
    }
    if (event.type === "session.updated" && state.idle && state.submitted && info.title !== state.name) background(submit(id, "rename"))
  }

  async function dispose() {
    if (disposed) return
    disposed = true
    clearTimer(poll)
    for (const state of states.values()) disarm(state)
    const settled = await abortOwned()
    if (!child.closed) {
      for (const [id, review] of reviewers) if (settled.has(id) && !review.terminal) {
        await child.request("cancel", { reviewID: review.reviewID, hostSettled: true }).catch(error => diagnostic(error.message))
      }
    }
    await child.dispose?.()
  }

  let poll
  function schedulePoll() {
    if (disposed || disabled) return
    poll = setTimer(() => { background(pump().finally(schedulePoll)) }, Math.min(5000, settings.leaseSeconds * 500))
    poll?.unref?.()
  }
  schedulePoll()

  return {
    dispose,
    config: async config => {
      config.skills ||= {}
      config.skills.paths ||= []
      if (!config.skills.paths.includes(settings.libraryRoot)) config.skills.paths.push(settings.libraryRoot)
      background(pump())
    },
    event: async ({ event }) => {
      if (native.creating && !reviewers.has(sessionIDOf(event))) { buffered.push(event); return }
      try { await processEvent(event) } catch (error) { diagnostic(error.message) }
    },
    "experimental.chat.system.transform": async (hookInput, output) => {
      await profileHooks["experimental.chat.system.transform"](hookInput, output)
      const review = reviewers.get(hookInput.sessionID)
      if (review) review.observedSystem = [...output.system]
    },
    "chat.headers": async (hookInput, output) => {
      await profileHooks["chat.headers"](hookInput, output)
      const review = reviewers.get(hookInput.sessionID)
      if (review?.currentCallID && !review.terminal) await child.request("record", {
        reviewID: review.reviewID, eventID: `headers:${review.currentCallID}`,
        event: { kind: "host_observed_headers", callID: review.currentCallID, headers: portableHeaders(output.headers), fidelity: "host_observed", wireBody: null },
      })
    },
    "chat.params": async (hookInput, output) => {
      const review = reviewers.get(hookInput.sessionID)
      if (!review) {
        if (!AUXILIARY.has(hookInput.agent)) await profileHooks["chat.params"](hookInput, output)
        return
      }
      if (disposed || disabled || review.state !== "ready" || review.terminal || review.hostID !== hostID || AUXILIARY.has(hookInput.agent)) throw new Error("Internal reviewer call is not eligible")
      if (review.model?.variant && !(review.model.variant in (hookInput.model?.variants || {}))) throw new Error(`Unsupported host variant: ${review.model.variant}`)
      if (review.retryBlocked) throw new Error("Diagnostic provider retries are disabled")
      try {
        await profileHooks["chat.params"](hookInput, output)
        if (review.mode === "fork" && (hookInput.registryFingerprint !== review.profile.registryFingerprint || hookInput.toolFingerprint !== review.profile.toolFingerprint)) throw new Error("Native reviewer registry/tool scope differs from the admitted parent profile")
        await recordSnapshot(hookInput.sessionID)
        const answer = await child.request("admit", { reviewID: review.reviewID, reviewerID: hookInput.sessionID, owner,
          contextWindow: hookInput.model?.limit?.context, profile: { model: review.model, agent: hookInput.agent,
            system: review.observedSystem, options: portableOptions(output.options),
            temperature: output.temperature, topP: output.topP, topK: output.topK, maxOutputTokens: output.maxOutputTokens, fidelity: "host_observed" } })
        if (!answer.allowed) { review.stopReason = `Review stopped: ${answer.reason}`; throw new Error(review.stopReason) }
        review.pendingCalls.push({ callID: answer.callID, started: Date.now() })
        review.currentCallID = answer.callID
      } catch (error) { review.stopReason ||= error.message; throw error }
    },
    "tool.execute.before": async (hookInput) => {
      const review = reviewers.get(hookInput.sessionID)
      if (!review) return
      if (disposed || disabled || review.terminal || review.hostID !== hostID || hookInput.tool !== "skill" || review.diagnostic) throw new Error("Internal reviews may only load skills; this tool cannot execute")
    },
    "tool.execute.after": async (hookInput, output) => {
      const review = reviewers.get(hookInput.sessionID)
      if (!review || hookInput.tool !== "skill") return
      const location = output.metadata?.dir
      let compatible = false
      if (location) {
        const actual = realpathSync(location), root = realpathSync(settings.libraryRoot)
        compatible = actual === root || actual.startsWith(root + "/")
      }
      await child.request("skill-loaded", { reviewID: review.reviewID, name: hookInput.args?.name || output.metadata?.name, location, body: output.output, discoveryCompatible: compatible })
      if (!compatible) throw new Error("Skill discovery location is unavailable or resolves to a different library root")
    },
    "native.retry": async event => {
      const review = reviewers.get(event.sessionID)
      if (review) {
        event.decision = { retry: false }
        review.retryBlocked = true
        review.stopReason ||= "Native provider retries are disabled for internal reviewers"
      }
    },
    "native.auxiliary": event => {
      if (reviewers.has(event.sessionID)) throw new Error("Auxiliary model calls are disabled for internal reviewers")
    },
    "native.skills": () => child.request("host-skills"),
    "native.internal": id => reviewers.has(id),
    "native.invalidate": id => profiles.parents.delete(id),
  }
}

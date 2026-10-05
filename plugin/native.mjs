import { createHash } from "node:crypto"

export const V2_HOST_VERSIONS = Object.freeze(["2.0.6", "2.0.21"])
export const supportedHostVersion = version => V2_HOST_VERSIONS.includes(version)

export function reviewMetadata(reviewID, metadata = {}) {
  return { ...metadata, automation: { ...metadata?.automation, owner: "skill-learn", kind: "skill-review", reviewID,
    suppressAudio: true, suppressMemoryCollection: true } }
}

const OPTION_TYPES = {
  instructions: "string", promptCacheKey: "string", reasoningEffort: "string",
  reasoningSummary: "string", textVerbosity: "string", store: "boolean",
  parallelToolCalls: "boolean", serviceTier: "string", include: "strings",
}
const AFFINITY_HEADERS = new Set(["session-id", "x-session-affinity", "x-session-id", "originator"])
const PARAMS = ["temperature", "topP", "topK", "maxOutputTokens"]

export function responseData(response) {
  if (response?.error) throw new Error(`OpenCode request failed: ${JSON.stringify(response.error)}`)
  return response && typeof response === "object" && "data" in response ? response.data : response
}

export function transcriptWatermark(messages) {
  // Display metadata is deliberately outside the transcript identity.
  return createHash("sha256").update(JSON.stringify(messages)).digest("hex")
}

export async function readMessages(client, directory, sessionID) {
  const pages = [], cursors = new Set()
  let before
  do {
    const response = await client.session.messages({
      path: { id: sessionID }, query: { directory, ...(before ? { before } : {}) },
      throwOnError: true,
    })
    const page = responseData(response)
    if (!Array.isArray(page)) throw new Error("OpenCode returned an invalid message page")
    pages.unshift(page)
    const link = response?.response?.headers?.get?.("link")
    const next = link?.match(/<([^>]+)>;\s*rel="?next"?/)?.[1]
    before = next ? new URL(next, "http://localhost").searchParams.get("before") : undefined
    if (before && cursors.has(before)) throw new Error("OpenCode repeated a message pagination cursor")
    if (before) cursors.add(before)
  } while (before)
  return pages.flat()
}

export function portableOptions(options = {}) {
  const values = {}, unavailable = []
  for (const [key, value] of Object.entries(options)) {
    const type = OPTION_TYPES[key]
    if (type === "strings" && Array.isArray(value) && value.every(entry => typeof entry === "string")) {
      values[key] = [...value]
    } else if (type && typeof value === type) {
      values[key] = value
    } else if (value !== undefined) {
      // Record names only: an unknown setting may itself be a credential.
      unavailable.push(key)
    }
  }
  return { values, unavailable }
}

export function portableHeaders(headers = {}) {
  return Object.fromEntries(Object.entries(headers).filter(([key, value]) => AFFINITY_HEADERS.has(key.toLowerCase()) && typeof value === "string"))
}

export class ParentProfiles {
  constructor({ reviewers = new Map(), diagnostic = () => {}, hostVersion = null } = {}) {
    this.parents = new Map()
    this.reviewers = reviewers
    this.diagnostic = diagnostic
    this.hostVersion = hostVersion
  }

  hooks() {
    return {
      "experimental.chat.system.transform": async (input, output) => {
        if (!input.sessionID) return
        const review = this.reviewers.get(input.sessionID)
        if (review?.mode === "fork") {
          output.system.splice(0, output.system.length, ...review.profile.system)
          return
        }
        if (review) return
        try {
          if (!Array.isArray(output.system) || !output.system.every(value => typeof value === "string" || (value.type === "text" && typeof value.text === "string"))) return
          this.parents.set(input.sessionID, { ...this.parents.get(input.sessionID), system: structuredClone(output.system) })
        } catch (error) { this.diagnostic(error.message) }
      },
      "chat.params": async (input, output) => {
        const review = this.reviewers.get(input.sessionID)
        if (review?.mode === "fork") {
          const profile = review.profile
          if (input.agent !== profile.agent || input.model?.providerID !== profile.model.providerID || input.model?.id !== profile.model.modelID) {
            throw new Error("Native reviewer agent/model differs from the admitted parent profile")
          }
          const observed = portableOptions(output.options)
          if (observed.unavailable.length) throw new Error("Native reviewer has unpreservable provider options")
          for (const key of Object.keys(output.options)) delete output.options[key]
          Object.assign(output.options, profile.options)
          for (const key of PARAMS) output[key] = profile.params[key]
          return
        }
        if (review) return
        try {
          const observed = portableOptions(output.options)
          this.parents.set(input.sessionID, {
            ...this.parents.get(input.sessionID),
            fidelity: "host_observed", hostVersion: this.hostVersion,
            turnID: input.message?.id, agent: input.agent,
            model: { providerID: input.model?.providerID, modelID: input.model?.id },
            variant: input.variant ?? null,
            contextWindow: input.model?.limit?.context ?? null,
            options: observed.values, unavailableOptions: observed.unavailable,
            params: Object.fromEntries(PARAMS.map(key => [key, output[key]])),
            registryFingerprint: input.registryFingerprint, toolFingerprint: input.toolFingerprint,
            unavailableDefaults: input.unavailableDefaults, permissionFingerprint: input.permissionFingerprint, affinity: input.affinity,
            limitations: ["Observations precede later plugin transformations; final wire/tool catalogue is unavailable"],
          })
        } catch (error) {
          this.parents.delete(input.sessionID)
          this.diagnostic(error.message)
        }
      },
      "chat.headers": async (input, output) => {
        const review = this.reviewers.get(input.sessionID)
        if (review?.mode === "fork") {
          Object.assign(output.headers, review.profile.headers)
          return
        }
        if (review) return
        try {
          const profile = this.parents.get(input.sessionID)
          if (!profile) return
          const headers = {}
          for (const [key, value] of Object.entries(output.headers)) {
            if (AFFINITY_HEADERS.has(key.toLowerCase()) && typeof value === "string") headers[key] = value
          }
          profile.headers = headers
        } catch (error) { this.diagnostic(error.message) }
      },
    }
  }

  compatibility(session, messages, selected, { agent = selected.agent, restorePermission = true } = {}) {
    const profile = this.parents.get(session.id)
    const latest = messages.findLast(message => message.info.role === "user" && !message.info.synthetic)?.info
    if (!profile?.system?.length || !profile.headers) return { compatible: false, reason: "parent-profile-unavailable" }
    if (!supportedHostVersion(this.hostVersion)) return { compatible: false, reason: "unsupported-host-version" }
    if (!profile.agent || !profile.model.providerID || !profile.model.modelID) return { compatible: false, reason: "parent-profile-incomplete" }
    if (profile.turnID !== latest?.id) return { compatible: false, reason: "parent-profile-stale" }
    if (profile.unavailableOptions.length) return { compatible: false, reason: "unpreservable-provider-options" }
    if (profile.unavailableDefaults?.length) return { compatible: false, reason: "unpreservable-request-defaults" }
    if (!profile.registryFingerprint || profile.registryFingerprint !== session.registryFingerprint) return { compatible: false, reason: "parent-registry-changed" }
    if (profile.permissionFingerprint !== session.permissionFingerprint) return { compatible: false, reason: "parent-permissions-changed" }
    if ((session.model?.variant ?? null) !== profile.variant) return { compatible: false, reason: "parent-variant-changed" }
    if (session.fork || session.parentID) return { compatible: false, reason: "fork-affinity-unpreservable" }
    if (selected.providerID !== profile.model.providerID || selected.modelID !== profile.model.modelID || (selected.variant ?? null) !== profile.variant) {
      return { compatible: false, reason: "selected-model-variant-differs" }
    }
    if (agent && agent !== profile.agent) return { compatible: false, reason: "reviewer-agent-differs" }
    if (session.permission?.length && !restorePermission) return { compatible: false, reason: "session-permission-unpreservable" }
    if (session.permission && !Array.isArray(session.permission)) return { compatible: false, reason: "invalid-session-permission" }
    return { compatible: true, reason: "native-parent-profile", profile: structuredClone(profile) }
  }
}

export class NativeReviews {
  constructor({ client, directory, reviewers = new Map(), bind = async () => {} }) {
    this.client = client
    this.directory = directory
    this.reviewers = reviewers
    this.bind = bind
    this.creating = 0
  }

  async read(sessionID) {
    const session = responseData(await this.client.session.get({ path: { id: sessionID }, query: { directory: this.directory }, throwOnError: true }))
    const messages = await readMessages(this.client, this.directory, sessionID)
    return { session, messages }
  }

  async mark(sessionID, reviewID, session) {
    const get = () => this.client.session.get({ path: { id: sessionID }, query: { directory: this.directory }, throwOnError: true }).then(responseData)
    session ||= await get()
    if (session?.id !== sessionID) throw new Error("Host did not return the registered reviewer session")
    const marker = reviewMetadata(reviewID).automation
    const marked = value => Object.entries(marker).every(([key, expected]) => value?.metadata?.automation?.[key] === expected)
    if (!marked(session)) {
      await this.client.session.update({ path: { id: sessionID }, query: { directory: this.directory },
        body: { metadata: reviewMetadata(reviewID, session.metadata) }, throwOnError: true }).then(responseData)
      session = await get()
    }
    if (session?.id !== sessionID || !marked(session)) throw new Error("Host did not persist reviewer suppression metadata")
  }

  async prepare(plan) {
    if (!["fork", "digest"].includes(plan.mode)) throw new Error("Unknown native review mode")
    if (typeof plan.instruction !== "string" || !plan.instruction.trim()) throw new Error("Review instruction is required")
    this.creating++
    let session
    try {
      if (plan.mode === "fork") {
        const parent = await this.read(plan.parentID)
        const statuses = responseData(await this.client.session.status({ query: { directory: this.directory }, throwOnError: true }))
        if (statuses[parent.session.id]?.type && statuses[parent.session.id].type !== "idle") throw new Error("Parent is no longer settled")
        if (transcriptWatermark(parent.messages) !== plan.watermark) throw new Error("Parent transcript changed before native fork")
        // No messageID: the host's boundary is exclusive; include the settled
        // assistant too. The host alone copies/remaps the inherited history.
        session = responseData(await this.client.session.fork({ path: { id: plan.parentID }, query: { directory: this.directory }, body: {}, throwOnError: true }))
      } else {
        session = responseData(await this.client.session.create({ query: { directory: this.directory }, body: {
          title: `Skill review [${plan.reviewID}]`, metadata: reviewMetadata(plan.reviewID),
        }, throwOnError: true }))
      }
      if (!session?.id || session.id === plan.parentID) throw new Error("Host did not return a distinct reviewer session")
      this.reviewers.set(session.id, { ...plan, state: "preparing" })
      await this.mark(session.id, plan.reviewID, session)
      const inherited = await readMessages(this.client, this.directory, session.id)
      await this.bind({ reviewID: plan.reviewID, reviewerID: session.id, inheritedIDs: inherited.map(message => message.info.id) })
      const permission = plan.mode === "fork" ? plan.permission : undefined
      await this.client.session.update({ path: { id: session.id }, query: { directory: this.directory }, body: {
        title: `Skill review [${plan.reviewID}]`, ...(permission?.length ? { permission } : {}),
      }, throwOnError: true }).then(responseData)
      this.reviewers.set(session.id, { ...plan, state: "ready" })
      return session.id
    } catch (error) {
      if (session?.id) this.reviewers.set(session.id, { ...plan, state: "preparation-failed" })
      throw error
    } finally { this.creating-- }
  }

  async prompt(reviewerID, plan) {
    const registered = this.reviewers.get(reviewerID)
    if (registered?.state !== "ready" || registered.reviewID !== plan.reviewID) throw new Error("Reviewer must be bound before dispatch")
    await this.mark(reviewerID, plan.reviewID)
    const body = {
      model: { providerID: plan.model.providerID, modelID: plan.model.modelID },
      agent: plan.mode === "fork" ? plan.profile.agent : plan.agent,
      ...(plan.model.variant ? { variant: plan.model.variant } : {}),
      parts: [{ type: "text", text: plan.instruction }],
    }
    return responseData(await this.client.session.prompt({ path: { id: reviewerID }, query: { directory: this.directory }, body, throwOnError: true }))
  }

  async abort(reviewerID) {
    if (!this.reviewers.has(reviewerID)) throw new Error("Refusing to abort an unregistered coding session")
    return responseData(await this.client.session.abort({ path: { id: reviewerID }, query: { directory: this.directory }, throwOnError: true }))
  }
}

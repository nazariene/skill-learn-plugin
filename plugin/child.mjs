import { spawn } from "node:child_process"
import { createInterface } from "node:readline"
import { randomUUID } from "node:crypto"
import { existsSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

export function pythonRuntime() {
  const directory = dirname(fileURLToPath(import.meta.url))
  const bundled = resolve(directory, "runtime")
  return existsSync(resolve(bundled, "skill_learn")) ? bundled : resolve(directory, "server")
}

export class ChildCore {
  constructor({ home, python = "python3", runtime = pythonRuntime(), diagnostic = () => {}, timeout = 30000, onFailure = () => {} }) {
    this.pending = new Map()
    this.diagnostic = diagnostic
    this.timeout = timeout
    this.closed = false
    this.child = spawn(python, ["-u", "-m", "skill_learn", "--home", home, "child"], {
      cwd: runtime, env: { ...process.env, PYTHONPATH: runtime }, stdio: ["pipe", "pipe", "pipe"], shell: false,
    })
    this.lines = createInterface({ input: this.child.stdout, crlfDelay: Infinity })
    this.lines.on("line", line => {
      try {
        const response = JSON.parse(line)
        if (response.version !== 1 || typeof response.id !== "string" || typeof response.ok !== "boolean") throw new Error("Invalid core protocol response")
        const pending = this.pending.get(response.id)
        if (!pending || response.op !== pending.operation) throw new Error("Uncorrelated core protocol response")
        this.pending.delete(response.id)
        clearTimeout(pending.timer)
        if (response.ok) pending.resolve(response.result)
        else pending.reject(new Error(response.error?.message || "Learning core rejected the request"))
      } catch (error) { this.fail(error) }
    })
    this.child.stderr.on("data", chunk => diagnostic(String(chunk).slice(0, 2000)))
    this.child.on("error", error => this.fail(error))
    this.child.on("exit", (code, signal) => this.fail(new Error(`Learning child exited (${signal || code})`)))
    this.child.on("exit", () => clearTimeout(this.killTimer))
    this.child.stdin.on("error", error => this.fail(error))
    this.onFailure = onFailure
  }

  fail(error) {
    if (this.closed) return
    this.closed = true
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer)
      pending.reject(error)
    }
    this.pending.clear()
    this.diagnostic(error.message)
    this.child.kill("SIGTERM")
    this.killTimer = setTimeout(() => this.child.kill("SIGKILL"), 2000)
    this.killTimer.unref()
    this.onFailure(error)
  }

  request(operation, payload = {}, identity = randomUUID()) {
    if (this.closed) return Promise.reject(new Error("Learning child is unavailable"))
    if (this.pending.has(identity)) return Promise.reject(new Error("Core request identity is already in flight"))
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail(new Error(`Learning core timed out: ${operation}`)), this.timeout)
      this.pending.set(identity, { resolve, reject, timer, operation })
      // Transcripts remain framed stdin text, never shell arguments.
      this.child.stdin.write(JSON.stringify({ version: 1, id: identity, op: operation, payload }) + "\n", error => {
        if (error) this.fail(error)
      })
    })
  }

  async dispose() {
    const child = this.child
    if (child.exitCode !== null || child.signalCode !== null) return
    this.closed = true
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer)
      pending.reject(new Error("Learning plugin disposed"))
    }
    this.pending.clear()
    await new Promise(resolve => {
      const timeout = setTimeout(() => { child.kill("SIGKILL"); resolve() }, 2000)
      child.once("exit", () => { clearTimeout(timeout); resolve() })
      child.stdin.end()
    })
    this.lines.close()
  }
}

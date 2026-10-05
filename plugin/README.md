# Native adapter and verification

`v2.mjs` registers V2 hooks/transforms/subscriptions and connects the queue engine to public fork/create/read/prompt/interrupt/wait APIs. It leaves inherited history in the host and binds a distinct reviewer before dispatch. `native.mjs` owns portable profiles and the internal execution seam. Unknown options, stale observations, changed registry/permissions and unpreservable scope select digest.

Offline-verified V2 service profiles: **2.0.6** and **2.0.21**. The adapter uses `ctx.app.version` and public service info, verifying the service PID against its own process. Its missing context methods come from the authenticated public `@opencode/client@2.0.21`; discovery never starts another service. Unregistered standalone/embedded hosts are incompatible.

V2 forks inherit model, agent, permissions and instructions. Both supported versions derive the native prompt cache key from the immediate fork source. Version 2.0.6 still uses the new ID in affinity headers, which the reviewer-only request hook restores; 2.0.21 aligns those defaults with the source. Forks of already-forked parents select digest because the public semantic option hook cannot replace the native cache-key field. Typed system parts are retained; runtime tool definitions are fingerprinted, not cloned. Raw body overlays and unknown semantic defaults are conservatively incompatible.

Reviewer sessions carry `metadata.automation` with `owner: skill-learn`, `kind: skill-review`, a durable `reviewID`, and `suppressAudio`/`suppressMemoryCollection` both true. Digest creation stamps it atomically; fork preparation merges/verifies it before binding or dispatch. Every prompt rechecks it, and recovery restores retained reviewers' markers. Consumers read the flags independently; titles and agent changes are not the identity contract. See the [metadata contract](../README.md#background-session-metadata).

```bash
npm ci --ignore-scripts --prefix tests
node --test tests/*.test.mjs
```

Adapter tests use controlled V2 host seams with real Python children. They cover settled triggers, native dispatch/completion waiting, supersession, reviewer exclusion/metadata, call binding, snapshots/guards, failure isolation and root-script installation from another working directory. V2 source fixtures expect unpacked `@opencode/core` releases at `/tmp/opencode/skill-learn-v2-fixtures/<version>/package/` (override the root with `OPENCODE_V2_FIXTURES`). Tests execute unchanged conversion/affinity/usage functions and the corresponding native AI package's protocol lowering without network/provider calls; external-source tests skip when their inputs are absent. Provider compaction checkpoints are outside the controlled conversion fixture; inherited checkpoint data is never rewritten by the adapter.

`plugin.mjs` supplies the durable queue engine; `child.mjs` launches the bundled Python runtime over correlated newline JSON. Root `install.sh` is a Bash installer: it stages dependencies, retains old settings/database files, removes other old package contents and copies the new helpers/Python/client with a generated `index.js` into `plugins/skill-learn/`. The package exports only `index.js`, avoiding duplicate discovery of helpers. Fixture dependencies are excluded. No temporary capability session is needed on V2.

`server/` contains the Python project (`pyproject.toml` and `skill_learn/`). Source adapter runs start Python there; installation copies it into the package's `runtime/` directory. This folder's `package.json`/lockfile supply the installed client dependency; `tests/` owns all test code and fixture dependencies. Only V2 and the current split database/package layout are supported.

## Available evidence

Public V2 context hooks expose typed system/messages and JSON tool definitions before later plugins. The adapter retains portable observations and tool fingerprints, without replacing inherited history or executable schemas. Final wire bodies and authenticated account identity remain unavailable. Tool/MCP preparation stays host-owned.

The released V2 `SessionUsage.tokens` normalizes absent noncached/visible-output/reasoning/cache counters to zero. Source fixtures demonstrate identical output for omissions and explicit zeros. Each V2 assistant is one step; the adapter projects it into core evidence with stable IDs, retaining the raw native message alongside the projection. Call-to-assistant binding is persisted after the assistant appears, so restart recovery can recover it without prompting again.

The approved contract retains host-normalized counts and zeros with explicit fidelity, and labels raw provider usage/original counter availability unavailable. Absent host fields remain unknown and partial host totals stay partial. These values may support admission but must not be represented as complete provider evidence.

# Skill Learn Plugin

An OpenCode plugin that reviews settled coding sessions and maintains a guarded skill library. OpenCode runs native fork/digest sessions and owns model authentication and tool preparation. A plugin-owned Python child keeps settings, SQLite, proposals, publication, sounds and offline HTML reports.

## Project locations

All source folders below are under `/opt/src/personal/agents/`:

| Folder | Role |
|---|---|
| `skill-learn-plugin` | This project: the OpenCode V2 plugin and Python core |
| `skill-learning-service` | Independent HTTP service; original starter source and schema/report reference |
| `skill-learning` | Older implementation extracted from the memory project, with an OpenCode 1.x adapter |

The plugin owns its implementation and has no shared runtime code or dependency on either sibling project. Its installed copy is separate: `~/.config/opencode/plugins/skill-learn/`, currently resolving to `/opt/src/personal/agents/harness/opencode/plugins/skill-learn/` on this machine.

### Source layout

```text
install.sh                     Bash installer
plugin/                        Native adapter and runtime npm manifests
  server/                      Python project: pyproject.toml and skill_learn/
tests/                         Adapter/Python/schema/report tests and fixture npm manifests
docs/settings.example.yaml     Configuration template
```

`plugin/server/skill_learn/` implements the queue, storage, library/publication guards, management, notifications and reports. The adapter launches it as `python -m skill_learn`; the installer bundles it under `runtime/`. Only the review instruction is adapted from Hermes, with its MIT attribution retained.

`plugin/package.json` and its lockfile pin the shipped `@opencode/client@2.0.21` dependency. `tests/package.json` holds offline fixture dependencies. `node_modules/` is ignored at every depth; manifests and lockfiles belong in Git.

## Install

Requires Python **3.11+** with **PyYAML 6**, npm, and an OpenCode V2 managed service. The offline-verified service profiles are **2.0.6** and **2.0.21**; check `opencode api get /api/info`, since the CLI version can differ.

```bash
./install.sh
# Explicit locations/interpreter:
./install.sh --dest "$HOME/.config/opencode/plugins" \
  --home "$HOME/.config/opencode/plugins/skill-learn" --python python3
```

`install.sh` is implemented in Bash and works from any working directory. It checks the selected Python runtime, stages pinned npm dependencies and bundled source, then replaces the old plugin. For options, run `./install.sh --help`.

Replacement keeps `settings.yaml` and database files (`*.sqlite`, `*.sqlite-*`, `*.db`, `*.db-*`), including SQLite sidecars and backups. All other old package contents are removed, including plugin-local skills, reports and operator files, before the new adapter/Python sources are copied. Dependencies are prepared before cleanup.

The installer preserves existing settings and unrelated plugins. Plugin assets and state are installed under `plugins/skill-learn/`: the `index.js` entry, adapter helpers, pinned V2 client dependency, bundled Python, settings, databases, and reports. The skill library defaults to OpenCode's global skills folder. Its package exports only `index.js`, so the helpers are not discovered as separate plugins. **Restart the OpenCode service** after installation. When replacing another learner, disable its triggers before enabling this plugin.

The adapter uses V2's stable `skill-learn` ID and `setup` lifecycle. It discovers the existing service through the public authenticated client and verifies that its PID/version match the plugin host. Standalone/embedded hosts without that service registration are currently incompatible and disable learning with a diagnostic.

Default home: `~/.config/opencode/plugins/skill-learn/` (respecting `OPENCODE_CONFIG_DIR`/`XDG_CONFIG_HOME` and installation symlinks). Reinstallation preserves settings and database files. `--home` or `SKILL_LEARN_HOME` overrides it; a custom installer `--dest` defaults to `<dest>/skill-learn/`. Library/report paths resolve from the selected home; absolute paths and `~` work. The previous service's state and credentials are not imported.

### Database compatibility

`state.sqlite` has the same learning schema as `/opt/src/personal/agents/skill-learning-service`: `submissions`, `reviews`, `model_calls`, `evidence`, `proposals`, and `skill_registry`, with matching columns, types, defaults, and constraints. Both projects own independent implementations; the plugin has no imports, symlinks, or runtime dependency on the standalone service.

Plugin-only leases, native identities, event deduplication, operation receipts, and probes live in adjacent `runtime.sqlite`. Back up both current databases together when retaining plugin state. Only the current V2 package layout and split schema are supported; V1/flat-layout upgrades and combined-schema conversion are removed. The standalone database is never modified.

## Configuration

See [`docs/settings.example.yaml`](docs/settings.example.yaml). Unknown/duplicate keys and invalid types disable learning with a diagnostic while ordinary chat remains usable.

| Group | Important defaults |
|---|---|
| `runtime` | `python: python3`, 60-second execution lease |
| `triggers` | 15 idle seconds; 25 user turns. Either trigger can be disabled |
| `llm` | `selection: follow`, configured fallback `openai/gpt-5.5` / `variant: medium`, 16 calls |
| `review` | `contextMode: auto`; first-fork limit 120000; next-call input limit derived from context |
| `library` | Optional `root`; omitted or `null` uses OpenCode's global skills folder |
| `approval` | Generated skills auto; user-owned manual. User settings never authorize background edits |
| `notifications` | Enabled, volume 1; individual initiated/started/finished/unchanged/failed/cancelled switches |
| `reports` | Enabled under `reports/` |

`configured` uses the configured model/variant through OpenCode. A different pair, unavailable/incompatible parent profile, forced digest or oversized/unassessable fork selects a fresh digest session. Digests contain role-labeled historical evidence, not executable historical tool calls.

`review.maxInputTokens: null` means 75% of a known selected context window, or 120000 when unknown. A positive integer sets the previous-call budget; zero/negative disables it. A call reaching the budget finishes before the next is blocked. `maxForkInputTokens: null` disables the separate initial size guard. `llm.steps` still caps calls. No Python provider fallback exists.

Omit `library`, omit its `root`, or set `library.root: null` to read and publish skills under `~/.config/opencode/skills/`. `OPENCODE_CONFIG_DIR` takes precedence over `XDG_CONFIG_HOME`, and skill-folder symlinks are resolved. This fallback is independent of the plugin home. Explicit paths keep their existing behavior: relative paths resolve from the plugin home, and absolute paths/`~` work. Existing `root: skills` settings still select the plugin-local library; reinstall preserves them.

## Library and review behavior

Reviews can load skills through the existing `skill` tool; other tools cannot execute for internal reviewers. Python applies final JSON feedback. Generated packages carry `metadata.origin: generated`; user-owned, pinned, bundled/hub/external packages and bare deletes are refused. Patches must match uniquely. Manual approval checks the saved base hash. Complete package publication is staged before replacement.

OpenCode's skill catalogue and bodies are an **instance snapshot until restart**. Old bodies and newly published names not yet available are accepted; learning is not restart-gated. Python reads and validates current files before publication/approval. Inherited skill bodies remain in native fork history.

Duplicate transcript watermarks do not run again. Renames update display metadata. Newer work cancels only that parent's review; coding parents are never aborted. Delegates are stored as not reviewed. Durable internal identities prevent recursive reviews, including after restart or record deletion.

One review executes per home across instances. V2 prompt admission is followed by native completion waiting. Interrupted work is reconciled with its native session; ambiguous execution/publication is retained for operator reconciliation, never blindly replayed. Internal model retries and auxiliary model requests are vetoed so they cannot bypass call admission; ordinary parent policy is unchanged. A missing Python/helper disables learning without stopping ordinary chat.

### Background-session metadata

Every reviewer carries this persistent `Session.Info.metadata` contract:

```json
{
  "automation": {
    "owner": "skill-learn",
    "kind": "skill-review",
    "reviewID": "rv_REVIEW_ID",
    "suppressAudio": true,
    "suppressMemoryCollection": true
  }
}
```

Digest sessions receive it at creation. Forks are marked immediately after creation, preserving unrelated metadata; persistence is checked before binding and every prompt. Recovery restores markers for retained reviewer identities, including terminal/deleted learning records. Renaming does not change the marker or the inherited agent/profile.

These flags are a plugin contract, not built-in OpenCode switches. Audio observers must check `metadata.automation.suppressAudio === true` before all notification paths. Memory collectors must check `metadata.automation.suppressMemoryCollection === true` before tracking, capture, dispatch and shutdown, and clear queued/cached work when a session becomes marked. Observe `session.metadata.updated` (`event.data.metadata`) and resolve uncached sessions with `session.get`; V2 forks can have `fork.sessionID` without `parentID`. Suppression does not grant tool permissions or require memory replay. The operator reports memory plugins now consume the memory flag; their implementation/validation is managed separately.

## Commands and reports

The bundled runtime supports management without a separate package install:

```bash
cd "$HOME/.config/opencode/plugins/skill-learn/runtime"
python3 -m skill_learn report
```

Use the interpreter selected with `--python` and the actual installed path if overridden. To get the optional `skill-learn` console command, run `python3 -m pip install ./plugin/server`. The commands below also work as `python3 -m skill_learn` from the bundled runtime; use `--home PATH` before the command for a non-default store:

```bash
skill-learn pending
skill-learn show rv_REVIEW_ID
skill-learn approve sp_PROPOSAL_ID
skill-learn reject sp_PROPOSAL_ID
skill-learn adopt skill-name
skill-learn pin skill-name
skill-learn unpin skill-name
skill-learn report
skill-learn delete-session opencode ses_SESSION_ID
# After confirming its host execution has stopped:
skill-learn reconcile rv_REVIEW_ID --state abandoned
```

Open the reported `index.html` using a file URL. The report matches the standalone dashboard's **Summary**, **Review sessions**, and **Skills & proposals** views, styling, counts, filters, and change summaries. Published skills, pending proposals, and history-only changes stay distinct. Session → review → call grouping loads complete available evidence inline when a call is expanded, with retry and review-detail links. Reports work after OpenCode exits; section navigation and on-demand evidence assets are local files. No listener, fetch, external assets or browser publication controls are used. Management and terminal reviews refresh snapshots; failed generation preserves the last complete report. Active work must be cancelled/reconciled before deletion. Deletion preserves skill files, coding sessions and minimal internal identity tombstones.

Usage is labeled **host-normalized**: uncached input plus cache read/write. Missing host fields remain unknown; partial totals stay partial. The pinned host converts provider omissions to zero, so original provider counter availability/raw usage is unavailable. Final HTTP bodies/response IDs are unavailable; requested/observed host evidence is retained instead. First-call and continuation cache reuse are separate observations, not proof of full conversation-prefix reuse.

Notifications use bundled **JennyNeural** voice recordings, matching user-memory's voice and loudness. Playback is local through PulseAudio/PipeWire or a compatible WAV player; no runtime TTS/network dependency. Voice cues are serialized within the core to avoid overlapping speech. `SKILL_LEARNING_NOTIFICATIONS=0` mutes sounds; zero volume also mutes. Playback failures are evidence, not review failures. See [recording details](plugin/server/skill_learn/sounds/README.md).

## Verification

Offline suites (no model/provider calls):

```bash
PYTHONPATH="$PWD/plugin/server" python3 -m unittest discover -s tests
# Fixture dependencies only:
npm ci --ignore-scripts --prefix tests
node --test tests/*.test.mjs
```

V2 source fixtures read unpacked `@opencode/core` releases from `/tmp/opencode/skill-learn-v2-fixtures/<version>/package/` for 2.0.6 and 2.0.21. Set `OPENCODE_V2_FIXTURES` to another fixture root. External-source tests are skipped when their checkout or fixtures are absent.

Chrome file-URL checks run when `google-chrome` is available. V2 fixtures execute released host conversion/usage functions and the corresponding native `@opencode/ai` protocol lowering, with a fake public-client transport. See [adapter evidence](plugin/README.md).

**Explicit live diagnostic**, with OpenCode running and an idle learning queue:

```bash
skill-learn cache-probe ses_SETTLED_PARENT_ID --mode fork
skill-learn show probe_REQUEST_ID
# Separate digest comparison:
skill-learn cache-probe ses_SETTLED_PARENT_ID --mode digest
```

Each request permits at most two provider calls and 8192 assessed input tokens per call, no tool execution/publication and no automatic paid retry. Unknown/oversized assessment refuses dispatch. Inspect the recorded review for counts and latency; low/missing cache reuse remains unresolved acceptance evidence. Live cache and operator cutover acceptance are separate from offline tests.

### Deployment status

The installed folder layout, schema split and report parity were verified earlier. The optional global-library fallback, project-layout/Bash/V2-only cleanup and reviewer metadata are source updates; they have not been deployed. Installed settings retain explicit `library.root: skills`, selecting the plugin-local library. Skill-learn deployment and sound relocation remain cancelled; cues stay bundled with Python. The authorized audio-plugin files are updated in the harness installation, without a service restart or live-activation claim. Live provider cache/latency and supported-host cutover/restart/rollback acceptance remain pending.

The Hermes-adapted skill-only instruction retains its MIT attribution: Copyright (c) 2025 Nous Research, `NousResearch/hermes-agent` revision `f42f579cf8bac4918ac9599bece71618afadd846`. Cursor and other adapters are deferred.

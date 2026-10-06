# Skill Learn Plugin

## Introduction

An OpenCode V2 plugin that reviews settled coding conversations and turns reusable workflows into skills. It creates or improves agent-managed skills, publishing changes automatically or keeping proposals for approval. User-owned and pinned skills are protected.

OpenCode handles reviewer sessions, models and authentication. A bundled Python child manages the queue, SQLite records, publication, sounds and offline reports. No separate HTTP service or provider credentials are needed.

## Source layout

```text
install.sh                     Bash installer
report.sh                      Generate an offline report using live settings
plugin/                        OpenCode V2 adapter and runtime npm manifests
  server/                      Python package and pyproject.toml
    skill_learn/               Queue, storage, publication, CLI, reports and sounds
tests/                         Python/adapter tests, schema fixtures and test npm manifests
docs/settings.example.yaml     Configuration template
```

The installed package contains the adapter and a `runtime/` directory for Python. Runtime and test dependencies are separate; see [adapter details](plugin/README.md).

## How to install

Requires **Python 3.11+ with PyYAML 6**, **npm**, and an **OpenCode V2 managed service**, version **2.0.6**, **2.0.21** or **2.0.24**. Check the service with `opencode api get /api/info`, not the CLI version. Other versions and unregistered standalone/embedded hosts disable learning.

**2.0.6 uses digest reviews** because it cannot update a fork's suppression metadata. **2.0.21/2.0.24 support native forks** through their authenticated metadata-update API.

Startup errors dispose the worker and fail plugin activation, so OpenCode reports **failed**, not **active**. Ordinary parent sessions remain usable.

```bash
./install.sh
# Configure settings.yaml if needed, then load the plugin:
opencode service restart
```

The default package and data home is `~/.config/opencode/plugins/skill-learn/`. The installer works from any working directory and accepts:

| Option | Purpose |
|---|---|
| `--dest DIRECTORY` | Plugin discovery directory; defaults to `<OpenCode config>/plugins` |
| `--home DIRECTORY` | Settings and data home; defaults to `<dest>/skill-learn` or `SKILL_LEARN_HOME` |
| `--python EXECUTABLE` | Python interpreter; defaults to `python3` |

**Reinstallation retains only `settings.yaml` and database files** matching `*.sqlite`, `*.sqlite-*`, `*.db` and `*.db-*` (including sidecars/backups). Other old package contents are removed, including plugin-local skills and reports; relocate anything you need before reinstalling. Dependencies and Python imports are checked before cleanup. Unrelated plugins are preserved.

Existing settings are preserved. The installer does not restart OpenCode. Disable other learner triggers when switching to this plugin. Usage: `./install.sh --help`.

## Data storage

Records are stored locally in the selected home:

| Location | Contents |
|---|---|
| `state.sqlite` | Submitted conversation snapshots, reviews, model calls/usage, evidence, proposals and skill registry |
| `runtime.sqlite` | Execution leases, native reviewer identities, event deduplication, operation receipts and diagnostic probes |
| `reports/` by default | Generated HTML reports and local evidence assets |
| Configured skill library | Skill packages (`SKILL.md` and support files), stored on disk |

Back up **both databases together**. Reports can be regenerated. Legacy combined databases are rejected; only the current split schema is supported.

## Configuration

Edit `<home>/settings.yaml` using the [template](docs/settings.example.yaml), then restart the OpenCode service. Omitted options use defaults. Invalid types, unknown keys or duplicate YAML keys disable learning with a diagnostic.

| Option | Default | Meaning |
|---|---|---|
| `runtime.python` | `python3` | Python child command; the installer's `--python` takes precedence |
| `runtime.leaseSeconds` | `60` | Renewable execution lease, in seconds |
| `triggers.idle` | `enabled: true`, `seconds: 120` | Submit after the configured idle interval |
| `triggers.turns` | `enabled: true`, `count: 25` | Submit when idle after this many new observed user turns |
| `llm.selection` | `follow` | Follow the parent model/variant, or use `configured` |
| `llm.model` / `llm.variant` | `openai/gpt-5.5` / `medium` | Configured model and fallback for an unavailable parent selection; null variant uses the host default |
| `llm.steps` | `16` | Maximum model calls per review |
| `llm.contextWindow` | `null` | Fallback context-window size for budget calculation |
| `review.contextMode` | `auto` | Choose fork/digest automatically, or force `digest` |
| `review.maxForkInputTokens` | `250000` | Initial fork size guard; `null` disables it |
| `review.maxInputTokens` | `null` | Next-call admission budget; see below |
| `library.root` | `null` | OpenCode's global skills folder; an explicit path selects another library |
| `approval.generated` | `auto` | Publish valid generated-skill changes; `manual` keeps proposals pending |
| `approval.user` | `manual` | Accepts `manual`/`auto`; neither overrides user-owned skill protection |
| `notifications.enabled` / `notifications.volume` | `true` / `1` | Lifecycle sounds; volume 0–1, with 0 muting playback |
| `notifications.types` | All `true` | Individual switches for `initiated`, `started`, `finished`, `unchanged`, `failed` and `cancelled` |
| `reports.root` | `reports` | Output directory for the explicit `report` command |

**Paths:** OpenCode config resolves from `OPENCODE_CONFIG_DIR`, then `$XDG_CONFIG_HOME/opencode`, then `~/.config/opencode`. Missing/null `library.root` uses its global `skills/` folder. Explicit library/report paths resolve from the plugin home unless absolute; `~` and symlinks work. `library.root: skills` selects `<home>/skills/` and is preserved on reinstall.

**Budgets:** null `review.maxInputTokens` uses 75% of the known context window, or 120000 when unknown; positive integers override it, zero/negative disables it. A call finishes normally, but reaching the budget blocks the next call. Fork size and call-count limits are separate.

**Environment:** `SKILL_LEARN_HOME` overrides the default installation/CLI home; `--home` takes precedence. `SKILL_LEARNING_NOTIFICATIONS=0` mutes sounds; `1` enables them, subject to per-cue switches and volume.

## Behaviour

1. **Trigger when idle.** Wait for the idle interval, or submit immediately when idle after the turn threshold. Resuming work cancels the timer. Either trigger can be disabled.
2. **Queue the active context.** A completed compaction starts a new review boundary: capture its checkpoint and subsequent messages through OpenCode's context API. Unchanged context is skipped; renames update display information. Subagents are recorded but not reviewed. Internal reviewers cannot trigger recursive learning.
3. **Review one at a time per home.** A native **fork** inherits the parent's active context and compatible settings. Profile freshness follows the latest real user turn, or the checkpoint when continuing immediately after compaction. A **digest** summarizes that active context in a fresh session when forced, when the parent model/profile cannot be preserved, or when the initial fork size guard fails.
4. **Extract changes.** Reviewers may load skills, but cannot execute other tools. Historical tool calls are evidence. Python validates the final feedback and publishes or stages proposals according to `approval.generated`.
5. **Protect skills.** New skills carry `metadata.origin: generated`. Reviews refuse edits to user-owned, pinned or protected skills and refuse whole-skill deletion. `adopt` explicitly transfers a skill to agent management. Support files stay under `references/`, `templates/` or `scripts/`; patches must match uniquely. Approval checks current file hashes before staged publication.
6. **Recover without replay.** New submissions supersede older work for the same parent, cancelling only its reviewer. Recovery reconnects to retained sessions and collects completed results once. Missing/ambiguous execution or interrupted publication is reconciled, not automatically replayed. Reviewer retries and auxiliary model calls are blocked.

Reports are generated only by the `report` command; reviews and management changes do not create or refresh them. Lifecycle sounds use bundled JennyNeural recordings, played locally without overlap or runtime TTS. A WAV player such as `paplay`, `pw-play` or `ffplay` is required. Sound failures do not change review outcomes.

The skill catalogue and loaded bodies remain a snapshot until restart; Python validates writes against current files. Missing runtime dependencies disable learning without stopping ordinary chat.

## Other information

### Management and reports

Generate a report directly from the checkout:

```bash
./report.sh
```

The script works from any working directory, uses the repository's Python code and the installed plugin's settings/databases, and prints the generated index path. `reports.root` selects the output directory. Options: `--home DIRECTORY`, `--python EXECUTABLE`; `SKILL_LEARN_HOME` and OpenCode config-directory overrides also apply. Python 3.11+ and PyYAML 6 are required; OpenCode need not be running.

Run commands from the installed runtime using the selected Python interpreter:

```bash
cd "$HOME/.config/opencode/plugins/skill-learn/runtime"
python3 -m skill_learn pending
```

Adjust the path for custom installations. Put `--home PATH` before the command to select another store. `python3 -m pip install ./plugin/server` optionally installs the equivalent `skill-learn` console command.

| Command | Purpose |
|---|---|
| `pending` | List pending skill proposals |
| `show ID` | Inspect a review, proposal or diagnostic probe |
| `approve PROPOSAL_ID` / `reject PROPOSAL_ID` | Apply or reject a pending proposal |
| `adopt SKILL_NAME` | Mark an existing skill as agent-managed |
| `pin SKILL_NAME` / `unpin SKILL_NAME` | Protect or release a skill from background edits |
| `report` | Regenerate the HTML report and print its index path |
| `delete-session opencode SESSION_ID` | Remove learning history; retain skills, coding sessions and minimal reviewer identities |
| `reconcile REVIEW_ID --state abandoned` | Settle interrupted work after confirming its native execution has stopped |

Stop/reconcile active reviews before deleting their history. Other recovery states: `reconcile --help`; `--state finished` requires a `--result` file with the final review text.

Open the reported `index.html` for **Summary**, **Review sessions** and **Skills & proposals**, with filters and expandable evidence. Reports work offline after OpenCode exits and have no editing controls. Run `report` again for an updated snapshot. Failed generation reports an error and preserves the previous complete report.

Parent transcripts are stored once in local evidence assets and loaded only when expanded. Complete raw records remain available; large records use compact JSON during generation.

### Background-session metadata

Reviewers carry persistent `Session.Info.metadata` for other plugins:

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

Markers are verified before prompts and restored during recovery where the host supports metadata updates, independently of titles. On 2.0.6, new digest sessions are marked at creation; older completed reviewers remain excluded by their stored identities without blocking startup. Consumers must honor these flags; OpenCode does not enforce them. They do not change permissions or mute this plugin's lifecycle cues.

### Tests and diagnostics

Offline tests from the checkout root (no provider calls):

```bash
PYTHONPATH="$PWD/plugin/server" python3 -m unittest discover -s tests
npm ci --ignore-scripts --prefix tests
node --test tests/*.test.mjs
```

Optional 2.0.6/2.0.21/2.0.24 fixtures: `/tmp/opencode/skill-learn-v2-fixtures/<version>/package/`, overridden by `OPENCODE_V2_FIXTURES`. Missing fixtures skip those tests. Browser checks require `google-chrome`. See [adapter verification](plugin/README.md).

Live cache/latency diagnostics make provider calls. Run from the installed runtime with OpenCode running and an idle parent/queue:

```bash
python3 -m skill_learn cache-probe ses_PARENT_ID --mode fork
python3 -m skill_learn show probe_REQUEST_ID
# Request a separate comparison if needed:
python3 -m skill_learn cache-probe ses_PARENT_ID --mode digest
```

Each request permits at most two calls and 8192 assessed input tokens per call, without tools, publication or retry. Unknown/oversized input refuses dispatch. Usage is **host-normalized**: absent host fields remain unknown, but provider omissions may become zero. Raw provider usage/wire bodies are unavailable; cache counts do not prove full parent-prefix reuse.

### Attribution and recordings

The review instruction is adapted from `NousResearch/hermes-agent` revision `f42f579cf8bac4918ac9599bece71618afadd846`, retaining its MIT attribution: Copyright (c) 2025 Nous Research. Bundled audio details are in the [recordings README](plugin/server/skill_learn/sounds/README.md).

# Roost — Spec & Implementation Plan

## Context

We're building a standalone CLI that sits ABOVE AI coding agents. Hutch is the post office where agents coordinate with each other (peer-to-peer). This tool is the postmaster — it spawns agents, monitors their health, restarts the stuck ones, and kills the dead (top-down control).

They must be separate processes. If Hutch crashes, the supervisor still runs. If the supervisor crashes, agents keep working and coordinating through Hutch.

**Lifecycle:**
1. User/script uses this CLI to spawn N agent panes in tmux
2. Each pane starts an agent CLI (claude, copilot — more agents later)
3. Agents boot, connect to Hutch, register, coordinate with each other
4. This CLI monitors pane health, detects stuck/dead agents, can restart them

## Design Constraints

- Single-file Python CLI (~660 LOC)
- libtmux as the only non-stdlib dependency
- `--json` on every command for machine-readable output
- No TUI, no dashboard, no async, no database
- Sync Python — libtmux is sync, the CLI is request-response
- Python 3.11+

## CLI Interface

```
roost spawn --program claude --cmd "claude --dangerously-skip-permissions" \
             --project /path/to/repo [--hutch-url URL] [--session NAME] \
             [--name NAME] [--count N] [--worktree PATH]

roost list   [--session SESSION]
roost status <pane-id-or-name> [--session SESSION]
roost send   <pane-id-or-name> <text> [--multiline] [--session SESSION]
roost capture <pane-id-or-name> [--lines 50] [--session SESSION]
roost restart <pane-id-or-name> [--session SESSION]
roost kill    <pane-id-or-name> [--session SESSION]
roost watch  [--session SESSION] [--interval 2] [--once]
```

Global flags: `--json`, `--session SESSION`

**Output convention:** `--json` → structured JSON to stdout. Human text → stderr. Enables `roost list --json | jq .` piping.

## State Tracking: tmux IS the database

No SQLite, no state files. Pane metadata stored as tmux user options (`@`-prefixed):

```
@roost_program   → claude, copilot (more later)
@roost_name      → human label (auto-generated or explicit)
@roost_cmd       → original command for restart
@roost_spawned   → ISO 8601 UTC timestamp
```

Discovery: iterate `session.panes`, try `show-option -p -v @roost_program`. If it exists, it's a managed pane. Survives supervisor crash, detach/reattach, session restart.

**Hutch integration:** On spawn, inject env vars into the pane before running the agent command:
```
HUTCH_URL, HUTCH_PROJECT, AGENT_PROGRAM
```
The agent reads these on boot and registers with Hutch. No shared database.

## Agent State Machine

```
SPAWNING → IDLE ⇄ WORKING → STUCK
                              ↓
                     DEAD / CONTEXT_EXHAUSTED
```

- **SPAWNING**: pane created, waiting for agent prompt
- **IDLE**: prompt detected (regex match or content stabilized 3s)
- **WORKING**: output flowing, spinner/working patterns detected
- **STUCK**: WORKING for >120s with no output change
- **DEAD**: pane process exited
- **CONTEXT_EXHAUSTED**: context limit strings detected in output

## Prompt Detection (3-tier, checked in order)

### Tier 1: Capture-pane regex (primary, 5ms)

Check last 12 lines after ANSI stripping. Working patterns checked first — they override idle.

**Claude Code** (validated across NTM, Batty, Agent Farm — all three converge):

Working (checked first, any match = WORKING):
- `esc to interrupt` in last 6 lines — THE most reliable signal
- Spinner chars `[·✻✢✳✶✽]` followed by `…` or `thinking`
- `Running…`

Idle (checked second, only if no working match):
- `❯[\s\u00a0]*$` — the primary prompt char (NBSP-aware)
- `>\s*$` — fallback prompt
- `╰─>\s*$` — arrow prompt variant
- `(?i)claude\s+code\s+v[\d.]+` — welcome screen (just started)

**Copilot CLI** (same codebase as Codex):

Working:
- Absence of `›` prompt + streaming output
- `Thinking|Running|Executing` keywords

Idle:
- `^\s*›` (U+203A) — the prompt char
- `\?\s*for\s*shortcuts` — footer hint

More agents (Gemini, Aider, OpenCode) = just new regex entries later.

**Context exhaustion** (universal, check last 30 lines):
`context window exceeded|is full|too long|limit reached|maximum context|prompt is too long`

**Rate limit** (universal, check last 50 lines):
`hit your limit|rate limit|too many requests|please wait.*try again|usage limit`

### Key detection principle (from NTM)

**Idle beats working in scrollback.** If the prompt is visible at the bottom, working keywords higher up are stale output. But spinner/interrupt patterns override idle — they appear in the status bar alongside the prompt.

### Tier 2: Content stabilization (universal fallback)

Hash last 20 lines of capture output. If unchanged for 3 consecutive seconds → IDLE. Works for any agent without knowing its prompt. Less precise but zero maintenance.

### Tier 3: Echo-marker probe (not implemented in v1)

Reserve for future. NTM, agtx, and Batty all moved away from it or never shipped it. Regex + stabilization covers the known agents.

## How Spawn Works

1. Get or create tmux session
2. Create pane (split existing window or use empty first pane)
3. Tag pane with `@roost_*` user options
4. Send `export` commands for Hutch env vars (literal=True)
5. Optionally `cd` to worktree
6. Send the agent command (literal=True)
7. Re-tile layout

## How Watch Works

Sync polling loop with `time.sleep(interval)`:

1. Discover all managed panes via `@roost_program` option
2. For each pane: check dead → capture output → detect_state()
3. Track in-memory: `{pane_id: {state, hash, stable_since, state_since}}`
4. Emit event on state transitions (JSON if --json, human text otherwise)
5. `--once` does a single poll and exits

Performance: ~5ms per pane capture. 10 panes at 2s intervals = ~50ms/cycle (2.5% of budget).

## send_keys Safety

**Always `literal=True`** for command text. Without it, semicolons are interpreted as tmux command separators — silent, devastating breakage.

For multiline: `tmux load-buffer -b roost-inject -` via stdin, then `paste-buffer -d -b roost-inject`, then Enter.

## Error Handling

Follow Hutch's `_err()` pattern:
```python
def _err(error: str, message: str, **data) -> dict:
    return {"error": error, "message": message, **data}
```

Error codes: `SESSION_NOT_FOUND`, `PANE_NOT_FOUND`, `TMUX_NOT_RUNNING`, `SPAWN_FAILED`, `INVALID_ARGUMENT`

All commands wrapped in try/except converting `libtmux.exc.LibTmuxException` to structured errors.

## File Structure

```
<project>/
├── pyproject.toml
├── roost.py              # Single file, ~640 LOC, the entire CLI
├── tests/
│   ├── smoke_test.py      # Happy path: spawn, list, status, send, capture, kill
│   └── stress_test.py     # Edge cases: 10 panes, external kill, multiline, restart
└── README.md
```

## LOC Estimate

| Section | LOC |
|---------|-----|
| Imports, constants, `_utcnow`, `_err`, `_emit` | 40 |
| Prompt patterns (compiled regex, claude + copilot) | 40 |
| `AgentState` enum + `detect_state()` | 70 |
| Dead/stuck detection helpers | 25 |
| Pane discovery + metadata helpers | 40 |
| `cmd_spawn` | 65 |
| `cmd_list` | 35 |
| `cmd_status` | 40 |
| `cmd_send` (+ multiline load-buffer) | 45 |
| `cmd_capture` | 25 |
| `cmd_restart` | 45 |
| `cmd_kill` | 25 |
| `cmd_watch` (polling loop) | 60 |
| Error wrapper + argparse setup | 70 |
| `main()` entry point | 15 |
| **Total** | **~640** |

## Dependencies

```toml
dependencies = ["libtmux>=0.37.0"]
```

That's it. Everything else is stdlib (argparse, dataclasses, enum, hashlib, json, re, shlex, sys, time).

## Test Strategy

Two levels: unit tests with bash (fast, CI-safe) and integration tests with real agents (manual, validates prompt detection).

**Unit tests (bash):** Creates test session, spawns `bash --norc` as fake agent (predictable `$` prompt), exercises all CLI commands (spawn, list, status, send, capture, restart, kill, watch --once). Edge cases: external kill-pane, multiline send, duplicate names, nonexistent panes.

**Integration tests (real agents):** Manual test script that spawns actual Claude Code and Copilot sessions, verifies:
- Prompt detection correctly identifies IDLE after startup
- State transitions to WORKING when given a task
- State returns to IDLE when task completes
- STUCK detection triggers after timeout with no output
- CONTEXT_EXHAUSTED detection works
- Restart successfully kills and respawns the agent
- watch --once correctly reports state for both agent types

## Verification

### Quick (bash, automated)
1. `pip install -e .`
2. `python tests/smoke_test.py` — spawns bash panes, exercises all commands
3. `python tests/stress_test.py` — edge cases

### Real agents (manual)
1. `pip install -e .`
2. `roost spawn --program claude --cmd "claude --dangerously-skip-permissions" --project $(pwd) --session agents`
3. `roost spawn --program copilot --cmd "copilot" --project $(pwd) --session agents`
4. `roost list --json` → see both panes
5. `roost watch --session agents --once --json` → both IDLE (or SPAWNING → IDLE)
6. `roost send <claude-pane> "echo hello"` → watch transitions to WORKING → IDLE
7. `roost status <copilot-pane> --json` → see state
8. `roost restart <claude-pane>` → agent restarts, reaches IDLE again
9. `roost kill --all --session agents` → clean up

## Research Sources

Prompt detection patterns and architecture decisions are derived from source code analysis of four production projects:

| Project | Language | What we took | Source |
|---------|----------|-------------|--------|
| **NTM** | Go | Claude/Codex prompt regex, spinner patterns, rate limit strings, conflict resolution rule ("idle beats working"), test fixtures (`cc_idle.txt`, `cc_working.txt`, `cod_idle.txt`) | [internal/agent/patterns.go](https://github.com/Dicklesworthstone/ntm/blob/main/internal/agent/patterns.go), [internal/agent/parser.go](https://github.com/Dicklesworthstone/ntm/blob/main/internal/agent/parser.go) |
| **Batty** | Rust | `esc to interrupt` as primary working signal, `❯`/`›` character-level prompt detection, context exhaustion strings, state machine design | [src/team/watcher.rs](https://github.com/battysh/batty/blob/main/src/team/watcher.rs) |
| **agtx** | Rust | Content stabilization fallback (hash + 3s threshold), `pane_current_command` for process detection, dedicated tmux server pattern | [github.com/fynnfluegge/agtx](https://github.com/fynnfluegge/agtx) |
| **Claude Code Agent Farm** | Python | `is_claude_ready()`/`is_claude_working()` indicator lists, echo-marker probe technique, adaptive idle timeout | [claude_code_agent_farm.py](https://github.com/Dicklesworthstone/claude_code_agent_farm/blob/main/claude_code_agent_farm.py) |
| **libtmux** | Python | `send_keys(literal=True)` safety, `capture_pane()` API, `@`-prefixed user options for pane tagging, `display_message("#{pane_dead}")` | [libtmux.git-pull.com](https://libtmux.git-pull.com/) |

All three agent orchestrators (NTM, Batty, Agent Farm) independently converge on `esc to interrupt` as the most reliable Claude Code working indicator, and `❯`/`›` as idle prompt chars. This cross-validation is stronger than any single-project reference.

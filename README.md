# Roost

**Tmux-based lifecycle manager for AI coding agents.**

Spawn Claude Code, Copilot, and other AI agents into tmux panes. Monitor their state. Restart the stuck ones. Kill the dead.

Roost is the postmaster — it manages the flock from above. Pair it with [Hutch](https://github.com/otherland/hutch) for peer-to-peer agent coordination.

## Why

AI coding agents run in terminals. When you're running several at once, you need something to:

- **Spawn** them into tmux panes with the right env vars and working directory
- **Detect** whether they're idle, working, stuck, or dead
- **Restart** the ones that crash or exhaust their context window
- **Monitor** state transitions across all agents in real time

Roost does this in ~620 lines of Python with one dependency (`libtmux`). No database, no daemon, no async. Tmux *is* the database — pane metadata is stored as tmux user options that survive crashes, detaches, and reattaches.

## Install

```bash
pip install roost
```

Requires Python 3.11+ and tmux.

## Quick start

```bash
# Spawn a Claude Code agent
roost spawn --program claude \
  --cmd "claude --dangerously-skip-permissions" \
  --project /path/to/repo

# See what's running
roost list --json

# Check agent state
roost status claude-0

# Send it a task
roost send claude-0 "fix the failing tests in src/auth.py"

# Watch state transitions in real time
roost watch --json

# Restart a stuck agent
roost restart claude-0

# Clean up
roost kill --all
```

## Commands

| Command | Description |
|---------|-------------|
| `spawn` | Create agent panes in tmux |
| `list` | Show all managed agents |
| `status` | Get state of a specific agent |
| `send` | Send text to an agent pane |
| `capture` | Read recent output from a pane |
| `restart` | Stop and re-run an agent |
| `kill` | Remove agent pane(s) |
| `watch` | Poll and report state transitions |

Every command supports `--json` for machine-readable output and `--session` to target a specific tmux session.

## State detection

Roost detects agent state through a 3-tier system:

**Tier 1: Pattern matching** (primary, ~5ms)
Regex patterns matched against the last few lines of pane output. Working patterns are checked first and override idle — a spinner alongside the prompt means the agent is still thinking.

**Tier 2: Content stabilization** (fallback)
Hash the last 20 lines. If unchanged for 3 seconds, the agent is idle. Works for any program without knowing its prompt format.

**Tier 3: Echo-marker probe** (reserved for future)

### Agent state machine

```
SPAWNING → IDLE ⇄ WORKING → STUCK
                              ↓
                     DEAD / CONTEXT_EXHAUSTED
```

### Supported agents

| Agent | Idle signal | Working signal |
|-------|------------|----------------|
| Claude Code | `❯` prompt | Spinner chars (`✽ ✻ ·`), `esc to interrupt` |
| Copilot CLI | `❯  Type @` prompt, `? for shortcuts` | `Esc to cancel`, `Thinking`, `Running` |

More agents (Gemini CLI, Aider, OpenCode) are just new regex entries.

## Hutch integration

If you're running [Hutch](https://github.com/otherland/hutch) for agent coordination, pass `--hutch-url` on spawn:

```bash
roost spawn --program claude \
  --cmd "claude --dangerously-skip-permissions" \
  --project /path/to/repo \
  --hutch-url http://localhost:8765/mcp
```

Roost injects `HUTCH_URL`, `HUTCH_PROJECT`, and `AGENT_PROGRAM` as environment variables before starting the agent. The agent reads these on boot and registers with Hutch.

**Separation of concerns:** Hutch handles peer-to-peer coordination (messaging, file reservations, shared context). Roost handles top-down control (spawn, monitor, restart, kill). They run as separate processes — if one crashes, the other keeps working.

## How it works

Roost stores all state as tmux pane options:

```
@roost_program   → claude, copilot
@roost_name      → human-readable label
@roost_cmd       → original command (for restart)
@roost_spawned   → ISO 8601 timestamp
```

Discovery is a scan: iterate session panes, check for `@roost_program`. If it exists, it's managed. This survives supervisor crashes, terminal detaches, and session restarts.

## Research

Prompt detection patterns were cross-validated against four production projects that independently converged on the same signals:

| Project | What we took |
|---------|-------------|
| [NTM](https://github.com/Dicklesworthstone/ntm) (Go) | Claude/Codex prompt regex, spinner patterns, "idle beats working in scrollback" rule |
| [Batty](https://github.com/battysh/batty) (Rust) | `esc to interrupt` as primary working signal, character-level prompt detection |
| [agtx](https://github.com/fynnfluegge/agtx) (Rust) | Content stabilization fallback (hash + timeout) |
| [Agent Farm](https://github.com/Dicklesworthstone/claude_code_agent_farm) (Python) | Working/idle indicator lists, adaptive timeout |

## License

MIT

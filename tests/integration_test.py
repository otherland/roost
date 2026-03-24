#!/usr/bin/env python3
"""Integration tests for roost — spawns REAL Claude Code and Copilot agents.

Exercises the full agent lifecycle: spawn, wait for idle, send tasks,
verify working state, restart, kill.

Requires:
  - tmux installed
  - claude CLI installed (Claude Code)
  - copilot CLI installed (GitHub Copilot CLI)

Run: python tests/integration_test.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

SESSION = "roost-integration-test"
ROOST = [sys.executable, "roost.py", "--json"]

# Timeouts
BOOT_TIMEOUT = 30       # seconds to wait for agent to reach IDLE after spawn
TASK_TIMEOUT = 120      # seconds to wait for agent to finish a task
POLL_INTERVAL = 2       # seconds between status polls
POST_SEND_DELAY = 3     # seconds to wait after sending before first status check


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(*args: str, check: bool = True) -> dict | list:
    """Run a roost command and return parsed JSON output."""
    cmd = [*ROOST, *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"FAIL: {' '.join(args)}", file=sys.stderr)
        print(f"  stdout: {result.stdout}", file=sys.stderr)
        print(f"  stderr: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def cleanup():
    """Kill any leftover test session."""
    subprocess.run(["tmux", "kill-session", "-t", SESSION],
                   capture_output=True)


def get_status(name: str) -> dict:
    """Get status for an agent, returning the parsed dict."""
    return run("status", name, "--session", SESSION)


def capture_output(name: str, lines: int = 50) -> str:
    """Capture pane output as a single string (for debug printing)."""
    r = run("capture", name, "--lines", str(lines), "--session", SESSION)
    return "\n".join(r.get("lines", []))


def wait_for_state(
    name: str,
    target_state: str,
    timeout: float,
    label: str = "",
) -> dict:
    """Poll agent status until it reaches target_state or timeout.

    Returns the final status dict. Raises AssertionError on timeout.
    """
    deadline = time.time() + timeout
    last_status = {}
    while time.time() < deadline:
        last_status = get_status(name)
        state = last_status.get("state", "")
        if state == target_state:
            return last_status
        # Bail early on terminal states we don't expect
        if state in ("dead", "context_exhausted"):
            break
        time.sleep(POLL_INTERVAL)

    # Timeout — capture debug output
    output = capture_output(name)
    msg = f"Timeout waiting for {name} to reach '{target_state}'"
    if label:
        msg = f"[{label}] {msg}"
    msg += f"\n  Last status: {json.dumps(last_status, indent=2)}"
    msg += f"\n  Pane output (last 50 lines):\n{output}"
    raise AssertionError(msg)


def assert_state_is(name: str, expected: str, label: str = ""):
    """Single status check — assert current state matches expected."""
    status = get_status(name)
    state = status.get("state", "")
    if state != expected:
        output = capture_output(name)
        msg = f"Expected {name} state='{expected}', got '{state}'"
        if label:
            msg = f"[{label}] {msg}"
        msg += f"\n  Status: {json.dumps(status, indent=2)}"
        msg += f"\n  Pane output:\n{output}"
        raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Test phases
# ---------------------------------------------------------------------------

def phase_spawn(has_claude: bool, has_copilot: bool) -> tuple[str | None, str | None]:
    """Spawn agents and return their names."""
    claude_name = None
    copilot_name = None

    if has_claude:
        print("  spawning claude...", end=" ", flush=True)
        r = run("spawn",
                "--program", "claude",
                "--cmd", "unset CLAUDECODE && claude --dangerously-skip-permissions",
                "--session", SESSION,
                "--name", "test-claude")
        assert r["name"] == "test-claude", f"Unexpected name: {r}"
        claude_name = "test-claude"
        print(f"ok (pane={r['pane_id']})")

    if has_copilot:
        print("  spawning copilot...", end=" ", flush=True)
        r = run("spawn",
                "--program", "copilot",
                "--cmd", "copilot --yolo",
                "--session", SESSION,
                "--name", "test-copilot")
        assert r["name"] == "test-copilot", f"Unexpected name: {r}"
        copilot_name = "test-copilot"
        print(f"ok (pane={r['pane_id']})")

    return claude_name, copilot_name


def phase_wait_idle(claude_name: str | None, copilot_name: str | None):
    """Wait for all spawned agents to reach IDLE."""
    if claude_name:
        print(f"  waiting for {claude_name} to reach idle...", end=" ", flush=True)
        wait_for_state(claude_name, "idle", BOOT_TIMEOUT, "boot")
        print("ok")

    if copilot_name:
        print(f"  waiting for {copilot_name} to reach idle...", end=" ", flush=True)
        wait_for_state(copilot_name, "idle", BOOT_TIMEOUT, "boot")
        print("ok")


def phase_send_task(name: str, task: str):
    """Send a task to an agent, verify it starts working, wait for idle."""
    print(f"  sending task to {name}...", end=" ", flush=True)
    r = run("send", name, task, "--session", SESSION)
    assert r["sent"] is True, f"Send failed: {r}"
    print("ok")

    # Give the agent a moment to start processing
    time.sleep(POST_SEND_DELAY)

    # Check it's working (soft check — may already be done for trivial tasks)
    status = get_status(name)
    state = status.get("state", "")
    if state == "working":
        print(f"  {name} is working (good)...", end=" ", flush=True)
    else:
        print(f"  {name} state={state} (may have finished quickly)...", end=" ", flush=True)

    # Wait for it to return to idle
    print("waiting for idle...", end=" ", flush=True)
    wait_for_state(name, "idle", TASK_TIMEOUT, f"task:{name}")
    print("ok")


def phase_watch_once():
    """Run watch --once and verify output contains agent states."""
    print("  watch --once...", end=" ", flush=True)
    result = subprocess.run(
        [*ROOST, "watch", "--session", SESSION, "--once"],
        capture_output=True, text=True,
    )
    text = result.stdout.strip()
    decoder = json.JSONDecoder()
    agents_seen = []
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \t\n\r":
            idx += 1
        if idx >= len(text):
            break
        obj, end = decoder.raw_decode(text, idx)
        if "state" in obj:
            agents_seen.append(obj)
        idx = end

    assert len(agents_seen) > 0, f"No agents in watch output: {result.stdout}"
    names = [a.get("name") for a in agents_seen]
    print(f"ok (saw {len(agents_seen)} agents: {names})")


def phase_restart(name: str):
    """Restart an agent and wait for it to come back to IDLE."""
    print(f"  restarting {name}...", end=" ", flush=True)
    r = run("restart", name, "--session", SESSION)
    assert r["restarted"] is True, f"Restart failed: {r}"
    print("ok")

    print(f"  waiting for {name} to reach idle after restart...", end=" ", flush=True)
    wait_for_state(name, "idle", BOOT_TIMEOUT, "restart")
    print("ok")


def phase_kill_all():
    """Kill all agents and verify the list is empty."""
    print("  killing all agents...", end=" ", flush=True)
    r = run("kill", "--all", "--session", SESSION)
    assert r["count"] >= 1, f"Expected to kill at least 1, got {r}"
    print(f"ok (killed {r['count']}: {r['killed']})")

    # Small delay for tmux to clean up
    time.sleep(1)

    print("  verifying list is empty...", end=" ", flush=True)
    r = run("list", "--session", SESSION)
    assert r["count"] == 0, f"Expected 0 agents, got {r['count']}"
    print("ok")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Check prerequisites
    has_claude = shutil.which("claude") is not None
    has_copilot = shutil.which("copilot") is not None

    if not has_claude and not has_copilot:
        print("SKIP: neither 'claude' nor 'copilot' found on PATH")
        sys.exit(0)

    print("roost integration tests (real agents)")
    print("=" * 50)
    print(f"  claude CLI: {'found' if has_claude else 'NOT FOUND (skipping)'}")
    print(f"  copilot CLI: {'found' if has_copilot else 'NOT FOUND (skipping)'}")
    print(f"  session: {SESSION}")
    print()

    cleanup()

    try:
        # Phase 1: Spawn
        print("[1/7] Spawn agents")
        claude_name, copilot_name = phase_spawn(has_claude, has_copilot)

        # Phase 2: Wait for idle
        print("[2/7] Wait for IDLE")
        phase_wait_idle(claude_name, copilot_name)

        # Phase 3: Send task to Claude Code
        if claude_name:
            print("[3/7] Send task to Claude Code")
            phase_send_task(
                claude_name,
                "list the files in the current directory and tell me how many there are",
            )
        else:
            print("[3/7] SKIP (no claude)")

        # Phase 4: Send task to Copilot
        if copilot_name:
            print("[4/7] Send task to Copilot")
            phase_send_task(
                copilot_name,
                "list the files in the current directory and tell me how many there are",
            )
        else:
            print("[4/7] SKIP (no copilot)")

        # Phase 5: Watch --once
        print("[5/7] Watch --once")
        phase_watch_once()

        # Phase 6: Restart Claude Code
        if claude_name:
            print("[6/7] Restart Claude Code")
            phase_restart(claude_name)
        else:
            print("[6/7] SKIP (no claude)")

        # Phase 7: Kill all + verify empty
        print("[7/7] Kill all agents")
        phase_kill_all()

    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    finally:
        cleanup()

    print()
    print("=" * 50)
    print("all integration tests passed")


if __name__ == "__main__":
    main()

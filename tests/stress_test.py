#!/usr/bin/env python3
"""Stress tests for roost — edge cases, concurrent spawns, external kills.

Requires tmux to be installed. Creates and destroys its own session.
Run: python tests/stress_test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

SESSION = "roost-stress-test"
ROOST = [sys.executable, "roost.py", "--json"]


def run(*args: str, check: bool = True) -> dict | list:
    cmd = [*ROOST, *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"FAIL: {' '.join(args)}", file=sys.stderr)
        print(f"  stdout: {result.stdout}", file=sys.stderr)
        print(f"  stderr: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def cleanup():
    subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True)


def test_rapid_spawn():
    """Spawn 10 panes rapidly."""
    print("  rapid spawn (10 panes)...", end=" ", flush=True)
    r = run("spawn", "--program", "claude", "--cmd", "bash --norc",
            "--session", SESSION, "--count", "10")
    assert isinstance(r, list) and len(r) == 10, f"Expected 10, got {len(r)}"
    r2 = run("list", "--session", SESSION)
    assert r2["count"] == 10, f"Expected 10 listed, got {r2['count']}"
    print("ok")


def test_external_kill():
    """Kill a pane externally via tmux, verify roost detects it."""
    print("  external kill...", end=" ", flush=True)
    agents = run("list", "--session", SESSION)["agents"]
    target = agents[0]
    subprocess.run(["tmux", "kill-pane", "-t", target["pane_id"]],
                   capture_output=True)
    time.sleep(0.5)
    # The pane is gone — status should fail or show dead
    r = run("list", "--session", SESSION)
    # Count should be 9 (externally killed pane is gone from tmux)
    assert r["count"] == 9, f"Expected 9 after external kill, got {r['count']}"
    print("ok")


def test_multiline_send():
    """Send multiline text via load-buffer."""
    print("  multiline send...", end=" ", flush=True)
    agents = run("list", "--session", SESSION)["agents"]
    name = agents[0]["name"]
    text = "echo line1\necho line2\necho line3"
    r = run("send", name, text, "--multiline", "--session", SESSION)
    assert r["sent"] is True
    time.sleep(1)
    cap = run("capture", name, "--lines", "20", "--session", SESSION)
    output = "\n".join(cap["lines"])
    assert "line1" in output, f"line1 not in output:\n{output}"
    assert "line3" in output, f"line3 not in output:\n{output}"
    print("ok")


def test_duplicate_names():
    """Spawn agents with duplicate names — should still work."""
    print("  duplicate names...", end=" ", flush=True)
    run("spawn", "--program", "claude", "--cmd", "bash --norc",
        "--session", SESSION, "--name", "dupe")
    run("spawn", "--program", "claude", "--cmd", "bash --norc",
        "--session", SESSION, "--name", "dupe")
    agents = run("list", "--session", SESSION)["agents"]
    dupes = [a for a in agents if a["name"] == "dupe"]
    assert len(dupes) == 2, f"Expected 2 dupes, got {len(dupes)}"
    # Resolve by name returns the first one
    r = run("status", "dupe", "--session", SESSION)
    assert r["name"] == "dupe"
    print("ok")


def test_restart_dead_pane():
    """Restart an agent after its process exits."""
    print("  restart dead pane...", end=" ", flush=True)
    run("spawn", "--program", "claude", "--cmd", "bash --norc",
        "--session", SESSION, "--name", "mortal")
    time.sleep(0.5)
    # Kill the bash process inside the pane
    run("send", "mortal", "exit", "--session", SESSION)
    time.sleep(1)
    r = run("restart", "mortal", "--session", SESSION)
    assert r["restarted"] is True
    time.sleep(0.5)
    r2 = run("status", "mortal", "--session", SESSION)
    assert r2["state"] != "dead", f"Expected alive after restart, got {r2['state']}"
    print("ok")


def test_nonexistent_session():
    """List against a nonexistent session returns empty, not an error."""
    print("  nonexistent session...", end=" ", flush=True)
    r = run("list", "--session", "no-such-session-xyz")
    assert r["count"] == 0, f"Expected 0 for nonexistent session, got {r['count']}"
    print("ok")


def test_kill_all_cleanup():
    """Kill --all should leave zero managed panes."""
    print("  kill --all cleanup...", end=" ", flush=True)
    r = run("kill", "--all", "--session", SESSION)
    assert r["count"] > 0, "Expected some panes to kill"
    # Session may be gone after killing all panes — that's fine
    print(f"ok (killed {r['count']})")


def main():
    cleanup()
    print("roost stress tests")
    print("=" * 40)

    try:
        test_rapid_spawn()
        test_external_kill()
        test_multiline_send()
        test_duplicate_names()
        test_restart_dead_pane()
        test_nonexistent_session()
        test_kill_all_cleanup()
    finally:
        cleanup()

    print("=" * 40)
    print("all stress tests passed")


if __name__ == "__main__":
    main()

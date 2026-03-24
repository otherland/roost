#!/usr/bin/env python3
"""Smoke tests for roost — exercises all commands with bash panes.

Requires tmux to be installed. Creates and destroys its own session.
Run: python tests/smoke_test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

SESSION = "roost-smoke-test"
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
    """Kill any leftover test session."""
    subprocess.run(["tmux", "kill-session", "-t", SESSION],
                   capture_output=True)


def test_spawn():
    print("  spawn...", end=" ", flush=True)
    r = run("spawn", "--program", "claude", "--cmd", "bash --norc",
            "--session", SESSION, "--name", "smoke-agent")
    assert r["name"] == "smoke-agent", f"Expected smoke-agent, got {r}"
    assert r["pane_id"].startswith("%"), f"Bad pane_id: {r['pane_id']}"
    print("ok")
    return r["pane_id"]


def test_list():
    print("  list...", end=" ", flush=True)
    r = run("list", "--session", SESSION)
    assert r["count"] == 1, f"Expected 1, got {r['count']}"
    assert r["agents"][0]["name"] == "smoke-agent"
    print("ok")


def test_status(pane_id: str):
    print("  status...", end=" ", flush=True)
    r = run("status", "smoke-agent", "--session", SESSION)
    assert r["pane_id"] == pane_id
    assert r["program"] == "claude"
    assert r["state"] in ("idle", "working", "spawning")
    print(f"ok (state={r['state']})")


def test_send():
    print("  send...", end=" ", flush=True)
    r = run("send", "smoke-agent", "echo roost-test-marker", "--session", SESSION)
    assert r["sent"] is True
    print("ok")


def test_capture():
    print("  capture...", end=" ", flush=True)
    time.sleep(0.5)  # let echo complete
    r = run("capture", "smoke-agent", "--lines", "20", "--session", SESSION)
    text = "\n".join(r["lines"])
    assert "roost-test-marker" in text, f"Marker not found in capture:\n{text}"
    print("ok")


def test_restart():
    print("  restart...", end=" ", flush=True)
    r = run("restart", "smoke-agent", "--session", SESSION)
    assert r["restarted"] is True
    assert r["cmd"] == "bash --norc"
    print("ok")


def test_watch_once():
    print("  watch --once...", end=" ", flush=True)
    result = subprocess.run(
        [*ROOST, "watch", "--session", SESSION, "--once"],
        capture_output=True, text=True,
    )
    # watch --once outputs multiple pretty-printed JSON objects
    # Use a decoder to parse them sequentially
    text = result.stdout.strip()
    decoder = json.JSONDecoder()
    found = False
    idx = 0
    while idx < len(text):
        # Skip whitespace
        while idx < len(text) and text[idx] in " \t\n\r":
            idx += 1
        if idx >= len(text):
            break
        obj, end = decoder.raw_decode(text, idx)
        if "state" in obj:
            found = True
        idx = end
    assert found, f"No state in watch output: {result.stdout}"
    print("ok")


def test_kill():
    print("  kill...", end=" ", flush=True)
    r = run("kill", "smoke-agent", "--session", SESSION)
    assert r["count"] == 1
    assert "smoke-agent" in r["killed"]
    print("ok")


def test_list_empty():
    print("  list (empty)...", end=" ", flush=True)
    r = run("list", "--session", SESSION)
    assert r["count"] == 0, f"Expected 0, got {r['count']}"
    print("ok")


def test_spawn_multiple():
    print("  spawn --count 3...", end=" ", flush=True)
    r = run("spawn", "--program", "claude", "--cmd", "bash --norc",
            "--session", SESSION, "--count", "3")
    assert isinstance(r, list) and len(r) == 3, f"Expected 3 results, got {r}"
    for i, item in enumerate(r):
        assert item["name"] == f"claude-{i}"
    print("ok")


def test_kill_all():
    print("  kill --all...", end=" ", flush=True)
    r = run("kill", "--all", "--session", SESSION)
    assert r["count"] == 3, f"Expected 3 killed, got {r['count']}"
    print("ok")


def test_nonexistent_pane():
    print("  status (nonexistent)...", end=" ", flush=True)
    r = run("status", "no-such-agent", "--session", SESSION, check=False)
    # Spawn a dummy so the session exists
    run("spawn", "--program", "claude", "--cmd", "bash --norc",
        "--session", SESSION, "--name", "dummy")
    r = run("status", "no-such-agent", "--session", SESSION)
    assert "error" in r, f"Expected error, got {r}"
    print("ok")


def main():
    cleanup()
    print("roost smoke tests")
    print("=" * 40)

    try:
        pane_id = test_spawn()
        time.sleep(1)  # let bash start
        test_list()
        test_status(pane_id)
        test_send()
        test_capture()
        test_restart()
        time.sleep(1)  # let restart settle
        test_watch_once()
        test_kill()
        test_list_empty()
        test_spawn_multiple()
        test_kill_all()
        test_nonexistent_pane()
    finally:
        cleanup()

    print("=" * 40)
    print("all smoke tests passed")


if __name__ == "__main__":
    main()

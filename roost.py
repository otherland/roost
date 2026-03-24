#!/usr/bin/env python3
"""Roost — tmux-based AI agent lifecycle manager.

Spawns, monitors, and manages AI coding agents in tmux panes.
Sister project to Hutch (agent coordination over MCP).

Commands     line
─────────────────
Helpers       30    _utcnow, _err, _emit, _strip_ansi, _resolve_pane
Patterns      80    Prompt detection regex (Claude Code, Copilot)
Detection    140    detect_state, _is_pane_dead, _pane_option, _discover
Spawn        210    cmd_spawn
List         280    cmd_list
Status       310    cmd_status
Send         340    cmd_send
Capture      380    cmd_capture
Restart      400    cmd_restart
Kill         430    cmd_kill
Watch        455    cmd_watch
CLI          510    argparse setup + main()
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from enum import Enum

import libtmux

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
_STUCK_THRESHOLD = 120  # seconds
_STABLE_THRESHOLD = 3.0  # seconds for content stabilization


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime(_UTC_FMT)


def _err(error: str, message: str, **data) -> dict:
    return {"error": error, "message": message, **data}


def _emit(data, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(data, indent=2))
    else:
        if isinstance(data, list):
            for item in data:
                _emit_human(item)
        elif "error" in data:
            print(f"error: {data['error']}: {data['message']}", file=sys.stderr)
        else:
            _emit_human(data)


def _emit_human(d: dict) -> None:
    parts = [f"{k}={v}" for k, v in d.items()]
    print("  ".join(parts), file=sys.stderr)


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?<=>]*[a-zA-Z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1bP[^\x1b]*(?:\x1b\\|$)"
    r"|\x1b[()][0-9A-Za-z]"
    r"|[\x0e\x0f]"
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _get_server() -> libtmux.Server:
    try:
        s = libtmux.Server()
        if not s.is_alive():
            raise RuntimeError
        return s
    except Exception:
        print("error: tmux server not running", file=sys.stderr)
        sys.exit(1)


def _get_session(server: libtmux.Server, name: str) -> libtmux.Session:
    sessions = server.sessions.filter(session_name=name)
    if not sessions:
        print(f"error: session '{name}' not found", file=sys.stderr)
        sys.exit(1)
    return sessions[0]


# ---------------------------------------------------------------------------
# Prompt patterns (sources: NTM, Batty, Claude Code Agent Farm)
# ---------------------------------------------------------------------------

class AgentState(Enum):
    SPAWNING = "spawning"
    IDLE = "idle"
    WORKING = "working"
    STUCK = "stuck"
    DEAD = "dead"
    CONTEXT_EXHAUSTED = "context_exhausted"
    RATE_LIMITED = "rate_limited"


# Working patterns — checked FIRST, override idle
_WORKING = {
    "claude": [
        re.compile(r"esc to interr"),  # may be truncated
        re.compile(r"[\u00b7\u2733\u2722\u2743\u273b\u273d].*[\u2026]"),  # spinner + …
        re.compile(r"[\u00b7\u2733\u2722\u273b]\s*think", re.I),
        re.compile(r"Running\u2026"),
    ],
    "copilot": [
        re.compile(r"Esc to cancel"),  # active task indicator
        re.compile(r"Thinking|Running|Executing", re.I),
    ],
}

# Idle patterns — checked SECOND, only if no working match
_IDLE = {
    "claude": [
        re.compile(r"\u276f[\s\u00a0]*$"),  # ❯ prompt (NBSP-aware)
        re.compile(r">\s*$"),  # fallback prompt
        re.compile(r"\u2570\u2500>\s*$"),  # ╰─> arrow prompt
        re.compile(r"(?i)claude\s+code\s+v[\d.]+"),  # welcome screen
    ],
    "copilot": [
        re.compile(r"\u276f\s+Type\s+@"),  # ❯  Type @ to mention files...
        re.compile(r"\u276f[\s\u00a0]*$"),  # ❯ prompt (same char as Claude)
        re.compile(r"\?\s*for\s*shortcuts"),  # footer hint
        re.compile(r"GitHub Copilot v[\d.]+"),  # welcome screen
    ],
}

# Universal patterns
_CONTEXT_EXHAUSTED_RE = re.compile(
    r"context window exceeded|context.{0,10}full|conversation is too long"
    r"|maximum context|context limit reached|prompt is too long",
    re.I,
)

_RATE_LIMITED_RE = re.compile(
    r"hit your limit|rate limit|too many requests"
    r"|please wait.*try again|usage limit|exceeded.{0,10}limit",
    re.I,
)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _pane_option(pane, key: str) -> str | None:
    try:
        result = pane.cmd("show-option", "-p", "-v", key)
        return result.stdout[0] if result.stdout else None
    except Exception:
        return None


def _is_pane_dead(pane) -> bool:
    try:
        pane.refresh()
        result = pane.cmd("display-message", "-p", "#{pane_dead}")
        return bool(result.stdout and result.stdout[0] == "1")
    except Exception:
        return True


def _discover(session: libtmux.Session) -> list:
    managed = []
    for pane in session.panes:
        prog = _pane_option(pane, "@roost_program")
        if prog:
            managed.append(pane)
    return managed


def _resolve_pane(session: libtmux.Session, target: str) -> libtmux.Pane | None:
    if target.startswith("%"):
        for pane in session.panes:
            if pane.pane_id == target:
                return pane
        return None
    for pane in _discover(session):
        if _pane_option(pane, "@roost_name") == target:
            return pane
    return None


def detect_state(
    lines: list[str], program: str, prev_hash: str, prev_stable: str,
) -> tuple[AgentState, str, str]:
    """Classify agent state from capture-pane output.

    Returns (state, content_hash, stable_since).
    """
    stripped = [_strip_ansi(l) for l in lines]
    now = _utcnow()

    # Content hash for stabilization
    tail20 = "\n".join(stripped[-20:]) if stripped else ""
    content_hash = hashlib.md5(tail20.encode()).hexdigest()

    # Check context exhaustion (last 30 lines)
    tail30 = "\n".join(stripped[-30:])
    if _CONTEXT_EXHAUSTED_RE.search(tail30):
        return AgentState.CONTEXT_EXHAUSTED, content_hash, now

    # Check rate limit (last 50 lines)
    tail50 = "\n".join(stripped[-50:])
    if _RATE_LIMITED_RE.search(tail50):
        return AgentState.RATE_LIMITED, content_hash, now

    # Tier 1: Pattern matching
    last = stripped[-12:] if stripped else []
    last6 = stripped[-6:] if stripped else []

    # Working patterns checked first
    check_lines = last6 if program == "claude" else last
    for line in check_lines:
        for pat in _WORKING.get(program, []):
            if pat.search(line):
                return AgentState.WORKING, content_hash, now

    # Idle patterns checked second (idle beats stale working keywords in scrollback)
    for line in reversed(last):
        for pat in _IDLE.get(program, []):
            if pat.search(line):
                return AgentState.IDLE, content_hash, now

    # Tier 2: Content stabilization
    if content_hash == prev_hash and prev_stable:
        try:
            stable_dt = datetime.strptime(prev_stable, _UTC_FMT).replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - stable_dt).total_seconds()
            if elapsed >= _STABLE_THRESHOLD:
                return AgentState.IDLE, content_hash, prev_stable
        except ValueError:
            pass
        return AgentState.WORKING, content_hash, prev_stable

    # Content changed — reset stabilization, assume working
    return AgentState.WORKING, content_hash, now


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_spawn(args) -> dict | list[dict]:
    server = libtmux.Server()

    # Get or create session
    try:
        sessions = server.sessions.filter(session_name=args.session) if server.is_alive() else []
    except Exception:
        sessions = []

    if sessions:
        session = sessions[0]
    else:
        session = server.new_session(session_name=args.session, attach=False)

    results = []
    for i in range(args.count):
        name = args.name if args.count == 1 and args.name else f"{args.program}-{i}"

        # Create pane — use empty first pane, split existing, or new window
        if i == 0 and len(session.windows[0].panes) == 1 and not _pane_option(session.windows[0].panes[0], "@roost_program"):
            pane = session.windows[0].panes[0]
        else:
            try:
                pane = session.windows[0].split()
            except libtmux.exc.LibTmuxException:
                # No space — create a new window
                w = session.new_window(attach=False)
                pane = w.panes[0]

        # Tag pane
        now = _utcnow()
        pane.cmd("set-option", "-p", "@roost_program", args.program)
        pane.cmd("set-option", "-p", "@roost_name", name)
        pane.cmd("set-option", "-p", "@roost_cmd", args.cmd)
        pane.cmd("set-option", "-p", "@roost_spawned", now)

        # Inject env vars for Hutch integration
        if args.hutch_url:
            pane.send_keys(f"export HUTCH_URL={shlex.quote(args.hutch_url)}", literal=True)
        if args.project:
            pane.send_keys(f"export HUTCH_PROJECT={shlex.quote(args.project)}", literal=True)
        pane.send_keys(f"export AGENT_PROGRAM={shlex.quote(args.program)}", literal=True)

        # cd to worktree if specified
        if args.worktree:
            pane.send_keys(f"cd {shlex.quote(args.worktree)}", literal=True)

        # Run the agent command
        pane.send_keys(args.cmd, literal=True)

        results.append({
            "pane_id": pane.pane_id,
            "name": name,
            "program": args.program,
            "session": args.session,
            "spawned_at": now,
        })

    # Re-tile all windows
    for w in session.windows:
        try:
            w.select_layout("tiled")
        except Exception:
            pass

    return results if len(results) > 1 else results[0]


def cmd_list(args) -> dict:
    try:
        server = libtmux.Server()
        if not server.is_alive():
            return {"agents": [], "count": 0}
        sessions = server.sessions.filter(session_name=args.session)
        if not sessions:
            return {"agents": [], "count": 0}
        session = sessions[0]
    except Exception:
        return {"agents": [], "count": 0}
    panes = _discover(session)

    agents = []
    for pane in panes:
        agents.append({
            "pane_id": pane.pane_id,
            "name": _pane_option(pane, "@roost_name") or "?",
            "program": _pane_option(pane, "@roost_program") or "?",
            "spawned_at": _pane_option(pane, "@roost_spawned") or "?",
            "dead": _is_pane_dead(pane),
        })
    return {"agents": agents, "count": len(agents)}


def cmd_status(args) -> dict:
    server = _get_server()
    session = _get_session(server, args.session)
    pane = _resolve_pane(session, args.target)
    if not pane:
        return _err("PANE_NOT_FOUND", f"No managed pane matching '{args.target}'")

    dead = _is_pane_dead(pane)
    program = _pane_option(pane, "@roost_program") or "unknown"
    lines = [] if dead else pane.capture_pane()
    state, _, _ = detect_state(lines, program, "", "") if not dead else (AgentState.DEAD, "", "")

    return {
        "pane_id": pane.pane_id,
        "name": _pane_option(pane, "@roost_name") or "?",
        "program": program,
        "state": state.value,
        "spawned_at": _pane_option(pane, "@roost_spawned") or "?",
        "last_lines": [_strip_ansi(l) for l in lines[-10:]],
    }


def cmd_send(args) -> dict:
    server = _get_server()
    session = _get_session(server, args.session)
    pane = _resolve_pane(session, args.target)
    if not pane:
        return _err("PANE_NOT_FOUND", f"No managed pane matching '{args.target}'")

    if args.multiline:
        # Use load-buffer + paste-buffer for multiline
        proc = subprocess.run(
            ["tmux", "load-buffer", "-b", "roost-inject", "-"],
            input=args.text, text=True, capture_output=True,
        )
        if proc.returncode != 0:
            return _err("SEND_FAILED", f"load-buffer failed: {proc.stderr.strip()}")
        pane.cmd("paste-buffer", "-d", "-b", "roost-inject")
        time.sleep(0.1)
        pane.send_keys("", literal=False, enter=True)
    else:
        # Send text literally, then delay before Enter.
        # TUI agents (Copilot, Codex) need time to process pasted text
        # before receiving Enter — without this, Enter can be lost.
        pane.send_keys(args.text, literal=True, enter=False)
        time.sleep(0.05)  # 50ms — matches NTM's DefaultEnterDelay
        pane.enter()

    return {"sent": True, "pane_id": pane.pane_id, "text_length": len(args.text)}


def cmd_capture(args) -> dict:
    server = _get_server()
    session = _get_session(server, args.session)
    pane = _resolve_pane(session, args.target)
    if not pane:
        return _err("PANE_NOT_FOUND", f"No managed pane matching '{args.target}'")

    lines = pane.capture_pane(start=-args.lines)
    stripped = [_strip_ansi(l) for l in lines]
    return {"pane_id": pane.pane_id, "lines": stripped, "count": len(stripped)}


def cmd_restart(args) -> dict:
    server = _get_server()
    session = _get_session(server, args.session)
    pane = _resolve_pane(session, args.target)
    if not pane:
        return _err("PANE_NOT_FOUND", f"No managed pane matching '{args.target}'")

    cmd = _pane_option(pane, "@roost_cmd")
    if not cmd:
        return _err("NO_CMD", "No @roost_cmd stored on this pane — cannot restart.")

    # Graceful shutdown: Ctrl-C, wait, check
    pane.send_keys("C-c", literal=False)
    time.sleep(1.5)

    if not _is_pane_dead(pane):
        # Still alive — send another Ctrl-C
        pane.send_keys("C-c", literal=False)
        time.sleep(1.0)

    # Re-run the command
    pane.send_keys(cmd, literal=True, enter=True)
    pane.cmd("set-option", "-p", "@roost_spawned", _utcnow())

    return {"restarted": True, "pane_id": pane.pane_id, "cmd": cmd}


def cmd_kill(args) -> dict:
    server = _get_server()
    session = _get_session(server, args.session)

    if args.all:
        panes = _discover(session)
        killed = []
        for pane in panes:
            name = _pane_option(pane, "@roost_name") or pane.pane_id
            pane.cmd("kill-pane")
            killed.append(name)
        return {"killed": killed, "count": len(killed)}

    pane = _resolve_pane(session, args.target)
    if not pane:
        return _err("PANE_NOT_FOUND", f"No managed pane matching '{args.target}'")

    name = _pane_option(pane, "@roost_name") or pane.pane_id
    pane.cmd("kill-pane")
    return {"killed": [name], "count": 1}


def cmd_watch(args) -> None:
    server = _get_server()
    session = _get_session(server, args.session)

    tracker: dict[str, dict] = {}

    while True:
        panes = _discover(session)

        for pane in panes:
            pid = pane.pane_id
            prev = tracker.get(pid, {
                "state": AgentState.SPAWNING, "hash": "", "stable_since": "",
                "state_since": _utcnow(),
            })

            if _is_pane_dead(pane):
                new_state = AgentState.DEAD
                new_hash, new_stable = prev["hash"], prev["stable_since"]
            else:
                lines = pane.capture_pane()
                program = _pane_option(pane, "@roost_program") or "unknown"
                new_state, new_hash, new_stable = detect_state(
                    lines, program, prev["hash"], prev["stable_since"],
                )

                # Stuck detection
                if (new_state == AgentState.WORKING
                        and prev["state"] == AgentState.WORKING
                        and new_hash == prev["hash"]):
                    try:
                        since = datetime.strptime(prev["state_since"], _UTC_FMT).replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - since).total_seconds() > _STUCK_THRESHOLD:
                            new_state = AgentState.STUCK
                    except ValueError:
                        pass

            now = _utcnow()
            changed = new_state != prev["state"]

            if changed:
                event = {
                    "pane_id": pid,
                    "name": _pane_option(pane, "@roost_name") or "?",
                    "program": _pane_option(pane, "@roost_program") or "?",
                    "prev_state": prev["state"].value if isinstance(prev["state"], AgentState) else prev["state"],
                    "state": new_state.value,
                    "timestamp": now,
                }
                _emit(event, args.json)

            tracker[pid] = {
                "state": new_state,
                "hash": new_hash,
                "stable_since": new_stable,
                "state_since": now if changed else prev["state_since"],
            }

        if args.once:
            # If --once, emit current state for all panes (even unchanged)
            if not any(tracker.get(p.pane_id, {}).get("state") != AgentState.SPAWNING for p in panes):
                pass  # all still spawning, already emitted
            for pane in panes:
                pid = pane.pane_id
                t = tracker.get(pid, {})
                st = t.get("state", AgentState.SPAWNING)
                _emit({
                    "pane_id": pid,
                    "name": _pane_option(pane, "@roost_name") or "?",
                    "program": _pane_option(pane, "@roost_program") or "?",
                    "state": st.value if isinstance(st, AgentState) else st,
                    "state_since": t.get("state_since", ""),
                }, args.json)
            break

        time.sleep(args.interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(prog="roost", description="Tmux-based AI agent lifecycle manager.")
    p.add_argument("--json", action="store_true", help="JSON output to stdout")
    sub = p.add_subparsers(dest="command", required=True)

    # spawn
    sp = sub.add_parser("spawn", help="Spawn agent panes")
    sp.add_argument("--program", required=True, choices=["claude", "copilot"], help="Agent type")
    sp.add_argument("--cmd", required=True, help="Shell command to run in pane")
    sp.add_argument("--session", default="roost", help="tmux session name (default: roost)")
    sp.add_argument("--project", default=None, help="Project path (HUTCH_PROJECT env var)")
    sp.add_argument("--hutch-url", default=None, help="Hutch MCP server URL")
    sp.add_argument("--name", default=None, help="Agent name (auto-generated if omitted)")
    sp.add_argument("--count", type=int, default=1, help="Number of panes to spawn")
    sp.add_argument("--worktree", default=None, help="cd to this path before starting agent")

    # list
    sl = sub.add_parser("list", help="List managed agent panes")
    sl.add_argument("--session", default="roost")

    # status
    ss = sub.add_parser("status", help="Get agent status")
    ss.add_argument("target", help="Pane ID (%%5) or agent name")
    ss.add_argument("--session", default="roost")

    # send
    sd = sub.add_parser("send", help="Send text to agent pane")
    sd.add_argument("target", help="Pane ID or agent name")
    sd.add_argument("text", help="Text to send")
    sd.add_argument("--multiline", action="store_true", help="Use load-buffer for multiline")
    sd.add_argument("--session", default="roost")

    # capture
    sc = sub.add_parser("capture", help="Capture pane output")
    sc.add_argument("target", help="Pane ID or agent name")
    sc.add_argument("--lines", type=int, default=50, help="Number of lines (default: 50)")
    sc.add_argument("--session", default="roost")

    # restart
    sr = sub.add_parser("restart", help="Restart an agent")
    sr.add_argument("target", help="Pane ID or agent name")
    sr.add_argument("--session", default="roost")

    # kill
    sk = sub.add_parser("kill", help="Kill agent pane(s)")
    sk.add_argument("target", nargs="?", default=None, help="Pane ID or agent name")
    sk.add_argument("--all", action="store_true", help="Kill all managed panes")
    sk.add_argument("--session", default="roost")

    # watch
    sw = sub.add_parser("watch", help="Monitor agent states")
    sw.add_argument("--session", default="roost")
    sw.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds")
    sw.add_argument("--once", action="store_true", help="Single poll then exit")

    args = p.parse_args()

    dispatch = {
        "spawn": cmd_spawn, "list": cmd_list, "status": cmd_status,
        "send": cmd_send, "capture": cmd_capture, "restart": cmd_restart,
        "kill": cmd_kill,
    }

    if args.command == "watch":
        try:
            cmd_watch(args)
        except KeyboardInterrupt:
            pass
        return

    if args.command == "kill" and not args.all and not args.target:
        p.error("kill requires a target or --all")

    fn = dispatch.get(args.command)
    if not fn:
        p.error(f"Unknown command: {args.command}")

    try:
        result = fn(args)
        _emit(result, args.json)
    except libtmux.exc.LibTmuxException as e:
        _emit(_err("TMUX_ERROR", str(e)), args.json)
        sys.exit(1)
    except Exception as e:
        _emit(_err("INTERNAL_ERROR", str(e)), args.json)
        sys.exit(1)


if __name__ == "__main__":
    main()

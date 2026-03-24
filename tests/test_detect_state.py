#!/usr/bin/env python3
"""Unit tests for detect_state() in roost.py."""

import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from roost import detect_state, AgentState, _UTC_FMT

passed = 0
failed = 0


def test(name, state, content_hash=None, stable_since=None,
         *, result=None):
    """Assert detect_state returned the expected state (and optionally hash/stable)."""
    global passed, failed
    actual_state, actual_hash, actual_stable = result
    ok = actual_state == state
    if content_hash is not None:
        ok = ok and actual_hash == content_hash
    if stable_since is not None:
        ok = ok and actual_stable == stable_since
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        extra = ""
        if content_hash is not None and actual_hash != content_hash:
            extra += f" hash={actual_hash!r}"
        if stable_since is not None and actual_stable != stable_since:
            extra += f" stable={actual_stable!r}"
        print(f"  FAIL  {name}: expected {state}, got {actual_state}{extra}")


def ds(lines, program="claude", prev_hash="", prev_stable=""):
    return detect_state(lines, program, prev_hash, prev_stable)


def past(seconds):
    """Return a UTC timestamp string `seconds` in the past."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.strftime(_UTC_FMT)


# ======================================================================
print("\n--- Claude Code idle patterns ---")
# ======================================================================

# NBSP after ❯  (U+276F followed by U+00A0)
test("claude idle: NBSP prompt",
     AgentState.IDLE,
     result=ds(["some output", "\u276f\u00a0"]))

# Plain ❯ with trailing spaces
test("claude idle: bare prompt",
     AgentState.IDLE,
     result=ds(["some output", "\u276f  "]))

# ❯ alone
test("claude idle: prompt no trailing",
     AgentState.IDLE,
     result=ds(["some output", "\u276f"]))

# > prompt
test("claude idle: > prompt",
     AgentState.IDLE,
     result=ds(["some output", "> "]))

# > prompt bare
test("claude idle: > prompt bare",
     AgentState.IDLE,
     result=ds(["some output", ">"]))

# ╰─> arrow prompt
test("claude idle: arrow prompt",
     AgentState.IDLE,
     result=ds(["some output", "\u2570\u2500> "]))

# ╰─> bare
test("claude idle: arrow prompt bare",
     AgentState.IDLE,
     result=ds(["some output", "\u2570\u2500>"]))

# Welcome screen
test("claude idle: welcome screen",
     AgentState.IDLE,
     result=ds(["", "  Claude Code v1.2.3", "", "  /help for commands"]))

test("claude idle: welcome screen lowercase",
     AgentState.IDLE,
     result=ds(["", "  claude code v0.1.99", ""]))


# ======================================================================
print("\n--- Claude Code working patterns ---")
# ======================================================================

# "esc to interrupt"
test("claude working: esc to interrupt",
     AgentState.WORKING,
     result=ds(["output", "esc to interr", "\u276f\u00a0"]))

# Spinner chars:  ✽… (U+273D + U+2026)
test("claude working: spinner ✽…",
     AgentState.WORKING,
     result=ds(["output", "\u273d Reading file\u2026"]))

# ·… (U+00B7 + U+2026)
test("claude working: spinner ·…",
     AgentState.WORKING,
     result=ds(["output", "\u00b7 Editing\u2026"]))

# ✻… (U+273B + U+2026)
test("claude working: spinner ✻…",
     AgentState.WORKING,
     result=ds(["output", "\u273b Processing\u2026"]))

# ✳… (U+2733 + U+2026)
test("claude working: spinner ✳…",
     AgentState.WORKING,
     result=ds(["output", "\u2733 Running command\u2026"]))

# ✢… (U+2722 + U+2026)
test("claude working: spinner ✢…",
     AgentState.WORKING,
     result=ds(["output", "\u2722 Searching\u2026"]))

# · thinking
test("claude working: thinking",
     AgentState.WORKING,
     result=ds(["output", "\u00b7 thinking"]))

# ✳ thinking
test("claude working: thinking alt spinner",
     AgentState.WORKING,
     result=ds(["output", "\u2733 Thinking"]))

# Running…
test("claude working: Running…",
     AgentState.WORKING,
     result=ds(["output", "Running\u2026"]))


# ======================================================================
print("\n--- Copilot idle patterns ---")
# ======================================================================

# ❯  Type @ to mention files
test("copilot idle: Type @ prompt",
     AgentState.IDLE,
     result=ds(["\u276f  Type @ to mention files, folders, and more"], "copilot"))

# ❯ with just whitespace
test("copilot idle: bare prompt",
     AgentState.IDLE,
     result=ds(["output", "\u276f   "], "copilot"))

# ? for shortcuts
test("copilot idle: ? shortcuts",
     AgentState.IDLE,
     result=ds(["output", "? for shortcuts"], "copilot"))

# Welcome screen
test("copilot idle: welcome screen",
     AgentState.IDLE,
     result=ds(["", "GitHub Copilot v1.0.5", ""], "copilot"))


# ======================================================================
print("\n--- Copilot working patterns ---")
# ======================================================================

test("copilot working: Esc to cancel",
     AgentState.WORKING,
     result=ds(["output", "Esc to cancel"], "copilot"))

test("copilot working: Thinking",
     AgentState.WORKING,
     result=ds(["output", "Thinking..."], "copilot"))

test("copilot working: Running",
     AgentState.WORKING,
     result=ds(["output", "Running something"], "copilot"))

test("copilot working: Executing",
     AgentState.WORKING,
     result=ds(["output", "Executing command"], "copilot"))

# Case insensitive
test("copilot working: thinking lowercase",
     AgentState.WORKING,
     result=ds(["output", "thinking about it"], "copilot"))


# ======================================================================
print("\n--- Working overrides idle ---")
# ======================================================================

# Claude: working patterns checked in last 6 lines, idle in last 12.
# If spinner AND prompt both appear in last 6 lines, working wins.
test("working overrides idle: spinner + prompt in last 6",
     AgentState.WORKING,
     result=ds([
         "some earlier output",
         "more output",
         "\u273d Reading file\u2026",
         "",
         "\u276f\u00a0",
     ]))

# esc to interrupt on same screen as prompt
test("working overrides idle: esc to interrupt + prompt",
     AgentState.WORKING,
     result=ds([
         "output",
         "esc to interr",
         "",
         "\u276f ",
     ]))


# ======================================================================
print("\n--- Context exhaustion ---")
# ======================================================================

test("context: window exceeded",
     AgentState.CONTEXT_EXHAUSTED,
     result=ds(["output"] * 10 + ["Error: context window exceeded"]))

test("context: conversation too long",
     AgentState.CONTEXT_EXHAUSTED,
     result=ds(["output"] * 5 + ["Your conversation is too long to continue."]))

test("context: maximum context",
     AgentState.CONTEXT_EXHAUSTED,
     result=ds(["output"] * 5 + ["maximum context reached, please start a new session"]))

test("context: context limit reached",
     AgentState.CONTEXT_EXHAUSTED,
     result=ds(["output"] * 5 + ["context limit reached"]))

test("context: prompt is too long",
     AgentState.CONTEXT_EXHAUSTED,
     result=ds(["output"] * 5 + ["Error: prompt is too long for model"]))

test("context: context full",
     AgentState.CONTEXT_EXHAUSTED,
     result=ds(["output"] * 5 + ["Warning: context full"]))


# ======================================================================
print("\n--- Rate limiting ---")
# ======================================================================

test("rate limit: hit your limit",
     AgentState.RATE_LIMITED,
     result=ds(["output"] * 5 + ["You've hit your limit for today."]))

test("rate limit: rate limit",
     AgentState.RATE_LIMITED,
     result=ds(["output"] * 5 + ["rate limit exceeded, try again later"]))

test("rate limit: too many requests",
     AgentState.RATE_LIMITED,
     result=ds(["output"] * 5 + ["Error 429: too many requests"]))

test("rate limit: usage limit",
     AgentState.RATE_LIMITED,
     result=ds(["output"] * 5 + ["usage limit reached"]))

test("rate limit: please wait try again",
     AgentState.RATE_LIMITED,
     result=ds(["output"] * 5 + ["please wait a moment and try again"]))

test("rate limit: exceeded limit",
     AgentState.RATE_LIMITED,
     result=ds(["output"] * 5 + ["You have exceeded your limit for this period"]))


# ======================================================================
print("\n--- Content stabilization (Tier 2) ---")
# ======================================================================

# No patterns match, same hash, enough time elapsed -> IDLE
lines_no_pattern = ["random output that matches nothing"] * 5
_, ref_hash, _ = ds(lines_no_pattern)
stable_time = past(5)  # 5 seconds ago (> 3s threshold)

test("stabilization: same hash + elapsed > 3s -> IDLE",
     AgentState.IDLE,
     stable_since=stable_time,
     result=ds(lines_no_pattern, "claude", ref_hash, stable_time))

# Same hash, not enough time -> WORKING
recent_time = past(1)  # 1 second ago (< 3s threshold)
test("stabilization: same hash + elapsed < 3s -> WORKING",
     AgentState.WORKING,
     stable_since=recent_time,
     result=ds(lines_no_pattern, "claude", ref_hash, recent_time))

# Different hash -> WORKING (reset)
test("stabilization: different hash -> WORKING (reset)",
     AgentState.WORKING,
     result=ds(lines_no_pattern, "claude", "completely_different_hash", stable_time))

# Same hash, no prev_stable -> WORKING (new stabilization window)
test("stabilization: same hash + no prev_stable -> WORKING",
     AgentState.WORKING,
     result=ds(lines_no_pattern, "claude", ref_hash, ""))


# ======================================================================
print("\n--- Empty lines ---")
# ======================================================================

test("empty lines: empty list -> WORKING",
     AgentState.WORKING,
     result=ds([]))

test("empty lines: list of blank strings -> WORKING",
     AgentState.WORKING,
     result=ds(["", "", ""]))


# ======================================================================
print("\n--- ANSI stripping ---")
# ======================================================================

# Prompt with ANSI color codes around it
ansi_prompt = "\x1b[32m\u276f\x1b[0m\u00a0"
test("ansi: colored prompt still detected as idle",
     AgentState.IDLE,
     result=ds(["output", ansi_prompt]))

# Spinner with ANSI
ansi_spinner = "\x1b[36m\u273d\x1b[0m Reading file\u2026"
test("ansi: colored spinner still detected as working",
     AgentState.WORKING,
     result=ds(["output", ansi_spinner]))

# Welcome screen with ANSI bold
ansi_welcome = "\x1b[1mClaude Code v1.5.0\x1b[0m"
test("ansi: bold welcome screen still detected as idle",
     AgentState.IDLE,
     result=ds(["", ansi_welcome, ""]))

# Context exhaustion with ANSI
ansi_context = "\x1b[31mError: context window exceeded\x1b[0m"
test("ansi: context exhaustion with color codes",
     AgentState.CONTEXT_EXHAUSTED,
     result=ds(["output"] * 5 + [ansi_context]))

# Rate limit with ANSI
ansi_rate = "\x1b[33mYou've hit your limit\x1b[0m"
test("ansi: rate limit with color codes",
     AgentState.RATE_LIMITED,
     result=ds(["output"] * 5 + [ansi_rate]))

# Copilot prompt with OSC escape sequences
osc_prompt = "\x1b]0;copilot\x07\u276f  Type @ to mention files"
test("ansi: OSC + copilot prompt",
     AgentState.IDLE,
     result=ds([osc_prompt], "copilot"))


# ======================================================================
# Summary
# ======================================================================

print(f"\n{'='*50}")
total = passed + failed
print(f"Results: {passed}/{total} passed, {failed} failed")
if failed:
    print("SOME TESTS FAILED")
    sys.exit(1)
else:
    print("ALL TESTS PASSED")
    sys.exit(0)

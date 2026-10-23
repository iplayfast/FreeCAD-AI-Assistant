# SPDX-License-Identifier: LGPL-2.1-or-later
"""Run the agent against several real tasks and rank what it gets wrong.

    python3 run_agent_suite.py               # every task
    python3 run_agent_suite.py bolt bracket  # just these

Each task is one ``freecadcmd test_bearing_run.py`` in its own process, with its
own document and log, driven by the provider in your settings. The verdict per
task is that harness's: solids counted in the finished document, never the
model's own account of how it went.

Why a suite. Every fix in this addon so far arrived as a traceback pasted from
the GUI — one failure at a time, each costing a person a full manual run to
find. The failures are not one-offs though: a model that says object_name to one
tool says it to the next, and a model that reaches for execute_code does it
everywhere. Running several tasks at once and grouping the errors turns "here is
the one that bit me today" into "here are the classes, most common first".

The tasks are the ones actually attempted in this workbench — bearing, bolt,
bracket, arm — not synthetic ones, because the interesting failures come from
real geometry: patterns, pockets that cut nothing, sketches that will not close.
"""

import os
import re
import subprocess
import sys
from collections import Counter

FREECADCMD = os.environ.get("FREECADCMD", "freecadcmd")
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "suite_logs")

# min_solids is the bar for "it built the thing", kept deliberately low: this
# measures whether the tools can be driven at all, not whether the model is a
# good engineer. A run that produces nothing is the failure worth chasing.
TASKS = {
    "bearing": dict(
        doc="SuiteBearing", min_solids=2,
        prompt="Build a parametric 608 skate bearing in a new document called "
               "'SuiteBearing'. Bore 8 mm, outside diameter 22 mm, width 7 mm, 8 balls "
               "of 3.969 mm on a 15 mm pitch circle. Drive every dimension from a "
               "VarSet so the bearing resizes when a variable changes.",
    ),
    "bolt": dict(
        doc="SuiteBolt", min_solids=1,
        prompt="Build an M8x40 hex bolt in a new document called 'SuiteBolt': a "
               "hexagonal head 13 mm across the flats and 5.3 mm thick, and a plain "
               "shank 8 mm in diameter and 40 mm long. No thread needed.",
    ),
    "bracket": dict(
        doc="SuiteBracket", min_solids=1,
        prompt="Build an L-bracket in a new document called 'SuiteBracket': 60 mm by "
               "40 mm, 5 mm thick, with a 6 mm mounting hole 10 mm in from each end.",
    ),
    "arm": dict(
        doc="SuiteArm", min_solids=2,
        prompt="Build a robot arm link in a new document called 'SuiteArm': a flat bar "
               "80 mm long, 20 mm wide and 8 mm thick, with a 10 mm boss at each end "
               "and a 5 mm hole through both bosses.",
    ),
}


def run_task(name: str, task: dict, max_steps: int) -> dict:
    """One task in its own FreeCAD, returning the parsed outcome."""
    log = os.path.join(LOG_DIR, f"{name}.log")
    env = {
        **os.environ,
        "BEARING_PROMPT": task["prompt"],
        "BEARING_DOC": task["doc"],
        "BEARING_LOG": log,
        "BEARING_MIN_SOLIDS": str(task["min_solids"]),
        "BEARING_MAX_STEPS": str(max_steps),
        "BEARING_EXIT": os.path.join(LOG_DIR, f"{name}.exit"),
    }
    subprocess.run([FREECADCMD, os.path.join(HERE, "test_bearing_run.py")],
                   env=env, cwd=HERE, capture_output=True, timeout=3600)

    text = open(log, encoding="utf-8", errors="replace").read() if os.path.exists(log) else ""
    verdict = re.search(r"^(PASS|FAIL|BLOCKED).*$", text, re.M)
    return {
        "name": name,
        "verdict": verdict.group(0) if verdict else "NO VERDICT (harness did not finish)",
        "passed": bool(verdict and verdict.group(1) == "PASS"),
        # Tool name of each call, and the error text of each failure, so the
        # report can say which tool and which mistake, not just how many.
        "calls": re.findall(r"^\[call \d+\] ([a-z_0-9]+):", text, re.M),
        "errors": re.findall(r"^    ERR (.*)$", text, re.M),
        "log": log,
    }


def _error_class(message: str) -> str:
    """Collapse an error to the thing that has to be fixed.

    Names and numbers differ every run; the shape does not. "Document 'A' not
    found" and "Document 'B' not found" are one bug, and counting them as two
    hides how common it is.
    """
    text = re.sub(r"'[^']*'", "'X'", message)
    text = re.sub(r"\b\d+(\.\d+)?\b", "N", text)
    return text[:110]


def main(argv: list) -> int:
    wanted = [a for a in argv if a in TASKS] or list(TASKS)
    max_steps = int(os.environ.get("SUITE_MAX_STEPS", "30"))
    os.makedirs(LOG_DIR, exist_ok=True)

    results = []
    for name in wanted:
        print(f"[suite] {name} …", flush=True)
        result = run_task(name, TASKS[name], max_steps)
        print(f"[suite] {name}: {result['verdict'][:80]}", flush=True)
        results.append(result)

    print("\n=== tasks ===")
    for r in results:
        calls, errs = len(r["calls"]), len(r["errors"])
        rate = f"{errs}/{calls}" if calls else "no calls"
        print(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['name']:9s} {rate:>9s} errors   {r['verdict'][:60]}")

    every_call = Counter(c for r in results for c in r["calls"])
    if every_call:
        raw = every_call.get("execute_code", 0)
        total = sum(every_call.values())
        print(f"\nexecute_code share: {raw}/{total} calls "
              f"({100 * raw // total}%) — the typed tools are the point")

    classes = Counter(_error_class(e) for r in results for e in r["errors"])
    print(f"\n=== error classes ({sum(classes.values())} errors) ===")
    for message, count in classes.most_common(15):
        print(f"  {count:3d}x  {message}")

    print(f"\nlogs in {LOG_DIR}")
    return 0 if all(r["passed"] for r in results) else 1


sys.exit(main(sys.argv[1:]))

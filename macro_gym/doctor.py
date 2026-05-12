"""Diagnostics CLI: ``python -m macro_gym.doctor``.

Prints a PASS/FAIL line per check with a one-line ``Fix:`` hint on
failure. Exit 0 iff every required check passes (warnings OK), else 1.

``--json`` emits a machine-readable summary for CI gates.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

KATAS_DIR = Path(__file__).parent.parent / "katas"

# ANSI; kept inline to avoid a stdlib dep beyond what's already imported.
_GREEN, _RED, _YELLOW, _DIM, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _emit(check: dict, use_color: bool) -> None:
    status = check["status"]
    name = check["name"]
    msg = check.get("message", "")
    if use_color:
        tag = {"PASS": _GREEN + "PASS" + _RST,
               "FAIL": _RED + "FAIL" + _RST,
               "NOTE": _YELLOW + "NOTE" + _RST}[status]
    else:
        tag = status
    line = f"[{tag}] {name}"
    if msg:
        line += f" — {msg}"
    print(line)
    if status == "FAIL" and check.get("fix"):
        print(f"       Fix: {check['fix']}")


def check_sbcl_present() -> dict:
    path = shutil.which("sbcl")
    if not path:
        return {
            "name": "sbcl on PATH",
            "status": "FAIL",
            "message": "sbcl binary not found",
            "fix": "apt install sbcl  |  dnf install sbcl  |  pacman -S sbcl  |  brew install sbcl",
        }
    return {"name": "sbcl on PATH", "status": "PASS", "message": path}


def check_sbcl_version() -> dict:
    path = shutil.which("sbcl")
    if not path:
        return {"name": "sbcl version >= 2.0", "status": "FAIL",
                "message": "sbcl not on PATH", "fix": "install sbcl first"}
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return {"name": "sbcl version >= 2.0", "status": "FAIL",
                "message": f"could not run sbcl --version: {e}",
                "fix": "reinstall sbcl"}
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        return {"name": "sbcl version >= 2.0", "status": "NOTE",
                "message": f"could not parse: {out!r}"}
    major, minor, _ = map(int, m.groups())
    if major < 2:
        return {"name": "sbcl version >= 2.0", "status": "FAIL",
                "message": out,
                "fix": "upgrade SBCL to >=2.0 (Roswell or distro backports)"}
    return {"name": "sbcl version >= 2.0", "status": "PASS", "message": out}


def check_katas_dir() -> dict:
    if not KATAS_DIR.is_dir():
        return {"name": "katas/ directory exists", "status": "FAIL",
                "message": f"missing: {KATAS_DIR}",
                "fix": "git clone the repo, or pass kata_dirs=[...] to MacroGrader"}
    kids = [d for d in KATAS_DIR.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not kids:
        return {"name": "katas/ directory exists", "status": "FAIL",
                "message": f"{KATAS_DIR} is empty",
                "fix": "add at least one kata under katas/<id>/"}
    return {"name": "katas/ directory exists", "status": "PASS",
            "message": f"{len(kids)} kata(s) at {KATAS_DIR}"}


def check_kata_files() -> dict:
    if not KATAS_DIR.is_dir():
        return {"name": "every kata has setup.lisp + tests.lisp",
                "status": "FAIL", "message": "no katas/ directory",
                "fix": "see previous check"}
    missing = []
    for d in KATAS_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        for fname in ("setup.lisp", "tests.lisp"):
            if not (d / fname).is_file():
                missing.append(f"{d.name}/{fname}")
    if missing:
        return {"name": "every kata has setup.lisp + tests.lisp",
                "status": "FAIL", "message": f"missing: {', '.join(missing)}",
                "fix": "create the missing file(s) — see docs/kata-authoring.md"}
    return {"name": "every kata has setup.lisp + tests.lisp",
            "status": "PASS", "message": ""}


def check_psutil() -> dict:
    try:
        import psutil  # noqa: F401
        return {"name": "psutil installed (optional, for RSS-based TTL)",
                "status": "PASS", "message": ""}
    except ImportError:
        return {"name": "psutil installed (optional, for RSS-based TTL)",
                "status": "NOTE",
                "message": "not installed — falling back to count-based TTL only"}


def check_heap_configurable() -> dict:
    path = shutil.which("sbcl")
    if not path:
        return {"name": "SBCL accepts --dynamic-space-size",
                "status": "FAIL", "message": "sbcl not on PATH",
                "fix": "install sbcl first"}
    try:
        r = subprocess.run(
            [path, "--dynamic-space-size", "256", "--noinform",
             "--non-interactive", "--eval", "(quit)"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        return {"name": "SBCL accepts --dynamic-space-size",
                "status": "FAIL", "message": str(e),
                "fix": "check your SBCL build"}
    if r.returncode != 0:
        return {"name": "SBCL accepts --dynamic-space-size",
                "status": "FAIL",
                "message": (r.stderr or r.stdout).strip()[:200],
                "fix": "rebuild SBCL with --with-dynamic-space-size support"}
    return {"name": "SBCL accepts --dynamic-space-size",
            "status": "PASS", "message": ""}


def check_smoke() -> dict:
    """Spawn a real grader and grade the with-logging reference solution."""
    if not shutil.which("sbcl"):
        return {"name": "smoke test (grade with-logging reference)",
                "status": "FAIL", "message": "sbcl not on PATH",
                "fix": "install sbcl first"}
    try:
        from .grader import MacroGrader  # local import: avoid early failure
    except Exception as e:  # noqa: BLE001
        return {"name": "smoke test (grade with-logging reference)",
                "status": "FAIL",
                "message": f"could not import MacroGrader: {e}",
                "fix": "reinstall macro-gym (pip install -e .)"}
    ref = (
        "(defmacro with-logging (ctx-name &body body)\n"
        '  (let ((r (gensym "RESULT")))\n'
        "    `(progn\n"
        "       (log-enter ,ctx-name)\n"
        "       (let ((,r (progn ,@body)))\n"
        "         (log-leave ,ctx-name ,r)\n"
        "         ,r))))"
    )
    t0 = time.time()
    grader = None
    try:
        grader = MacroGrader(pool_size=1)
        res = grader.grade("with-logging", ref, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        return {"name": "smoke test (grade with-logging reference)",
                "status": "FAIL", "message": f"grader raised: {e}",
                "fix": "run `python -m macro_gym.doctor` after fixing other checks"}
    finally:
        if grader is not None:
            try:
                grader.close()
            except Exception:  # noqa: BLE001
                pass
    elapsed = time.time() - t0
    reward = float(res.get("reward", 0.0))
    if reward < 1.0:
        return {"name": "smoke test (grade with-logging reference)",
                "status": "FAIL",
                "message": f"reward={reward} in {elapsed:.1f}s (expected 1.0)",
                "fix": "check katas/with-logging/ files and lisp/server.lisp"}
    return {"name": "smoke test (grade with-logging reference)",
            "status": "PASS", "message": f"reward=1.0 in {elapsed:.1f}s"}


CHECKS = (
    check_sbcl_present,
    check_sbcl_version,
    check_katas_dir,
    check_kata_files,
    check_psutil,
    check_heap_configurable,
    check_smoke,
)


def run(as_json: bool = False) -> int:
    results = [fn() for fn in CHECKS]
    if as_json:
        json.dump({"checks": results,
                   "ok": all(r["status"] != "FAIL" for r in results)},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        use_color = sys.stdout.isatty()
        for r in results:
            _emit(r, use_color)
    return 0 if all(r["status"] != "FAIL" for r in results) else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m macro_gym.doctor",
                                description="macro-gym environment diagnostics")
    p.add_argument("--json", action="store_true", help="emit structured JSON")
    args = p.parse_args()
    return run(as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())

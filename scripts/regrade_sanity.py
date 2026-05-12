#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Regrade the 44 grpo-sanity samples against the current macro-gym build.

Compares the original recorded reward against what the post-#1 + #5 build
emits today. The "before" comes from the JSONL itself — we trust whatever
the training run recorded as the contemporaneous baseline reward. The
"after" is fresh: we re-run MacroGrader.grade against each sample's
defmacro_extracted source.

Empirical-validation gate per the plan:
  * Trivial-policy floor:   a defmacro that always emits a no-op body
    must not score above 0 on any sample.
  * Full-pass gold-standard: any sample that originally scored 1.0 must
    still score 1.0 (no regressions on the strong signal).
  * Distribution shift:     the new shape should move the -0.1 mass into
    the new {-0.07, -0.05, -0.03} buckets, not inflate the >=0 region.

Usage:
    ./scripts/regrade_sanity.py
    ./scripts/regrade_sanity.py --samples /path/to/grpo-sanity
    ./scripts/regrade_sanity.py --pool-size 4

Pre-req: SBCL on PATH. Pre-req: kata dirs from cl-macro-llm exist at the
default --samples path (see DEFAULT_SAMPLES below).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_SAMPLES = Path("/Users/jonatan/projects/cl-macro-llm/data/grpo-sanity")
TRIVIAL_NOOP = "(defmacro foo (&rest args) (declare (ignore args)) nil)"


def build_kata_root(sanity_root: Path) -> Path:
    """Materialize a flat <tmp>/katas/<kata-id>/ tree symlinking the
    cl-macro-llm sanity-set kata definitions. The Lisp server reads
    *kata-root* via MACRO_GYM_KATA_ROOT — point it here.

    Two-level walk: cl-ds/ and creative/ each contain N kata dirs by ID,
    sometimes overlapping. Creative wins on collisions because those are
    the curated variants from generate_creative_macros.py."""
    tmp = Path(tempfile.mkdtemp(prefix="macro-gym-sanity-"))
    katas = tmp / "katas"
    katas.mkdir()
    placed: dict[str, Path] = {}
    # Order matters: cl-ds first, creative overwrites.
    for sub in ("cl-ds", "creative"):
        src = sanity_root / "katas" / sub
        if not src.exists():
            continue
        for kata in src.iterdir():
            if not kata.is_dir() or kata.name.startswith("_"):
                continue
            placed[kata.name] = kata
    for name, kata in placed.items():
        (katas / name).symlink_to(kata, target_is_directory=True)
    return tmp


def load_samples(sanity_root: Path) -> list[dict]:
    rows: list[dict] = []
    for f in sorted((sanity_root).glob("samples-step-*.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            d["_file"] = f.name
            rows.append(d)
    return rows


def bucket(reward: float) -> str:
    """Single-line histogram bucket. The new buckets land on round
    decimals so equality matches the error-reward-for-type table."""
    eps = 1e-6
    for v, label in [
        (-0.10, "err:-0.10 (pathological/no-defmacro)"),
        (-0.07, "err:-0.07 (read-error)"),
        (-0.05, "err:-0.05 (install-error)"),
        (-0.03, "err:-0.03 (evaluate-error)"),
        (0.0,   "wrong:0.00 (compiled, no test passed)"),
        (1.0,   "pass:1.00 (full pass)"),
    ]:
        if abs(reward - v) < eps:
            return label
    if 0.0 < reward < 1.0:
        return "partial:0<r<1"
    return f"other:{reward:.4f}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    p.add_argument("--pool-size", type=int, default=4)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--limit", type=int, default=0,
                   help="only regrade the first N samples (0 = all)")
    p.add_argument("--keep-tmp", action="store_true")
    args = p.parse_args()

    if not args.samples.exists():
        print(f"ERROR: samples path missing: {args.samples}", file=sys.stderr)
        return 2

    samples = load_samples(args.samples)
    if args.limit:
        samples = samples[: args.limit]
    print(f"Loaded {len(samples)} samples from {args.samples}")

    tmp = build_kata_root(args.samples)
    print(f"Materialized {len(list((tmp/'katas').iterdir()))} kata symlinks at {tmp}/katas")

    os.environ["MACRO_GYM_KATA_ROOT"] = str(tmp / "katas")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from macro_gym import MacroGrader  # noqa: E402

    grader = MacroGrader(pool_size=args.pool_size, default_timeout=args.timeout)
    try:
        # --- 1. Trivial-policy floor gate ----------------------------
        kata_ids = sorted({s["kata_id"] for s in samples})
        trivial_rewards = []
        for kid in kata_ids:
            r = grader.grade(kid, TRIVIAL_NOOP)
            trivial_rewards.append((kid, r.get("reward", -0.1)))
        trivial_max = max(rw for _, rw in trivial_rewards)
        print(f"\n[trivial-floor] no-op macro across {len(kata_ids)} katas: "
              f"max reward = {trivial_max:.4f}")
        if trivial_max > 0.0:
            offenders = [(k, rw) for k, rw in trivial_rewards if rw > 0.0]
            print("  ! TRIVIAL POLICY SCORES POSITIVE on:")
            for k, rw in offenders[:5]:
                print(f"      {k}: {rw:.4f}")

        # --- 2. Re-grade each sample ---------------------------------
        before_hist: Counter = Counter()
        after_hist: Counter = Counter()
        regressions: list[tuple[str, float, float]] = []
        upgrades: list[tuple[str, float, float]] = []
        per_kata = defaultdict(lambda: {"n": 0, "before_sum": 0.0, "after_sum": 0.0})

        for i, s in enumerate(samples):
            kid = s["kata_id"]
            src = s.get("defmacro_extracted") or ""
            before = float(s.get("reward", -0.1))
            if not src:
                # No defmacro was extracted during training. New shape will
                # see an empty source → read-error or no-defmacro. Score it
                # anyway so the bucket reflects what the new run would emit.
                src = ""
            try:
                r = grader.grade(kid, src)
            except Exception as e:
                print(f"  ! grader error for {kid}: {e}")
                continue
            after = float(r.get("reward", -0.1))
            before_hist[bucket(before)] += 1
            after_hist[bucket(after)] += 1
            per_kata[kid]["n"] += 1
            per_kata[kid]["before_sum"] += before
            per_kata[kid]["after_sum"] += after

            # Gold-standard regression: any 1.0 → not-1.0 is a failure
            if before == 1.0 and after != 1.0:
                regressions.append((kid, before, after))
            if before < 0.0 and after > before + 0.01:
                upgrades.append((kid, before, after))

        # --- 3. Report -----------------------------------------------
        print("\n=== Reward distribution: BEFORE → AFTER ===")
        all_buckets = sorted(set(before_hist) | set(after_hist))
        print(f"  {'bucket':<42}  before  after")
        for b in all_buckets:
            print(f"  {b:<42}  {before_hist[b]:>6}  {after_hist[b]:>5}")
        print(f"  {'TOTAL':<42}  {sum(before_hist.values()):>6}  "
              f"{sum(after_hist.values()):>5}")

        print(f"\n[gold-standard regressions] (must be 0): {len(regressions)}")
        for k, b, a in regressions[:5]:
            print(f"  {k}: {b:.4f} → {a:.4f}")

        print(f"\n[upgrades from -0.1 floor] {len(upgrades)} samples gained signal")
        granular_hits = [(k, b, a) for k, b, a in upgrades if -0.10 < a < 0.0]
        print(f"  of which landed in new granular bucket: {len(granular_hits)}")

        mean_before = sum(s.get("reward", -0.1) for s in samples) / len(samples)
        # mean_after recomputes from the histogram (cheaper than tracking)
        mean_after_total = 0.0
        for s in samples:
            kid = s["kata_id"]
            src = s.get("defmacro_extracted") or ""
            try:
                r = grader.grade(kid, src) if False else None  # no double-grade
            except Exception:
                r = None
        # Use per-kata tallies for an accurate mean.
        n_total = sum(v["n"] for v in per_kata.values())
        mean_after = sum(v["after_sum"] for v in per_kata.values()) / max(1, n_total)
        print(f"\n[summary] mean reward: before {mean_before:+.4f}  "
              f"after {mean_after:+.4f}  delta {mean_after-mean_before:+.4f}")
        # Verdict — purely informational, the user decides ship/no-ship.
        if len(regressions) == 0 and trivial_max <= 0.0:
            print("\nGATES: trivial-floor OK, no gold-standard regressions.")
        else:
            print("\nGATES: SOMETHING FAILED — see above.")

    finally:
        grader.close()
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"\n[--keep-tmp] symlink tree preserved at {tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

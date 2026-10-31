"""Reproduce skill_view's GIL convoy without private skills or provider calls.

Run from the Hermes worktree with the Hermes interpreter. Each invocation is a
fresh process. Alternate --side base/head at least five times; both sides use the
same 440-skill synthetic library, interpreter, default GIL switch interval and
full skill_view(preprocess=False) result validation. Point --repo at the
base or head worktree; --side is only a label, never an implementation override.
"""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument("--side", choices=("base", "head"), required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.repo))

with tempfile.TemporaryDirectory(prefix="skill-index-bench-") as temp:
    home = Path(temp)
    os.environ["HERMES_HOME"] = str(home)
    names = []
    for i in range(440):
        name = f"bench-{i}"
        directory = home / "skills" / f"category-{i // 11}" / f"package-{i}"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use when benchmarking.\n---\n\nInstructions {i}.\n", encoding="utf-8")
        for support in ("references", "scripts"):
            (directory / support).mkdir()
        (directory / "references" / "example.md").write_text("Reference instructions.\n", encoding="utf-8")
        names.append(name)

    from tools import skills_tool as st
    def view(index):
        start = time.perf_counter()
        result = json.loads(st.skill_view(names[index], preprocess=False))
        elapsed = (time.perf_counter() - start) * 1000
        assert result["success"] and result["name"] == names[index], result
        assert f"Instructions {index}." in result["content"]
        return elapsed

    view(0)  # import/frontmatter/snapshot warmup is not timed
    solo = [view(i) for i in range(5)]
    barrier = threading.Barrier(12)

    def concurrent_view(index):
        barrier.wait()
        return view(index)

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        concurrent_ms = list(pool.map(concurrent_view, range(12)))

    view(0)  # warm again before CPU contention; no special TTL override
    stop = threading.Event()
    start_busy = threading.Barrier(12)

    def busy():
        start_busy.wait()
        number = 0
        while not stop.is_set():
            number = (number + 1) % 1000003

    threads = [threading.Thread(target=busy) for _ in range(11)]
    for thread in threads:
        thread.start()
    start_busy.wait()
    try:
        busy_ms = view(0)
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    print(json.dumps({"side": args.side, "skills": len(names), "errors": 0,
                      "solo_ms": solo, "solo_median_ms": statistics.median(solo),
                      "concurrent_12_ms": concurrent_ms,
                      "concurrent_12_median_ms": statistics.median(concurrent_ms),
                      "one_vs_11_cpu_busy_ms": busy_ms,
                      "python": sys.version, "switch_interval": sys.getswitchinterval()}))

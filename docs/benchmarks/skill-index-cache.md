# Skill lookup index benchmark

## Result

The warm full `skill_view` CPU-contention case improved from **52,329 ms to 556 ms** (median of five fresh-process runs per revision). This is a synthetic microbenchmark, not a production latency guarantee.

| Shape | Base median (range), ms | Indexed median (range), ms |
|---|---:|---:|
| Warm solo | 42.35 (38.41–48.76) | 2.75 (2.00–3.12) |
| 12 concurrent views, per-call median | 947.71 (686.09–2805.96) | 57.49 (22.26–63.46) |
| One view vs. 11 CPU-busy Python threads | 52328.87 (12856.46–136955.61) | 555.86 (152.32–3862.45) |

## Method

- Base: `90472a57e411754c790c7a6524e14378bcf7cccb`; head: the implementation in this change. Separate base/head worktrees, same interpreter and benchmark script. No implementation monkeypatches in these reported runs.
- Five alternating base/head pairs, each in a fresh process. Python 3.14.7, default 5 ms GIL switch interval, Linux, eight logical CPUs. The shared host was busy (observed load averages roughly 2.6–4.8); focused/broader tests also ran during part of the measurement window. Alternation reduces drift but does not eliminate scheduler noise, especially in the contention case.
- Public synthetic fixture: 440 skills in 40 categories, directory names distinct from declared names, one reference file and an empty scripts directory per skill. All state stays under a temporary isolated `HERMES_HOME`; no private skills, provider requests or inline skill execution.
- The timed region calls full `skill_view(name, preprocess=False)` and parses its JSON. Every call asserts success, exact resolved name and expected body. All ten processes completed successfully with zero reported errors.
- Each run warms imports/frontmatter/snapshot before five solo views, starts twelve concurrent views through a barrier, warms again, then measures one view after a barrier releases eleven pure-Python CPU-busy threads. No GIL or TTL tuning.

Reproduce with a configured Hermes interpreter from the head worktree, alternating both commands five times:

```bash
python scripts/benchmarks/skill_index.py --repo /path/to/base-worktree --side base
python scripts/benchmarks/skill_index.py --repo /path/to/head-worktree --side head
```

### Per-process summaries (milliseconds)

| Pair | Base solo | Head solo | Base 12-way | Head 12-way | Base CPU-busy | Head CPU-busy |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 42.35 | 2.75 | 2805.96 | 63.46 | 12856.46 | 3862.45 |
| 2 | 43.64 | 2.82 | 760.79 | 57.49 | 42093.69 | 555.86 |
| 3 | 39.65 | 3.12 | 686.09 | 22.26 | 52328.87 | 392.98 |
| 4 | 48.76 | 2.36 | 947.71 | 44.95 | 136955.61 | 152.32 |
| 5 | 38.41 | 2.00 | 1251.98 | 62.47 | 109696.40 | 1066.41 |

## Attribution and verification

- Separate instrumented runs (not included in the timing table): a warm full view made 468 `stat`, 1,409 `lstat` and 1,850 `scandir` calls before; 30, 48 and 8 after. Remaining directory scans belong to linked-file discovery, not the library lookup. The new lookup regression asserts zero library `scandir`/`os.walk` calls on repeat.
- Separate cProfile run of twelve full views: base lookup consumed 0.605 s of 0.687 s; the indexed run took 0.075 s total and was dominated by config loading/deep-copy rather than library traversal. Repeated GIL-releasing filesystem calls, not YAML parsing alone, caused the convoy.
- RED: the final repeat-lookup regression fails on the exact base at the zero-walk assertion. GREEN: all 21 index tests and all 56 existing `test_skills_tool.py` tests pass, including edited-frontmatter freshness.
- Broad canonical runner sweep: all test files referencing/importing `tools.skills_tool` or `agent.skill_utils` (including `from ... import ...` forms), prompt-surface tests and the changed mutation owners: 82 files, 2,217 passed, 3 failed, 6 pre-existing host-platform skips. The same three Kanban opt-in tests fail on the exact base because their card creation does not select a model. No skips, xfails or weakened assertions were introduced.
- Required lint gate: `scripts/check --base origin/main` reports **12 checks, ok**.

## Deliberate bounds

- Snapshots expire after five seconds and are generation-invalidated by management writes, rollbacks, curator relocation attempts and explicit cache reload. Hits validate only returned candidates; a miss or invalid candidate rebuilds and retries once.
- A duplicate created by another process can remain unseen until TTL expiry. Same-process writes invalidate immediately; existing same-tier collisions and all precedence/quarantine decisions remain uncached in the lookup owner.
- Cold builds are serialized by a global publication lock, preventing stampedes but allowing cross-root head-of-line blocking. Preserving explicitly named legacy symlink paths can enlarge a cold snapshot. These are not measured as warm-path speedups.

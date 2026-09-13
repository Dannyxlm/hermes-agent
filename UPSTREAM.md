# CloudSeed Hermes upstream contract

This repository is the **CloudSeed publication fork** of Hermes Agent. It is not
a second upstream and it is not a production checkout.

## One-way lineage

```text
NousResearch/hermes-agent:main
          ↓ reviewed upstream sync
Dannyxlm/hermes-agent:main
          ↓ immutable candidate build and approval
/opt/cloudseed-immutable/hermes/current
```

The official upstream remains `NousResearch/hermes-agent:main`. Danny's fork
contains only the reviewed CloudSeed overlay that is not yet suitable for, or
has not yet landed in, official upstream.

Production never runs from either mutable Git checkout. The live release is an
immutable build selected beneath `/opt/cloudseed-immutable/hermes/current`.

## Upstream sync

`.github/workflows/cloudseed-upstream-sync.yml` is the only repository sync
mechanism. It:

1. fetches official upstream into an isolated GitHub Actions checkout;
2. calculates the fork overlay from the exact merge base;
3. reapplies that overlay onto the pinned upstream head with Git's three-way
   machinery;
4. fails and reports conflict paths rather than choosing an unsafe blanket
   `ours` or `theirs` strategy;
5. runs focused Python and Desktop checks;
6. force-updates one automation branch and opens or refreshes one review PR.

The workflow never deploys, restarts Hermes, updates a production selector, or
runs `hermes update` on the cloud box. Its reviewed automation identities are
registered in the repository contributor map, so generated sync commits remain
attributable and pass the same checks as human-authored commits.

Upstream sync PRs must be merged with a **merge commit**, not squash-merged. A
merge commit preserves the upstream ancestry so the next `commits behind`
calculation stays meaningful.

## Update visibility

The CloudSeed source monitor owns update visibility. It reads a dedicated clean
clone, compares the running release's recorded upstream base with official
upstream, and publishes content-free status including:

- upstream head;
- commits behind;
- CloudSeed overlay commit count;
- source-integrity blockers;
- immutable candidate state.

Desktop and dashboard update controls consume that status. For managed
CloudSeed installations, the update action may request a candidate build, but
it must never mutate the running source tree or production release in place.

## Local overlay rule

Keep fork-only changes small, tested, and easy to reapply. Prefer contributing
general fixes upstream. CloudSeed-only policy, deployment, secrets, service
units, and path contracts belong in `Dannyxlm/cloudseed-infra`, not here.

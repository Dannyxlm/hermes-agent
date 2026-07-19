# Hermes Memory V3 runtime contract

This directory defines the local, provider-neutral configuration loaded by
`agent.memory_runtime.MemoryRuntimeController`. It is separate from the exact
vendored CloudSeed protocol bundle in `contracts/cloudseed-memory/v1`; adding
or changing files here does not change that frozen bundle digest.

Memory V3 activates only when `HERMES_MEMORY_V3_CONFIG_FILE` is set. Once set,
an incomplete or invalid configuration stays in local-only mode and never
falls back to legacy memory writes or global `MEMORY.md` / `USER.md` prompt
injection.

The runtime requires these explicit values:

- `HERMES_MEMORY_V3_CONFIG_FILE`
- `HERMES_MEMORY_V3_RUNTIME_MANIFEST_FILE`
- `HERMES_MEMORY_V3_RUNTIME_MANIFEST_DIGEST`
- `HERMES_MEMORY_V3_POLICY_FILE`
- `HERMES_MEMORY_V3_CAPABILITY_SNAPSHOT_FILE`
- `HERMES_MEMORY_V3_CAPABILITY_PUBLIC_KEY_FILE`
- `HERMES_MEMORY_V3_REPLAY_STATE_FILE`
- `HERMES_MEMORY_SUBJECT_BINDINGS_FILE`

The configuration file is owner-private. Runtime manifest, policy, public key,
and snapshot files must be owned by the service user or root and must not be
group/world writable. The replay-state parent is private and service-owned.
The snapshot is published by atomic replacement; Hermes never calls CloudSeed
or the provider while deciding whether a turn may read memory.

`runtime.config.digest` is the SHA-256 of the canonical JSON configuration
(sorted keys, UTF-8, no extra whitespace). The configuration includes the raw
SHA-256 of the exact owner-private subject-binding file, so identity mapping
and provider target/limits are part of the signed release state. The runtime
rechecks the binding file before every explicit provider read.

Provider target fields contain identifiers and endpoint coordinates only.
Credentials, tokens, private keys, and password-shaped fields are forbidden.
The separate memory proxy proof key must not equal `GATEWAY_PROXY_KEY`.

The first generation permits only explicit bounded reads. Group, webhook,
cron, restored, background, and delegated origins are hard-denied. Built-in
memory writes, ambient prefetch, turn sync, session extraction, compression
capture, provider creation, provider writes, and paid reasoning are denied.

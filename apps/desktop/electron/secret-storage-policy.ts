/**
 * secret-storage-policy.ts
 *
 * Single owner of the "do we use the OS keychain at all?" decision for
 * desktop-stored secrets (remote gateway tokens, CF Access headers, native
 * OAuth token sets).
 *
 * Why this exists: Electron safeStorage on macOS parks a per-app key
 * ("Hermes Key") in the login keychain. On machines with a locked, missing,
 * or corrupted default keychain, ANY safeStorage touch — including
 * isEncryptionAvailable() — makes macOS throw a blocking "Keychain Not
 * Found" / password dialog on every launch. That is an unacceptable default
 * for a chat app, so keychain-backed encryption is OPT-IN:
 *
 *   - Setting OFF (default): secrets are written with encoding 'plain' and
 *     NO safeStorage API is ever called. decryptDesktopSecret already
 *     returns non-safeStorage encodings verbatim, so reads need no change.
 *   - Setting ON: the previous behavior — strict safeStorage encryption,
 *     loud failure when the keychain is unavailable, per-save plain-text
 *     confirm dialog as the escape hatch.
 *
 * Legacy blobs written before the flag existed are safeStorage-encoded on
 * disk. On an upgraded install, their presence preserves encryption ON until
 * the user explicitly turns it off; a new default must never silently weaken
 * an existing credential store. Pre-release installs that already wrote an
 * explicit opt-out policy still use the one-shot migration below.
 *
 * Kept standalone (no `import 'electron'`) so it unit-tests under the
 * electron vitest project, same pattern as native-token-store.ts. main.ts
 * injects the file path and fs.
 */

export interface SecretStoragePolicy {
  /** Keychain-backed encryption enabled (explicit user opt-in). */
  on: boolean
  /** One-shot legacy-blob migration already attempted. */
  migrated: boolean
}

export const SECRET_STORAGE_POLICY_FILE = 'secure-token-storage.json'

export interface SecretStoragePolicyIo {
  readText: () => string
  writeText: (text: string) => void
}

/**
 * Normalize whatever is on disk into a policy. Anything unreadable,
 * unparseable, or hand-mangled is the default: encryption OFF, migration
 * not yet attempted. `on` uses strict `=== true` — a truthy-but-not-true
 * value must not silently enable keychain prompts (mirrors the
 * allowPlainText coercion rule in hardening.ts).
 */
export function readSecretStoragePolicy(io: SecretStoragePolicyIo): SecretStoragePolicy {
  try {
    const parsed = JSON.parse(io.readText())

    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return { on: parsed.on === true, migrated: parsed.migrated === true }
    }
  } catch {
    // fall through to default
  }

  return { on: false, migrated: false }
}

export function writeSecretStoragePolicy(policy: SecretStoragePolicy, io: SecretStoragePolicyIo): void {
  io.writeText(JSON.stringify({ on: policy.on === true, migrated: policy.migrated === true }))
}

/**
 * Resolve the first policy written by a build that introduced opt-in storage.
 * A genuinely new install remains opt-out. An upgraded install whose policy
 * file does not exist yet but whose stores already contain safeStorage blobs
 * must retain the legacy encrypted behavior until the user explicitly turns
 * it off; silently decrypting those credentials would be a security downgrade.
 */
export function initialSecretStoragePolicy(
  policyFileExists: boolean,
  hasLegacyEncryptedSecrets: boolean
): SecretStoragePolicy | null {
  if (!policyFileExists && hasLegacyEncryptedSecrets) {
    return { on: true, migrated: true }
  }

  return null
}

/** One stored secret blob as it appears on disk. */
interface StoredSecret {
  encoding?: string
  value?: string
}

/**
 * Decide what to do with one stored blob under the current policy.
 *
 *   - 'keep'    — blob is fine as-is under this policy.
 *   - 'migrate' — safeStorage blob while encryption is OFF and migration has
 *                 not run: caller should decrypt once and rewrite as plain.
 *   - 'drop'    — safeStorage blob while encryption is OFF and the migration
 *                 pass already ran (i.e. it could not be decrypted last
 *                 time): treat as absent WITHOUT touching safeStorage, so a
 *                 dead keychain never prompts again.
 */
export function classifyStoredSecret(
  secret: StoredSecret | null | undefined,
  policy: SecretStoragePolicy
): 'keep' | 'migrate' | 'drop' {
  if (!secret || typeof secret !== 'object' || secret.encoding !== 'safeStorage') {
    return 'keep'
  }

  if (policy.on) {
    return 'keep'
  }

  return policy.migrated ? 'drop' : 'migrate'
}

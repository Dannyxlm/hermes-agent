---
title: Passwords & Logins
description: The agent signs into sites, pays and fills addresses for you without ever seeing a password.
---

# Passwords & Logins

Say **"log into GitHub"** and the agent signs in for you. The first time it
reaches a sign-in page it has no login for, it asks you, right there, in a
masked prompt. After that it just works. Passwords are encrypted on this
machine and injected straight into the page; the model never sees them.

There is nothing to set up.

## What it looks like

**CLI / TUI**

```
🔐 Save login for github.com
   The agent reached a sign-in page with no saved login for this site.
   Type the email / username you sign in with (shown), then Enter.
   ...
   Now the password (hidden). It is encrypted on this machine, bound to
   https://github.com, and filled into the page without the model ever seeing it.
```

**Desktop** — a "Save your github.com login?" card with an identifier field and
a masked password field. *Save & sign in* stores it and continues; *Don't save*
tells the agent to stop asking for this turn.

From then on the agent lists your saved logins, types the identifier itself and
fills the password through Hermes. The tool result it sees is
`{filled_fields: 1, origin: "https://github.com"}`; the password is also
registered with the redactor so a later page read cannot echo it back.

## Two-factor codes

Sites that ask for a code after the password are handled the same way:

- **Authenticator key saved with the login** (the "setup key" or `otpauth://`
  link a site shows when you enable 2FA; 1Password and Bitwarden items that
  hold a TOTP seed count too): Hermes generates the current code and enters
  it. Nobody is asked. Add the key in **Settings → Passwords & Logins → Add**
  or `hermes vault add`; the item shows a *2FA auto* badge.
- **Code sent to your email, read by Hermes** (opt-in, `vault.email_codes`):
  when the login's identifier is an address whose inbox Hermes can already
  read, Hermes waits up to about a minute for the site's email, takes the
  code out of it and enters it. Nobody is asked, headless sessions included.
  Only mail from the site's own domain counts (a display name or subject
  saying "Acme" is not enough), only mail from the last ten minutes, and only
  when the newest such email holds exactly one code; anything ambiguous falls
  through to the prompt below. The inbox is read, never changed: Gmail via
  `messages.list/get` (nothing is marked read), IMAP via `EXAMINE` and
  `BODY.PEEK`. The email and the code never reach the model. When a site
  offers to send the code by email or text, the agent picks email.
- **Code sent to your phone or email**: a small prompt appears in your
  surface ("Verification code for github.com"), you type the code, Hermes
  enters it into the page. The code never enters the conversation either.
- **Passkeys, hardware keys, app approvals** ("tap Approve in Duo"): nothing
  to type. The agent tells you to complete it on your device and waits for
  the page to move on.

```yaml
vault:
  email_codes:
    enabled: true
    wait_seconds: 75            # how long to wait for the email (max 180)
    lookback_seconds: 600       # emails older than this are never used
    poll_interval_seconds: 5
    inboxes:
      # Gmail through any gws-compatible CLI (`<command> gmail users messages list|get`)
      - addresses: [me@example.com, me@example.org]   # identifiers that land in this inbox
        gmail_command: [/usr/local/bin/gws, --account, work]
      # Any IMAP inbox; the app password stays in 1Password and is read at connect time
      - addresses: [me@icloud.com]
        imap: {host: imap.mail.me.com, port: 993, username: me@icloud.com,
               password_ref: "op://<vault-id>/<item-id>/credential", folder: INBOX}
```

## Already using 1Password or Bitwarden?

Nothing to enable. If the `op` or `bw` command-line tool is installed and signed
in, Hermes picks it up automatically and its website logins become fillable
alongside the local ones. The first time the agent needs one of those logins it
asks you to unlock the manager with your master password (masked prompt; once
per session, 30 minutes idle). Hermes hands the master password to the manager's
CLI through its non-interactive channel (`op signin` on stdin, `bw unlock
--passwordenv` in the child's environment) and keeps only the session token in
memory. The agent never sees the master password, the token, or any login.
A manager item that lists several websites (say `amazon.co.uk`,
`www.amazon.co.uk` and `eu.account.amazon.com`) fills on each of those exact
origins; nothing is inferred beyond the URLs saved on the item.

Prefer not to use a detected manager? `hermes vault sources --disable bitwarden`,
or the switch in **Settings → Passwords & Logins**.

## Paying and filling addresses

Cards and addresses work the same way as logins: saved once (**Settings →
Passwords & Logins → Add**, or `hermes vault add`), bound to the checkout site,
and filled by the agent on that site only. **Every card fill asks you first**,
with the same approval prompt as a dangerous command; declining writes nothing.
Headless sessions (cron, webhooks, the API server) cannot confirm and are
refused, so a prompt injection that reaches a checkout page can ask, but it
cannot spend. Address fills need no confirmation.

The card approval names exactly what you are agreeing to: the **total** read from
the checkout page (labelled as page-sourced; "Total not found" when the page has
no clear total, and the prompt still lets you approve), the **exact site** from
the browser's address bar (page text never chooses it), the items when they add
up to that total, and the **card label plus its last four digits**. The full
number, expiry and CVC never appear. It is once only: no "allow for this chat"
or "always" scope is offered, and Hermes refuses one if a client sends it. A
timeout reports *expired*, not *declined*; either way nothing is filled. The
saved tool result keeps a non-secret receipt (site, total, items, card, decision
and time), so apps can redraw the order card after a reload.

To show a card limit on the approval, set it in config (or fill the card item's
own "credit limit" field in 1Password). Going over the limit warns but does not
block, because the issuer enforces the real limit:

```yaml
vault:
  payment_limits:
    "Expense Card": {amount: 50, currency: USD, period: per_purchase}  # or monthly
```

## Managing what's saved

- **Desktop → Settings → Passwords & Logins**: everything saved, the detected
  password managers with Unlock/Lock, Add, Remove.
- **CLI**: `hermes vault list`, `hermes vault add`, `hermes vault rm <handle>`,
  `hermes vault sources`.

Items live encrypted under `~/.hermes/vault/` (Fernet key + vault file, both
`0600`), scoped to the profile. Labels, site origins and login identifiers are
visible metadata; passwords and card values never leave the vault except into
the page.

## Headless sessions

Cron jobs, webhooks, the API server and `hermes chat -q` have nobody to answer a
prompt. Saved local logins keep working there; a locked password manager reports
`unavailable_in_this_session` and a missing login reports `prompt_unavailable`.
Unlock or save from an interactive session first, or give 1Password a service
account token (`OP_SERVICE_ACCOUNT_TOKEN`).

```yaml
vault:
  onepassword:
    enabled: false          # opt OUT of a detected manager (default: on when installed)
    account: ""             # `op --account` shorthand; empty = default
    service_account_token_env: OP_SERVICE_ACCOUNT_TOKEN
  bitwarden:
    enabled: false
```

## What this does and does not guarantee

**Does:** the password never enters the model's context through Hermes: not in
tool results, logs, the session database, or the CLI arguments of any process.
Fills happen over the supervised browser session's direct CDP socket and are
refused unless the page origin exactly matches the saved origin, checked again
inside the page immediately before the write.

**Does not:** protect against the page itself. Once a password is typed into a
site, that site (and any script it runs) has it, exactly as when you type it
yourself. On a cloud browser backend the vendor's browser sees the page like any
other. The origin binding is the guard against filling on the wrong site, not
against a compromised right one.

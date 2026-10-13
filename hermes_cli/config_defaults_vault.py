"""Pure-data vault defaults; runtime readers live in agent/vault_backends and vault_email_codes."""

DEFAULT_VAULT_CONFIG = {
    "onepassword": {
        # Detected managers are login sources unless the user opts out (vault.<name>.enabled: false).
        "enabled": True,        # `op` CLI: Login items with a website URL become fillable handles.
        "account": "",          # account shorthand for `op --account`; empty = default account.
        "binary_path": "",      # absolute path to op; empty = PATH.
        # Env var holding a service-account token (headless auth, no unlock prompt). Unset = prompt.
        "service_account_token_env": "OP_SERVICE_ACCOUNT_TOKEN",
        # Opt-in: list Credit Card items as payment handles. They carry no website, so each fill
        # binds to the current https page only after the user approves that exact site.
        "payment_cards": False,
    },
    "bitwarden": {
        "enabled": True,        # `bw` CLI (Password Manager, not Secrets Manager); run `bw login` once first.
        "binary_path": "",      # absolute path to bw; empty = PATH.
    },
    # Opt-in: browser_vault_enter_code reads a site's emailed sign-in code from an inbox Hermes can
    # already read, chosen by the saved login's identifier; otherwise the user is asked for the code.
    "email_codes": {
        "enabled": False,
        # Each inbox: {addresses: [...], gmail_command: [...]} or {addresses: [...], imap: {host,
        # port, username, password_ref: op://..., folder}}.
        "inboxes": [],
        # Explicit site domain -> sender domains; both include dot-boundary subdomains.
        "sender_aliases": {},
        "lookback_seconds": 600,     # mail received this long before the request still counts
        "wait_seconds": 75,          # how long to wait for the email (capped at 180)
        "poll_interval_seconds": 5,
    },
}

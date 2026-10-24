# redaction-pack

A Hermes plugin that teaches the built-in secret redactor 71 more vendor
credential formats: HashiCorp Vault, Doppler, Amazon Bedrock, Atlassian,
Cloudflare, Supabase, Tailscale, Docker Hub, JFrog Artifactory, CircleCI,
HubSpot, PostHog, LangSmith, Pinecone, Grafana, Sentry, Shopify and others. It registers regexes and nothing else.

## Why

Hermes masks credentials it recognises by their vendor prefix (`sk-`, `ghp_`,
`AIza`, `glpat-`, `AKIA`, ...) in logs, tool output and model-bound text. A
token from a vendor that is not on that list goes through unchanged: a deploy
script that prints its Doppler token, or a failing `curl` that echoes a Vault
token, lands in the transcript in clear text.

Hermes has a seam for exactly this: `ctx.register_redaction_patterns()` adds
prefix patterns to the same matcher the built-in rules run in. The seam is
additive-only. A plugin can widen what gets masked, never weaken a built-in
rule. This pack uses it for formats Hermes does not ship.

## Install

```bash
hermes plugins install redaction-pack
hermes plugins enable redaction-pack
```

Restart Hermes (CLI, gateway or desktop backend) afterwards, since patterns are
registered when plugins load. Nothing to configure.

## What changes

With the pack enabled, a token in one of the formats below is masked like a
built-in one. Normal output keeps the first 6 and last 4 characters so you can
still tell which credential it was:

```
deploy failed: 401 Unauthorized for PMAK-6...e3b9 (retrying)
```

File content returned to the agent gets Hermes's non-reusable sentinel
(`«redacted:...»`) instead, so the agent cannot copy a truncated value back into
a config file. Setting `security.redact_secrets: false` turns redaction off for
these formats as well as the built-in ones.

## Formats

| Vendor | Token | Pattern | Source |
|---|---|---|---|
| 1Password | account Secret Key | `A3-[A-Z0-9]{6}-(?:[A-Z0-9]{11}\|[A-Z0-9]{6}-[A-Z0-9]{5})-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}` | gitleaks |
| HashiCorp Vault | service token | `hvs\.[A-Za-z0-9_-]{90,120}` | gitleaks |
| HashiCorp Vault | batch token | `hvb\.[A-Za-z0-9_-]{138,300}` | gitleaks |
| Doppler | CLI, personal, service account, SCIM and audit tokens | `dp\.(?:ct\|pt\|sa\|said\|scim\|audit)\.[A-Za-z0-9]{40,44}` | vendor docs |
| Doppler | service token | `dp\.st\.(?:[a-z0-9_-]{2,35}\.)?[A-Za-z0-9]{40,44}` | vendor docs |
| Amazon Bedrock | long-term API key | `ABSK[A-Za-z0-9+/]{109,269}={0,2}` | gitleaks |
| Amazon Bedrock | short-term API key | `bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29t[A-Za-z0-9+/]{20,}={0,2}` | gitleaks |
| Alibaba Cloud | AccessKey ID | `LTAI[A-Za-z0-9]{20}` | gitleaks |
| DigitalOcean | OAuth refresh token | `dor_v1_[a-f0-9]{64}` | gitleaks |
| Fly.io | access token | `fo1_[A-Za-z0-9_-]{43}` | gitleaks |
| Fly.io | macaroon token | `fm1[ar]_[A-Za-z0-9+/]{100,}={0,3}` | gitleaks |
| Fly.io | macaroon token (v2) | `fm2_[A-Za-z0-9+/]{100,}={0,3}` | gitleaks |
| Heroku | API key | `HRKU-AA[A-Za-z0-9_-]{58}` | gitleaks |
| OpenShift | user token | `sha256~[A-Za-z0-9_-]{43}` | gitleaks |
| Scalingo | API token | `tk-us-[A-Za-z0-9_-]{48}` | gitleaks |
| Cloudflare | global API key (cfk_ format) | `cfk_[A-Za-z0-9]{40}[a-f0-9]{8}` | trufflehog |
| Pulumi | access token | `pul-[a-f0-9]{40}` | gitleaks |
| Infracost | API key | `ico-[A-Za-z0-9]{32}` | gitleaks |
| Databricks | personal access token | `dapi[a-f0-9]{32}(?:-[0-9])?` | gitleaks |
| PlanetScale | service token, OAuth token and password | `pscale_(?:tkn\|oauth\|pw)_[A-Za-z0-9_.=-]{32,64}` | gitleaks |
| Supabase | personal and OAuth access token | `sbp_(?:oauth_\|v0_)?[a-f0-9]{40}` | vendor source |
| Tailscale | API, auth, OAuth client, SCIM and webhook keys | `tskey-(?:api\|auth\|client\|scim\|webhook)-[A-Za-z0-9_]+-[A-Za-z0-9_]{16,}` | vendor docs |
| Hugging Face | organization API token | `api_org_[A-Za-z]{34}` | gitleaks |
| LangSmith | personal access token and service key | `lsv2_(?:pt\|sk)_[a-f0-9]{32}_[a-f0-9]{10}` | trufflehog |
| Pinecone | API key | `pcsk_[A-Za-z0-9]{5,6}_[A-Za-z0-9]{63}` | trufflehog |
| Weights & Biases | API key (v1 format) | `wandb_v1_[A-Za-z0-9]{27}_[A-Za-z0-9]{49}` | trufflehog |
| Grafana | Cloud API token | `glc_[A-Za-z0-9+/]{32,400}={0,3}` | gitleaks |
| Grafana | service account token | `glsa_[A-Za-z0-9]{32}_[A-Fa-f0-9]{8}` | gitleaks |
| Sentry | user auth token | `sntryu_[a-f0-9]{64}` | gitleaks |
| Sentry | organization auth token | `sntrys_eyJ[A-Za-z0-9+/]{30,400}={0,2}_[A-Za-z0-9+/]{43}` | gitleaks |
| Dynatrace | API token | `dt0c01\.[A-Za-z0-9]{24}\.[A-Za-z0-9]{64}` | gitleaks |
| New Relic | user API key | `NRAK-[A-Za-z0-9]{27}` | gitleaks |
| New Relic | insert key | `NRII-[A-Za-z0-9_-]{32}` | gitleaks |
| New Relic | Insights query key | `NRIQ-[A-Za-z0-9_-]{25}` | trufflehog |
| PostHog | personal API key | `phx_[A-Za-z0-9_]{43,48}` | trufflehog |
| Rootly | API key | `rootly_[a-f0-9]{64}` | trufflehog |
| Postman | API key | `PMAK-[a-fA-F0-9]{24}-[a-fA-F0-9]{34}` | gitleaks |
| Sourcegraph | access token | `sgp_(?:[a-fA-F0-9]{16}_\|local_)?[a-fA-F0-9]{40}` | gitleaks |
| Prefect | API key | `pnu_[A-Za-z0-9]{36}` | gitleaks |
| ReadMe | API key | `rdme_[a-z0-9]{70}` | gitleaks |
| RubyGems | API key | `rubygems_[a-f0-9]{48}` | gitleaks |
| Clojars | deploy token | `CLOJARS_[A-Za-z0-9]{60}` | gitleaks |
| JFrog Artifactory | API key | `AKCp[A-Za-z0-9]{69}` | gitleaks |
| JFrog Artifactory | reference token | `cmVmdGtu[A-Za-z0-9]{56}` | trufflehog |
| Typeform | personal access token | `tfp_[A-Za-z0-9_.=-]{59}` | gitleaks |
| Frame.io | developer token | `fio-u-[A-Za-z0-9_=-]{64}` | gitleaks |
| Docker Hub | personal access token | `dckr_pat_[A-Za-z0-9_-]{27}` | trufflehog |
| Docker Hub | organization access token | `dckr_oat_[A-Za-z0-9_-]{32}` | trufflehog |
| Figma | personal access token | `figd_[A-Za-z0-9_-]{40}` | trufflehog |
| Figma | personal access token (figp_ format) | `figp_[A-Za-z0-9_=-]{40,54}` | trufflehog |
| Slack | app configuration refresh token | `xoxe-[0-9]-[A-Za-z0-9]{146}` | gitleaks |
| Atlassian | API token (Jira, Confluence) | `ATATT[A-Za-z0-9+/=_-]+=[A-Za-z0-9]{8}` | trufflehog |
| Atlassian | organization admin API key | `ATCTT3xFfG[A-Za-z0-9+/=_-]+=[A-Za-z0-9]{8}` | trufflehog |
| Bitbucket Data Center | HTTP access token | `BBDC-[A-Za-z0-9+/@_-]{40,50}` | trufflehog |
| CircleCI | personal API token | `CCIPAT_[A-Za-z0-9]{22}_[a-fA-F0-9]{40}` | trufflehog |
| Buildkite | user API access token | `bkua_[a-z0-9]{40}` | trufflehog |
| SonarCloud | token | `sqco_[A-Za-z0-9]{59}` | trufflehog |
| Sourcegraph | Cody gateway access token | `slk_[a-f0-9]{64}` | trufflehog |
| Contentful | personal access token | `CFPAT-[A-Za-z0-9_-]{43}` | trufflehog |
| Apify | API token | `apify_api_[A-Za-z0-9]{36}` | trufflehog |
| Adobe | client secret | `p8e-[A-Za-z0-9]{32}` | gitleaks |
| Shopify | access tokens and shared secret | `shp(?:at\|ca\|pa\|ss)_[a-fA-F0-9]{32}` | gitleaks |
| Square | access token | `sq0atp-[A-Za-z0-9_-]{22,60}` | gitleaks |
| Shippo | API token | `shippo_(?:live\|test)_[a-fA-F0-9]{40}` | gitleaks |
| Duffel | API token | `duffel_(?:test\|live)_[A-Za-z0-9_=-]{43}` | gitleaks |
| EasyPost | production and test API keys | `EZ[AT]K[A-Za-z0-9]{54}` | gitleaks |
| Brevo | API key | `xkeysib-[a-f0-9]{64}-[A-Za-z0-9]{16}` | gitleaks |
| HubSpot | private app access token | `pat-(?:na\|eu)1-[A-Za-z0-9]{8}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{12}` | trufflehog |
| Flutterwave | live secret key | `FLWSECK-[0-9a-z]{32}-X` | trufflehog |
| Flutterwave | test secret key | `FLWSECK_TEST-[A-Ha-h0-9]{32}-X` | gitleaks |
| Ramp | API client secret | `ramp_sec_[A-Za-z0-9]{48}` | trufflehog |

"gitleaks" means the format follows the rule of that name in
[gitleaks](https://github.com/gitleaks/gitleaks) (MIT); `patterns.py` gives the
rule id for each entry. Doppler's formats are the ones Doppler publishes in
[Auth Token Formats](https://docs.doppler.com/reference/auth-token-formats).
Supabase's is the `AccessTokenPattern` the
[Supabase CLI](https://github.com/supabase/cli) validates tokens against, and
Tailscale's prefixes are the ones listed in
[Key prefixes](https://tailscale.com/kb/1277/key-prefixes).
"trufflehog" means the format follows that
[TruffleHog](https://github.com/trufflesecurity/trufflehog) detector
(`pkg/detectors/<name>`). Patterns are rewritten for Hermes's matcher: explicit
character classes instead of `(?i)`, no capturing groups, no top-level `|`.

### Left out on purpose

- **Formats Hermes already masks.** The test suite checks every format above
  against plain Hermes. Slack's `xoxe.xoxb-` / `xoxe.xoxp-` configuration
  tokens are not listed because the built-in `xox[baprs]-` rule already masks
  their body.
- **Formats with an open Hermes pull request**, for example age keys (#95440),
  NVIDIA API keys (#28332), Google OAuth tokens (#55467, #117441), 1Password
  service-account tokens (#67438) and chat webhook URLs (#118018). Those belong
  in core.
- **Formats whose shape no source pins down.** Supabase's newer `sb_secret_`
  API keys have a documented prefix but no published length or alphabet, so a
  pattern would be a guess.
- **Formats without a distinctive literal prefix** (Terraform Cloud, Mailgun
  `key-`, Airtable `pat`, Resend `re_`). Hermes needs a literal prefix to gate each pattern
  cheaply, and a short common one would run the full matcher on most lines.
  ClickHouse Cloud's `4b1d` prefix is left out for the same reason: it reads as hex
  and would gate on hashes. Salesforce refresh tokens are out because the only
  source matches their `5AEP861` prefix case-insensitively and none pins down the one
  spelling a literal prefix gate needs.

## Security and footprint

- `register()` makes a single call, `ctx.register_redaction_patterns(...)`. No
  tools, hooks, middleware, commands or slash commands.
- No network access, no subprocesses, no files read or written, no environment
  variables or credentials read.
- The patterns join the matcher Hermes already runs over logs and tool output,
  behind its literal-prefix substring gate. Every pattern has a literal vendor
  prefix and no nested repeat (Hermes refuses ReDoS-shaped patterns at
  registration).
- The pack can only mask more text. A false positive shows up as a masked
  string, never as a leaked one.

## Compatibility

Hermes 0.21.0 (`v2026.8.31`) or newer. CI runs the full test suite against
that release and against Hermes `main` every day.

## Development

```bash
git clone https://github.com/NousResearch/hermes-agent ../hermes-agent
# Hermes-free checks (pattern table, samples, README sync):
python -m pytest tests/test_formats.py
# Against a Hermes checkout:
PYTHONPATH=../hermes-agent python -m pytest tests
```

The tests build every sample token at run time from a prefix and a generated
body, so the repository holds no credential-shaped literals. For each format
they check that:

- plain Hermes leaks the token;
- the pack masks it;
- Hermes accepts every pattern;
- built-in rules still work;
- Hermes's own source tree (thousands of `.py` and `.md` files) gets no new
  masks;
- a copy installed under a fresh `HERMES_HOME`, enabled with the CLI, loads and
  masks in a separate process.

To add a format: add a `TokenFormat` to `redaction-pack/patterns.py` with its
source, add a sample in `tests/conftest.py` and a row to this table (the tests
fail until all three agree).

## License

MIT

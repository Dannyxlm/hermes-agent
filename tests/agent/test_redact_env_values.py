"""Credential env names with a trailing qualifier (``VENDOR_API_KEY_ALPHA``) and exact
env-value masking. Every value here is synthetic."""

import json

import pytest

import agent.redact as redact
from agent.redact import redact_sensitive_text, redact_terminal_output

# Opaque, prefix-less: no vendor pattern can catch these, only a name or exact-value rule.
OPAQUE = "Zq8xLm2Pq7Rt5Wv9Yb3Nc4Df6Gh"
OPAQUE_2 = "Hk3Jd9Fs2La7Qp5Wm1Xn8Bv4"


@pytest.fixture(autouse=True)
def _redaction_on(monkeypatch):
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)
    redact._reset_env_secret_value_cache()
    yield
    redact._reset_env_secret_value_cache()


class TestSuffixedCredentialNames:
    @pytest.mark.parametrize("key", [
        "VENDOR_API_KEY_ALPHA", "API_KEY_ALPHA", "SERVICE_TOKEN_PROD", "DB_PASSWORD_MAIN",
        "APP_SECRET_V2", "GITHUB_PAT_ALPHA", "PRIVATE_KEY_PROD", "ACCESS_KEY_ALPHA",
        "SECRET_STORE_TOKEN_PROD",  # metadata after the first word, a qualifier after the second
    ])
    def test_python_repr_field_is_masked(self, key):
        text = f"env={{'{key}': '{OPAQUE}', 'SHLVL': '1'}}"
        result = redact_sensitive_text(text, force=True)
        assert OPAQUE not in result
        assert f"'{key}': '***'" in result
        assert "'SHLVL': '1'" in result

    @pytest.mark.parametrize("command", [
        "cd /x && (cd /y && env -u XDG_RUNTIME_DIR python w.py; echo rc=$?)",
        "cd /x && (env -u XDG_RUNTIME_DIR python w.py; echo rc=$?)",
        "( env -u XDG_RUNTIME_DIR python w.py )",
        "cd /x && (env)",
    ])
    def test_execve_env_dict_through_env_command_is_masked(self, command):
        """The reported shape: a wrapper printed its execve env dict; ``env -u`` made the
        command an env dump, so only the repr pass stood between the value and the model
        (the value is not in this process's environment)."""
        from agent.redact import is_env_dump_command
        assert is_env_dump_command(command)
        output = f"/usr/bin/python ['/usr/bin/python', '-I'] {{'VENDOR_API_KEY_ALPHA': '{OPAQUE}'}}"
        assert OPAQUE not in redact_terminal_output(output, command)

    @pytest.mark.parametrize("key", [
        "VENDOR_API_KEY_ALPHA", "SERVICE_TOKEN_PROD", "DB_PASSWORD_MAIN", "ACCESS_KEY_ALPHA",
        "GITHUB_PAT_ALPHA",
    ])
    def test_json_field_is_masked(self, key):
        result = redact_sensitive_text(json.dumps({key: OPAQUE, "model": "gpt"}), force=True)
        assert OPAQUE not in result
        assert json.loads(result)["model"] == "gpt"

    def test_short_password_under_suffixed_name_is_masked(self):
        result = redact_sensitive_text("{'DB_PASSWORD_MAIN': 'hunter2'}", force=True)
        assert "hunter2" not in result

    @pytest.mark.parametrize("key", [
        "GITHUB_TOKEN_PATH", "OPENAI_API_KEY_FILE", "ACME_TOKEN_URL", "DB_PASSWORD_POLICY",
        "APP_SECRET_NAME", "CLIENT_SECRET_EXPIRES_AT", "SESSION_TOKEN_COUNT", "SSH_AUTH_SOCK",
        "TOKEN_ADDRESS", "TOKEN_MODEL", "PASSWORD_RESET", "SECRET_MANAGER_PROJECT", "TOKEN_ISSUER",
        # snake/camel data fields stay on the end-of-name rule
        "user_password_hash", "api_token_id", "token_address", "tokenAddress", "token_contract",
        "password_reset", "secret_manager_project", "api_key_alpha",
    ])
    def test_metadata_about_a_credential_is_unchanged(self, key):
        text = f"{{'{key}': 'public-metadata-value-123'}}"
        assert redact_sensitive_text(text, force=True) == text
        text = json.dumps({key: "public-metadata-value-123"})
        assert redact_sensitive_text(text, force=True) == text

    @pytest.mark.parametrize("value", ["vault", "4096", "cl100k_base"])
    def test_plain_value_under_suffixed_token_or_secret_is_unchanged(self, value):
        text = f"{{'APP_SECRET_V2': '{value}', 'MAX_TOKEN_OUTPUT': '{value}'}}"
        assert redact_sensitive_text(text, force=True) == text


class TestEnvSecretValues:
    def _set(self, monkeypatch, **env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        redact._reset_env_secret_value_cache()

    @pytest.mark.parametrize("name", [
        "ACME_API_KEY_ALPHA", "ACME_TOKEN", "ACME_PROJECT_SECRET", "ACME_PG_PASS",
        "VENDOR_API_TEAM_KEY", "ACME_PRIVATE_KEY", "ACME_AUTH_TAG", "PGPASSWORD", "MYSQL_PWD",
        "SSHPASS", "ACME_DB_PW",
    ])
    def test_env_value_is_masked_in_any_shape(self, monkeypatch, name):
        self._set(monkeypatch, **{name: OPAQUE})
        for text in (f"the value is {OPAQUE} today", f"x={OPAQUE}", f"{{'v': '{OPAQUE}'}}"):
            assert OPAQUE not in redact_sensitive_text(text)
            assert OPAQUE not in redact_sensitive_text(text, code_file=True)
        # Source-preserving terminal output (not an env dump) is covered too.
        assert OPAQUE not in redact_terminal_output(f"print -> {OPAQUE}", "python3 w.py")

    def test_file_read_gets_non_reusable_sentinel(self, monkeypatch):
        self._set(monkeypatch, ACME_API_KEY_ALPHA=OPAQUE)
        result = redact_sensitive_text(f"value: {OPAQUE}", file_read=True)
        assert OPAQUE not in result and "«redacted-secret»" in result

    def test_opt_out_is_honoured_and_force_is_not(self, monkeypatch):
        self._set(monkeypatch, ACME_API_KEY_ALPHA=OPAQUE)
        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
        assert OPAQUE in redact_sensitive_text(f"v {OPAQUE}")
        assert OPAQUE not in redact_sensitive_text(f"v {OPAQUE}", force=True)

    def test_longest_value_wins_when_one_contains_another(self, monkeypatch):
        self._set(monkeypatch, ACME_TOKEN=OPAQUE, ACME_TOKEN_LONG=OPAQUE + OPAQUE_2)
        result = redact_sensitive_text(f"a {OPAQUE + OPAQUE_2} b {OPAQUE}")
        assert OPAQUE not in result and OPAQUE_2 not in result

    @pytest.mark.parametrize("name,value", [
        ("HERMES_SESSION_KEY", "agent:main:telegram:dm:123456789"),  # unqualified key
        ("SSH_AUTH_SOCK", "/run/user/1000/gnupg/S.gpg-agent.ssh"),   # path
        ("SSH_AUTH_SOCK", "/tmp/ssh-XXXXa1B2c3D4e5/agent.12345"),     # OpenSSH socket: opaque segment
        ("ACME_AUTH_DOMAIN", "tenant42.auth.example.com"),           # metadata after the word
        ("ACME_TOKEN", "123456789012345678901234"),                   # all digits: ids, JSON numbers
        ("PW_TEST_HTML_REPORT_OPEN", "Zq8xLm2Pq7Rt5Wv9"),            # PW not last: Playwright
        ("ACME_TOKEN_URL", "https://auth2.example.com/oauth/token"),  # metadata name
        ("HERMES_REDACT_SECRETS", "true"),                          # flag
        ("ACME_TOKEN", "short1A"),                                    # too short to be distinctive
        ("ACME_WIDGET", OPAQUE),                                      # no credential word
    ])
    def test_non_secret_env_values_are_not_registered(self, monkeypatch, name, value):
        self._set(monkeypatch, **{name: value})
        text = f"see {value} here"
        assert redact_sensitive_text(text, code_file=True) == text

    def test_profile_secret_scope_values_are_masked_only_while_bound(self, monkeypatch):
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        self._set(monkeypatch, ACME_API_KEY_ALPHA=OPAQUE)
        third = "Pw7Rk2Lm9Qs4Tx1Vb8Nc3Zd"
        for scope_values in ({"ACME_API_KEY_BETA": OPAQUE_2}, {"ACME_TOKEN_X": third}, {"ACME_API_KEY_BETA": OPAQUE_2}):
            token = set_secret_scope(scope_values)
            try:
                text = f"v {OPAQUE} {OPAQUE_2} {third}"
                result = redact_sensitive_text(text, code_file=True)
                assert OPAQUE not in result  # process values always apply
                for value in (OPAQUE_2, third):
                    assert (value in result) == (value not in scope_values.values())
            finally:
                reset_secret_scope(token)
        assert OPAQUE_2 in redact_sensitive_text(f"v {OPAQUE_2}", code_file=True)

    def test_values_are_re_read_after_the_refresh_window(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(redact.time, "monotonic", lambda: clock[0])
        assert OPAQUE in redact_sensitive_text(f"v {OPAQUE}", code_file=True)
        monkeypatch.setenv("ACME_API_KEY_ALPHA", OPAQUE)
        assert OPAQUE in redact_sensitive_text(f"v {OPAQUE}", code_file=True)  # cached until refresh
        clock[0] += redact._ENV_SECRET_REFRESH_SECONDS + 1
        assert OPAQUE not in redact_sensitive_text(f"v {OPAQUE}", code_file=True)

    def test_unreadable_environment_keeps_previous_values(self, monkeypatch):
        self._set(monkeypatch, ACME_API_KEY_ALPHA=OPAQUE)
        assert OPAQUE not in redact_sensitive_text(f"v {OPAQUE}")
        clock = [time_now := redact.time.monotonic()]
        monkeypatch.setattr(redact.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(redact, "_collect_env_secret_values", lambda env: None)
        clock[0] = time_now + redact._ENV_SECRET_REFRESH_SECONDS + 1
        assert OPAQUE not in redact_sensitive_text(f"v {OPAQUE}")

    def test_delegation_live_log_masks_env_value(self, monkeypatch, tmp_path):
        from tools.delegation_live_log import LiveTranscriptWriter
        self._set(monkeypatch, ACME_API_KEY_ALPHA=OPAQUE)
        writer = LiveTranscriptWriter("deleg-test", 0, "goal", root=tmp_path)
        writer.tool_result("terminal", result=f"{{'output': \"{{'X': '{OPAQUE}'}}\"}}")
        log = writer.path.read_text(encoding="utf-8")
        assert "terminal ok" in log
        assert OPAQUE not in log

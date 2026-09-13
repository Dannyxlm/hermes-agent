"""Fixed-topic Apple HTTP/2 sender. No tokens, keys or response bodies enter logs."""

import time
from dataclasses import dataclass
from pathlib import Path

from .mobile_push_payloads import TOPIC


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str  # accepted, retry, invalid, failed
    reason: str = ""


class APNsProvider:
    def __init__(self, config, *, client=None, clock=time.time):
        import httpx
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.clock = clock
        self.team_id = config["team_id"]
        self.key_id = config["key_id"]
        for identifier in (self.team_id, self.key_id):
            if not isinstance(identifier, str) or len(identifier) != 10 or not identifier.isalnum():
                raise ValueError("invalid APNs issuer identifier")
        key_path = Path(config["private_key_path"])
        if not key_path.is_absolute() or key_path.stat().st_mode & 0o077:
            raise ValueError("APNs private key must be protected")
        self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(self.key, ec.EllipticCurvePrivateKey) or not isinstance(self.key.curve, ec.SECP256R1):
            raise ValueError("APNs requires an ES256 key")
        self._jwt = jwt
        self._authorization = None
        self._issued_at = 0
        self._client = client or httpx.Client(http2=True, timeout=10, follow_redirects=False)

    def send(self, job):
        import httpx

        now = self.clock()
        if self._authorization is None or now - self._issued_at > 2400:
            self._authorization = self._jwt.encode({"iss": self.team_id, "iat": int(now)}, self.key,
                algorithm="ES256", headers={"kid": self.key_id})
            self._issued_at = now
        push_type = {"activity": "liveactivity", "alert": "alert", "widget": "widgets"}.get(job["kind"])
        if push_type is None:
            return DeliveryResult("failed", "invalid_channel")
        topic = TOPIC + (".push-type." + push_type if push_type != "alert" else "")
        host = "api.push.apple.com" if job["environment"] == "production" else "api.sandbox.push.apple.com"
        headers = {"authorization": "bearer " + self._authorization, "apns-topic": topic,
            "apns-push-type": push_type,
            "apns-priority": "10" if job["urgent"] else "5", "apns-id": job["job_id"],
            "apns-expiration": str(int(job["expires_at"])), "apns-collapse-id": job["collapse_id"]}
        try:
            response = self._client.post(f"https://{host}/3/device/{job['token']}",
                                         headers=headers, json=job["payload"])
        except httpx.TransportError:
            return DeliveryResult("retry", "transport")
        if response.status_code == 200:
            return DeliveryResult("accepted")
        try:
            reason = response.json().get("reason", "")
        except (ValueError, AttributeError):
            reason = ""
        if response.status_code == 410 or reason in {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}:
            return DeliveryResult("invalid", "token_invalid")
        if reason == "ExpiredProviderToken":
            self._authorization = None
            return DeliveryResult("retry", "provider_expired")
        if response.status_code == 429 or response.status_code >= 500:
            return DeliveryResult("retry", "provider_unavailable")
        return DeliveryResult("failed", "provider_rejected")

    def close(self):
        self._client.close()

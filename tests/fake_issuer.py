"""A local OIDC issuer for tests: RSA and EC keys made in the test, a JWKS served through an
injected fetcher (no network), and tokens minted with whatever claims a test needs."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.api.oidc import OidcSettings, OidcVerifier

ISSUER = "https://login.test/realms/agb-check"
AUDIENCE = "agb-check-api"
JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int(value: int) -> str:
    return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class FakeIssuer:
    """Holds signing keys, answers discovery and JWKS fetches, and mints tokens."""

    def __init__(self, issuer: str = ISSUER) -> None:
        self.issuer = issuer
        self.rsa: dict[str, rsa.RSAPrivateKey] = {}
        self.ec: dict[str, ec.EllipticCurvePrivateKey] = {}
        self.published: list[str] = []  # kids in the JWKS, in order
        self.fetches: list[str] = []
        self.down = False
        self.discovery_issuer = issuer
        self.add_rsa("rsa-1")

    # --- keys -----------------------------------------------------------------------

    def add_rsa(self, kid: str, *, publish: bool = True) -> None:
        self.rsa[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if publish:
            self.published.append(kid)

    def add_ec(self, kid: str, *, publish: bool = True) -> None:
        self.ec[kid] = ec.generate_private_key(ec.SECP256R1())
        if publish:
            self.published.append(kid)

    def publish(self, kid: str) -> None:
        self.published.append(kid)

    def retire(self, kid: str) -> None:
        self.published.remove(kid)

    def jwk(self, kid: str) -> dict[str, Any]:
        if kid in self.rsa:
            numbers = self.rsa[kid].public_key().public_numbers()
            return {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _int(numbers.n),
                "e": _int(numbers.e),
            }
        numbers = self.ec[kid].public_key().public_numbers()
        return {
            "kty": "EC",
            "kid": kid,
            "use": "sig",
            "crv": "P-256",
            "x": b64(numbers.x.to_bytes(32, "big")),
            "y": b64(numbers.y.to_bytes(32, "big")),
        }

    # --- the provider's endpoints ---------------------------------------------------

    def fetch(self, url: str) -> Mapping[str, Any]:
        from app.api.oidc import ProviderUnavailable

        self.fetches.append(url)
        if self.down:
            raise ProviderUnavailable("issuer down")
        if url == f"{self.issuer}/.well-known/openid-configuration":
            return {"issuer": self.discovery_issuer, "jwks_uri": JWKS_URI}
        if url == JWKS_URI:
            return {"keys": [self.jwk(kid) for kid in self.published]}
        raise ProviderUnavailable(f"404 {url}")

    def jwks_fetches(self) -> int:
        return self.fetches.count(JWKS_URI)

    # --- tokens -----------------------------------------------------------------------

    def claims(self, subject: str = "user-a", **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "sub": subject,
            "exp": now + 300,
            "iat": now,
            "nbf": now,
            "name": f"Anwältin {subject}",
            "preferred_username": subject,
            "realm_access": {"roles": ["agb-user"]},
        }
        claims.update(overrides)
        return {key: value for key, value in claims.items() if value is not None}

    def token(
        self,
        subject: str = "user-a",
        *,
        kid: str = "rsa-1",
        alg: str | None = None,
        header: dict[str, Any] | None = None,
        **claims: Any,
    ) -> str:
        alg = alg or ("ES256" if kid in self.ec else "RS256")
        head = {"alg": alg, "typ": "JWT", "kid": kid, **(header or {})}
        head = {key: value for key, value in head.items() if value is not None}
        signing_input = (
            b64(json.dumps(head).encode())
            + "."
            + b64(json.dumps(self.claims(subject, **claims)).encode())
        ).encode()
        return signing_input.decode() + "." + b64(self.sign(signing_input, kid, alg))

    def sign(self, signing_input: bytes, kid: str, alg: str) -> bytes:
        if alg == "none":
            return b""
        if alg.startswith("HS"):
            # The classic forgery: HMAC keyed with the issuer's *public* key.
            secret = json.dumps(self.jwk(kid), sort_keys=True).encode()
            mac = hmac.HMAC(secret, hashes.SHA256())
            mac.update(signing_input)
            return mac.finalize()
        if alg == "ES256":
            der = self.ec[kid].sign(signing_input, ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        if alg == "PS256":
            return self.rsa[kid].sign(
                signing_input,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
        return self.rsa[kid].sign(signing_input, padding.PKCS1v15(), hashes.SHA256())

    # --- the API side ---------------------------------------------------------------

    def settings(self, **overrides: Any) -> OidcSettings:
        values: dict[str, Any] = {
            "issuer": self.issuer,
            "audience": AUDIENCE,
            "admin_role": "agb-admin",
        }
        values.update(overrides)
        return OidcSettings(**values)

    def verifier(self, **kwargs: Any) -> OidcVerifier:
        settings = kwargs.pop("settings", None) or self.settings()
        return OidcVerifier(settings, fetch=self.fetch, **kwargs)


def auth_config(**extra: Any) -> dict[str, Any]:
    """App settings with login on, matching `FakeIssuer`."""
    return {
        "auth": {
            "enabled": True,
            "oidc": {"issuer": ISSUER, "audience": AUDIENCE, "admin_role": "agb-admin"},
        },
        **extra,
    }


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

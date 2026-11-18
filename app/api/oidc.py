"""Validating OIDC access tokens against the issuer's published keys (Phase 9, 9.E).

The API never talks to the identity provider per request. It reads the provider's
discovery document once (`{issuer}/.well-known/openid-configuration`), caches the key set
(JWKS) it points to, and checks each bearer token locally: the signature against a key
from that set, then `iss`, `aud`, `exp` and `nbf`. Only standard claims are read, so
Keycloak in development and any production provider (decision #2) differ in configuration
only: `auth.oidc.issuer`, `audience`, `name_claim` and `roles_claim`.

Key rotation: a provider publishes the next key before it signs with it, and keeps the old
one until the tokens it signed have expired. A token whose `kid` is not in the cached set
therefore triggers one refetch, rate-limited so a stream of garbage tokens cannot make the
API hammer the provider; the cache also expires after `jwks_ttl_seconds`.

The signature itself is checked by a `JwsBackend`. `PyJwtBackend` (the `pyjwt[crypto]`
library) is used whenever it is installed. Until it is a dependency, `CryptographyBackend`
does the same with the `cryptography` primitives the project already has; it only verifies
a signature, every claim check is the same code for both (`validate_claims`). Neither
accepts `none` or an HMAC algorithm: an HMAC "signature" keyed with the public key is the
classic way to forge a token for a library that trusts the token's own `alg` header.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.logging import get_logger

from app.domain import Principal

log = get_logger("api.oidc")

# Asymmetric algorithms only. `none` and HS* are never accepted, whatever the config says.
DEFAULT_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384")
_FORBIDDEN = frozenset({"none", "HS256", "HS384", "HS512"})
_KTY_BY_FAMILY = {"RS": "RSA", "PS": "RSA", "ES": "EC"}

Fetch = Callable[[str], Mapping[str, Any]]


class InvalidToken(Exception):
    """The bearer token is missing, malformed, badly signed or not for this API.

    The message is for the server log only; clients get one generic 401.
    """


class ProviderUnavailable(RuntimeError):
    """The issuer's discovery document or key set could not be read."""


@dataclass(frozen=True)
class OidcSettings:
    """`auth.oidc` from configs/app.yaml."""

    issuer: str
    audience: str
    client_id: str | None = None
    name_claim: str = "name"
    roles_claim: str = "realm_access.roles"
    admin_role: str | None = None
    algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS
    leeway_seconds: int = 30
    jwks_ttl_seconds: int = 600
    # Some providers' discovery documents point at another host (a container name in
    # Compose); `jwks_uri` overrides discovery when set.
    jwks_uri: str | None = None

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise ValueError("auth.oidc.issuer and auth.oidc.audience are required")
        bad = [alg for alg in self.algorithms if alg in _FORBIDDEN or alg[:2] not in _KTY_BY_FAMILY]
        if bad or not self.algorithms:
            raise ValueError(f"auth.oidc.algorithms must be asymmetric, got {bad or 'none'}")

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> OidcSettings:
        block = dict((config.get("auth") or {}).get("oidc") or {})
        algorithms = block.get("algorithms") or DEFAULT_ALGORITHMS
        return cls(
            issuer=str(block.get("issuer") or "").rstrip("/"),
            audience=str(block.get("audience") or ""),
            client_id=block.get("client_id"),
            name_claim=str(block.get("name_claim") or "name"),
            roles_claim=str(block.get("roles_claim") or "realm_access.roles"),
            admin_role=block.get("admin_role"),
            algorithms=tuple(str(alg) for alg in algorithms),
            leeway_seconds=int(block.get("leeway_seconds", 30)),
            jwks_ttl_seconds=int(block.get("jwks_ttl_seconds", 600)),
            jwks_uri=block.get("jwks_uri"),
        )


# --- signature backends ---------------------------------------------------------------


class JwsBackend(Protocol):
    def verify(
        self, signing_input: bytes, signature: bytes, jwk: Mapping[str, Any], alg: str
    ) -> bool:
        """True if `signature` over `signing_input` verifies with `jwk` under `alg`."""
        ...


class PyJwtBackend:
    """Signature checks through PyJWT's algorithm objects (`pyjwt[crypto]`)."""

    def __init__(self) -> None:
        from jwt.algorithms import get_default_algorithms

        self._algorithms = get_default_algorithms()

    def verify(
        self, signing_input: bytes, signature: bytes, jwk: Mapping[str, Any], alg: str
    ) -> bool:
        algorithm = self._algorithms.get(alg)
        if algorithm is None:
            return False
        try:
            key = algorithm.from_jwk(json.dumps(dict(jwk)))
            return bool(algorithm.verify(signing_input, key, signature))
        except Exception:  # noqa: BLE001 - a key that does not load cannot verify anything
            return False


class CryptographyBackend:
    """The same checks with `cryptography` directly, for while PyJWT is not installed."""

    def verify(
        self, signing_input: bytes, signature: bytes, jwk: Mapping[str, Any], alg: str
    ) -> bool:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        digests = {"256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512}
        family, bits = alg[:2], alg[2:]
        if bits not in digests:
            return False
        digest = digests[bits]()
        try:
            if family in ("RS", "PS"):
                public = rsa.RSAPublicNumbers(e=_b64int(jwk["e"]), n=_b64int(jwk["n"])).public_key()
                if family == "RS":
                    pad: Any = padding.PKCS1v15()
                else:
                    pad = padding.PSS(
                        mgf=padding.MGF1(digests[bits]()), salt_length=digest.digest_size
                    )
                public.verify(signature, signing_input, pad, digest)
                return True
            if family == "ES":
                curves = {"P-256": ec.SECP256R1, "P-384": ec.SECP384R1, "P-521": ec.SECP521R1}
                curve_type = curves.get(jwk.get("crv", ""))
                if curve_type is None:
                    return False
                curve = curve_type()
                size = (curve.key_size + 7) // 8
                if len(signature) != 2 * size:  # JWS ES* signatures are raw r || s
                    return False
                public = ec.EllipticCurvePublicNumbers(
                    x=_b64int(jwk["x"]), y=_b64int(jwk["y"]), curve=curve
                ).public_key()
                der = encode_dss_signature(
                    int.from_bytes(signature[:size], "big"), int.from_bytes(signature[size:], "big")
                )
                public.verify(der, signing_input, ec.ECDSA(digest))
                return True
        except (InvalidSignature, KeyError, ValueError, TypeError):
            return False
        return False


def default_backend() -> JwsBackend:
    """PyJWT when installed, else the `cryptography` fallback."""
    try:
        return PyJwtBackend()
    except ImportError:
        return CryptographyBackend()


# --- the verifier ---------------------------------------------------------------------


def http_fetch(url: str) -> Mapping[str, Any]:
    """GET a JSON document from the provider; a short timeout, since requests wait on it."""
    import httpx

    try:
        response = httpx.get(url, timeout=5.0, follow_redirects=False)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderUnavailable(f"could not read {url}: {exc}") from exc


@dataclass
class _KeySet:
    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    unnamed: list[dict[str, Any]] = field(default_factory=list)  # keys without a `kid`
    fetched_at: float = float("-inf")


class OidcVerifier:
    """Turns a bearer token into a `Principal`, or raises `InvalidToken`.

    Thread-safe: requests are served from worker threads, and only one of them refetches
    the key set at a time.
    """

    # A token with an unknown `kid` refetches the key set at most this often.
    REFETCH_SECONDS = 30.0

    def __init__(
        self,
        settings: OidcSettings,
        *,
        fetch: Fetch = http_fetch,
        backend: JwsBackend | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._fetch = fetch
        self._backend = backend or default_backend()
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._jwks_uri: str | None = settings.jwks_uri
        self._keys = _KeySet()
        self._last_forced = float("-inf")

    # -- public ------------------------------------------------------------------

    def principal(self, token: str) -> Principal:
        claims = self.verify(token)
        name = claims.get(self.settings.name_claim) or claims.get("preferred_username")
        return Principal(
            subject=claims["sub"],
            issuer=claims["iss"],
            name=str(name) if name else None,
            roles=_roles(claims, self.settings.roles_claim),
            authenticated=True,
        )

    def verify(self, token: str) -> dict[str, Any]:
        """The token's claims once signature and claims are valid."""
        header, claims, signing_input, signature = _split(token)
        alg = header.get("alg")
        if not isinstance(alg, str) or alg not in self.settings.algorithms or alg in _FORBIDDEN:
            raise InvalidToken(f"algorithm {alg!r} is not allowed")
        if header.get("crit"):
            raise InvalidToken("critical header extensions are not supported")
        if not signature:
            raise InvalidToken("unsigned token")
        jwk = self._key_for(header.get("kid"), alg)
        if not self._backend.verify(signing_input, signature, jwk, alg):
            raise InvalidToken("bad signature")
        validate_claims(claims, self.settings, now=self._clock())
        return claims

    # -- keys --------------------------------------------------------------------

    def _key_for(self, kid: Any, alg: str) -> dict[str, Any]:
        if kid is not None and not isinstance(kid, str):
            raise InvalidToken("kid must be a string")
        keys = self._current_keys()
        key = _pick(keys, kid, alg)
        if key is None:
            # Perhaps the provider rotated: look once more, but not on every bad token.
            keys = self._current_keys(force=True)
            key = _pick(keys, kid, alg)
        if key is None:
            raise InvalidToken(f"no key {kid!r} for {alg} in the issuer's key set")
        return key

    def _current_keys(self, *, force: bool = False) -> _KeySet:
        with self._lock:
            now = self._monotonic()
            fresh = now - self._keys.fetched_at < self.settings.jwks_ttl_seconds
            if fresh and not force:
                return self._keys
            if force and fresh and now - self._last_forced < self.REFETCH_SECONDS:
                return self._keys
            if force:
                self._last_forced = now
            try:
                self._keys = self._load_keys(now)
            except ProviderUnavailable:
                if self._keys.fetched_at == float("-inf"):
                    raise
                # Keep serving with the keys we have; the provider may be restarting.
                log.warning("could not refresh the issuer's key set", exc_info=True)
            return self._keys

    def _load_keys(self, now: float) -> _KeySet:
        if self._jwks_uri is None:
            discovery = self._fetch(f"{self.settings.issuer}/.well-known/openid-configuration")
            if str(discovery.get("issuer", "")).rstrip("/") != self.settings.issuer:
                raise ProviderUnavailable("the discovery document names another issuer")
            uri = discovery.get("jwks_uri")
            if not isinstance(uri, str) or not uri:
                raise ProviderUnavailable("the discovery document has no jwks_uri")
            self._jwks_uri = uri
        document = self._fetch(self._jwks_uri)
        keys = _KeySet(fetched_at=now)
        for jwk in document.get("keys") or []:
            if not isinstance(jwk, dict) or jwk.get("use", "sig") != "sig":
                continue
            if jwk.get("kty") not in ("RSA", "EC"):
                continue
            if "kid" in jwk:
                keys.keys[str(jwk["kid"])] = jwk
            else:
                keys.unnamed.append(jwk)
        return keys


def validate_claims(claims: Mapping[str, Any], settings: OidcSettings, *, now: float) -> None:
    """`iss`, `aud`, `exp`, `nbf`, `iat` and `sub`, with `leeway_seconds` of clock skew."""
    leeway = settings.leeway_seconds
    if str(claims.get("iss", "")).rstrip("/") != settings.issuer:
        raise InvalidToken("wrong issuer")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or settings.audience not in audiences:
        raise InvalidToken("wrong audience")
    exp = claims.get("exp")
    if not _number(exp):
        raise InvalidToken("exp is missing")
    if now >= exp + leeway:
        raise InvalidToken("token expired")
    nbf = claims.get("nbf")
    if nbf is not None and (not _number(nbf) or now < nbf - leeway):
        raise InvalidToken("token not yet valid")
    iat = claims.get("iat")
    if iat is not None and (not _number(iat) or iat > now + leeway):
        raise InvalidToken("token issued in the future")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise InvalidToken("sub is missing")


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _pick(keys: _KeySet, kid: str | None, alg: str) -> dict[str, Any] | None:
    if kid is not None:
        candidates = [keys.keys[kid]] if kid in keys.keys else []
    else:
        # Without a kid the token is only accepted if exactly one key could have signed it.
        candidates = [*keys.keys.values(), *keys.unnamed]
    usable = [jwk for jwk in candidates if _fits(jwk, alg)]
    if kid is None and len(usable) != 1:
        return None
    return usable[0] if usable else None


def _fits(jwk: Mapping[str, Any], alg: str) -> bool:
    if jwk.get("kty") != _KTY_BY_FAMILY.get(alg[:2]):
        return False
    return jwk.get("alg") in (None, alg)


def _split(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    if not isinstance(token, str) or token.count(".") != 2:
        raise InvalidToken("not a compact JWS")
    head, body, sig = token.split(".")
    try:
        header = json.loads(_b64decode(head))
        claims = json.loads(_b64decode(body))
        signature = _b64decode(sig)
    except (ValueError, binascii.Error) as exc:
        raise InvalidToken("malformed token") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise InvalidToken("malformed token")
    return header, claims, f"{head}.{body}".encode("ascii"), signature


def _b64decode(part: str) -> bytes:
    if "=" in part:
        raise ValueError("bad base64url")
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64int(part: str) -> int:
    return int.from_bytes(_b64decode(part), "big")


def _roles(claims: Mapping[str, Any], path: str) -> list[str]:
    """The roles at a dotted claim path (`realm_access.roles` in Keycloak); [] if absent."""
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return []
        value = value.get(part)
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(role) for role in value if isinstance(role, str)]
    return []

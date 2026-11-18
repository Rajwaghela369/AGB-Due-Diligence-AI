"""Bearer token validation (9.E) against a local fake issuer: no network, keys made here.

Every check runs on both signature backends: `cryptography` (always installed) and PyJWT
(skipped until `pyjwt[crypto]` is a dependency).
"""

from __future__ import annotations

import time

import pytest

from app.api.oidc import (
    CryptographyBackend,
    InvalidToken,
    OidcSettings,
    ProviderUnavailable,
)
from tests.fake_issuer import AUDIENCE, ISSUER, FakeIssuer, b64


def _pyjwt():
    pytest.importorskip("jwt")
    from app.api.oidc import PyJwtBackend

    return PyJwtBackend()


@pytest.fixture(params=["cryptography", "pyjwt"])
def backend(request):
    return CryptographyBackend() if request.param == "cryptography" else _pyjwt()


@pytest.fixture
def issuer() -> FakeIssuer:
    return FakeIssuer()


def test_a_valid_token_gives_the_principal_from_standard_claims(issuer, backend):
    verifier = issuer.verifier(backend=backend)
    caller = verifier.principal(
        issuer.token("user-a", realm_access={"roles": ["agb-user", "agb-admin"]})
    )

    assert caller.subject == "user-a" and caller.owner_id == "user-a"
    assert caller.issuer == ISSUER and caller.authenticated is True
    assert caller.name == "Anwältin user-a"
    assert caller.roles == ["agb-user", "agb-admin"]
    assert caller.reviewer().authenticated is True


def test_the_name_falls_back_to_preferred_username_and_roles_to_none(issuer, backend):
    verifier = issuer.verifier(backend=backend)
    caller = verifier.principal(issuer.token("user-b", name=None, realm_access=None))
    assert caller.name == "user-b" and caller.roles == []


def test_ec_and_pss_tokens_verify(issuer, backend):
    issuer.add_ec("ec-1")
    verifier = issuer.verifier(backend=backend)
    assert verifier.verify(issuer.token(kid="ec-1"))["sub"] == "user-a"
    # The published RSA key says alg RS256, so a PS256 token with it is refused …
    with pytest.raises(InvalidToken):
        verifier.verify(issuer.token(alg="PS256"))
    # … but verifies against an RSA key that does not pin its algorithm.
    issuer.add_rsa("rsa-any")
    jwk = issuer.jwk("rsa-any")
    del jwk["alg"]
    assert backend.verify(*_parts(issuer.token(kid="rsa-any", alg="PS256")), jwk, "PS256")


def _parts(token: str) -> tuple[bytes, bytes]:
    import base64

    head, body, sig = token.split(".")
    return f"{head}.{body}".encode(), base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"iss": "https://evil.test/realms/agb-check"}, "wrong issuer"),
        ({"aud": "another-api"}, "wrong audience"),
        ({"aud": ["account", "another-api"]}, "wrong audience"),
        ({"exp": -120}, "expired"),
        ({"exp": None}, "exp is missing"),
        ({"exp": "soon"}, "exp is missing"),
        ({"nbf": 600}, "not yet valid"),
        ({"iat": 600}, "future"),
        ({"sub": None}, "sub is missing"),
        ({"sub": "  "}, "sub is missing"),
    ],
)
def test_claims_are_checked(issuer, backend, claims, reason):
    # Times are offsets from now, applied when the test runs (not at collection).
    now = int(time.time())
    claims = {
        key: now + value if key in ("exp", "nbf", "iat") and isinstance(value, int) else value
        for key, value in claims.items()
    }
    verifier = issuer.verifier(backend=backend)
    with pytest.raises(InvalidToken, match=reason):
        verifier.verify(issuer.token(**claims))


def test_an_audience_list_containing_ours_is_accepted(issuer, backend):
    verifier = issuer.verifier(backend=backend)
    assert verifier.verify(issuer.token(aud=["account", AUDIENCE]))


def test_clock_skew_within_the_leeway_is_accepted(issuer, backend):
    verifier = issuer.verifier(backend=backend)
    assert verifier.verify(issuer.token(exp=int(time.time()) - 5))
    assert verifier.verify(issuer.token(nbf=int(time.time()) + 5))


@pytest.mark.parametrize("alg", ["none", "HS256"])
def test_none_and_hmac_tokens_are_refused_even_if_configured(issuer, backend, alg):
    verifier = issuer.verifier(backend=backend)
    with pytest.raises(InvalidToken, match="not allowed"):
        verifier.verify(issuer.token(alg=alg))
    with pytest.raises(ValueError, match="asymmetric"):
        OidcSettings(issuer=ISSUER, audience=AUDIENCE, algorithms=(alg,))


def test_an_algorithm_outside_the_allow_list_is_refused(issuer, backend):
    verifier = issuer.verifier(backend=backend, settings=issuer.settings(algorithms=("ES256",)))
    with pytest.raises(InvalidToken, match="not allowed"):
        verifier.verify(issuer.token())


def test_a_tampered_or_foreign_token_fails_the_signature(issuer, backend):
    verifier = issuer.verifier(backend=backend)
    head, _body, sig = issuer.token("user-a").split(".")
    forged_body = b64(
        __import__("json").dumps(issuer.claims("user-b")).encode()
    )  # B's claims with A's signature
    with pytest.raises(InvalidToken, match="signature"):
        verifier.verify(f"{head}.{forged_body}.{sig}")

    stranger = FakeIssuer()  # same issuer URL and kid, a different key
    with pytest.raises(InvalidToken, match="signature"):
        verifier.verify(stranger.token())


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b", "a.b.c.d", "!!.!!.!!", "e30.e30.", "e30=.e30.c2ln"],
)
def test_malformed_tokens_are_invalid_not_errors(issuer, token):
    with pytest.raises(InvalidToken):
        issuer.verifier().verify(token)


def test_critical_headers_are_refused(issuer):
    with pytest.raises(InvalidToken, match="critical"):
        issuer.verifier().verify(issuer.token(header={"crit": ["exp"]}))


def test_the_key_set_is_discovered_once_and_cached(issuer):
    verifier = issuer.verifier()
    for _ in range(5):
        verifier.verify(issuer.token())
    assert issuer.jwks_fetches() == 1
    assert issuer.fetches[0] == f"{ISSUER}/.well-known/openid-configuration"


def test_the_cache_expires_after_its_ttl(issuer):
    now = [1000.0]
    verifier = issuer.verifier(
        settings=issuer.settings(jwks_ttl_seconds=60), monotonic=lambda: now[0]
    )
    verifier.verify(issuer.token())
    now[0] += 61
    verifier.verify(issuer.token())
    assert issuer.jwks_fetches() == 2


def test_rotation_an_unknown_kid_refetches_once(issuer):
    now = [1000.0]
    verifier = issuer.verifier(monotonic=lambda: now[0])
    verifier.verify(issuer.token())
    issuer.add_rsa("rsa-2")  # the provider rotates: new key published and used
    issuer.retire("rsa-1")

    assert verifier.verify(issuer.token(kid="rsa-2"))["sub"] == "user-a"
    assert issuer.jwks_fetches() == 2


def test_unknown_kids_cannot_make_the_api_hammer_the_provider(issuer):
    now = [1000.0]
    verifier = issuer.verifier(monotonic=lambda: now[0])
    verifier.verify(issuer.token())
    issuer.add_rsa("unpublished", publish=False)
    for _ in range(10):
        with pytest.raises(InvalidToken, match="no key"):
            verifier.verify(issuer.token(kid="unpublished"))
    assert issuer.jwks_fetches() == 2  # one forced refetch, then rate-limited
    now[0] += 31
    issuer.publish("unpublished")
    assert verifier.verify(issuer.token(kid="unpublished"))
    assert issuer.jwks_fetches() == 3


def test_a_token_without_kid_needs_exactly_one_fitting_key(issuer):
    verifier = issuer.verifier()
    assert verifier.verify(issuer.token(header={"kid": None}))
    issuer.add_rsa("rsa-2")
    verifier = issuer.verifier()
    with pytest.raises(InvalidToken, match="no key"):
        verifier.verify(issuer.token(header={"kid": None}))


def test_a_provider_outage_keeps_the_cached_keys_but_fails_closed_without_them(issuer):
    now = [1000.0]
    verifier = issuer.verifier(
        settings=issuer.settings(jwks_ttl_seconds=60), monotonic=lambda: now[0]
    )
    verifier.verify(issuer.token())
    issuer.down = True
    now[0] += 120
    assert verifier.verify(issuer.token())  # stale keys still verify

    cold = issuer.verifier()
    with pytest.raises(ProviderUnavailable):
        cold.verify(issuer.token())


def test_discovery_naming_another_issuer_is_refused(issuer):
    issuer.discovery_issuer = "https://evil.test/realms/agb-check"
    with pytest.raises(ProviderUnavailable, match="another issuer"):
        issuer.verifier().verify(issuer.token())


def test_settings_come_from_the_auth_block():
    settings = OidcSettings.from_config(
        {
            "auth": {
                "oidc": {
                    "issuer": "http://localhost:8080/realms/agb-check/",
                    "audience": "agb-check-api",
                    "admin_role": "agb-admin",
                }
            }
        }
    )
    assert settings.issuer == "http://localhost:8080/realms/agb-check"
    assert settings.roles_claim == "realm_access.roles" and settings.leeway_seconds == 30
    with pytest.raises(ValueError, match="required"):
        OidcSettings.from_config({"auth": {"oidc": {"issuer": "x"}}})

"""JWT validation for the service-to-service path.

This is the PREFERRED credential (PRD §5.3). The C# API already resolves caller
identity from a Bearer token, so it mints a short-lived asymmetrically-signed
JWT carrying `sub`, `scope` and a <=5 minute expiry, and the gateway verifies the
signature.

WHY ASYMMETRIC, NON-NEGOTIABLY
==============================
With RS256/ES256 the gateway holds only a PUBLIC key. It can verify tokens and
cannot mint them, so compromising the gateway -- the internet-facing component,
and therefore the one most likely to be compromised -- yields no ability to
forge caller identity.

With HS256 the verifying key IS the signing key. Every gateway replica would
hold a secret sufficient to mint a token for any `sub` and any `scope`, and the
blast radius of a gateway compromise would include impersonating the C# API.

So `algorithms` is pinned to an asymmetric family explicitly. Passing the
configured algorithm straight through would reintroduce the classic
algorithm-confusion attack, where a token with `"alg": "HS256"` is verified
using the RSA PUBLIC key as an HMAC secret -- a value the attacker also has,
because it is public. PyJWT guards this, and we do not rely on it alone.

The <=5 minute expiry is the revocation story: there is no revocation list for
JWTs here, and none is needed, because a stolen token is useless within minutes.
That is a deliberate trade against the API key path, which does carry a
revocation list precisely because its credentials are long-lived.
"""
from __future__ import annotations

import jwt
from jwt import PyJWTError

from gateway.auth.keys import AuthError
from gateway.config import settings

# Asymmetric only. See the module docstring.
_ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})


class JwtVerifier:
    def __init__(self, public_key: str | None = None, algorithm: str | None = None) -> None:
        self.public_key = public_key if public_key is not None else settings.JWT_PUBLIC_KEY
        self.algorithm = algorithm or settings.JWT_ALGORITHM
        if self.algorithm not in _ALLOWED_ALGORITHMS:
            raise ValueError(
                f"JWT_ALGORITHM={self.algorithm!r} is not asymmetric. "
                f"Allowed: {sorted(_ALLOWED_ALGORITHMS)}. A symmetric algorithm would "
                f"require this process to hold a key capable of MINTING tokens."
            )

    @property
    def enabled(self) -> bool:
        return bool(self.public_key)

    def verify(self, token: str) -> dict:
        """Validate a token and return its claims. Raises AuthError on failure."""
        if not self.enabled:
            raise AuthError("jwt not configured")
        try:
            claims = jwt.decode(
                token,
                self.public_key,
                # A single-element list, not a pass-through of caller or config
                # intent. This is the algorithm-confusion guard.
                algorithms=[self.algorithm],
                issuer=settings.JWT_ISSUER,
                audience=settings.JWT_AUDIENCE,
                leeway=settings.JWT_LEEWAY_SEC,
                options={
                    "require": ["exp", "iat", "sub", "iss", "aud"],
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except PyJWTError as e:
            # The reason is logged, never returned: distinguishing "expired" from
            # "bad signature" in a response tells an attacker which half of a
            # forgery attempt worked.
            raise AuthError("invalid token") from e

        return claims


def scopes_of(claims: dict) -> list[str]:
    """Extract scopes from either the OAuth-style space-delimited `scope` or a
    list-valued `scopes`. Both appear in the wild and accepting one silently
    would fail closed in a confusing way."""
    raw = claims.get("scope") or claims.get("scopes") or []
    if isinstance(raw, str):
        return [s for s in raw.split() if s]
    return list(raw)

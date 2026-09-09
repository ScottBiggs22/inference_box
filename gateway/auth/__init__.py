"""Authentication dependency shared by every protected route.

Two credential types, tried in this order (PRD §5.3):

  1. a JWT, if one validates -- the preferred service-to-service path;
  2. an API key, for non-interactive jobs and break-glass.

Both arrive as `Authorization: Bearer <credential>`, because that is what the
OpenAI client libraries send and the whole point of this surface is that an
existing OpenAI client can be pointed at it unchanged.

Telling them apart is done by SHAPE, not by trying each in turn: a JWT has three
dot-separated segments, an API key starts with `bkn_`. Attempting JWT validation
on an API key would produce a misleading error and burn CPU on a signature check
that cannot succeed.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from gateway.auth.jwt import JwtVerifier, scopes_of
from gateway.auth.keys import AuthError, KeyStore


@dataclass
class Principal:
    """Who is making this request, however they authenticated."""

    subject: str
    scopes: list[str]
    method: str          # "jwt" | "apikey"
    keyid: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes or "*" in self.scopes


_key_store: KeyStore | None = None
_jwt_verifier: JwtVerifier | None = None


def get_key_store() -> KeyStore:
    global _key_store
    if _key_store is None:
        _key_store = KeyStore()
    return _key_store


def get_jwt_verifier() -> JwtVerifier:
    global _jwt_verifier
    if _jwt_verifier is None:
        _jwt_verifier = JwtVerifier()
    return _jwt_verifier


def reset_auth_singletons() -> None:
    """Test seam. Lets a test point the store at a temp file without monkeypatching."""
    global _key_store, _jwt_verifier
    _key_store = None
    _jwt_verifier = None


def _unauthorized() -> HTTPException:
    # WWW-Authenticate is what makes this a well-behaved Bearer endpoint, and the
    # detail is deliberately generic -- see keys.AuthError on why the reason for
    # a rejection never reaches the caller.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_principal(
    authorization: str | None = Header(default=None),
    store: KeyStore = Depends(get_key_store),
    verifier: JwtVerifier = Depends(get_jwt_verifier),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized()

    credential = authorization[len("bearer "):].strip()
    if not credential:
        raise _unauthorized()

    # Shape-based dispatch. A JWT is header.payload.signature.
    if credential.count(".") == 2 and not credential.startswith("bkn_"):
        try:
            claims = verifier.verify(credential)
        except AuthError as e:
            raise _unauthorized() from e
        return Principal(
            subject=str(claims.get("sub")),
            scopes=scopes_of(claims),
            method="jwt",
        )

    try:
        record = store.verify(credential)
    except AuthError as e:
        raise _unauthorized() from e
    return Principal(
        subject=f"key:{record.keyid}",
        scopes=record.scopes,
        method="apikey",
        keyid=record.keyid,
    )

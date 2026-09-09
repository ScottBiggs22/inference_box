"""API key issuance and verification.

KEY FORMAT
==========
    bkn_live_<keyid>_<secret>
    bkn_test_<keyid>_<secret>

The `keyid` is the reason for the structure. Without it, verifying a presented
key means hashing it against every stored record in turn -- a table scan whose
cost grows with the number of issued keys, on the hot path of every request, and
one that leaks timing information about how many keys exist. With it, the record
is a dictionary lookup and exactly one argon2 verification runs.

STORAGE
=======
Only `argon2id(secret + pepper)` is stored. Never the key.

argon2id rather than bcrypt, for a specific reason: bcrypt silently truncates
input at 72 bytes. A key format with a fixed prefix plus a keyid means two
distinct keys can share a long prefix, and if that shared prefix reaches 72
bytes they would verify against each other. argon2id has no such limit.

The pepper is applied on top of argon2's own per-key salt and is held in the
environment, not in the store -- so a stolen key file is not by itself enough to
mount an offline dictionary attack.

WHAT IS DELIBERATELY NOT HERE YET
=================================
Per-key token budgets and concurrency caps (PRD §5.3) are Phase 2. The record
carries the fields so the store format does not have to change when they land,
and `check_quota` is the seam they plug into.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from gateway.config import settings

_KEY_PREFIXES = ("bkn_live_", "bkn_test_")

# Defaults are argon2-cffi's, which target ~50ms on server hardware. Tuning them
# down to speed up request handling would be a false economy: this runs once per
# request in front of a multi-second GPU operation.
_hasher = PasswordHasher()


class AuthError(Exception):
    """Authentication or authorisation failed.

    Carries no detail about WHY on purpose -- "unknown keyid" and "wrong secret"
    must be indistinguishable to a caller, or the error becomes an oracle for
    enumerating valid key ids.
    """


@dataclass
class ApiKeyRecord:
    keyid: str
    hash: str
    #

    scopes: list[str] = field(default_factory=list)
    # Absolute epoch seconds. Every key expires; a key with no expiry is a
    # credential nobody will ever remember to remove.
    expires_at: float | None = None
    revoked: bool = False
    label: str = ""
    # Phase 2 (PRD §5.3). Present in the schema now so adding enforcement is not
    # also a storage migration.
    token_budget: int | None = None
    max_concurrency: int | None = None

    def to_json(self) -> dict:
        return {
            "keyid": self.keyid,
            "hash": self.hash,
            "scopes": self.scopes,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "label": self.label,
            "token_budget": self.token_budget,
            "max_concurrency": self.max_concurrency,
        }

    @classmethod
    def from_json(cls, raw: dict) -> ApiKeyRecord:
        return cls(
            keyid=raw["keyid"],
            hash=raw["hash"],
            scopes=raw.get("scopes") or [],
            expires_at=raw.get("expires_at"),
            revoked=bool(raw.get("revoked", False)),
            label=raw.get("label", ""),
            token_budget=raw.get("token_budget"),
            max_concurrency=raw.get("max_concurrency"),
        )


def split_key(presented: str) -> tuple[str, str]:
    """Split `bkn_live_<keyid>_<secret>` into (keyid, secret)."""
    for prefix in _KEY_PREFIXES:
        if presented.startswith(prefix):
            rest = presented[len(prefix):]
            keyid, sep, secret = rest.partition("_")
            if not sep or not keyid or not secret:
                raise AuthError("malformed key")
            return keyid, secret
    raise AuthError("malformed key")


def mint_key(*, live: bool = True, scopes: list[str] | None = None,
             ttl_sec: float | None = None, label: str = "") -> tuple[str, ApiKeyRecord]:
    """Create a new key. Returns (plaintext, record).

    The plaintext is returned exactly once and is never recoverable afterwards,
    which is the whole point of storing only the hash.
    """
    keyid = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    prefix = "bkn_live_" if live else "bkn_test_"
    plaintext = f"{prefix}{keyid}_{secret}"
    record = ApiKeyRecord(
        keyid=keyid,
        hash=_hasher.hash(secret + settings.API_KEY_PEPPER),
        scopes=scopes or ["chat"],
        expires_at=(time.time() + ttl_sec) if ttl_sec else None,
        label=label,
    )
    return plaintext, record


class KeyStore:
    """JSON-backed key store.

    Deliberately behind a class so the request path depends on `verify`, not on
    a file. Swapping in a database or a secret manager is then a constructor
    change rather than a refactor of the auth dependency.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.API_KEY_STORE_PATH)
        self._records: dict[str, ApiKeyRecord] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            self._records = {}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        self._records = {
            r["keyid"]: ApiKeyRecord.from_json(r) for r in raw.get("keys", [])
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"keys": [r.to_json() for r in self._records.values()]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def add(self, record: ApiKeyRecord) -> None:
        self._records[record.keyid] = record

    def revoke(self, keyid: str) -> bool:
        record = self._records.get(keyid)
        if record is None:
            return False
        record.revoked = True
        return True

    def verify(self, presented: str) -> ApiKeyRecord:
        """Verify a presented key. Raises AuthError on any failure.

        Every rejection path raises the same bare AuthError. Distinguishing
        "no such keyid" from "bad secret" from "revoked" in the RESPONSE would
        let a caller enumerate valid key ids; the audit log records which it was.
        """
        keyid, secret = split_key(presented)
        record = self._records.get(keyid)
        if record is None:
            # Spend the time anyway. Returning immediately on an unknown keyid
            # makes the response measurably faster than a wrong-secret rejection,
            # which turns response latency into a keyid oracle.
            _dummy_verify()
            raise AuthError("invalid key")

        if record.revoked:
            raise AuthError("invalid key")
        if record.expires_at is not None and time.time() > record.expires_at:
            raise AuthError("invalid key")

        try:
            _hasher.verify(record.hash, secret + settings.API_KEY_PEPPER)
        except VerifyMismatchError as e:
            raise AuthError("invalid key") from e

        return record


_DUMMY_HASH = _hasher.hash("dummy-value-for-constant-time-rejection")


def _dummy_verify() -> None:
    try:
        _hasher.verify(_DUMMY_HASH, "not-the-value")
    except VerifyMismatchError:
        pass

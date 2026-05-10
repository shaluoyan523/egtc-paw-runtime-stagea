from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from .models import ActorIdentity, CapabilityToken, to_plain_dict


class IdentityService:
    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret or secrets.token_bytes(32)

    def actor(self, actor_id: str, actor_type: str) -> ActorIdentity:
        return ActorIdentity(actor_id=actor_id, actor_type=actor_type)

    def issue_token(
        self,
        actor: ActorIdentity,
        scopes: list[str],
        ttl_seconds: int = 3600,
    ) -> CapabilityToken:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).isoformat()
        token_id = secrets.token_hex(12)
        payload = {
            "token_id": token_id,
            "actor_id": actor.actor_id,
            "scopes": scopes,
            "expires_at": expires_at,
        }
        signature = self._sign(payload)
        return CapabilityToken(signature=signature, **payload)

    def verify(self, token: CapabilityToken, required_scope: str) -> bool:
        payload = {
            "token_id": token.token_id,
            "actor_id": token.actor_id,
            "scopes": token.scopes,
            "expires_at": token.expires_at,
        }
        if not hmac.compare_digest(token.signature, self._sign(payload)):
            return False
        if required_scope not in token.scopes:
            return False
        expires_at = datetime.fromisoformat(token.expires_at)
        return expires_at > datetime.now(timezone.utc)

    def _sign(self, payload: dict[str, object]) -> str:
        encoded = json.dumps(
            to_plain_dict(payload), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hmac.new(self._secret, encoded, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

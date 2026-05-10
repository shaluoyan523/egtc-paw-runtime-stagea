from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .identity import IdentityService
from .models import ActorIdentity, ArtifactRef, CapabilityToken, to_plain_dict


class ArtifactStore:
    def __init__(self, root: Path, identity: IdentityService) -> None:
        self.root = root
        self.identity = identity
        self.objects_dir = self.root / "objects"
        self.meta_dir = self.root / "metadata"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        content: bytes,
        media_type: str,
        metadata: dict[str, Any],
        actor: ActorIdentity,
        token: CapabilityToken,
    ) -> ArtifactRef:
        if not self.identity.verify(token, "artifact:write"):
            raise PermissionError("token lacks artifact:write")

        sha256 = hashlib.sha256(content).hexdigest()
        object_path = self.objects_dir / sha256[:2] / sha256
        object_path.parent.mkdir(parents=True, exist_ok=True)
        if not object_path.exists():
            object_path.write_bytes(content)

        enriched = {
            **metadata,
            "actor_id": actor.actor_id,
            "actor_type": actor.actor_type,
        }
        meta_path = self.meta_dir / f"{sha256}.json"
        meta_path.write_text(
            json.dumps(to_plain_dict(enriched), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return ArtifactRef(
            uri=f"artifact://{sha256}",
            sha256=sha256,
            size_bytes=len(content),
            media_type=media_type,
            metadata=enriched,
        )

    def put_json(
        self,
        document: Any,
        metadata: dict[str, Any],
        actor: ActorIdentity,
        token: CapabilityToken,
    ) -> ArtifactRef:
        content = json.dumps(
            to_plain_dict(document), indent=2, sort_keys=True
        ).encode("utf-8")
        return self.put_bytes(content, "application/json", metadata, actor, token)

    def get_bytes(
        self,
        ref: ArtifactRef,
        _actor: ActorIdentity,
        token: CapabilityToken,
    ) -> bytes:
        if not self.identity.verify(token, "artifact:read"):
            raise PermissionError("token lacks artifact:read")
        object_path = self.objects_dir / ref.sha256[:2] / ref.sha256
        return object_path.read_bytes()

    def get_json(
        self,
        ref: ArtifactRef,
        actor: ActorIdentity,
        token: CapabilityToken,
    ) -> Any:
        return json.loads(self.get_bytes(ref, actor, token).decode("utf-8"))

    def verify(self, ref: ArtifactRef) -> bool:
        object_path = self.objects_dir / ref.sha256[:2] / ref.sha256
        if not object_path.exists():
            return False
        return hashlib.sha256(object_path.read_bytes()).hexdigest() == ref.sha256

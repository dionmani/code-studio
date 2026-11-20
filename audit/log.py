from __future__ import annotations

import json
import logging

from cs_common.redact import redact, redact_obj

log = logging.getLogger("codestudio.audit")
MAX_DETAIL_CHARS = 4000


def record(actor=None, action: str = "", project=None, target: str = "", outcome: str = "success",
           request=None, **detail):
    """Persist an AuditEvent. Never raises (auditing must not break requests)."""
    from .models import AuditEvent

    try:
        if request is not None and actor is None:
            actor = request.user if request.user.is_authenticated else None
        clean = redact_obj(detail)
        text = json.dumps(clean, default=str)
        if len(text) > MAX_DETAIL_CHARS:
            clean = {"truncated": True, "preview": text[:MAX_DETAIL_CHARS]}
        ip = request.META.get("REMOTE_ADDR", "")[:64] if request is not None else ""
        return AuditEvent.objects.create(
            actor=actor if (actor is not None and getattr(actor, "pk", None)) else None,
            project=project, action=action[:64], target=redact(str(target))[:1024],
            outcome=outcome, detail=clean, ip=ip,
        )
    except Exception:  # pragma: no cover - defensive
        log.exception("Failed to record audit event %s", action)
        return None

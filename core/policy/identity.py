from __future__ import annotations

import contextlib
from typing import Any

from .access import AccessIdentity
from .quota import QuotaLimit
from ..config import get_section


def identity_from_event(event: Any) -> AccessIdentity:
    """Extract a stable access identity from an AstrBot message event."""
    message_obj = getattr(event, "message_obj", None)
    group_id = str(getattr(message_obj, "group_id", "") or "").strip()
    session_id = str(getattr(message_obj, "session_id", "") or getattr(event, "unified_msg_origin", "") or "").strip()
    user_id = ""
    with contextlib.suppress(Exception):
        user_id = str(event.get_sender_id() or "").strip()
    if not user_id:
        sender = getattr(message_obj, "sender", None)
        user_id = str(
            getattr(sender, "user_id", "")
            or getattr(sender, "id", "")
            or getattr(sender, "uin", "")
            or ""
        ).strip()
    return AccessIdentity(user_id=user_id, group_id=group_id, session_id=session_id, is_private=not group_id)


def quota_scope(identity: AccessIdentity, config: dict) -> tuple[str, str, QuotaLimit]:
    """Choose the quota ledger scope and limit for a private or group identity."""
    quota_config = get_section(config, "quota")
    if identity.is_private:
        key = identity.user_id or identity.session_id or "private"
        return "private", key, QuotaLimit.from_config(get_section(quota_config, "private"))
    key = identity.group_id or identity.session_id or "group"
    return "group", key, QuotaLimit.from_config(get_section(quota_config, "group"))

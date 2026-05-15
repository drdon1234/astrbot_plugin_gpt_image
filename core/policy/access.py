from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..config import bool_value, string_list


@dataclass(frozen=True)
class AccessIdentity:
    user_id: str
    group_id: str = ""
    session_id: str = ""
    is_private: bool = True


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str = ""
    is_admin: bool = False


def evaluate_access(config: Mapping[str, Any], identity: AccessIdentity) -> AccessDecision:
    user_id = str(identity.user_id or "").strip()
    group_id = str(identity.group_id or "").strip()

    admin_id = str(config.get("admin_id") or "").strip()
    if user_id and admin_id and user_id == admin_id:
        return AccessDecision(True, is_admin=True)

    whitelist = _mapping(config.get("whitelist"))
    blacklist = _mapping(config.get("blacklist"))
    whitelist_enabled = bool_value(whitelist.get("enable"), False)
    blacklist_enabled = bool_value(blacklist.get("enable"), False)

    user_whitelist = set(string_list(whitelist.get("user")))
    user_blacklist = set(string_list(blacklist.get("user")))
    group_whitelist = set(string_list(whitelist.get("group")))
    group_blacklist = set(string_list(blacklist.get("group")))

    if whitelist_enabled and user_id and user_id in user_whitelist:
        return AccessDecision(True)
    if blacklist_enabled and user_id and user_id in user_blacklist:
        return AccessDecision(False, "当前用户在生图黑名单中。")
    if whitelist_enabled and group_id and group_id in group_whitelist:
        return AccessDecision(True)
    if blacklist_enabled and group_id and group_id in group_blacklist:
        return AccessDecision(False, "当前群聊在生图黑名单中。")

    if whitelist_enabled:
        if identity.is_private:
            return AccessDecision(False, "当前用户不在生图白名单中。")
        return AccessDecision(False, "当前群聊或用户不在生图白名单中。")
    return AccessDecision(True)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

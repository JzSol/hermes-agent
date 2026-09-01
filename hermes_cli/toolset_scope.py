"""Platform scope rules for configured toolsets.

This module is intentionally independent of tool resolution and CLI setup so
configuration validation and runtime resolution apply the same platform policy.
"""

from typing import Set


# Toolsets without a restriction entry are available on every platform.
_TOOLSET_PLATFORM_RESTRICTIONS = {
    "discord": {"discord"},
    "discord_admin": {"discord"},
    # The watchlist mutates profile-owned diligence/reminder state. Keep it
    # available only on interactive, authenticated user surfaces; webhook and
    # cron inputs can be unattended or derived from untrusted external data.
    "watchlist": {
        "api_server",
        "bluebubbles",
        "cli",
        "dingtalk",
        "discord",
        "feishu",
        "matrix",
        "mattermost",
        "qqbot",
        "signal",
        "slack",
        "telegram",
        "wecom",
        "wecom_callback",
        "weixin",
        "whatsapp",
        "whatsapp_cloud",
        "yuanbao",
    },
}


def toolset_allowed_for_platform(ts_key: str, platform: str) -> bool:
    """Return whether ``ts_key`` is available on ``platform``."""
    allowed: Set[str] | None = _TOOLSET_PLATFORM_RESTRICTIONS.get(ts_key)
    return allowed is None or platform in allowed

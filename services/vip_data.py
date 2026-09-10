"""
VIP / VIP+ (Matchmaking) - pricing, ELO bonus and draft-priority logic.

The purchase flow itself (Stripe checkout, Pix, webhook) lives entirely
on the site (app/api/vip/checkout, app/api/vip/webhook), which writes to
`vip_payments` and `vip_subscriptions`. This module only *reads*
`vip_subscriptions` to apply the resulting benefits inside the bot (ELO
bonus, draft priority, badges). cogs/vip.py handles expiry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import database

VIP_TIERS = ("vip", "vip_plus")

# Keep this in sync with app/api/vip/checkout/route.ts (VIP_PRICING) on the site.
VIP_PRICING = {
    "vip": {"cents": 500, "label": "R$ 5.00"},
    "vip_plus": {"cents": 1000, "label": "R$ 10.00"},
}

VIP_DURATION_DAYS = 30

# ELO bonus applies only to the amount gained on a win (it never
# reduces the amount lost on a loss, even for VIP members).
VIP_ELO_WIN_BONUS_PERCENT = {
    "vip": 0.10,
    "vip_plus": 0.20,
}

# Extra weight used to sort/highlight captain candidates.
VIP_CAPTAIN_PRIORITY_WEIGHT = {
    "vip": 2,
    "vip_plus": 4,
}


def vip_tier_label(tier: str | None) -> str | None:
    if tier == "vip_plus":
        return "VIP+"
    if tier == "vip":
        return "VIP"
    return None


async def get_active_vip(discord_id: int | str) -> dict[str, Any] | None:
    """
    Return {'tier', 'status', 'expires_at'} for the active VIP
    subscription, or None. A subscription that has technically expired
    but is not yet marked so (the expiry job in cogs/vip.py runs every
    few minutes, not instantly) is treated here as "no VIP", so it can
    never give an undue ELO/draft advantage.
    """
    if not database.is_ready():
        return None

    row = await database.fetchone(
        """
        SELECT tier, status, expires_at FROM vip_subscriptions
        WHERE discord_id = $1 AND status = 'active'
        LIMIT 1
        """,
        database.did(discord_id),
    )
    if not row:
        return None

    if row["expires_at"] < datetime.now(timezone.utc):
        return None

    return dict(row)


async def get_elo_win_bonus_multiplier(discord_id: int | str) -> float:
    vip = await get_active_vip(discord_id)
    if not vip:
        return 1.0
    return 1.0 + VIP_ELO_WIN_BONUS_PERCENT.get(vip["tier"], 0.0)


async def get_captain_priority_weight(discord_id: int | str) -> int:
    vip = await get_active_vip(discord_id)
    if not vip:
        return 0
    return VIP_CAPTAIN_PRIORITY_WEIGHT.get(vip["tier"], 0)


async def get_vip_badge(discord_id: int | str) -> str | None:
    vip = await get_active_vip(discord_id)
    if not vip:
        return None
    return vip_tier_label(vip.get("tier"))

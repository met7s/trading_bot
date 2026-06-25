"""
market_scanner.py
=================
Finds the *currently tradeable* "BTC 15-Min Up/Down" market via Polymarket's
public Gamma API (https://gamma-api.polymarket.com), no authentication required.

Polymarket data model (the part we need):
  * A `market` carries: question, slug, conditionId, endDate, outcomes,
    outcomePrices, and `clobTokenIds` — the two ERC-1155 token IDs (one per
    outcome). Those token IDs are what we subscribe to on the CLOB websocket
    and what we trade. `outcomes`/`outcomePrices`/`clobTokenIds` arrive as
    JSON-encoded STRINGS, so they must be json.loads()'d.

Discovery strategy (robust to naming drift):
  1. Pull active, non-closed markets from Gamma.
  2. Keep only those whose question/slug matches the BTC + up/down patterns.
  3. Keep only those resolving inside [MIN_TIME_TO_RESOLVE, RESOLVE_WITHIN].
  4. Pick the soonest-resolving survivor.

IMPORTANT: the exact on-site naming of these short-dated crypto markets changes.
The pattern lists live in config.STRAT — verify them against polymarket.com.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

from config import GAMMA_HOST, STRAT


@dataclass
class TargetMarket:
    slug: str
    question: str
    condition_id: str
    up_token_id: str       # token for the "Up"/"Yes" outcome
    down_token_id: str     # token for the "Down"/"No" outcome
    end_ts: float          # unix seconds when the market resolves

    def seconds_to_resolve(self, now_ts: float) -> float:
        return self.end_ts - now_ts


def _parse_iso(dt_str: str) -> float:
    """Gamma returns ISO-8601 like '2026-06-22T15:30:00Z'. -> unix seconds."""
    # Normalise a trailing 'Z' to an explicit UTC offset for fromisoformat.
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str).astimezone(timezone.utc).timestamp()


def _matches_btc_updown(question: str, slug: str) -> bool:
    """True if the text looks like a BTC up/down market."""
    hay = f"{question} {slug}".lower()
    name_ok = any(p in hay for p in STRAT.MARKET_NAME_PATTERNS)
    updown_ok = any(p in hay for p in STRAT.MARKET_UPDOWN_PATTERNS)
    return name_ok and updown_ok


class MarketScanner:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def find_active_15m_market(self, now_ts: float) -> TargetMarket | None:
        """
        Query Gamma for active markets and return the best BTC 15-min up/down
        candidate, or None if nothing currently qualifies.
        """
        session = await self._ensure_session()

        # active=true & closed=false => only live markets. We over-fetch and
        # filter client-side because Gamma has no "ends within N minutes" param.
        params = {
            "active": "true",
            "closed": "false",
            "limit": "200",
            "order": "endDate",
            "ascending": "true",
        }

        try:
            async with session.get(
                f"{GAMMA_HOST}/markets",
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                markets = await resp.json()
        except Exception as exc:  # noqa: BLE001 - scanner must never crash loop
            print(f"[scanner] Gamma query failed: {exc}")
            return None

        candidates: list[TargetMarket] = []
        for m in markets:
            try:
                question = m.get("question", "") or ""
                slug = m.get("slug", "") or ""
                if not _matches_btc_updown(question, slug):
                    continue

                end_date = m.get("endDate")
                if not end_date:
                    continue
                end_ts = _parse_iso(end_date)
                ttr = end_ts - now_ts
                if ttr < STRAT.MIN_TIME_TO_RESOLVE_SEC:
                    continue  # too close to resolution; skip
                if ttr > STRAT.RESOLVE_WITHIN_SEC:
                    continue  # resolves too far out; not our 15-min window

                # clobTokenIds is a JSON-encoded string: '["<up>", "<down>"]'.
                raw_tokens = m.get("clobTokenIds")
                if not raw_tokens:
                    continue
                token_ids = json.loads(raw_tokens)
                if len(token_ids) < 2:
                    continue

                # outcomes is also a JSON string, e.g. '["Up","Down"]'. We use
                # it to map index 0/1 to the correct direction. Default
                # assumption (Polymarket binary order) is [Yes/Up, No/Down].
                outcomes_raw = m.get("outcomes") or '["Up","Down"]'
                outcomes = [o.lower() for o in json.loads(outcomes_raw)]
                up_idx, down_idx = self._direction_indices(outcomes)

                candidates.append(
                    TargetMarket(
                        slug=slug,
                        question=question,
                        condition_id=m.get("conditionId", ""),
                        up_token_id=str(token_ids[up_idx]),
                        down_token_id=str(token_ids[down_idx]),
                        end_ts=end_ts,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - skip a malformed market
                print(f"[scanner] skipped a market (parse issue): {exc}")
                continue

        if not candidates:
            return None

        # Soonest-resolving qualifying market wins.
        candidates.sort(key=lambda c: c.end_ts)
        best = candidates[0]
        print(
            f"[scanner] target: '{best.question}' "
            f"(resolves in {best.seconds_to_resolve(now_ts):.0f}s)"
        )
        return best

    @staticmethod
    def _direction_indices(outcomes: list[str]) -> tuple[int, int]:
        """
        Map the outcome list to (up_index, down_index). Handles both
        ['up','down'] and ['yes','no'] style labelling. Falls back to (0, 1).
        """
        up_index = 0
        down_index = 1
        for i, o in enumerate(outcomes):
            if o in ("up", "yes", "above"):
                up_index = i
            elif o in ("down", "no", "below"):
                down_index = i
        return up_index, down_index

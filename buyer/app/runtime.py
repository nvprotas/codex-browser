from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from .auth_scripts import domain_from_url, normalize_domain


@dataclass(frozen=True)
class BrowserSlot:
    slot_id: str
    cdp_endpoint: str
    novnc_url: str


@dataclass
class BrowserSlotLease:
    slot: BrowserSlot
    session_id: str
    domain: str


class BrowserSlotManager:
    def __init__(
        self,
        *,
        slots: list[BrowserSlot],
        domain_limits: dict[str, int] | None = None,
    ) -> None:
        if not slots:
            raise ValueError('Нужен хотя бы один browser slot.')
        self._slots = list(slots)
        self._domain_limits = {
            normalize_domain(domain): max(int(limit), 0)
            for domain, limit in (domain_limits or {}).items()
            if normalize_domain(domain) and int(limit) > 0
        }
        self._lock = asyncio.Lock()
        self._leases_by_session: dict[str, BrowserSlotLease] = {}
        self._leases_by_slot: dict[str, BrowserSlotLease] = {}

    @property
    def slots(self) -> list[BrowserSlot]:
        return list(self._slots)

    async def acquire(self, *, session_id: str, domain: str) -> BrowserSlot | None:
        normalized_domain = normalize_domain(domain)
        async with self._lock:
            existing = self._leases_by_session.get(session_id)
            if existing is not None:
                return existing.slot

            limit = self._domain_limits.get(normalized_domain)
            if limit is not None:
                active_for_domain = sum(1 for lease in self._leases_by_session.values() if lease.domain == normalized_domain)
                if active_for_domain >= limit:
                    return None

            for slot in self._slots:
                if slot.slot_id in self._leases_by_slot:
                    continue
                lease = BrowserSlotLease(slot=slot, session_id=session_id, domain=normalized_domain)
                self._leases_by_session[session_id] = lease
                self._leases_by_slot[slot.slot_id] = lease
                return slot
            return None

    async def release(self, session_id: str) -> BrowserSlot | None:
        async with self._lock:
            lease = self._leases_by_session.pop(session_id, None)
            if lease is None:
                return None
            self._leases_by_slot.pop(lease.slot.slot_id, None)
            return lease.slot

    async def get_slot(self, session_id: str) -> BrowserSlot | None:
        async with self._lock:
            lease = self._leases_by_session.get(session_id)
            return lease.slot if lease is not None else None


def build_browser_slot_manager(
    *,
    browser_slots_json: str,
    legacy_cdp_endpoint: str,
    legacy_novnc_url: str,
    domain_concurrency_limits: str,
    min_browser_slots: int,
    max_browser_slots: int,
) -> BrowserSlotManager:
    slots = _parse_browser_slots_json(browser_slots_json)
    if not slots:
        slots = [BrowserSlot(slot_id='browser-1', cdp_endpoint=legacy_cdp_endpoint, novnc_url=legacy_novnc_url)]

    bounded_max = max(max_browser_slots, 1)
    bounded_min = max(min_browser_slots, 1)
    selected = slots[:bounded_max]
    if len(selected) < bounded_min:
        raise ValueError(
            f'Настроено {len(selected)} browser slots, но MIN_BROWSER_SLOTS={bounded_min}.'
        )
    return BrowserSlotManager(slots=selected, domain_limits=parse_domain_concurrency_limits(domain_concurrency_limits))


def parse_domain_concurrency_limits(raw: str) -> dict[str, int]:
    limits: dict[str, int] = {}
    for item in (raw or '').split(','):
        if not item.strip() or '=' not in item:
            continue
        domain, raw_limit = item.split('=', 1)
        normalized_domain = normalize_domain(domain)
        try:
            limit = int(raw_limit.strip())
        except ValueError:
            continue
        if normalized_domain and limit > 0:
            limits[normalized_domain] = limit
    return limits


def _parse_browser_slots_json(raw: str) -> list[BrowserSlot]:
    if not (raw or '').strip():
        return []
    payload: Any = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError('BROWSER_SLOTS_JSON должен быть JSON-массивом.')
    slots: list[BrowserSlot] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError('Каждый browser slot должен быть JSON-объектом.')
        slot_id = str(item.get('id') or item.get('slot_id') or f'browser-{index}').strip()
        cdp_endpoint = str(item.get('cdp_endpoint') or item.get('cdpEndpoint') or '').strip()
        novnc_url = str(item.get('novnc_url') or item.get('novncUrl') or '').strip()
        if not cdp_endpoint or not novnc_url:
            raise ValueError('Для каждого browser slot нужны cdp_endpoint и novnc_url.')
        slots.append(BrowserSlot(slot_id=slot_id, cdp_endpoint=cdp_endpoint, novnc_url=novnc_url))
    return slots


def domain_for_slot_limit(start_url: str) -> str:
    return normalize_domain(domain_from_url(start_url))

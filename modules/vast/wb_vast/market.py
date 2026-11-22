"""Offer search. Vast's search samples: identical queries return different subsets of the matching offers
(seen 2026-11-20), so a lookup repeats the query and merges what each returns."""

from __future__ import annotations

import time

from .api import Vast, branch_range


def query(card: str, max_dph: float, *, driver_branch: str | None = None, verified: bool = False,
          limit: int = 100, storage_gb: float | None = None) -> dict:
    """A search body. ``storage_gb`` prices each offer's ``dph_total`` for that much disk, as Vast bills the instance
    a rental creates; without it Vast prices a small default disk, and the bill comes out above the quote."""
    body = {"gpu_name": {"eq": card}, "num_gpus": {"eq": 1}, "rentable": {"eq": True}, "type": "ondemand",
            "dph_total": {"lte": max_dph}, "order": [["dph_total", "asc"]], "limit": limit}
    if storage_gb:
        body["allocated_storage"] = float(storage_gb)
    if verified:
        body["verified"] = {"eq": True}
    if driver_branch:
        low, high = branch_range(driver_branch)
        body["driver_version"] = {"gte": low, "lt": high}
    return body


def in_branch(offer: dict, branch: str | None) -> bool:
    if not branch:
        return True
    major = str(offer.get("driver_version") or "").split(".")[0]
    return major == branch.upper().removeprefix("R")


def search(vast: Vast, cards: list[str], max_dph: float, *, driver_branch: str | None = None, repeats: int = 3,
           pause: float = 1.0, purpose: str = "market", stop_when=None, limit: int = 100,
           storage_gb: float | None = None) -> dict[str, dict]:
    """Every row a search returns counts against Vast's daily search quota (20,000 rows, seen 2026-11-21), so
    ``limit`` stays small: offers come sorted by price, and the cheap end is what matters."""
    seen: dict[str, dict] = {}
    for attempt in range(repeats):
        for card in cards:
            for name in dict.fromkeys((card, card.replace(" ", "_"))):
                offers = vast.call("POST", "/bundles/", query(name, max_dph, driver_branch=driver_branch, limit=limit,
                                                               storage_gb=storage_gb),
                                   purpose=purpose).get("offers", [])
                for offer in offers:
                    if in_branch(offer, driver_branch):
                        seen[str(offer.get("id"))] = offer
                if offers:
                    break
            if stop_when and stop_when(seen):
                return seen
        if attempt + 1 < repeats and pause:
            time.sleep(pause)
    return seen


def find(vast: Vast, offer_id: str, cards: list[str], max_dph: float, tries: int = 10, limit: int = 100,
         storage_gb: float | None = None) -> dict | None:
    """The offer if it is still rentable at max_dph or less (asked up to ``tries`` times, see above)."""
    found = search(vast, cards, max_dph, repeats=tries, pause=2, purpose="find",
                   stop_when=lambda seen: str(offer_id) in seen, limit=limit, storage_gb=storage_gb)
    return found.get(str(offer_id))


def describe(offer: dict) -> str:
    return (f"offer {offer.get('id')}: {offer.get('gpu_name')} x{offer.get('num_gpus')}, {offer.get('gpu_ram')} MB, "
            f"${offer.get('dph_total', 0):.3f}/h, driver {offer.get('driver_version')}, "
            f"{offer.get('cpu_cores_effective')} cores, reliability {offer.get('reliability', 0):.3f}, "
            f"{offer.get('geolocation')}")


def host_text(offer: dict) -> str:
    return (f"machine {offer.get('machine_id')}, {offer.get('geolocation') or '?'}, {offer.get('cpu_name') or '?'}, "
            f"{offer.get('cpu_cores_effective')} effective cores")

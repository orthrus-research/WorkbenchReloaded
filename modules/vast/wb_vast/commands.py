"""vast: rent GPU machines on Vast.ai and run bundles on them (ported from vast_probe.py, rent.py and ledger.py).

Every rental waits for an explicit yes on one exact quote (D13); the core enforces that, and ``rent`` checks
the price again before creating anything (S14). An instance is destroyed in ``finally``, at the cap, when
it is billed above the quote, and, if Workbench itself dies, by the remote dead man's switch and by the
clean-up at the next start (S15).
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tarfile
import time
import urllib.parse
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import bundle as bundles
from . import market, remote
from .api import DEFAULT_API, Vast, VastError

def label_prefix() -> str:
    """Rentals are labelled wb-<installation>-<job>: the start-up clean-up destroys only this installation's."""
    install = os.environ.get("WB_INSTALL_ID")
    if not install:
        raise EnvironmentProblem("no installation id (WB_INSTALL_ID); refusing to label or clean up rentals")
    return f"wb-{install}-"
HOST_FIELDS = ("cpu_name", "cpu_cores_effective", "cpu_ram", "cpu_ghz", "pcie_bw", "gpu_mem_bw", "inet_up",
               "inet_down", "disk_bw", "direct_port_count", "machine_id")


def utc(seconds=None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _guard(function):
    """A quota refusal is the machine's state, not a crash: outcome environment, saying when to retry."""
    import functools

    @functools.wraps(function)
    def wrapper(ctx, *args, **kwargs):
        from .api import QuotaExceeded
        try:
            return function(ctx, *args, **kwargs)
        except QuotaExceeded as error:
            raise EnvironmentProblem(str(error)) from None
    return wrapper


def client(ctx: Context) -> Vast:
    from wb_ledger import store

    connection = None
    try:
        connection = store.connect()
    except Exception as error:  # noqa: BLE001
        ctx.log.warning(f"requests will not be recorded: {error}")

    def recorder(method, endpoint, body, status, ms, raw, purpose, job):
        if connection is not None:
            store.record_call(connection, "vast", method, endpoint, body, status, ms, raw, purpose, job=job)
    return Vast(ctx.secret("vast_api_key"), ctx.config.get("api") or DEFAULT_API, recorder, ctx.job)


def cards(ctx: Context, card: str | None) -> list[str]:
    return [card] if card else list(ctx.config.get("cards") or [])


# ----------------------------------------------------------------------------------------------
# read-only commands
@_guard
def offers(ctx: Context, gpu: str, max_dph: float, driver_branch=None, min_cores: float = 0):
    vast = client(ctx)
    found = market.search(vast, cards(ctx, gpu), max_dph, driver_branch=driver_branch, repeats=2, pause=1,
                          purpose="offers")
    listed = sorted((o for o in found.values() if (o.get("cpu_cores_effective") or 0) >= min_cores),
                    key=lambda o: o.get("dph_total", 0))
    for offer in listed[:20]:
        ctx.log.info(market.describe(offer))
    ctx.summary(f"{len(listed)} offer(s) for {gpu or 'the reference cards'} at or below ${max_dph}/h"
                + (f" on {driver_branch}" if driver_branch else ""))
    keep = ("id", "gpu_name", "gpu_ram", "dph_total", "driver_version", "cpu_cores_effective", "cpu_name",
            "reliability", "geolocation", "machine_id", "pcie_bw", "inet_down", "verified")
    return {"offers": [{k: o.get(k) for k in keep} for o in listed]}


def rows_today(purpose: str | None = None) -> int:
    """Search rows Vast has returned since 00:00 UTC (all, or one purpose's), from the ledger's record of every
    request."""
    from wb_ledger import store
    midnight = time.strftime("%Y-%m-%dT00:00:00", time.gmtime())
    sql = ("SELECT coalesce(sum(result_count), 0) FROM ledger_api_call WHERE provider = 'vast' "
           "AND endpoint = '/bundles/' AND time >= ?")
    args: tuple = (midnight,)
    if purpose:
        sql += " AND purpose = ?"
        args += (purpose,)
    return int(store.connect(read_only=True).execute(sql, args).fetchone()[0] or 0)


def market_allowance(config: dict, now: float | None = None) -> dict:
    """How many search rows a market snapshot may take now.

    Vast allows ``daily_rows`` search rows a day (20,000, seen 2026-11-21). ``market_share`` of them (30%) is
    the market's; the rest is reserved for work (rentals, offer lookups, probes) and the market never touches it.
    The market's share is paced over the UTC day: by any moment it may have used only the share of the day so far,
    so context builds a little at a time instead of in a burst."""
    now = time.time() if now is None else now
    daily = int(config.get("daily_rows", 20000))
    share = float(config.get("market_share", 0.3))
    midnight = now - now % 86400
    elapsed = (now - midnight) / 86400
    market_used, total_used = rows_today("market"), rows_today()
    paced = daily * share * elapsed - market_used
    cap = daily * share - market_used
    headroom = daily - total_used
    return {"available": max(0, int(min(paced, cap, headroom))), "market_used": market_used,
            "total_used": total_used, "daily": daily, "share": share, "elapsed": round(elapsed, 3)}


def _cursor(ctx: Context) -> Path:
    return Path(os.environ.get("WB_STATE_DIR", str(ctx.work))) / "vast" / "market-cursor.json"


@_guard
def market_snapshot(ctx: Context, max_dph: float, gpus: list):
    names = list(gpus) or list(ctx.config.get("cards") or [])
    if not names:
        raise ParamError("no cards to survey")
    page = int(ctx.config.get("market_limit", 25))
    allowance = market_allowance(ctx.config)
    queries = min(len(names), allowance["available"] // page)
    if queries < 1:
        ctx.summary(f"skipped: the market has used {allowance['market_used']} of its "
                    f"{allowance['share']:.0%} paced share so far today ({allowance['total_used']} of "
                    f"{allowance['daily']} rows in all); {page} rows are needed for the next card")
        return {"offers": 0, "cards": [], "skipped": True, **allowance}
    cursor = _cursor(ctx)
    try:
        position = json.loads(cursor.read_text(encoding="utf-8")).get("next", 0)
    except (OSError, ValueError):
        position = 0
    chosen = [names[(position + i) % len(names)] for i in range(queries)]
    vast = client(ctx)
    found = market.search(vast, chosen, max_dph, repeats=1, pause=0, purpose="market", limit=page)
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps({"next": (position + queries) % len(names)}), encoding="utf-8")
    ctx.measure("offers", len(found))
    ctx.measure("rows_today", rows_today())
    ctx.summary(f"{len(found)} offers from {', '.join(chosen)} ({queries} of {len(names)} cards this run, "
                f"rotating); market {rows_today('market')} rows today, paced to {allowance['share']:.0%}")
    return {"offers": len(found), "cards": chosen, "skipped": False, **market_allowance(ctx.config)}


@_guard
def charges(ctx: Context, days: int):
    from wb_ledger import store
    vast = client(ctx)
    now = int(time.time())
    params = {"select_filters": {"day": {"gte": now - days * 86400, "lte": now}}, "limit": 100, "format": "table"}
    results, token = [], None
    while True:
        if token:
            params["after_token"] = token
        query = "&".join(f"{k}={urllib.parse.quote_plus(v if isinstance(v, str) else json.dumps(v))}"
                         for k, v in params.items())
        page = vast.call("GET", "/charges/?" + query, purpose="charges")
        results += page.get("results", [])
        token = page.get("next_token")
        if not token or not page.get("results"):
            break
    count = store.add_charges(store.connect(), "vast", results)
    total = sum(r.get("amount", 0) for r in results)
    ctx.summary(f"{len(results)} instance(s) charged, ${total:.3f}, {count} item(s) recorded")
    return {"instances": len(results), "amount": total}


@_guard
def instances(ctx: Context):
    vast = client(ctx)
    listed = vast.call("GET", "/instances/?owner=me", purpose="instances").get("instances", [])
    keep = ("id", "label", "actual_status", "gpu_name", "dph_total", "start_date", "machine_id")
    ours = [{k: i.get(k) for k in keep} for i in listed]
    ctx.summary(f"{len(ours)} instance(s), {sum(1 for i in ours if str(i.get('label') or '').startswith(label_prefix()))} "
                f"started by this Workbench")
    return {"instances": ours}


@_guard
def destroy(ctx: Context, instance: str):
    vast = client(ctx)
    result = vast.call("DELETE", f"/instances/{instance}/", purpose="destroy")
    ctx.summary(f"destroyed {instance}: {result}")
    return {"instance": instance, "result": result}


@_guard
def keys(ctx: Context):
    listed = client(ctx).call("GET", "/ssh/", purpose="keys")
    entries = listed if isinstance(listed, list) else listed.get("ssh_keys", listed.get("keys", []))
    if isinstance(entries, str):
        entries = json.loads(entries or "[]")
    return {"keys": [{"id": k.get("id"), "key": str(k.get("public_key") or k.get("ssh_key") or "")[:40] + "..."}
                     for k in entries if isinstance(k, dict)]}


# ----------------------------------------------------------------------------------------------
# rent: quote, then run
def _bundle(ctx: Context, path: str) -> bundles.Bundle:
    from wb_core.paths import PathMap
    from wb_core.settings import Root
    roots = PathMap([Root(**r) for r in ctx.spec.get("roots", [])])

    def resolve(value: str) -> str:
        try:
            return roots.resolve(value).container
        except Exception:  # noqa: BLE001
            return value
    return bundles.load(Path(path), resolve)


def _check_offer(ctx: Context, offer: dict | None, offer_id: str, max_dph: float, min_cores: float):
    if offer is None:
        raise EnvironmentProblem(f"offer {offer_id} is gone or above ${max_dph}/h; nothing rented")
    cores = offer.get("cpu_cores_effective") or 0
    if cores < min_cores:
        raise ParamError(f"offer {offer_id} has {cores} effective cores, under min_cores {min_cores} (2 cores gave "
                         "Cubature one build thread and a tour that never settled, cal-01); nothing rented")


# Vast states prices to a few decimals; a bill this close to the quote is the same price, not a rise. It is at most
# $0.00015 over a 90-minute rental.
BILLING_SLACK = 1e-4


def _quote_for(ctx: Context, offer: dict, cap: float, disk: int, upload_bytes: int, summary: str) -> dict:
    dph = float(offer.get("dph_total") or 0)
    hours = cap / 3600
    transfer = (upload_bytes / 1e9) * float(offer.get("inet_down_cost") or 0)
    return {"summary": summary, "offer": str(offer.get("id")), "card": offer.get("gpu_name"),
            "gpu_ram_mb": offer.get("gpu_ram"), "driver": offer.get("driver_version"),
            # The exact price, not a rounded one: the re-check before renting compares the live price with it, and a
            # rounded price (0.0422 for 0.042222...) reads as a rise on every pass. The most it can cost rounds up.
            "host": market.host_text(offer), "dph": dph, "cap_minutes": round(cap / 60, 1), "disk_gb": disk,
            "transfer_estimate": round(transfer, 4), "max_cost": math.ceil((dph * hours + transfer) * 1e4) / 1e4,
            "currency": "USD",
            "prompt": {
                "question": f"Rent {offer.get('gpu_name')} (offer {offer.get('id')}, driver {offer.get('driver_version')}) "
                            f"at ${dph:.3f}/h for at most {cap / 60:.0f} min, ${dph * hours + transfer:.2f} at most?",
                "options": [
                    {"label": "Approve", "description": f"Rent offer {offer.get('id')} now; it is destroyed at "
                                                        f"{cap / 60:.0f} min at the latest."},
                    {"label": "Decline", "description": "Cancel the job; nothing is rented."}]}}


@_guard
def quote_rent(ctx: Context, offer: str, bundle: str, max_dph: float, cap: float, min_cores=None, disk=None):
    b = _bundle(ctx, bundle)
    vast = client(ctx)
    min_cores = ctx.config.get("min_cores", 8) if min_cores is None else min_cores
    found = market.find(vast, offer, list(ctx.config.get("cards") or []), max_dph,
                        storage_gb=disk or b.disk_gb)
    _check_offer(ctx, found, offer, max_dph, min_cores)
    return _quote_for(ctx, found, cap, disk or b.disk_gb, b.upload_bytes(), f"run bundle {b.name} on a rented GPU")


@_guard
def quote_probe(ctx: Context, offer: str, max_dph: float):
    vast = client(ctx)
    found = market.find(vast, offer, list(ctx.config.get("cards") or []), max_dph, storage_gb=16)
    _check_offer(ctx, found, offer, max_dph, 0)
    return _quote_for(ctx, found, 15 * 60, 16, 0, "an OpenGL feasibility probe (about 10 minutes)")


def _transport(ctx: Context, host: str, port: int):
    fake = os.environ.get("WB_VAST_FAKE_TRANSPORT")
    if fake:  # tests: a module:attribute that builds a fake transport
        module, _, attribute = fake.partition(":")
        __import__(module)
        return getattr(sys.modules[module], attribute)(ctx, host, port)
    key = remote.private_key_copy(ctx.secret_path("vast_ssh_key"), ctx.work)
    return remote.Transport(host, port, key, ctx.work / ".ssh" / "known_hosts")


def _public_key(ctx: Context) -> str:
    if os.environ.get("WB_VAST_FAKE_TRANSPORT"):
        return "ssh-ed25519 AAAAfake test"
    key = remote.private_key_copy(ctx.secret_path("vast_ssh_key"), ctx.work)
    pub = ctx.spec.get("secrets", {}).get("vast_ssh_pub")
    return remote.public_key(key, Path(pub) if pub else None)


@_guard
def rent(ctx: Context, offer: str, bundle: str, max_dph: float, cap: float, min_cores=None, disk=None):
    quote = ctx.quote or {}
    if str(quote.get("offer")) != str(offer):
        raise EnvironmentProblem("the approved quote is for another offer")
    b = _bundle(ctx, bundle)
    vast = client(ctx)
    min_cores = ctx.config.get("min_cores", 8) if min_cores is None else min_cores
    ctx.progress("find", msg=f"looking up offer {offer} again")
    found = market.find(vast, offer, list(ctx.config.get("cards") or []), max_dph,
                        storage_gb=disk or b.disk_gb)
    _check_offer(ctx, found, offer, max_dph, min_cores)
    price = float(found.get("dph_total") or 0)
    if price > float(quote.get("dph", 0)) + 1e-9:
        ctx.requote(_quote_for(ctx, found, cap, disk or b.disk_gb, b.upload_bytes(),
                               f"run bundle {b.name} on a rented GPU"),
                    f"the price rose from ${quote.get('dph')}/h to ${price:.4f}/h")
    ctx.machine_info(provider="vast", gpu=found.get("gpu_name"), driver=found.get("driver_version"),
                     host=market.host_text(found))
    image = ctx.config.get("image", "ubuntu:26.04")
    label = f"{label_prefix()}{ctx.job}"
    ctx.progress("create", msg=market.describe(found))
    created = vast.call("PUT", f"/asks/{offer}/", {"image": image, "disk": disk or b.disk_gb,
                                                   "runtype": "ssh_direc ssh_proxy", "cancel_unavail": True,
                                                   "label": label, "env": {"NVIDIA_DRIVER_CAPABILITIES": "all"}},
                        purpose="rent")
    instance = created.get("new_contract")
    if not instance:
        raise EnvironmentProblem(f"Vast did not create an instance: {created}")
    start = time.time()
    deadline = start + cap
    record = {"provider": "vast", "instance": str(instance), "observed": utc(start), "offer": found,
              "label": b.name, "purpose": b.purpose, "created": utc(start), "disk_gb": disk or b.disk_gb,
              "outcome": "failed", "job": ctx.job, "quote": quote.get("id"), "dph_quoted": quote.get("dph")}
    marks: dict[str, float] = {}
    out = ctx.out
    billed = price
    ctx.log.info(f"instance {instance}; destroyed by {utc(deadline)} at the latest")
    try:
        ctx.progress("running", msg="waiting for the instance")
        host = port = None
        while time.time() < deadline:
            ctx.check_cancel()
            info = vast.call("GET", f"/instances/{instance}/", purpose="status").get("instances", {}) or {}
            if info.get("dph_total"):
                billed = float(info["dph_total"])
                if billed > float(quote["dph"]) + BILLING_SLACK:
                    raise EnvironmentProblem(f"instance billed at ${billed:.4f}/h, above the approved ${quote['dph']}/h")
            status = info.get("actual_status")
            if status == "running" and info.get("ssh_host"):
                host, port = info["ssh_host"], info["ssh_port"]
                direct = (info.get("ports") or {}).get("22/tcp") or []
                if direct and info.get("public_ipaddr"):
                    host, port = info["public_ipaddr"].strip(), direct[0].get("HostPort")
                record["dph_billed"] = billed
                marks["running"] = time.time()
                break
            if status in ("exited", "offline"):
                raise EnvironmentProblem(f"instance went {status}")
            ctx.sleep(float(ctx.config.get("poll_seconds", 20)))
        if not host:
            raise EnvironmentProblem("the instance never reached running")
        vast.call("POST", f"/instances/{instance}/ssh", {"ssh_key": _public_key(ctx)}, purpose="attach")
        link = _transport(ctx, host, port)
        ctx.progress("ssh", msg=f"ssh {host}:{port}")
        for attempt in range(30):
            ctx.check_cancel()
            try:
                link.run("true", timeout=60)
                break
            except Exception as error:  # noqa: BLE001
                last = error
                ctx.sleep(float(ctx.config.get("ssh_retry_seconds", 10)))
        else:
            raise EnvironmentProblem(f"SSH never answered: {last}")
        marks["ssh"] = time.time()
        link.run(remote.deadman(int(deadline - time.time()) + 300), timeout=60)
        ctx.log.info("dead man's switch armed: the container stops itself 5 min after the cap")
        ctx.progress("upload", msg=f"{b.upload_bytes() / 1e6:.0f} MB")
        link.run("mkdir -p /root/bundle", timeout=60)
        for upload in b.uploads:
            link.put(upload.files(), upload.to, timeout=max(60, deadline - time.time()))
        boot = ctx.work / "bootstrap.sh"
        boot.write_text(bootstrap_script(b), encoding="utf-8", newline="\n")
        link.put([boot, b.script], "/root/", timeout=300)
        record["upload_bytes"] = b.upload_bytes()
        marks["uploaded"] = time.time()
        ctx.progress("bootstrap")
        booted = link.run("bash /root/bootstrap.sh 2>&1", timeout=1200)
        (out / "bootstrap.log").write_text(booted, encoding="utf-8")
        if "BOOT ready" not in booted:
            raise EnvironmentProblem("bootstrap did not finish: " + booted.strip().splitlines()[-1] if booted.strip()
                                     else "bootstrap printed nothing")
        ctx.log.info(booted.strip().splitlines()[-1])
        marks["booted"] = time.time()
        ctx.progress("work")
        args = " ".join(b.args)
        link.run(f"nohup bash /root/{b.script.name} {args} > /root/run.log 2>&1 < /dev/null &", timeout=60)
        progress = re.compile(b.progress)
        seen_lines: set[str] = set()
        while time.time() < deadline - 120:
            ctx.sleep(float(ctx.config.get("poll_seconds", 20)) * 3)
            text = link.run("tail -200 /root/run.log", timeout=60, check=False)
            for line in text.splitlines():
                if progress.search(line) and line not in seen_lines:
                    seen_lines.add(line)
                    ctx.log.info("  " + line)
            if b.done in text:
                failed = 0
                if b.failed:
                    failed = sum(1 for line in text.splitlines() if re.search(b.failed, line))
                record["outcome"] = "completed" if failed == 0 else "partial"
                break
        else:
            ctx.log.warning("cap reached before the run finished; fetching what there is")
            record["outcome"], record["failure"] = "partial", "cap reached"
            link.run(f"{b.stop}; sleep 5" + (f"; {b.partial}" if b.partial else ""), timeout=120, check=False)
        marks["worked"] = time.time()
        ctx.progress("fetch")
        fetched = link.get(b.results, out)
        marks["fetched"] = time.time()
        record["download_bytes"] = sum(p.stat().st_size for p in fetched if p.exists())
        for path in fetched:
            ctx.output(path.name, path)
    except Exception as error:
        record["outcome"] = "failed" if record["outcome"] != "partial" else "partial"
        record["failure"] = str(error)[:300]
        try:
            logs = _instance_log(vast, instance)
            if logs:
                (out / "instance.log").write_text(logs, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        ctx.progress("destroy")
        try:
            ctx.log.info(f"destroying {instance}: {vast.call('DELETE', f'/instances/{instance}/', purpose='destroy')}")
        except Exception as error:  # noqa: BLE001
            ctx.log.error(f"DESTROY FAILED for {instance}: {error}; the next start's clean-up and the dead man's "
                          "switch remain")
        end = time.time()
        steps = (("to_running_s", None, "running"), ("to_ssh_s", "running", "ssh"), ("upload_s", "ssh", "uploaded"),
                 ("bootstrap_s", "uploaded", "booted"), ("work_s", "booted", "worked"), ("fetch_s", "worked", "fetched"))
        for name, since, until in steps:
            if until in marks:
                record[name] = round(marks[until] - (marks[since] if since else start), 1)
        record.update(destroyed=utc(end), held_s=round(end - start, 1))
        (out / f"rental-{instance}.json").write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
        try:
            from wb_ledger import store
            store.add_rental(store.connect(), record)
        except Exception as error:  # noqa: BLE001
            ctx.log.error(f"rental not recorded in the ledger: {error}")
        ctx.cost(round((end - start) / 3600 * billed, 6), rental=str(instance), estimated=True,
                 held_minutes=round((end - start) / 60, 1))
        ctx.measure("held_minutes", round((end - start) / 60, 2), unit="min")
    if record["outcome"] == "partial":
        ctx.finding(f"the run on {instance} was partial: {record.get('failure') or 'a tour failed'}")
    else:
        ctx.summary(f"bundle {b.name} completed on {found.get('gpu_name')} ({instance}) in "
                    f"{record['held_s'] / 60:.1f} min")
    return {"instance": str(instance), "outcome": record["outcome"], "held_minutes": round(record["held_s"] / 60, 1),
            "offer": str(offer)}


def _instance_log(vast: Vast, instance) -> str:
    import urllib.request
    result = vast.call("PUT", f"/instances/request_logs/{instance}", {"tail": "4000"}, purpose="logs")
    url = result.get("result_url")
    if not url:
        return ""
    for _ in range(6):
        time.sleep(5)
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
    return ""


def bootstrap_script(b: bundles.Bundle) -> str:
    """Generated from image/pins.toml, the image's own pins, for exactly this bundle's needs (D21)."""
    import importlib.util
    from wb_core.service import REPO_ROOT
    path = REPO_ROOT / "image" / "pins.py"
    spec = importlib.util.spec_from_file_location("wb_image_pins", path)
    pins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pins)
    return pins.bootstrap(pins.load(REPO_ROOT / "image" / "pins.toml"), b.jdks or ["26"], b.gpu)


# ----------------------------------------------------------------------------------------------
# the GL feasibility probe (vast_probe.py probe)
PROBE_ONSTART = r"""#!/bin/bash
exec > /root/probe.log 2>&1
echo "PROBE start $(date -u +%FT%TZ)"
nvidia-smi --query-gpu=name,memory.total,driver_version,pcie.link.gen.max,clocks.max.sm,clocks.max.mem --format=csv,noheader | sed 's/^/PROBE smi /'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq mesa-utils xvfb wget libegl1 libglx0 vulkan-tools >/dev/null 2>&1
wget -q -O /tmp/vgl.deb https://github.com/VirtualGL/virtualgl/releases/download/3.1.1/virtualgl_3.1.1_amd64.deb && echo "796866b5a84dc47e57e9d45f5ea621c9dbc49a26344b89111a63d39ec8fc2184  /tmp/vgl.deb" | sha256sum -c - >/dev/null && apt-get install -y -qq /tmp/vgl.deb >/dev/null 2>&1 && echo "PROBE virtualgl installed"
Xvfb :1 -screen 0 1920x1080x24 >/dev/null 2>&1 &
sleep 3
V="/opt/VirtualGL/bin/vglrun -d egl"
DISPLAY=:1 $V glxinfo -B 2>&1 | sed 's/^/PROBE glxB /'
vulkaninfo --summary 2>&1 | grep -iE 'apiVersion|driverVersion|deviceName|deviceType|driverName' | sed 's/^ *//' | sed 's/^/PROBE vk /'
echo "PROBE done $(date -u +%FT%TZ)"
cat /root/probe.log > /proc/1/fd/1 2>/dev/null
(sleep 1200; kill -TERM 1) &
sleep 3600
"""


@_guard
def probe(ctx: Context, offer: str, max_dph: float):
    quote = ctx.quote or {}
    vast = client(ctx)
    found = market.find(vast, offer, list(ctx.config.get("cards") or []), max_dph, storage_gb=16)
    _check_offer(ctx, found, offer, max_dph, 0)
    if float(found.get("dph_total") or 0) > float(quote.get("dph", 0)) + BILLING_SLACK:
        ctx.requote(_quote_for(ctx, found, 15 * 60, 16, 0, "an OpenGL feasibility probe"), "the price rose")
    created = vast.call("PUT", f"/asks/{offer}/", {"image": "ubuntu:24.04", "disk": 16, "runtype": "ssh",
                                                   "onstart": PROBE_ONSTART, "label": f"{label_prefix()}{ctx.job}",
                                                   "env": {"NVIDIA_DRIVER_CAPABILITIES": "all"}}, purpose="probe")
    instance = created.get("new_contract")
    if not instance:
        raise EnvironmentProblem(f"not created: {created}")
    start = time.time()
    lines: list[str] = []
    try:
        deadline = start + 15 * 60
        while time.time() < deadline:
            status = vast.call("GET", f"/instances/{instance}/", purpose="status").get("instances", {}).get("actual_status")
            ctx.progress("probe", msg=f"status {status}")
            if status in ("exited", "offline", "unknown"):
                break
            if status == "running":
                text = _instance_log(vast, instance)
                if "PROBE done" in text:
                    lines = [line for line in text.splitlines() if line.startswith("PROBE")]
                    break
            ctx.sleep(30)
    finally:
        vast.call("DELETE", f"/instances/{instance}/", purpose="destroy")
        ctx.cost(round((time.time() - start) / 3600 * float(found.get("dph_total") or 0), 6), rental=str(instance),
                 estimated=True)
    report = ctx.out / "probe.txt"
    report.write_text("\n".join(lines), encoding="utf-8")
    ctx.output("probe", report)
    gl = next((line for line in lines if "OpenGL version string" in line or "core profile version" in line.lower()), "")
    if not lines:
        ctx.finding("no probe result within 15 minutes")
    elif "4.6" not in gl:
        ctx.finding(f"no OpenGL 4.6 compatibility context: {gl or 'no GL line'}")
    else:
        ctx.summary(f"OpenGL through VirtualGL works: {gl.split(':', 1)[-1].strip()}")
    return {"instance": str(instance), "lines": len(lines), "gl": gl}


# ----------------------------------------------------------------------------------------------
# bundles
def bundle_check(ctx: Context, bundle: str):
    b = _bundle(ctx, bundle)
    ctx.summary(f"bundle {b.name}: {len(b.uploads)} upload(s), {b.upload_bytes() / 1e6:.0f} MB, needs JDK "
                f"{', '.join(b.jdks) or '26'}{' and the GPU' if b.gpu else ''}")
    return {"name": b.name, "uploads": len(b.uploads), "bytes": b.upload_bytes(), "jdks": b.jdks, "gpu": b.gpu,
            "bootstrap": bootstrap_script(b)}


def bundle_riven(ctx: Context, plan: str, lab: str, name=None):
    """Makes a riven-tours bundle (calibration 01's shape): the plan folder (plan.env, jars/, tours) plus the
    four linux-lab tars, with the run script ported from rent/run.sh."""
    plan_dir, lab_dir = Path(plan), Path(lab)
    if not (plan_dir / "plan.env").is_file():
        raise ParamError(f"{plan} has no plan.env")
    tars = ["riven.tar", "cubature.tar", "pins.tar", "cache.tar"]
    missing = [t for t in tars if not (lab_dir / t).is_file()]
    if missing:
        raise ParamError(f"{lab} lacks {', '.join(missing)} (make them with linux-lab/pack.sh)")
    from wb_core.module import sha256_file
    name = name or plan_dir.name
    lines = [f'name = "{name}"', 'purpose = "calibration"', "", "[needs]", 'jdk = ["26"]', "gpu = true",
             "disk_gb = 32", ""]
    for tar in tars:
        lines += ["[[upload]]", f"path = '{ctx.host_path(lab_dir / tar)}'", f'sha256 = "{sha256_file(lab_dir / tar)}"', ""]
    lines += ["[[upload]]", f"path = '{ctx.host_path(plan_dir)}'", "contents = true", 'exclude = ["out", "windows*"]', "",
              "[run]", 'script = "run.sh"', 'args = ["/root/bundle"]', 'progress = "^RUN "', 'done = "RUN done"',
              "failed = '^RUN tour .* exit [^0]'", 'results = ["/root/bundle/results.tar", "/root/run.log"]',
              'stop = "pkill -f runtime.py"', 'partial = "cd /root/bundle && tar -cf results.tar results"', ""]
    (ctx.out / "bundle.toml").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    template = Path(__file__).parent / "templates" / "riven-tours" / "run.sh"
    (ctx.out / "run.sh").write_text(template.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    ctx.output("bundle", ctx.out)
    ctx.summary(f"bundle {name} written")
    return {"bundle": ctx.host_path(ctx.out)}


# ----------------------------------------------------------------------------------------------
# startup: rentals no running job holds are destroyed before new work (D10)
def reconcile(ctx: Context):
    if not ctx.spec.get("secrets", {}).get("vast_api_key") or not Path(ctx.spec["secrets"]["vast_api_key"]).is_file():
        ctx.summary("no Vast key mounted; nothing to clean up")
        return {}
    vast = client(ctx)
    try:
        listed = vast.call("GET", "/instances/?owner=me", purpose="reconcile").get("instances", [])
    except VastError as error:
        ctx.summary(f"could not list instances: {error}")
        return {}
    destroyed, mine = [], label_prefix()
    for item in listed:
        label = str(item.get("label") or "")
        if label.startswith(mine):
            try:
                vast.call("DELETE", f"/instances/{item['id']}/", purpose="reconcile")
                destroyed.append(str(item["id"]))
                ctx.log.warning(f"destroyed instance {item['id']} ({label}): no running job holds it")
            except VastError as error:
                ctx.log.error(f"could not destroy {item['id']}: {error}")
    ctx.summary(f"{len(destroyed)} orphaned rental(s) of this installation destroyed" if destroyed
                else "no orphaned rentals of this installation")
    return {"destroyed": destroyed}

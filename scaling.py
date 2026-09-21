"""
scaling.py
Scale this worker out while there is a car to scrape and back in when idle.

App Platform bills instance-seconds, so eight 1 GB instances for the ~20 min a
car takes cost a few cents, while the idle baseline stays one instance. The
worker does the scaling itself (rather than the backend) so it self-heals: an
idle fleet always shrinks back even if the backend restarted.

Needs DO_API_TOKEN and DO_APP_ID. AUTOSCALE=0 disables it entirely.

Safety limits — this code can spend money, so every path is capped:
  * HARD_MAX_INSTANCES (12) can't be raised by env; PARALLEL_INSTANCES is clamped to it.
  * Only DO_APP_ID's worker named DO_WORKER_NAME is ever touched, and only if it
    is on the expected instance size (EXPECTED_SIZE_SLUG). Anything unexpected
    in the spec → no call is made and the worker just runs single-instance.
  * A fleet that has been scaled out longer than MAX_SCALED_OUT_MINUTES (default
    120) is forced back to 1 even if work remains (a stuck car must not burn
    instance-hours all night). The scale-out timestamp is persisted in the DB so
    the cap survives restarts and is shared by all instances.
  * At most MAX_SCALE_OUTS_PER_DAY (default 12) scale-outs in a rolling 24 h.
  * Two spec updates are never issued closer than 90 s apart, and after every
    update the live spec is re-read to confirm the count actually applied.
"""

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

AUTOSCALE = os.environ.get("AUTOSCALE", "1") not in ("0", "false", "False", "")
DO_API_TOKEN = os.environ.get("DO_API_TOKEN", "")
DO_APP_ID = os.environ.get("DO_APP_ID", "")
WORKER_NAME = os.environ.get("DO_WORKER_NAME", "scraper-1")
EXPECTED_SIZE_SLUG = os.environ.get("DO_EXPECTED_SIZE_SLUG", "apps-s-1vcpu-1gb")

HARD_MAX_INSTANCES = 12
PARALLEL_INSTANCES = max(1, min(int(os.environ.get("PARALLEL_INSTANCES", "8")), HARD_MAX_INSTANCES))
MAX_SCALED_OUT_MINUTES = int(os.environ.get("MAX_SCALED_OUT_MINUTES", "120"))
MAX_SCALE_OUTS_PER_DAY = int(os.environ.get("MAX_SCALE_OUTS_PER_DAY", "12"))
# Don't shrink on the first empty poll: a customer may press "yes" on the next car.
SCALE_IN_AFTER_IDLE_S = int(os.environ.get("SCALE_IN_AFTER_IDLE_S", "180"))
_MIN_GAP_S = 90

_STATE_KEY = "_autoscale_state"   # row in scraped_files: {"scaled_out_at": iso|null, "scale_outs": [iso, ...]}

_idle_since = None
_last_update = 0.0
_cached_count = None
_cached_at = 0.0
_disabled_reason = None


def enabled() -> bool:
    return AUTOSCALE and bool(DO_API_TOKEN and DO_APP_ID) and _disabled_reason is None


def _disable(reason: str):
    global _disabled_reason
    _disabled_reason = reason
    logger.error(f"Autoscale DISABLED for this process: {reason}")


# ── persisted state (shared by all instances) ───────────────────────────────

def _load_state() -> dict:
    from storage.db import get_file_content
    try:
        raw = get_file_content(_STATE_KEY)
        return json.loads(raw) if raw else {}
    except Exception as e:
        logger.warning(f"autoscale state read failed: {e}")
        return {}


def _save_state(state: dict):
    from storage.db import save_with_lock
    save_with_lock(_STATE_KEY, json.dumps(state))


def _now():
    return datetime.now(timezone.utc)


# ── DO API ──────────────────────────────────────────────────────────────────

def _request(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.digitalocean.com/v2{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {DO_API_TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


_deploy_in_progress = False


def _get_spec():
    global _deploy_in_progress
    app = _request("GET", f"/apps/{DO_APP_ID}")["app"]
    _deploy_in_progress = bool(app.get("in_progress_deployment"))
    return app["spec"]


def _find_worker(spec):
    """Return the one worker we manage, or disable autoscale if the spec looks wrong."""
    workers = [w for w in spec.get("workers", []) if w.get("name") == WORKER_NAME]
    if len(workers) != 1:
        _disable(f"expected exactly one worker named {WORKER_NAME} in app {DO_APP_ID}, found {len(workers)}")
        return None
    w = workers[0]
    if w.get("instance_size_slug") != EXPECTED_SIZE_SLUG:
        _disable(f"worker size is {w.get('instance_size_slug')}, expected {EXPECTED_SIZE_SLUG} — refusing to scale a different size")
        return None
    # The kill switch must work even for pods still running with the old env:
    # honour the value in the LIVE spec, not the one this process booted with.
    for e in w.get("envs", []):
        if e.get("key") == "AUTOSCALE" and str(e.get("value", "1")) in ("0", "false", "False", ""):
            _disable("AUTOSCALE=0 in the live app spec")
            return None
    return w


def current_instances(max_age_s: int = 120):
    global _cached_count, _cached_at
    if _cached_count is not None and time.time() - _cached_at < max_age_s:
        return _cached_count
    w = _find_worker(_get_spec())
    if w is None:
        return 1
    _cached_count, _cached_at = int(w.get("instance_count", 1)), time.time()
    return _cached_count


def _set_instances(n: int):
    global _last_update, _cached_count, _cached_at
    n = max(1, min(int(n), HARD_MAX_INSTANCES))
    spec = _get_spec()
    w = _find_worker(spec)
    if w is None:
        return False
    w["instance_count"] = n
    _request("PUT", f"/apps/{DO_APP_ID}", {"spec": spec})
    _last_update = time.time()
    # Confirm it took; if DO reports something else, stop touching it.
    w2 = _find_worker(_get_spec())
    applied = int(w2.get("instance_count", -1)) if w2 else -1
    if applied != n:
        _disable(f"asked for {n} instances but spec now says {applied}")
        return False
    _cached_count, _cached_at = n, time.time()
    logger.info(f"Autoscale: {WORKER_NAME} → {n} instance(s)")
    return True


# ── policy ──────────────────────────────────────────────────────────────────

def _over_time_cap(state) -> bool:
    since = state.get("scaled_out_at")
    if not since:
        return False
    return _now() - datetime.fromisoformat(since) > timedelta(minutes=MAX_SCALED_OUT_MINUTES)


def _scale_outs_last_24h(state) -> int:
    cutoff = _now() - timedelta(hours=24)
    return sum(1 for ts in state.get("scale_outs", []) if datetime.fromisoformat(ts) > cutoff)


def on_work_found():
    """A car is queued: bring the fleet to PARALLEL_INSTANCES, within the caps."""
    global _idle_since
    _idle_since = None
    if not enabled():
        return
    try:
        if time.time() - _last_update < _MIN_GAP_S:
            return
        state = _load_state()
        if state.get("hold_until_idle"):
            # The time cap forced us down earlier; stay at 1 until the queue has
            # drained once (2026-09-21: without this the fleet flapped 1↔8).
            return
        if _over_time_cap(state):
            if current_instances(max_age_s=0) > 1:
                logger.warning(f"Autoscale: scaled out > {MAX_SCALED_OUT_MINUTES} min — forcing back to 1 and holding until idle")
                if _set_instances(1):
                    state["scaled_out_at"] = None
                    state["hold_until_idle"] = True
                    _save_state(state)
            return
        n = current_instances(max_age_s=0)
        if _deploy_in_progress:
            return  # never stack spec updates on a running deployment
        if n >= PARALLEL_INSTANCES:
            return
        if _scale_outs_last_24h(state) >= MAX_SCALE_OUTS_PER_DAY:
            logger.warning(f"Autoscale: {MAX_SCALE_OUTS_PER_DAY} scale-outs in 24h reached — staying at {n}")
            return
        if _set_instances(PARALLEL_INSTANCES):
            state["scaled_out_at"] = _now().isoformat()
            state["scale_outs"] = [ts for ts in state.get("scale_outs", [])
                                   if datetime.fromisoformat(ts) > _now() - timedelta(hours=24)]
            state["scale_outs"].append(_now().isoformat())
            _save_state(state)
    except Exception as e:
        logger.warning(f"Autoscale (out) failed: {e}")


def on_idle():
    """Queue empty: after a grace period, shrink back to one instance."""
    global _idle_since
    if not enabled():
        return
    now = time.time()
    if _idle_since is None:
        _idle_since = now
        return
    if now - _idle_since < SCALE_IN_AFTER_IDLE_S or now - _last_update < _MIN_GAP_S:
        return
    try:
        n = current_instances(max_age_s=0)
        if n > 1:
            if _deploy_in_progress:
                return
            if _set_instances(1):
                state = _load_state()
                state["scaled_out_at"] = None
                state["hold_until_idle"] = False
                _save_state(state)
        else:
            state = _load_state()
            if state.get("hold_until_idle") or state.get("scaled_out_at"):
                state["hold_until_idle"] = False
                state["scaled_out_at"] = None
                _save_state(state)
    except Exception as e:
        logger.warning(f"Autoscale (in) failed: {e}")


def watchdog():
    """Called on every loop iteration, busy or idle: enforce the time cap."""
    if not enabled():
        return
    try:
        state = _load_state()
        if _over_time_cap(state) and time.time() - _last_update >= _MIN_GAP_S:
            if current_instances(max_age_s=0) > 1:
                logger.warning(f"Autoscale watchdog: > {MAX_SCALED_OUT_MINUTES} min scaled out — forcing back to 1")
                if _set_instances(1):
                    state["scaled_out_at"] = None
                    state["hold_until_idle"] = True
                    _save_state(state)
    except Exception as e:
        logger.warning(f"Autoscale watchdog failed: {e}")

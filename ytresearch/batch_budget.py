"""Phase 5C: transactional model-token/API-equivalent cost guard and global rate stop.

ONLY applies to the Phase 5B SYNTHETIC_DRYRUN_ONLY queue. No model call,
media read, network action or automatic retry occurs here.

Balances include RESERVED worst-case tokens/cost and SETTLED user-supplied
synthetic usage. Rates are SNAPSHOTTED from the configured local pricing
table; API-equivalent estimates are NOT AGY account billing/quota consumption.
All operations using this guard run within the queue's SQLite BEGIN IMMEDIATE.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Mapping, Optional

from .multimodal import load_pricing_config
from .storage import json_text, read_json


MODEL_TOKEN_KEYS = ("uncached_input_tokens", "cache_read_tokens", "output_tokens")
RATE_KEYS = ("input_per_1m_usd", "cached_input_per_1m_usd", "output_per_1m_usd")
MONEY_QUANTUM = Decimal("0.000001")
MAX_TOKENS = 1_000_000_000
MAX_USD = Decimal("1000000")
RATE_ERRORS = frozenset({"429", "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED"})
BUDGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
GUARD_STATE = 1


class BatchBudgetError(ValueError):
    """Fail-closed, safe-to-print budget or rate-limit error."""


def _money(raw: Any, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        raise BatchBudgetError("BUDGET_INVALID_MONEY:" + label)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise BatchBudgetError("BUDGET_INVALID_MONEY:" + label) from exc
    if not value.is_finite() or value < 0 or value > MAX_USD or (positive and not value):
        raise BatchBudgetError("BUDGET_INVALID_MONEY:" + label)
    return value


def _count(value: Any, label: str, *, minimum: int = 0, maximum: int = MAX_TOKENS) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BatchBudgetError("BUDGET_INVALID_INTEGER:" + label)
    return value


def _format_usd(amount: Decimal) -> str:
    # Always round UP to avoid permitting requests due to under-estimation.
    return str(amount.quantize(MONEY_QUANTUM, rounding=ROUND_UP))


def _timestamp(t: datetime) -> str:
    if t.tzinfo is None:
        raise BatchBudgetError("BUDGET_TIMEZONE_REQUIRED")
    return t.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            raise ValueError("timezone")
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise BatchBudgetError("BUDGET_STORED_TIMESTAMP_INVALID") from exc


def _model_cost(tokens: Mapping[str, int], rates: Mapping[str, Any],
                unit: int) -> Decimal:
    return (
        Decimal(tokens["uncached_input_tokens"]) * Decimal(str(rates["input_per_1m_usd"]))
        + Decimal(tokens["cache_read_tokens"]) * Decimal(str(rates["cached_input_per_1m_usd"]))
        + Decimal(tokens["output_tokens"]) * Decimal(str(rates["output_per_1m_usd"]))
    ) / Decimal(unit)


def _tokens(row: Mapping[str, Any]) -> Dict[str, int]:
    if not isinstance(row, dict) or set(row) != set(MODEL_TOKEN_KEYS):
        raise BatchBudgetError("BUDGET_USAGE_TOKEN_FIELDS_INVALID")
    return {k: _count(row[k], k) for k in MODEL_TOKEN_KEYS}


def _total_tokens(row: Mapping[str, int]) -> int:
    return sum(row[k] for k in MODEL_TOKEN_KEYS)


def _has_tables(db: sqlite3.Connection) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='budget_policies'"
    ).fetchone() is not None


def _schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS budget_policies (
      plan_sha TEXT PRIMARY KEY,
      budget_id TEXT NOT NULL,
      policy_sha256 TEXT NOT NULL,
      frozen_policy_json TEXT NOT NULL,
      created_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS budget_reservations (
      plan_sha TEXT NOT NULL,
      budget_id TEXT NOT NULL,
      video_id TEXT NOT NULL,
      attempt INTEGER NOT NULL,
      model TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('RESERVED','SETTLED')),
      reserved_tokens_json TEXT NOT NULL,
      reserved_cost_usd TEXT NOT NULL,
      settled_tokens_json TEXT,
      settled_cost_usd TEXT,
      usage_origin TEXT,
      created_utc TEXT NOT NULL,
      updated_utc TEXT NOT NULL,
      PRIMARY KEY(plan_sha,video_id,attempt)
    );
    CREATE INDEX IF NOT EXISTS budget_reservations_budget
       ON budget_reservations(budget_id,model,status);
    CREATE TABLE IF NOT EXISTS global_rate_events (
      plan_sha TEXT NOT NULL,
      video_id TEXT NOT NULL,
      attempt INTEGER NOT NULL,
      event_utc TEXT NOT NULL,
      PRIMARY KEY(plan_sha,video_id,attempt)
    );
    CREATE TABLE IF NOT EXISTS global_rate_state (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
      paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
      reason_code TEXT,
      retry_not_before_utc TEXT,
      updated_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS global_rate_actions (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,
      action TEXT NOT NULL,
      reason_code TEXT NOT NULL,
      actor TEXT NOT NULL,
      rationale TEXT NOT NULL,
      event_utc TEXT NOT NULL,
      retry_not_before_utc TEXT
    );
    CREATE TABLE IF NOT EXISTS budget_reconciliation_actions (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,
      plan_sha TEXT NOT NULL,
      video_id TEXT NOT NULL,
      attempt INTEGER NOT NULL,
      actor TEXT NOT NULL,
      rationale TEXT NOT NULL,
      reported_usage_sha256 TEXT NOT NULL,
      event_utc TEXT NOT NULL
    );
    """)


def _frozen_policy(root: Path, raw: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise BatchBudgetError("BUDGET_POLICY_SCHEMA_INVALID")
    if raw.get("mode") != "SYNTHETIC_DRYRUN_ONLY":
        raise BatchBudgetError("BUDGET_POLICY_LIVE_MODE_FORBIDDEN")
    budget_id = raw.get("budget_id")
    if not isinstance(budget_id, str) or not BUDGET_ID_RE.fullmatch(budget_id):
        raise BatchBudgetError("BUDGET_ID_INVALID")
    global_policy = raw.get("global")
    models = raw.get("models")
    rate = raw.get("rate_limit")
    if not isinstance(global_policy, dict) or not isinstance(models, dict) or not models:
        raise BatchBudgetError("BUDGET_GLOBAL_OR_MODELS_MISSING")
    if not isinstance(rate, dict):
        raise BatchBudgetError("BUDGET_RATE_POLICY_MISSING")
    global_limits = {
        "max_tokens": _count(global_policy.get("max_tokens"), "global_max_tokens", minimum=1),
        "max_api_equivalent_usd": str(_money(global_policy.get("max_api_equivalent_usd"),
                                           "global_max_usd", positive=True)),
    }
    rate_limit = {
        "max_requests_per_window": _count(rate.get("max_requests_per_window"),
                                          "rate_requests", minimum=1, maximum=100),
        "window_seconds": _count(rate.get("window_seconds"), "rate_window",
                                 minimum=1, maximum=86400),
    }
    pricing = load_pricing_config(root)
    if pricing.get("currency") != "USD":
        raise BatchBudgetError("BUDGET_ONLY_USD_PRICING_SUPPORTED")
    unit = _count(pricing.get("unit_tokens"), "pricing_unit", minimum=1)
    rates_map = pricing.get("models", {})
    if not isinstance(rates_map, dict):
        raise BatchBudgetError("BUDGET_PRICING_MODEL_MAP_INVALID")
    freeze_models: Dict[str, Any] = {}
    for name, limit in models.items():
        if not isinstance(name, str) or not name or name not in rates_map:
            raise BatchBudgetError("BUDGET_UNKNOWN_EXACT_MODEL")
        if not isinstance(limit, dict):
            raise BatchBudgetError("BUDGET_MODEL_LIMIT_INVALID")
        max_tokens = _count(limit.get("max_tokens"), "model_max_tokens", minimum=1)
        max_usd = _money(limit.get("max_api_equivalent_usd"), "model_max_usd", positive=True)
        reserve = _tokens(limit.get("reserve", {}))
        if not _total_tokens(reserve):
            raise BatchBudgetError("BUDGET_EMPTY_RESERVATION")
        source_rates = rates_map[name]
        if not isinstance(source_rates, dict):
            raise BatchBudgetError("BUDGET_PRICE_RATES_INVALID")
        frozen_rates = {
            k: str(_money(source_rates.get(k), "pricing_rate:" + k))
            for k in RATE_KEYS
        }
        reservation_usd = _model_cost(reserve, frozen_rates, unit)
        if _total_tokens(reserve) > max_tokens or reservation_usd > max_usd:
            raise BatchBudgetError("BUDGET_REQUEST_RESERVATION_EXCEEDS_MODEL_CAP")
        freeze_models[name] = {
            "max_tokens": max_tokens,
            "max_api_equivalent_usd": str(max_usd),
            "reserve": reserve,
            "reserve_api_equivalent_usd": _format_usd(reservation_usd),
            "rates_per_unit_usd": frozen_rates,
        }
    for m in freeze_models.values():
        if (_total_tokens(m["reserve"]) > global_limits["max_tokens"] or
            Decimal(m["reserve_api_equivalent_usd"]) >
                Decimal(global_limits["max_api_equivalent_usd"])):
            raise BatchBudgetError("BUDGET_REQUEST_RESERVATION_EXCEEDS_GLOBAL_CAP")
    return {
        "schema_version": 1,
        "mode": "SYNTHETIC_DRYRUN_ONLY",
        "budget_id": budget_id,
        "global": global_limits,
        "rate_limit": rate_limit,
        "pricing": {
            "pricing_version": str(pricing.get("pricing_version", "UNKNOWN")),
            "effective_date": str(pricing.get("effective_date", "UNKNOWN")),
            "source_url": str(pricing.get("source_url", "UNKNOWN")),
            "currency": "USD",
            "unit_tokens": unit,
            "billing_interpretation": "API_EQUIVALENT_ESTIMATE_NOT_AGY_BILL",
            "agy_quota_deduction": "UNKNOWN",
        },
        "models": freeze_models,
    }


def _get_policy(db: sqlite3.Connection, plan_sha: str) -> Optional[Dict[str, Any]]:
    if not _has_tables(db):
        return None
    row = db.execute(
        "SELECT policy_sha256,frozen_policy_json FROM budget_policies WHERE plan_sha=?",
        (plan_sha,),
    ).fetchone()
    if row is None:
        return None
    raw = row["frozen_policy_json"]
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != row["policy_sha256"]:
        raise BatchBudgetError("BUDGET_POLICY_HASH_MISMATCH")
    return json.loads(raw)


def _global_state(db: sqlite3.Connection) -> Dict[str, Any]:
    if not _has_tables(db):
        return {"paused": False, "reason_code": None, "retry_not_before_utc": None}
    row = db.execute(
        "SELECT paused,reason_code,retry_not_before_utc FROM global_rate_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        return {"paused": False, "reason_code": None, "retry_not_before_utc": None}
    return {"paused": bool(row["paused"]), "reason_code": row["reason_code"],
            "retry_not_before_utc": row["retry_not_before_utc"]}


def configure_batch_budget(root: Path, plan_sha: str, policy_file: Path,
                           *, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Install immutable policy only on a not-yet-attempted dry-run plan."""
    from .batch_queue import _connect, _plan
    root = Path(root).resolve()
    _plan(root, plan_sha, check_stale=True)
    cfg = read_json(Path(policy_file))
    frozen = _frozen_policy(root, cfg)
    frozen_text = json_text(frozen)
    sha = hashlib.sha256(frozen_text.encode("utf-8")).hexdigest()
    stamp = _timestamp(now_utc or datetime.now(timezone.utc))
    with closing(_connect(root)) as db:
        # Ensure schema before transactional writes; no DDL inside BEGIN.
        _schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            exists = db.execute(
                "SELECT 1 FROM queue_plans WHERE plan_sha=?", (plan_sha,)
            ).fetchone()
            if not exists:
                raise BatchBudgetError("BUDGET_QUEUE_PLAN_MUST_BE_INITIALIZED_FIRST")
            current = db.execute(
                "SELECT policy_sha256 FROM budget_policies WHERE plan_sha=?",(plan_sha,)
            ).fetchone()
            if current is not None:
                if current["policy_sha256"] != sha:
                    raise BatchBudgetError("BUDGET_POLICY_ALREADY_CONFIGURED")
            else:
                if db.execute("SELECT 1 FROM queue_jobs WHERE plan_sha=? AND "
                              "(attempts>0 OR status='LEASED') LIMIT 1",(plan_sha,)).fetchone():
                    raise BatchBudgetError("BUDGET_RETROFIT_AFTER_ATTEMPTS_FORBIDDEN")
                previous = db.execute(
                    "SELECT frozen_policy_json FROM budget_policies WHERE budget_id=? LIMIT 1",
                    (frozen["budget_id"],),
                ).fetchone()
                if previous and previous["frozen_policy_json"] != frozen_text:
                    raise BatchBudgetError("BUDGET_GROUP_POLICY_MISMATCH")
                db.execute(
                    "INSERT INTO budget_policies(plan_sha,budget_id,policy_sha256,"
                    "frozen_policy_json,created_utc) VALUES (?,?,?,?,?)",
                    (plan_sha,frozen["budget_id"],sha,frozen_text,stamp),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return batch_budget_status(root, plan_sha, now_utc=now_utc)


def _current_load(db: sqlite3.Connection, budget_id: str) -> Dict[str, Any]:
    rows = db.execute(
        "SELECT model,status,reserved_tokens_json,reserved_cost_usd,"
        "settled_tokens_json,settled_cost_usd FROM budget_reservations WHERE budget_id=?",
        (budget_id,),
    ).fetchall()
    overall_tokens = 0
    overall_usd = Decimal("0")
    per_model: Dict[str, Dict[str, Any]] = {}
    unsettled = 0
    for row in rows:
        model = row["model"]
        model_totals = per_model.setdefault(model, {
            "tokens": 0, "cost_usd": Decimal("0"), "unsettled": 0,
        })
        if row["status"] == "RESERVED":
            used = json.loads(row["reserved_tokens_json"])
            usd = Decimal(row["reserved_cost_usd"])
            unsettled += 1
            model_totals["unsettled"] += 1
        elif row["status"] == "SETTLED":
            used = json.loads(row["settled_tokens_json"])
            usd = Decimal(row["settled_cost_usd"])
        else:
            raise BatchBudgetError("BUDGET_RESERVATION_UNKNOWN_STATUS")
        number = _total_tokens(used)
        overall_tokens += number
        overall_usd += usd
        model_totals["tokens"] += number
        model_totals["cost_usd"] += usd
    return {
        "global_tokens": overall_tokens, "global_cost_usd": overall_usd,
        "per_model": per_model, "unsettled": unsettled,
    }


def _rate_window_count(db: sqlite3.Connection, now: datetime, seconds: int) -> int:
    earliest = now - timedelta(seconds=seconds)
    rows = db.execute("SELECT event_utc FROM global_rate_events").fetchall()
    times = [_parse_time(row["event_utc"]) for row in rows]
    if any(t > now for t in times):
        raise BatchBudgetError("GLOBAL_RATE_CLOCK_REWIND")
    return sum(earliest < t <= now for t in times)


def authorize_reservation_locked(
    db: sqlite3.Connection, plan_sha: str, video_id: str,
    attempt: int, model: Optional[str], now: datetime,
) -> Optional[Dict[str, Any]]:
    """Called from batch_queue.claim_batch_job inside the SAME transaction."""
    # Global 429 blocks ALL queue plans, including legacy model-free dry-runs.
    if _global_state(db)["paused"]:
        raise BatchBudgetError("GLOBAL_RATE_LIMIT_PAUSED")
    policy = _get_policy(db, plan_sha)
    if policy is None:
        if model is not None:
            raise BatchBudgetError("BUDGET_POLICY_REQUIRED_FOR_MODEL")
        # Existing Phase 5B simulation remains backward-compatible.
        return None
    if model not in policy["models"]:
        raise BatchBudgetError("BUDGET_EXACT_MODEL_REQUIRED")
    rate = policy["rate_limit"]
    if _rate_window_count(db, now, rate["window_seconds"]) >= rate["max_requests_per_window"]:
        raise BatchBudgetError("GLOBAL_RATE_WINDOW_LIMIT")
    limit = policy["models"][model]
    usage = limit["reserve"]
    expected_tokens = _total_tokens(usage)
    expected_usd = Decimal(limit["reserve_api_equivalent_usd"])
    load = _current_load(db, policy["budget_id"])
    group = load["per_model"].get(model, {"tokens":0,"cost_usd":Decimal("0")})
    if (group["tokens"] + expected_tokens > limit["max_tokens"] or
        group["cost_usd"] + expected_usd > Decimal(limit["max_api_equivalent_usd"])):
        raise BatchBudgetError("BUDGET_MODEL_CAP_EXCEEDED")
    if (load["global_tokens"] + expected_tokens > policy["global"]["max_tokens"] or
        load["global_cost_usd"] + expected_usd >
            Decimal(policy["global"]["max_api_equivalent_usd"])):
        raise BatchBudgetError("BUDGET_GLOBAL_CAP_EXCEEDED")
    now_str = _timestamp(now)
    db.execute(
        "INSERT INTO budget_reservations(plan_sha,budget_id,video_id,attempt,model,"
        "status,reserved_tokens_json,reserved_cost_usd,created_utc,updated_utc)"
        " VALUES (?,?,?,?,?,'RESERVED',?,?,?,?)",
        (plan_sha, policy["budget_id"], video_id, attempt, model,
         json_text(usage),str(expected_usd),now_str,now_str),
    )
    db.execute(
        "INSERT INTO global_rate_events(plan_sha,video_id,attempt,event_utc) VALUES (?,?,?,?)",
        (plan_sha,video_id,attempt,now_str),
    )
    return {
        "model": model, "reservation_tokens": expected_tokens,
        "reservation_api_equivalent_usd": _format_usd(expected_usd),
        "budget_id": policy["budget_id"], "usage_provenance": "SYNTHETIC_RESERVATION",
    }


def settle_reservation_locked(
    db: sqlite3.Connection, plan_sha: str, video_id: str,
    attempt: int, usage: Optional[Dict[str, Any]], now: datetime,
    *, provenance: str = "SYNTHETIC_REPORTED_USAGE",
) -> Optional[Dict[str, Any]]:
    policy = _get_policy(db, plan_sha)
    if policy is None:
        if usage is not None:
            raise BatchBudgetError("BUDGET_USAGE_WITHOUT_POLICY")
        return None
    if usage is None:
        raise BatchBudgetError("BUDGET_USAGE_REQUIRED_BEFORE_FINISH")
    cleaned = _tokens(usage)
    row = db.execute(
        "SELECT model,status,reserved_tokens_json FROM budget_reservations "
        "WHERE plan_sha=? AND video_id=? AND attempt=?",
        (plan_sha,video_id,attempt),
    ).fetchone()
    if row is None or row["status"] != "RESERVED":
        raise BatchBudgetError("BUDGET_NO_UNSETTLED_RESERVATION")
    reserved = json.loads(row["reserved_tokens_json"])
    if any(cleaned[k] > reserved[k] for k in MODEL_TOKEN_KEYS):
        raise BatchBudgetError("BUDGET_ACTUAL_EXCEEDS_RESERVATION")
    model = row["model"]
    rates = policy["models"][model]["rates_per_unit_usd"]
    cost = _model_cost(cleaned, rates, policy["pricing"]["unit_tokens"])
    cost_str = _format_usd(cost)
    db.execute(
        "UPDATE budget_reservations SET status='SETTLED',settled_tokens_json=?,"
        "settled_cost_usd=?,usage_origin=?,updated_utc=? "
        "WHERE plan_sha=? AND video_id=? AND attempt=? AND status='RESERVED'",
        (json_text(cleaned),cost_str,provenance,_timestamp(now),plan_sha,video_id,attempt),
    )
    return {
        "model":model,
        "reported_tokens":_total_tokens(cleaned),
        "estimated_api_equivalent_usd":cost_str,
        "usage_provenance":provenance,
        "agy_actual_billing_known":False,
    }


def assert_retry_reconciled_locked(db: sqlite3.Connection, plan_sha: str,
                                   video_id: str) -> None:
    if not _has_tables(db):
        return
    row = db.execute(
        "SELECT 1 FROM budget_reservations WHERE plan_sha=? AND video_id=? "
        "AND status='RESERVED' LIMIT 1",(plan_sha,video_id),
    ).fetchone()
    if row is not None:
        raise BatchBudgetError("BUDGET_UNKNOWN_USAGE_MUST_BE_RECONCILED")


def reconcile_paused_reservation(
    root: Path, plan_sha: str, video_id: str, *,
    actor: str, reason: str, usage: Dict[str, Any],
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Manual synthetic-only accounting for a paused expired lease."""
    from .batch_queue import _connect, _plan
    _plan(root, plan_sha, check_stale=False)
    if not isinstance(actor,str) or not actor.strip() or len(str(reason).strip()) < 25:
        raise BatchBudgetError("BUDGET_MANUAL_RECONCILIATION_REASON_REQUIRED")
    now = now_utc or datetime.now(timezone.utc)
    with closing(_connect(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            q = db.execute(
                "SELECT status,attempts FROM queue_jobs WHERE plan_sha=? AND video_id=?",
                (plan_sha,video_id),
            ).fetchone()
            if q is None or q["status"] not in ("PAUSED_EXPIRED", "PAUSED_FAILURE"):
                raise BatchBudgetError("BUDGET_RECONCILIATION_REQUIRES_PAUSED_JOB")
            if _get_policy(db, plan_sha) is None:
                raise BatchBudgetError("BUDGET_NOT_CONFIGURED")
            result = settle_reservation_locked(
                db,plan_sha,video_id,q["attempts"],usage,now,
                provenance="MANUAL_SYNTHETIC_RECONCILIATION",
            )
            db.execute(
                "INSERT INTO budget_reconciliation_actions "
                "(plan_sha,video_id,attempt,actor,rationale,reported_usage_sha256,event_utc)"
                " VALUES (?,?,?,?,?,?,?)",
                (plan_sha,video_id,q["attempts"],actor.strip(),reason.strip(),
                 hashlib.sha256(json_text(_tokens(usage)).encode("utf-8")).hexdigest(),
                 _timestamp(now)),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {**result,"video_id":video_id,"actor":actor,
            "mode":"SYNTHETIC_DRYRUN_ONLY","operator_reason_recorded":True}


def batch_budget_status(root: Path, plan_sha: str,
                        *, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    from .batch_queue import _connect, _plan
    _plan(root,plan_sha,check_stale=False)
    with closing(_connect(root)) as db:
        policy = _get_policy(db,plan_sha)
        if policy is None:
            raise BatchBudgetError("BUDGET_NOT_CONFIGURED")
        load = _current_load(db,policy["budget_id"])
        state = _global_state(db)
        now = now_utc or datetime.now(timezone.utc)
        rate = policy["rate_limit"]
        window_count = _rate_window_count(db,now,rate["window_seconds"])
    models = {}
    for name, limit in policy["models"].items():
        usage = load["per_model"].get(name,{
            "tokens":0,"cost_usd":Decimal("0"),"unsettled":0,
        })
        models[name] = {
            "max_tokens":limit["max_tokens"],
            "used_and_reserved_tokens":usage["tokens"],
            "remaining_tokens":limit["max_tokens"]-usage["tokens"],
            "max_api_equivalent_usd":limit["max_api_equivalent_usd"],
            "used_and_reserved_api_equivalent_usd":_format_usd(usage["cost_usd"]),
            "unsettled_reservations":usage["unsettled"],
        }
    return {
        "mode":"SYNTHETIC_DRYRUN_ONLY",
        "plan_sha256":plan_sha,
        "budget_id":policy["budget_id"],
        "pricing":policy["pricing"],
        "global": {
            "max_tokens":policy["global"]["max_tokens"],
            "used_and_reserved_tokens":load["global_tokens"],
            "remaining_tokens":policy["global"]["max_tokens"]-load["global_tokens"],
            "max_api_equivalent_usd":policy["global"]["max_api_equivalent_usd"],
            "used_and_reserved_api_equivalent_usd":_format_usd(load["global_cost_usd"]),
            "unsettled_reservations":load["unsettled"],
        },
        "models":models,
        "rate_limit": {
            **rate,
            "requests_in_window":window_count,
            "window_available":window_count < rate["max_requests_per_window"],
            "global_paused":state["paused"],
            "pause_reason":state["reason_code"],
            "retry_not_before_utc":state["retry_not_before_utc"],
        },
        "actual_agy_invoice_observed":False,
        "live_model_calls_enabled":False,
    }



def pause_global_rate_locked(
    db: sqlite3.Connection, *, error_code: str, actor: str,
    reason: str, retry_after_seconds: int, now: datetime,
) -> None:
    """Atomic GLOBAL pause, also callable from a simulated 429 queue finish."""
    if error_code not in RATE_ERRORS:
        raise BatchBudgetError("GLOBAL_RATE_ERROR_CODE_UNSUPPORTED")
    if not isinstance(actor, str) or not actor.strip() or len(str(reason).strip()) < 25:
        raise BatchBudgetError("GLOBAL_RATE_OPERATOR_REASON_REQUIRED")
    retry_after_seconds = _count(retry_after_seconds, "retry_after", maximum=86400)
    stamp = _timestamp(now)
    not_before = _timestamp(now + timedelta(seconds=retry_after_seconds))
    prev = _global_state(db)
    if prev["paused"]:
        old = prev["retry_not_before_utc"]
        if old and _parse_time(old) > _parse_time(not_before):
            not_before = old
    db.execute(
        "INSERT INTO global_rate_state "
        "(singleton,paused,reason_code,retry_not_before_utc,updated_utc)"
        " VALUES (1,1,?,?,?) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "paused=1,reason_code=excluded.reason_code,"
        "retry_not_before_utc=excluded.retry_not_before_utc,"
        "updated_utc=excluded.updated_utc",
        (error_code,not_before,stamp),
    )
    db.execute(
        "INSERT INTO global_rate_actions "
        "(action,reason_code,actor,rationale,event_utc,retry_not_before_utc)"
        " VALUES ('PAUSE',?,?,?,?,?)",
        (error_code,actor.strip(),reason.strip(),stamp,not_before),
    )

def mark_global_rate_limited(root: Path, plan_sha: str, *, error_code: str,
                             actor: str, reason: str, retry_after_seconds: int = 0,
                             now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    from .batch_queue import _connect, _plan
    _plan(root,plan_sha,check_stale=False)
    now = now_utc or datetime.now(timezone.utc)
    with closing(_connect(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if _get_policy(db,plan_sha) is None:
                raise BatchBudgetError("BUDGET_NOT_CONFIGURED")
            pause_global_rate_locked(
                db, error_code=error_code, actor=actor, reason=reason,
                retry_after_seconds=retry_after_seconds, now=now,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return batch_budget_status(root,plan_sha,now_utc=now)


def resume_global_rate_limit(root: Path, plan_sha: str, *, actor: str,
                             reason: str, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    from .batch_queue import _connect, _plan
    _plan(root,plan_sha,check_stale=False)
    if not isinstance(actor,str) or not actor.strip() or len(str(reason).strip()) < 25:
        raise BatchBudgetError("GLOBAL_RATE_OPERATOR_REASON_REQUIRED")
    now=now_utc or datetime.now(timezone.utc)
    with closing(_connect(root)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if _get_policy(db,plan_sha) is None:
                raise BatchBudgetError("BUDGET_NOT_CONFIGURED")
            state = _global_state(db)
            if not state["paused"]:
                raise BatchBudgetError("GLOBAL_RATE_NOT_PAUSED")
            if state["retry_not_before_utc"] and (
                now < _parse_time(state["retry_not_before_utc"])
            ):
                raise BatchBudgetError("GLOBAL_RATE_COOLDOWN_ACTIVE")
            if db.execute(
                "SELECT 1 FROM budget_reservations WHERE status='RESERVED' LIMIT 1"
            ).fetchone():
                raise BatchBudgetError("GLOBAL_RATE_UNSETTLED_USAGE_REQUIRES_RECONCILIATION")
            stamp=_timestamp(now)
            db.execute(
                "UPDATE global_rate_state SET paused=0,reason_code=NULL,"
                "retry_not_before_utc=NULL,updated_utc=? WHERE singleton=1",
                (stamp,),
            )
            db.execute(
                "INSERT INTO global_rate_actions"
                "(action,reason_code,actor,rationale,event_utc,retry_not_before_utc)"
                " VALUES ('RESUME',?,?,?,?,NULL)",
                (state["reason_code"],actor.strip(),reason.strip(),stamp),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return batch_budget_status(root,plan_sha,now_utc=now)

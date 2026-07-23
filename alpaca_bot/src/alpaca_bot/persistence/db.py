"""SQLite persistence -- the source of truth (spec section 13). Excel
reporting reads from this database; it never writes decisions back into it.

Also home to the startup reconciliation check (spec section 1, rule 6):
refuse to start if the previous run left unreconciled orders or positions.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Order statuses that mean "the broker still considers this order live" --
# if any of these survive a restart, the previous run has unreconciled
# orders and the system must refuse to start until they're resolved.
OPEN_ORDER_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with open(_SCHEMA_PATH) as f:
            self._conn.executescript(f.read())
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- generic helpers ----

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, tuple(params))
        self._conn.commit()
        return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with closing(self._conn.execute(sql, tuple(params))) as cur:
            return cur.fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- assets ----

    def upsert_assets(self, records: list) -> None:
        rows = [
            (
                r.asset_id, r.symbol, r.name, r.asset_class, r.exchange, r.status,
                int(r.tradable), int(r.fractionable), int(r.marginable), int(r.shortable),
                int(r.easy_to_borrow), r.maintenance_margin_requirement, r.min_order_increment,
                r.min_trade_increment, r.price_increment, r.last_checked.isoformat(),
                r.exclusion_reason,
            )
            for r in records
        ]
        self._conn.executemany(
            """
            INSERT INTO assets (asset_id, symbol, name, asset_class, exchange, status,
                                 tradable, fractionable, marginable, shortable, easy_to_borrow,
                                 maintenance_margin_requirement, min_order_increment,
                                 min_trade_increment, price_increment, last_checked, exclusion_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                symbol=excluded.symbol, name=excluded.name, asset_class=excluded.asset_class,
                exchange=excluded.exchange, status=excluded.status, tradable=excluded.tradable,
                fractionable=excluded.fractionable, marginable=excluded.marginable,
                shortable=excluded.shortable, easy_to_borrow=excluded.easy_to_borrow,
                maintenance_margin_requirement=excluded.maintenance_margin_requirement,
                min_order_increment=excluded.min_order_increment,
                min_trade_increment=excluded.min_trade_increment,
                price_increment=excluded.price_increment, last_checked=excluded.last_checked,
                exclusion_reason=excluded.exclusion_reason
            """,
            rows,
        )
        self._conn.commit()

    # ---- regimes ----

    def record_regime(self, asset_class: str, regime: str, inputs: dict) -> None:
        self.execute(
            "INSERT INTO regimes (asset_class, ts, regime, inputs_json) VALUES (?, ?, ?, ?)",
            (asset_class, _now_iso(), regime, json.dumps(inputs, default=_json_default)),
        )

    def latest_regime(self, asset_class: str) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM regimes WHERE asset_class = ? ORDER BY ts DESC LIMIT 1",
            (asset_class,),
        )

    # ---- signals ----

    def record_signal(self, signal: dict) -> None:
        payload = {
            "signal_id": signal["signal_id"],
            "ts": signal.get("ts") or _now_iso(),
            "strategy": signal["strategy"],
            "symbol": signal["symbol"],
            "asset_class": signal["asset_class"],
            "direction": signal["direction"],
            "regime": signal.get("regime"),
            "entry": signal.get("entry"),
            "stop": signal.get("stop"),
            "target": signal.get("target"),
            "max_holding_seconds": signal.get("max_holding_seconds"),
            "feature_snapshot_json": json.dumps(signal.get("feature_snapshot", {}), default=_json_default),
            "raw_model_scores_json": json.dumps(signal.get("raw_model_scores", {}), default=_json_default),
            "calibrated_probability": signal.get("calibrated_probability"),
            "expected_value_after_costs": signal.get("expected_value_after_costs"),
            "accepted": int(signal.get("accepted", False)),
            "rejection_reasons_json": json.dumps(signal.get("rejection_reasons", []), default=_json_default),
            "model_version": signal.get("model_version"),
        }
        self._conn.execute(
            """
            INSERT OR IGNORE INTO signals (signal_id, ts, strategy, symbol, asset_class, direction, regime,
                                  entry, stop, target, max_holding_seconds, feature_snapshot_json,
                                  raw_model_scores_json, calibrated_probability,
                                  expected_value_after_costs, accepted, rejection_reasons_json,
                                  model_version)
            VALUES (:signal_id, :ts, :strategy, :symbol, :asset_class, :direction, :regime,
                    :entry, :stop, :target, :max_holding_seconds, :feature_snapshot_json,
                    :raw_model_scores_json, :calibrated_probability, :expected_value_after_costs,
                    :accepted, :rejection_reasons_json, :model_version)
            """,
            payload,
        )
        self._conn.commit()

    # ---- orders / events / fills ----

    def record_order(self, order: dict) -> None:
        now = _now_iso()
        self.execute(
            """
            INSERT INTO orders (client_order_id, broker_order_id, signal_id, symbol, asset_class,
                                 side, order_type, qty, limit_price, time_in_force, status,
                                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                broker_order_id=excluded.broker_order_id, status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                order["client_order_id"], order.get("broker_order_id"), order.get("signal_id"),
                order["symbol"], order["asset_class"], order["side"], order["order_type"],
                order.get("qty"), order.get("limit_price"), order.get("time_in_force"),
                order["status"], order.get("created_at", now), now,
            ),
        )

    def record_order_event(self, client_order_id: str, event_type: str, raw: dict | None = None) -> None:
        self.execute(
            "INSERT INTO order_events (client_order_id, ts, event_type, raw_json) VALUES (?, ?, ?, ?)",
            (client_order_id, _now_iso(), event_type, json.dumps(raw or {}, default=_json_default)),
        )

    def record_fill(self, client_order_id: str, symbol: str, side: str, qty: float, price: float) -> None:
        self.execute(
            "INSERT INTO fills (client_order_id, symbol, side, qty, price, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (client_order_id, symbol, side, qty, price, _now_iso()),
        )

    def open_orders(self) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(OPEN_ORDER_STATUSES))
        return self.query(
            f"SELECT * FROM orders WHERE status IN ({placeholders})",
            tuple(OPEN_ORDER_STATUSES),
        )

    # ---- positions ----

    def upsert_position(self, position: dict) -> None:
        self.execute(
            """
            INSERT INTO positions (symbol, asset_class, qty, avg_entry_price, current_price,
                                    market_value, unrealized_pl, unrealized_plpc, opened_at,
                                    strategy, stop, target, max_holding_deadline, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                qty=excluded.qty, avg_entry_price=excluded.avg_entry_price,
                current_price=excluded.current_price, market_value=excluded.market_value,
                unrealized_pl=excluded.unrealized_pl, unrealized_plpc=excluded.unrealized_plpc,
                strategy=excluded.strategy, stop=excluded.stop, target=excluded.target,
                max_holding_deadline=excluded.max_holding_deadline, updated_at=excluded.updated_at
            """,
            (
                position["symbol"], position["asset_class"], position["qty"],
                position["avg_entry_price"], position.get("current_price"),
                position.get("market_value"), position.get("unrealized_pl"),
                position.get("unrealized_plpc"), position.get("opened_at", _now_iso()),
                position.get("strategy"), position.get("stop"), position.get("target"),
                position.get("max_holding_deadline"), _now_iso(),
            ),
        )

    def remove_position(self, symbol: str) -> None:
        self.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))

    def open_positions(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM positions")

    # ---- pnl / risk state / errors ----

    def record_pnl_snapshot(self, snapshot: dict) -> None:
        self.execute(
            """
            INSERT INTO pnl_snapshots (ts, equity, cash, realized_pnl_today, unrealized_pnl,
                                        gross_exposure_pct, open_positions)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_iso(), snapshot["equity"], snapshot["cash"],
                snapshot.get("realized_pnl_today"), snapshot.get("unrealized_pnl"),
                snapshot.get("gross_exposure_pct"), snapshot.get("open_positions"),
            ),
        )

    def set_risk_state(self, key: str, value: Any) -> None:
        self.execute(
            """
            INSERT INTO risk_state (key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, default=_json_default), _now_iso()),
        )

    def get_risk_state(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value_json FROM risk_state WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row else default

    def record_error(self, component: str, message: str, traceback_str: str = "") -> None:
        self.execute(
            "INSERT INTO errors (ts, component, message, traceback) VALUES (?, ?, ?, ?)",
            (_now_iso(), component, message, traceback_str),
        )

    def record_reconnect(self, feed: str, reason: str = "") -> None:
        self.execute(
            "INSERT INTO reconnects (ts, feed, reason) VALUES (?, ?, ?)",
            (_now_iso(), feed, reason),
        )

    def record_signal_outcome(self, signal_id: str, won: bool) -> None:
        self.execute(
            "UPDATE signals SET outcome_label = ? WHERE signal_id = ?",
            (int(won), signal_id),
        )

    def bucket_outcomes_near_probability(
        self, bucket_key: str, target_probability: float, tolerance: float, limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Accepted signals in this bucket whose predicted probability was
        within `tolerance` of `target_probability` and whose outcome is
        now known -- the raw material for checking whether "predicted 85%"
        actually wins near 85% of the time."""
        strategy, direction, asset_class, regime = bucket_key.split("|")
        return self.query(
            """
            SELECT calibrated_probability, outcome_label FROM signals
            WHERE strategy = ? AND direction = ? AND asset_class = ? AND regime = ?
              AND accepted = 1 AND outcome_label IS NOT NULL
              AND ABS(calibrated_probability - ?) <= ?
            ORDER BY ts DESC LIMIT ?
            """,
            (strategy, direction, asset_class, regime, target_probability, tolerance, limit),
        )

    # ---- calibration buckets ----

    def get_calibration_bucket(self, bucket_key: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM calibration_buckets WHERE bucket_key = ?", (bucket_key,))

    def upsert_calibration_bucket(self, bucket_key: str, n_examples: int, model_version: str | None = None,
                                    calibration: dict | None = None, disabled: bool = False,
                                    disabled_reason: str | None = None) -> None:
        self.execute(
            """
            INSERT INTO calibration_buckets (bucket_key, n_examples, model_version, calibration_json,
                                              disabled, disabled_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bucket_key) DO UPDATE SET
                n_examples=excluded.n_examples, model_version=excluded.model_version,
                calibration_json=excluded.calibration_json, disabled=excluded.disabled,
                disabled_reason=excluded.disabled_reason, updated_at=excluded.updated_at
            """,
            (bucket_key, n_examples, model_version,
             json.dumps(calibration or {}, default=_json_default), int(disabled), disabled_reason,
             _now_iso()),
        )

    # ---- startup reconciliation (spec section 1, rule 6) ----

    def has_unreconciled_state(self) -> tuple[bool, str]:
        """True + reason if the previous run left open orders or tracked
        positions that haven't been reconciled against the broker yet.
        Callers (main.py) must reconcile against broker.get_all_positions()/
        get_open_orders() before clearing this, not just ignore it."""
        open_orders = self.open_orders()
        if open_orders:
            return True, f"{len(open_orders)} order(s) still in an open broker status from a prior run"
        pending_reconciliation = self.get_risk_state("pending_reconciliation", False)
        if pending_reconciliation:
            return True, "prior run exited without completing position reconciliation"
        return False, ""

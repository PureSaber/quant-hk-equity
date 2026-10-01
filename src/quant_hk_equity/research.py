"""Lagged fixed-candidate study, exact cash accounting and immutable artifacts."""

from __future__ import annotations

import html
import importlib.metadata
import json
import subprocess
from dataclasses import replace
from datetime import UTC, date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from quant_data_kit import AssetClass, FixedPoint, InstrumentSpec
from quant_data_kit.financial.actions import ActionTerms
from quant_data_kit.financial.calendars import CalendarBook, PurposeCalendar
from quant_data_kit.financial.common import utc
from quant_data_kit.financial.lifecycle import select_universe
from quant_data_kit.financial.status import permission_asof
from quant_data_kit.hong_kong import hk_symbol, load_hk_snapshot, sha256
from quant_execution.contracts import Side
from quant_execution.hong_kong import HKDailyExecution, HKFeeSchedule
from quant_execution.schemas import execution_payload
from quant_factors import compute_factors
from quant_lab.contracts import write_standard_run
from quant_lab.research_v2 import clean_git_commit, write_exploratory_run_v2

CANDIDATES = {"momentum_20d", "low_volatility_20d"}
LIMITATIONS = [
    "历史价格收益，不含分红、供股、拆并股调整；不是股东总收益。",
    "当前观察名单存在幸存者与选择偏差；整手数按当前资料作固定情景，非历史PIT主表。",
    "缺少完整停牌、退市和公司行动历史；输入缺失会终止研究，不自动补价。",
    "交收日暂用XHKG交易日代理，T+2收盘释放卖出款；未经CCASS交收日历认证。",
    "日频开盘参考价加成本情景，不模拟竞价成交、盘口、报价档位和VCM。",
    "日线按当日收盘后可得的研究假设使用；当前下载的数据不是历史发布快照。",
    "先按训练期选择固定候选，再评价留出期；这是回顾性时间切分，不是前瞻预注册。",
]


def limitations(config):
    if config.get("schema") != "quant-hk-study/v2":
        return list(LIMITATIONS)
    return [
        "按输入的拆股和分红权益构造信号总收益；现金到账另记，复杂行动不进入价格信号研究。",
        "证券池受已知生命周期约束，但候选名单和整手数仍是固定情景，非完整历史市场。",
        "未知、过期或冲突交易状态阻断下单；数据完整性仍需独立证据。",
        "交收使用版本化 settlement 日历，不推断其实际CCASS认证或覆盖完整性。",
        *LIMITATIONS[4:],
    ]


def fp(value, scale=6):
    # Vendor decimal serialization has floating-point tails; price precision is
    # recorded to six decimals, never rounded to an invented exchange tick.
    return FixedPoint.from_decimal(Decimal(str(value)), scale, rounding="ROUND_HALF_UP")


def _action_ready_at(action: ActionTerms) -> pd.Timestamp:
    return max(utc(action.effective_at), utc(action.available_at))


def _dividend_key(action: ActionTerms) -> tuple[str, str, str] | None:
    if not action.kind.startswith("dividend_"):
        return None
    return (
        action.instrument_id,
        action.currency,
        str(pd.Timestamp(action.entitlement_date).date()),
    )


def _apply_due_actions(account, actions, processed, skipped_entitlements, *, at, opening) -> None:
    """Apply known actions chronologically while preserving dividend phase dependency."""
    due = [
        action
        for action in actions
        if action.event_id not in processed and _action_ready_at(action) <= pd.Timestamp(at)
    ]
    while due:
        progressed = False
        for action in due:
            key = _dividend_key(action)
            if utc(action.effective_at) < pd.Timestamp(opening):
                processed.add(action.event_id)
                if action.kind == "dividend_entitlement":
                    skipped_entitlements.add(key)
                progressed = True
                continue
            if action.kind == "dividend_payment":
                entitlements = [
                    candidate
                    for candidate in actions
                    if candidate.kind == "dividend_entitlement" and _dividend_key(candidate) == key
                ]
                if entitlements and any(x.event_id not in processed for x in entitlements):
                    continue
                if key in skipped_entitlements:
                    processed.add(action.event_id)
                    progressed = True
                    continue
            account.ledger.apply_corporate_action(action, at=at)
            processed.add(action.event_id)
            progressed = True
        if not progressed:
            break
        due = [action for action in due if action.event_id not in processed]


def validate_config(config: dict) -> dict:
    if config.get("schema") not in {"quant-hk-study/v1", "quant-hk-study/v2"}:
        raise ValueError("Unsupported study schema")
    if config.get("universe_scope") != "current_watchlist_not_historical_universe":
        raise ValueError("Only explicit watchlist studies are supported in v1")
    if config.get("instrument_rules_scope") != "constant_board_lot_scenario_not_pit":
        raise ValueError("v1 requires an explicitly declared constant-rule scenario")
    if config.get("schema") == "quant-hk-study/v2":
        if config.get("settlement_calendar_scope") != "evidenced_purpose_calendar":
            raise ValueError("v2 requires an evidenced settlement-purpose calendar")
        foundations = config.get("financial", {})
        if not {"calendars", "settlement_calendar_id", "status", "lifecycle", "actions"} <= set(
            foundations
        ):
            raise ValueError("v2 requires explicit financial evidence, including empty action list")
    elif (
        config.get("settlement_calendar_scope") != "XHKG_sessions_proxy_not_verified_CCASS_calendar"
    ):
        raise ValueError("v1 requires the explicitly labelled settlement-calendar proxy")
    points = [
        pd.Timestamp(config[k]) for k in ("data_start", "train_start", "test_start", "data_end")
    ]
    if not points[0] < points[1] < points[2] <= points[3]:
        raise ValueError("Data, training and test dates must be ordered without overlap")
    names = config["candidates"]
    if not names or len(names) != len(set(names)) or not set(names) <= CANDIDATES:
        raise ValueError("Candidates must be unique supported strategies")
    for key in ("top_n", "rebalance_sessions"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("initial_cash", "invested_fraction", "min_price", "min_median_turnover_hkd"):
        value = Decimal(str(config[key]))
        if not value.is_finite() or value <= 0:
            raise ValueError(f"Invalid {key}")
    if Decimal(config["invested_fraction"]) > 1:
        raise ValueError("Invested fraction must not exceed one")
    symbols = [hk_symbol(x["symbol"]) for x in config["instruments"]]
    if len(set(symbols)) != len(symbols) or len(symbols) < config["top_n"]:
        raise ValueError("Instrument list must be unique and cover top_n")
    for item in config["instruments"]:
        if type(item["lot_size"]) is not int or item["lot_size"] <= 0:
            raise ValueError("Invalid board lot")
        if type(item["stamp_exempt"]) is not bool:
            raise ValueError("Explicit boolean stamp exemption required")
    schedule = fee_schedule(config)
    if not schedule.valid_from <= points[1].date() <= points[3].date() <= schedule.valid_to:
        raise ValueError("Fee schedule must cover all evaluated sessions")
    return config


def fee_schedule(config):
    values = dict(config["fees"])
    for key, value in values.items():
        if key in {"valid_from", "valid_to"}:
            values[key] = date.fromisoformat(value)
        elif key != "source" and value is not None:
            values[key] = Decimal(value)
    return HKFeeSchedule(**values)


def prepare(bars: pd.DataFrame, calendar: pd.DataFrame, config: dict) -> pd.DataFrame:
    symbols = [x["symbol"] for x in config["instruments"]]
    if set(bars.symbol) != set(symbols):
        raise ValueError("Snapshot and configured universe differ")
    dates = calendar.loc[calendar.date.between(config["data_start"], config["data_end"]), "date"]
    expected = pd.MultiIndex.from_product([dates, symbols], names=["date", "symbol"])
    indexed = bars.set_index(["date", "symbol"])
    if not indexed.index.is_unique:
        raise ValueError("Duplicate daily bars")
    missing = expected.difference(indexed.index)
    unexpected = indexed.index.difference(expected)
    if len(missing) or len(unexpected):
        raise ValueError(
            f"Calendar coverage mismatch: missing={len(missing)}, unexpected={len(unexpected)}"
        )
    if (bars.volume <= 0).any():
        raise ValueError("Non-trading bars need explicit historical status evidence")
    factor_input = bars
    if config.get("financial"):
        from quant_data_kit.financial.returns import total_return_panel

        factor_input = total_return_panel(
            bars, config["financial"]["actions"], timezone="Asia/Hong_Kong"
        )
        factor_input["raw_close"] = factor_input.close
        factor_input["close"] = factor_input.return_close
    result = compute_factors(factor_input, ["momentum_20d", "volatility_20d"])
    if "raw_close" in result:
        result["close"] = result.pop("raw_close")
    result = result.sort_values(["symbol", "date"])
    result["turnover_20d"] = result.groupby("symbol").amount.transform(
        lambda x: x.rolling(20, min_periods=20).median()
    )
    # Shift against full, gap-checked session grid: all signals precede the execution day.
    result["signal_date"] = result.groupby("symbol").date.shift(1)
    for col in ("momentum_20d", "volatility_20d", "turnover_20d", "close"):
        result[f"prior_{col}"] = result.groupby("symbol")[col].shift(1)
    return result.sort_values(["date", "symbol"]).reset_index(drop=True)


def select(frame: pd.DataFrame, strategy: str, config: dict) -> list[str]:
    eligible = frame[
        (frame.prior_close >= config["min_price"])
        & (frame.prior_turnover_20d >= config["min_median_turnover_hkd"])
        & frame.prior_momentum_20d.notna()
        & frame.prior_volatility_20d.notna()
    ].copy()
    if strategy == "equal_weight":
        return sorted(eligible.symbol)
    score = "prior_momentum_20d" if strategy == "momentum_20d" else "prior_volatility_20d"
    return (
        eligible.sort_values([score, "symbol"], ascending=[strategy != "momentum_20d", True])
        .head(config["top_n"])
        .symbol.tolist()
    )


def metrics(returns: pd.DataFrame, initial_cash: float) -> dict:
    r = returns.net_return.astype(float)
    path = np.r_[initial_cash, returns.nav.to_numpy()]
    drawdown = path / np.maximum.accumulate(path) - 1
    std = float(r.std(ddof=1))
    return {
        "sessions": len(r),
        "total_return": float(path[-1] / initial_cash - 1),
        "annualized_return": float((path[-1] / initial_cash) ** (252 / len(r)) - 1),
        "sharpe_zero_rf": float(r.mean() / std * np.sqrt(252)) if std > 0 else 0.0,
        "max_drawdown": float(drawdown.min()),
        "ending_nav_hkd": float(path[-1]),
    }


def simulate(prepared, calendar, config, *, strategy, start, end, cost_multiplier=1):
    sessions = calendar[calendar.date.between(start, end)]
    if sessions.empty:
        raise ValueError("Empty evaluation interval")
    opening = sessions.iloc[0].open.to_pydatetime()
    assumption_start = pd.Timestamp(config["data_start"], tz="UTC").to_pydatetime()
    specs = {
        item["symbol"]: InstrumentSpec(
            instrument_id=item["symbol"],
            asset_class=AssetClass.EQUITY,
            product_type="hk-cash-equity",
            venue="XHKG",
            native_symbol=item["symbol"],
            settlement_currency="HKD",
            base_currency="HKD",
            quote_currency="HKD",
            price_tick=fp("0.000001"),
            quantity_step=FixedPoint(1, 0),
            contract_multiplier=FixedPoint(1, 0),
            calendar_id="XHKG",
            effective_from=assumption_start,
            available_at=assumption_start,
            metadata={
                "lot_size": str(item["lot_size"]),
                "stamp_exempt": str(item["stamp_exempt"]).lower(),
                "rules_scope": config["instrument_rules_scope"],
            },
        )
        for item in config["instruments"]
    }
    fees = fee_schedule(config)
    if cost_multiplier != 1:
        numeric = {
            k: getattr(fees, k) * cost_multiplier
            for k in fees.__dataclass_fields__
            if isinstance(getattr(fees, k), Decimal)
        }
        fees = replace(fees, **numeric)
    financial = config.get("financial")
    purpose_book = (
        CalendarBook([PurposeCalendar(**x) for x in financial["calendars"]]) if financial else None
    )
    action_phase = {"dividend_entitlement": 0, "dividend_payment": 2}
    actions = (
        sorted(
            (ActionTerms(**x) for x in financial["actions"]),
            key=lambda action: (
                _action_ready_at(action),
                utc(action.effective_at),
                action_phase.get(action.kind, 1),
                action.event_id,
            ),
        )
        if financial
        else []
    )
    processed_actions = set()
    skipped_entitlements = set()
    if any(
        action.kind != "dividend_payment" and utc(action.available_at) > utc(action.effective_at)
        for action in actions
    ):
        raise ValueError(
            "late-known non-payment action requires historical position replay; "
            "current holdings cannot reconstruct past entitlements"
        )
    if any(
        x.kind in {"merger", "spin_off", "rights_distribution", "rights_exercise"} for x in actions
    ):
        raise ValueError(
            "complex actions require the shared action replay API; HK price-signal study blocks them"
        )
    account = HKDailyExecution(
        specs,
        initial_cash=fp(config["initial_cash"], 2),
        opened_at=opening,
        fees=fees,
        settlement_days=calendar.date.dt.date.tolist(),
    )
    rows, orders, costs, positions, signals = [], [], [], [], []
    previous_nav = Decimal(config["initial_cash"])
    for day_index, session in enumerate(sessions.itertuples()):
        frame = prepared[prepared.date.eq(session.date)].set_index("symbol", drop=False)
        at = session.open.to_pydatetime()
        if purpose_book:
            settlement = purpose_book.asof(
                financial["settlement_calendar_id"], "settlement", at, session.date
            )
            # Refresh against that day's known calendar; never substitute trading dates.
            account.settlement_days = pd.DatetimeIndex(settlement.open_days).date.tolist()
        _apply_due_actions(
            account,
            actions,
            processed_actions,
            skipped_entitlements,
            at=at,
            opening=opening,
        )
        for symbol, row in frame.iterrows():
            account.mark(symbol, fp(row.open), at)
        opening_nav = account.ledger.snapshot(at).nav.to_decimal()
        day_costs = Decimal(0)
        if day_index % config["rebalance_sessions"] == 0:
            selected = select(frame.reset_index(drop=True), strategy, config)
            if financial:
                eligible = select_universe(
                    pd.DataFrame(financial["lifecycle"]),
                    at,
                    at,
                    universe_id=financial.get("universe_id"),
                )
                selected = [symbol for symbol in selected if symbol in eligible.eligible]
            holding = account.ledger.snapshot(at).positions
            target = {}
            for symbol, row in frame.iterrows():
                budget = (
                    opening_nav * Decimal(config["invested_fraction"]) / len(selected)
                    if symbol in selected
                    else Decimal(0)
                )
                lot = int(specs[symbol].metadata["lot_size"])
                target[symbol] = int(budget / Decimal(str(row.open))) // lot * lot
                signals.append(
                    {
                        "date": str(session.date.date()),
                        "symbol": symbol,
                        "signal_date": str(row.signal_date.date()),
                        "selected": symbol in selected,
                        "target_quantity": target[symbol],
                    }
                )
            # Execute sells first, but their proceeds remain unavailable until settlement.
            for side in (Side.SELL, Side.BUY):
                for symbol in sorted(specs):
                    held = int(holding.get(symbol, FixedPoint(0, 0)).to_decimal())
                    delta = target[symbol] - held
                    if (side is Side.BUY and delta <= 0) or (side is Side.SELL and delta >= 0):
                        continue
                    row = frame.loc[symbol]
                    quantity = abs(delta)
                    if financial:
                        permission = permission_asof(
                            pd.DataFrame(financial["status"]), symbol, at, at
                        )
                        allowed = permission.buy if side is Side.BUY else permission.sell
                        if allowed != "tradable":
                            orders.append(
                                {
                                    "timestamp": at.isoformat(),
                                    "strategy": strategy,
                                    "symbol": symbol,
                                    "side": side.value,
                                    "quantity": 0,
                                    "requested_quantity": quantity,
                                    "status": f"blocked_{allowed}",
                                    "reason": permission.reason,
                                }
                            )
                            continue
                    if side is Side.BUY:
                        quantity = min(
                            quantity,
                            account.affordable_quantity(
                                symbol, fp(row.open), account.available_cash(), session.date.date()
                            ),
                        )
                    status = "filled" if quantity else "rejected_settled_cash"
                    orders.append(
                        {
                            "timestamp": at.isoformat(),
                            "strategy": strategy,
                            "symbol": symbol,
                            "side": side.value,
                            "quantity": quantity,
                            "requested_quantity": abs(delta),
                            "target_weight": (
                                float(Decimal(config["invested_fraction"]) / len(selected))
                                if symbol in selected
                                else 0
                            ),
                            "order_type": "daily_open_scenario",
                            "status": status,
                        }
                    )
                    if not quantity:
                        continue
                    _, charges = account.execute(
                        order_id=f"{strategy}:{session.date.date()}:{symbol}",
                        symbol=symbol,
                        quantity=quantity,
                        side=side,
                        price=fp(row.open),
                        at=at,
                    )
                    charge_values = {k: v.to_decimal() for k, v in charges.items()}
                    total = sum(charge_values.values(), Decimal(0))
                    day_costs += total
                    costs.append(
                        {
                            "date": str(session.date.date()),
                            "strategy": strategy,
                            "symbol": symbol,
                            **{k: float(v) for k, v in charge_values.items()},
                            "market_impact": 0,
                            "borrow_cost": 0,
                            "total_cost": float(total),
                        }
                    )
        closing = session.close.to_pydatetime()
        for symbol, row in frame.iterrows():
            account.mark(symbol, fp(row.close), closing)
        snapshot = account.ledger.snapshot(closing)
        nav = snapshot.nav.to_decimal()
        account.settle_end_of_day(session.date.date())
        rows.append(
            {
                "date": str(session.date.date()),
                "strategy": strategy,
                "gross_return": float((nav + day_costs) / previous_nav - 1),
                "net_return": float(nav / previous_nav - 1),
                "nav": float(nav),
                "benchmark_return": 0.0,
                "cash_hkd": float(account.ledger.cash_balance("HKD")),
                "available_cash_hkd": float(account.available_cash()),
                "cost_hkd": float(day_costs),
            }
        )
        for symbol, quantity in snapshot.positions.items():
            market_value = quantity.to_decimal() * Decimal(str(frame.loc[symbol].close))
            positions.append(
                {
                    "date": str(session.date.date()),
                    "strategy": strategy,
                    "symbol": symbol,
                    "quantity": int(quantity.to_decimal()),
                    "market_value": float(market_value),
                    "weight": float(market_value / nav),
                    "side": "long",
                }
            )
        previous_nav = nav
    returns = pd.DataFrame(rows)
    summary = metrics(returns, float(config["initial_cash"]))
    summary.update(
        {
            "fees_and_slippage_hkd": float(returns.cost_hkd.sum()),
            "filled_orders": sum(o["status"] == "filled" for o in orders),
            "return_basis": (
                "price_plus_evidenced_corporate_actions"
                if config["schema"] == "quant-hk-study/v2"
                else "price_only_excludes_corporate_actions"
            ),
            "investable": False,
        }
    )
    journal = [execution_payload(x) for x in account.ledger.transactions]
    return (
        {
            "returns": returns,
            "orders": pd.DataFrame(orders),
            "costs": pd.DataFrame(costs),
            "positions": pd.DataFrame(positions),
        },
        summary,
        journal,
        pd.DataFrame(signals),
    )


def _publish_hk_v2(run_dir, frames, summary, config, snapshot_sha256) -> None:
    commit = clean_git_commit(Path(__file__).resolve().parents[2])
    if commit is None:
        return
    returns = frames["returns"]
    positions = frames["positions"].copy()
    positions["instrument_id"] = positions["symbol"]
    positions["mark_price"] = positions["market_value"] / positions["quantity"].replace(0, pd.NA)
    positions = positions.dropna(subset=["mark_price"])
    write_exploratory_run_v2(
        run_dir,
        project="quant-hk-equity",
        run_id=run_dir.name,
        strategy_id=str(returns.strategy.iloc[0]),
        currency="HKD",
        code_version=commit,
        dataset_snapshots={"hk": snapshot_sha256},
        nav=returns.rename(columns={"date": "date"})[["date", "nav", "gross_return", "net_return"]],
        positions=positions[["date", "instrument_id", "quantity", "mark_price"]],
        comparability="current_watchlist_not_historical_universe",
        metrics={
            key: summary[key] for key in summary if isinstance(summary[key], (int, float, str))
        },
        config={"universe_scope": config.get("universe_scope", "current_watchlist")},
    )


def code_revision() -> str:
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return "source-without-git-metadata"
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False
    )
    commit = result.stdout.strip() if result.returncode == 0 else "uncommitted"
    return commit + ("-dirty" if status.stdout.strip() else "")


def run_study(snapshot: Path, config_path: Path, output: Path) -> dict:
    config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))
    bars, calendar, manifest = load_hk_snapshot(snapshot)
    if manifest["start"] != config["data_start"] or manifest["end"] != config["data_end"]:
        raise ValueError("Snapshot date range differs from the frozen recipe")
    if manifest["provider"] != config["provider"]:
        raise ValueError("Snapshot provider differs from the frozen recipe")
    prepared = prepare(bars, calendar, config)
    output.mkdir(parents=True, exist_ok=False)
    frozen = {
        "config": config,
        "snapshot_sha256": sha256(snapshot / "manifest.json"),
        "code_revision": code_revision(),
        "limitations": limitations(config),
        "selection": "maximum training Sharpe; alphabetical tie break",
        "created_at": pd.Timestamp.now(tz=UTC).isoformat(),
        "source_hashes": {p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
        "versions": {
            p: importlib.metadata.version(p)
            for p in (
                "pandas",
                "numpy",
                "quant-data-kit",
                "quant-execution",
                "quant-factors",
                "quant-lab",
            )
        },
        "installed_stack": {
            p: json.loads(importlib.metadata.distribution(p).read_text("direct_url.json") or "null")
            for p in ("quant-data-kit", "quant-execution", "quant-factors", "quant-lab")
        },
    }
    stack = Path(__file__).resolve().parents[2] / "stack.lock"
    if stack.exists():
        frozen["stack_lock"] = stack.read_text(encoding="utf-8")
        frozen["stack_sha256"] = sha256(stack)
    requirements = stack.with_name("requirements.lock")
    if requirements.exists():
        frozen["requirements_sha256"] = sha256(requirements)
    (output / "study.json").write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result = {
        "status": "running",
        "investable": False,
        "training": {},
        "limitations": limitations(config),
    }
    try:
        train_end = (pd.Timestamp(config["test_start"]) - pd.Timedelta(days=1)).date().isoformat()
        train_runs = {}
        for candidate in config["candidates"]:
            run = simulate(
                prepared,
                calendar,
                config,
                strategy=candidate,
                start=config["train_start"],
                end=train_end,
            )
            train_runs[candidate] = run
            result["training"][candidate] = run[1]
        selected = min(
            config["candidates"], key=lambda k: (-result["training"][k]["sharpe_zero_rf"], k)
        )
        result["selected"] = selected
        # Persist selection before evaluating holdout.
        (output / "selection.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        holdout = simulate(
            prepared,
            calendar,
            config,
            strategy=selected,
            start=config["test_start"],
            end=config["data_end"],
        )
        benchmark = simulate(
            prepared,
            calendar,
            config,
            strategy="equal_weight",
            start=config["test_start"],
            end=config["data_end"],
        )
        stress = simulate(
            prepared,
            calendar,
            config,
            strategy=selected,
            start=config["test_start"],
            end=config["data_end"],
            cost_multiplier=2,
        )
        result.update(
            {
                "holdout": holdout[1],
                "benchmark": benchmark[1],
                "double_cost": stress[1],
                "status": "complete_exploratory",
            }
        )
        all_runs = {
            **{f"train-{k}": v for k, v in train_runs.items()},
            "holdout": holdout,
            "benchmark": benchmark,
            "double-cost": stress,
        }
        for label, (frames, summary, journal, signals) in all_runs.items():
            run_dir = output / label
            if label in {"holdout", "double-cost"}:
                frames["returns"]["benchmark_return"] = benchmark[0][
                    "returns"
                ].net_return.to_numpy()
            write_standard_run(
                run_dir,
                project="quant-hk-equity",
                run_id=label,
                strategy=str(frames["returns"].strategy.iloc[0]),
                frames=frames,
                metrics=summary,
                config=config,
                code_version=frozen["code_revision"],
                dataset_snapshots={"hk": frozen["snapshot_sha256"]},
                tags={
                    "market": "XHKG",
                    "evidence": "exploratory",
                    "investable": "false",
                    "comparability": "current_watchlist_not_historical_universe",
                    "rankable": "false",
                    "cost_unit": "currency",
                },
            )
            _publish_hk_v2(run_dir, frames, summary, config, frozen["snapshot_sha256"])
            (run_dir / "ledger.json").write_text(json.dumps(journal, indent=2), encoding="utf-8")
            signals.to_csv(run_dir / "signals.csv", index=False)
        render_report(output, result, holdout[0]["returns"], benchmark[0]["returns"])
    except Exception as exc:
        result.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        (output / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
        files = {
            p.relative_to(output).as_posix(): sha256(p)
            for p in sorted(output.rglob("*"))
            if p.is_file()
        }
        (output / "checksums.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    return result


def render_report(output, result, returns, benchmark):
    labels = [
        ("留出期策略", result["holdout"]),
        ("同池等权基准", result["benchmark"]),
        ("双倍成本", result["double_cost"]),
    ]
    rows = "".join(
        f"<tr><td>{label}</td><td>{m['total_return']:.2%}</td>"
        f"<td>{m['max_drawdown']:.2%}</td><td>{m['sharpe_zero_rf']:.2f}</td>"
        f"<td>{m['fees_and_slippage_hkd']:,.2f}</td></tr>"
        for label, m in labels
    )
    risks = "".join(f"<li>{html.escape(x)}</li>" for x in result["limitations"])
    document = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>港股日频研究</title><style>body{{font:16px/1.7 system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#183344;background:#f7f9fa}}h1{{font-size:34px}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #dae2e8}}.note{{background:#fff1cf;padding:16px;border-left:5px solid #ae7100}}a{{color:#096888}}</style>
<p>PURESABER / XHKG / HKD</p><h1>港股日频基线研究</h1>
<div class="note"><b>探索性研究，不能作为可投资回测认证。</b><br>公司行动及日历口径见下方证据边界；候选名单与固定整手数情景仍有历史偏差。</div>
<p>训练期选中：{html.escape(result["selected"])}。留出期：{returns.date.iloc[0]}至{returns.date.iloc[-1]}。
所有信号取前一交易日，按下一交易日开盘参考价计入费用；同池等权基准使用同样成本和现金约束。</p>
<table><tr><th>情景</th><th>价格净收益</th><th>最大回撤</th><th>Sharpe（无风险利率0）</th><th>费用及滑点/HKD</th></tr>{rows}</table>
<h2>证据边界</h2><ul>{risks}</ul><h2>可复核文件</h2>
<p><a href="summary.json">指标与结论</a> · <a href="study.json">冻结配方</a> · <a href="selection.json">训练期选择</a> · <a href="holdout/standard/returns.csv">逐日净值</a> · <a href="holdout/signals.csv">信号时点</a> · <a href="holdout/ledger.json">平衡账本</a> · <a href="checksums.json">文件哈希</a></p></html>"""
    (output / "report.html").write_text(document, encoding="utf-8")

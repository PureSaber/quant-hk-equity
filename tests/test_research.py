import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from quant_data_kit.hong_kong import load_hk_snapshot, normalize_hk_bars, sha256

from quant_hk_equity.research import (
    metrics,
    prepare,
    run_study,
    select,
    simulate,
    validate_config,
)


@pytest.fixture
def case():
    config = json.loads((Path(__file__).parents[1] / "configs/baseline.json").read_text())
    config["provider"] = "fixture"
    days = pd.bdate_range("2025-07-02", periods=100)
    config.update(
        {
            "data_start": str(days[0].date()),
            "data_end": str(days[91].date()),
            "train_start": str(days[25].date()),
            "test_start": str(days[60].date()),
            "top_n": 1,
            "min_median_turnover_hkd": 100,
        }
    )
    config["instruments"] = config["instruments"][:2]
    calendar = pd.DataFrame(
        {
            "date": days,
            "open": days.tz_localize("UTC") + pd.Timedelta(hours=1, minutes=30),
            "close": days.tz_localize("UTC") + pd.Timedelta(hours=8),
        }
    )
    frames = []
    for idx, item in enumerate(config["instruments"]):
        close = 100 + np.arange(92) * (0.3 if idx == 0 else 0.1) + np.sin(np.arange(92)) * (idx + 1)
        frame = pd.DataFrame(
            {
                "date": days[:92],
                "open": close - 0.2,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 100000,
                "amount": 10000000,
            }
        )
        frames.append(normalize_hk_bars(frame, item["symbol"], "fixture"))
    return config, pd.concat(frames, ignore_index=True), calendar


def snapshot(tmp_path, case):
    config, bars, calendar = case
    root = tmp_path / "snapshot"
    root.mkdir()
    bars.to_parquet(root / "bars.parquet", index=False)
    calendar.to_csv(root / "calendar.csv", index=False)
    manifest = {
        "schema": "quant-hk-snapshot/v1",
        "status": "complete",
        "provider": "fixture",
        "symbols": sorted(bars.symbol.unique()),
        "corporate_actions_complete": False,
        "start": config["data_start"],
        "end": config["data_end"],
        "files": {p.name: sha256(p) for p in root.iterdir()},
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def financial_evidence(config, calendar, actions):
    known = "2025-01-01T00:00:00Z"
    symbols = [item["symbol"] for item in config["instruments"]]
    return {
        "settlement_calendar_id": "TEST-CCASS",
        "actions": actions,
        "calendars": [
            {
                "calendar_id": "TEST-CCASS",
                "purpose": "settlement",
                "version": "v1",
                "available_at": known,
                "valid_from": "2025-01-01",
                "valid_to": "2026-01-01",
                "open_days": [str(value.date()) for value in calendar.date],
                "source": "synthetic",
                "evidence_kind": "synthetic",
            }
        ],
        "lifecycle": [
            {
                "event_id": symbol,
                "instrument_id": symbol,
                "kind": "listing",
                "effective_at": known,
                "available_at": known,
                "source": "synthetic",
                "evidence_id": "fixture",
                "symbol": symbol,
                "venue": "XHKG",
                "universe_id": None,
                "successor_id": None,
            }
            for symbol in symbols
        ],
        "status": [
            {
                "instrument_id": symbol,
                "effective_from": known,
                "effective_to": "2026-01-01T00:00:00Z",
                "available_at": known,
                "buy_status": "tradable",
                "sell_status": "tradable",
                "reason": "fixture",
                "source": "synthetic",
                "evidence_id": "fixture",
            }
            for symbol in symbols
        ],
    }


def dividend_actions(
    symbol, entitlement_date, payment_date, entitlement_available, payment_available
):
    common = {
        "instrument_id": symbol,
        "currency": "HKD",
        "source": "synthetic",
        "evidence_id": "fixture",
        "cash_per_unit": "10",
        "entitlement_date": str(entitlement_date.date()),
    }
    return [
        {
            **common,
            "event_id": "payment",
            "kind": "dividend_payment",
            "effective_at": payment_date.tz_localize("UTC").isoformat(),
            "available_at": payment_available,
        },
        {
            **common,
            "event_id": "entitlement",
            "kind": "dividend_entitlement",
            "effective_at": entitlement_date.tz_localize("UTC").isoformat(),
            "available_at": entitlement_available,
        },
    ]


def test_v2_calendar_and_unknown_status_block_orders(case):
    config, bars, calendar = case
    config["schema"] = "quant-hk-study/v2"
    config["settlement_calendar_scope"] = "evidenced_purpose_calendar"
    known = "2025-01-01T00:00:00Z"
    config["financial"] = {
        "settlement_calendar_id": "TEST-CCASS",
        "actions": [],
        "calendars": [
            {
                "calendar_id": "TEST-CCASS",
                "purpose": "settlement",
                "version": "v1",
                "available_at": known,
                "valid_from": "2025-01-01",
                "valid_to": "2026-01-01",
                "open_days": [str(x.date()) for x in calendar.date],
                "source": "synthetic",
                "evidence_kind": "synthetic",
            }
        ],
        "lifecycle": [
            {
                "event_id": item["symbol"],
                "instrument_id": item["symbol"],
                "kind": "listing",
                "effective_at": known,
                "available_at": known,
                "source": "synthetic",
                "evidence_id": "fixture",
                "symbol": item["symbol"],
                "venue": "XHKG",
                "universe_id": None,
                "successor_id": None,
            }
            for item in config["instruments"]
        ],
        "status": [
            {
                "instrument_id": item["symbol"],
                "effective_from": known,
                "effective_to": "2026-01-01T00:00:00Z",
                "available_at": known,
                "buy_status": "unknown",
                "sell_status": "unknown",
                "reason": "missing_evidence",
                "source": "synthetic",
                "evidence_id": "fixture",
            }
            for item in config["instruments"]
        ],
    }
    validate_config(config)
    prepared = prepare(bars, calendar, config)
    output = simulate(
        prepared,
        calendar,
        config,
        strategy="momentum_20d",
        start=config["train_start"],
        end=config["data_end"],
    )[0]
    assert not output["orders"].empty
    assert output["orders"].status.eq("blocked_unknown").all()
    assert output["orders"].quantity.eq(0).all()
    config["financial"]["calendars"][0]["purpose"] = "trading"
    with pytest.raises(ValueError):
        simulate(
            prepared,
            calendar,
            config,
            strategy="momentum_20d",
            start=config["train_start"],
            end=config["data_end"],
        )


def test_v2_actions_wait_for_availability_and_preserve_dividend_order(case):
    config, bars, calendar = case
    config.update(
        schema="quant-hk-study/v2",
        settlement_calendar_scope="evidenced_purpose_calendar",
        top_n=2,
        invested_fraction="1",
    )
    sessions = calendar.loc[calendar.date.ge(pd.Timestamp(config["train_start"])), "date"].tolist()
    actions = dividend_actions(
        config["instruments"][0]["symbol"],
        sessions[1],
        sessions[5],
        "2025-01-01T00:00:00Z",
        sessions[6].tz_localize("UTC").isoformat(),
    )
    config["financial"] = financial_evidence(config, calendar, actions)

    _, summary, journal, _ = simulate(
        prepare(bars, calendar, config),
        calendar,
        config,
        strategy="momentum_20d",
        start=config["train_start"],
        end=str(sessions[8].date()),
    )

    action_ids = [
        row["reference_id"]
        for row in journal
        if str(row.get("reference_id", "")).startswith("financial-action:")
    ]
    assert action_ids == ["financial-action:entitlement", "financial-action:payment"]
    assert summary["return_basis"] == "price_plus_evidenced_corporate_actions"


def test_v2_late_pre_start_entitlement_does_not_grant_new_holdings(case):
    config, bars, calendar = case
    config.update(
        schema="quant-hk-study/v2",
        settlement_calendar_scope="evidenced_purpose_calendar",
        top_n=2,
        invested_fraction="1",
    )
    sessions = calendar.loc[calendar.date.ge(pd.Timestamp(config["train_start"])), "date"].tolist()
    prior_session = calendar.loc[
        calendar.date.lt(pd.Timestamp(config["train_start"])), "date"
    ].iloc[-1]
    actions = dividend_actions(
        config["instruments"][0]["symbol"],
        prior_session,
        sessions[5],
        "2025-01-01T00:00:00Z",
        sessions[5].tz_localize("UTC").isoformat(),
    )
    config["financial"] = financial_evidence(config, calendar, actions)

    with_actions = simulate(
        prepare(bars, calendar, config),
        calendar,
        config,
        strategy="equal_weight",
        start=config["train_start"],
        end=str(sessions[8].date()),
    )
    without = copy.deepcopy(config)
    without["financial"]["actions"] = []
    without_actions = simulate(
        prepare(bars, calendar, without),
        calendar,
        without,
        strategy="equal_weight",
        start=config["train_start"],
        end=str(sessions[8].date()),
    )

    assert not any("financial-action:" in json.dumps(row) for row in with_actions[2])
    assert with_actions[1]["ending_nav_hkd"] == without_actions[1]["ending_nav_hkd"]

    late_known = copy.deepcopy(config)
    late_known["financial"]["actions"][1]["available_at"] = (
        sessions[4].tz_localize("UTC").isoformat()
    )
    with pytest.raises(ValueError, match="late-known corporate action"):
        prepare(bars, calendar, late_known)


@pytest.mark.parametrize("kind", ["dividend_entitlement", "split"])
def test_v2_simulation_rejects_late_known_historical_position_actions(case, kind):
    config, bars, calendar = case
    config.update(
        schema="quant-hk-study/v2",
        settlement_calendar_scope="evidenced_purpose_calendar",
    )
    sessions = calendar.loc[calendar.date.ge(pd.Timestamp(config["train_start"])), "date"].tolist()
    config["financial"] = financial_evidence(config, calendar, [])
    prepared = prepare(bars, calendar, config)
    action = {
        "event_id": kind,
        "instrument_id": config["instruments"][0]["symbol"],
        "kind": kind,
        "effective_at": sessions[1].tz_localize("UTC").isoformat(),
        "available_at": sessions[2].tz_localize("UTC").isoformat(),
        "currency": "HKD",
        "source": "synthetic",
        "evidence_id": "fixture",
        "ratio": "2" if kind == "split" else "1",
    }
    if kind == "dividend_entitlement":
        action.update(cash_per_unit="10", entitlement_date=str(sessions[1].date()))
    config["financial"]["actions"] = [action]

    with pytest.raises(ValueError, match="historical position replay"):
        simulate(
            prepared,
            calendar,
            config,
            strategy="momentum_20d",
            start=config["train_start"],
            end=str(sessions[4].date()),
        )


def test_future_perturbations_leave_prior_signals_and_orders_unchanged(case):
    config, bars, calendar = case
    original = prepare(bars, calendar, config)
    cutoff = pd.Timestamp(config["test_start"])
    changed = bars.copy()
    changed.loc[changed.date.ge(cutoff), ["open", "high", "low", "close"]] *= 2
    altered = prepare(changed, calendar, config)
    signal_cols = ["date", "symbol", "prior_close", "prior_momentum_20d", "prior_volatility_20d"]
    pd.testing.assert_frame_equal(
        original[original.date.le(cutoff)][signal_cols],
        altered[altered.date.le(cutoff)][signal_cols],
    )
    old_run = simulate(
        original,
        calendar,
        config,
        strategy="momentum_20d",
        start=config["train_start"],
        end=str((cutoff - pd.Timedelta(days=1)).date()),
    )
    new_run = simulate(
        altered,
        calendar,
        config,
        strategy="momentum_20d",
        start=config["train_start"],
        end=str((cutoff - pd.Timedelta(days=1)).date()),
    )
    pd.testing.assert_frame_equal(old_run[0]["orders"], new_run[0]["orders"])
    pd.testing.assert_frame_equal(old_run[0]["returns"], new_run[0]["returns"])


def test_missing_and_nontrading_sessions_are_not_filled(case):
    config, bars, calendar = case
    with pytest.raises(ValueError, match="coverage"):
        prepare(bars.iloc[1:], calendar, config)
    changed = bars.copy()
    changed.loc[0, "volume"] = 0
    with pytest.raises(ValueError, match="historical status"):
        prepare(changed, calendar, config)


def test_study_is_reproducible_auditable_and_immutable(tmp_path, case, monkeypatch):
    from quant_lab.contracts_v2 import load_and_validate_run_v2

    monkeypatch.setattr("quant_hk_equity.research.clean_git_commit", lambda root: "a" * 40)
    config, _, _ = case
    root = snapshot(tmp_path, case)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    results = [run_study(root, config_file, tmp_path / label) for label in ("one", "two")]
    published = load_and_validate_run_v2(tmp_path / "one/holdout")
    assert published.tags["rankable"] == "false"
    assert published.base_currency == "HKD"
    exported = json.loads(
        (tmp_path / "one/holdout/standard/v2/metrics.json").read_text(encoding="utf-8")
    )
    native = results[0]["holdout"]
    assert all(exported[key] == value for key, value in native.items())
    row = exported["backtest_stats"][0]
    assert row["total_return"] == native["total_return"]
    assert row["ann_return"] == native["annualized_return"]
    assert row["sharpe"] == native["sharpe_zero_rf"]
    assert row["max_drawdown"] == native["max_drawdown"]
    assert exported["measurement_basis"]["annualization_periods"] == 252
    assert results[0] == results[1]
    assert results[0]["investable"] is False
    assert results[0]["holdout"]["return_basis"] == "price_only_excludes_corporate_actions"
    assert (tmp_path / "one/holdout/ledger.json").read_bytes() == (
        tmp_path / "two/holdout/ledger.json"
    ).read_bytes()
    signals = pd.read_csv(tmp_path / "one/holdout/signals.csv")
    assert (pd.to_datetime(signals.signal_date) < pd.to_datetime(signals.date)).all()
    train = pd.read_csv(tmp_path / "one/train-momentum_20d/standard/returns.csv")
    test = pd.read_csv(tmp_path / "one/holdout/standard/returns.csv")
    assert train.date.max() < test.date.min()
    assert (test.available_cash_hkd >= 0).all()
    manifest = json.loads((tmp_path / "one/checksums.json").read_text())
    assert all(sha256(tmp_path / "one" / name) == digest for name, digest in manifest.items())
    standard_manifest = json.loads(
        (tmp_path / "one/holdout/standard/run_manifest.json").read_text()
    )
    assert standard_manifest["tags"]["cost_unit"] == "currency"
    with pytest.raises(FileExistsError):
        run_study(root, config_file, tmp_path / "one")


def test_modified_snapshot_rejected(tmp_path, case):
    root = snapshot(tmp_path, case)
    with (root / "calendar.csv").open("a") as stream:
        stream.write("\n2027-01-01,broken,broken\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_hk_snapshot(root)


@pytest.mark.parametrize(
    "field,value",
    [
        ("top_n", 0),
        ("rebalance_sessions", True),
        ("initial_cash", "NaN"),
        ("invested_fraction", "1.1"),
        ("test_start", "2025-07-01"),
        ("candidates", ["unknown"]),
    ],
)
def test_invalid_recipe_rejected(case, field, value):
    config = copy.deepcopy(case[0])
    config[field] = value
    with pytest.raises(ValueError):
        validate_config(config)


def test_costs_are_in_drawdown_from_initial_capital():
    m = metrics(pd.DataFrame({"nav": [99, 99], "net_return": [-0.01, 0]}), 100)
    assert m["max_drawdown"] == pytest.approx(-0.01)


def test_liquidity_filter_only_uses_prior_turnover(case):
    config, bars, calendar = case
    prepared = prepare(bars, calendar, config)
    frame = prepared[prepared.date.eq(config["test_start"])].copy()
    expected = select(frame, "momentum_20d", config)
    frame["amount"] = 0
    assert select(frame, "momentum_20d", config) == expected


def test_hong_kong_calendar_is_not_mainland_or_weekday_calendar():
    from quant_data_kit.hong_kong import hk_calendar

    calendar = hk_calendar("2025-12-23", "2025-12-29")
    assert pd.Timestamp("2025-12-25") not in set(calendar.date)
    assert pd.Timestamp("2025-12-26") not in set(calendar.date)
    half_day = calendar[calendar.date.eq("2025-12-24")].iloc[0]
    assert half_day["close"].tz_convert("Asia/Hong_Kong").hour == 12

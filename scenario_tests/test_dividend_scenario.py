from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from quant_data_kit.financial import CurrencyReference, IssuerFxConversion, PublishedAmount
from quant_execution import (
    DividendExecutionMode,
    DividendExecutionPhase,
    FxValuationMode,
    replay_dividend_run,
)
from quant_execution.hong_kong import HKDailyExecution

import quant_hk_equity.dividend_scenario as scenario_module
from quant_hk_equity.dividend_scenario import (
    ScenarioValidationError,
    build_scenario_timeline,
    load_dividend_scenario,
    run_dividend_scenario_files,
    validate_dividend_scenario_output,
)
from quant_hk_equity.research import prepare
from scenario_tests.helpers import (
    create_case,
    evidence,
    iso,
    lifecycle,
    pit_fx,
    scenario_payload,
    write_scenario,
)


@pytest.fixture(autouse=True)
def explicit_source_identity_fixture(monkeypatch):
    identities = {
        **scenario_module.DEPENDENCY_COMMITS,
        "stack.dividend-scenario.lock": "f" * 64,
    }
    monkeypatch.setattr(scenario_module, "_verify_dependency_stack", lambda: identities)
    return identities


def event_times(case):
    sessions = case.calendar[case.calendar.date.ge(case.config["test_start"])].reset_index(
        drop=True
    )
    ex_at = sessions.iloc[0].close.to_pydatetime() + timedelta(hours=1)
    conversion_at = ex_at + timedelta(hours=1)
    payment_at = ex_at + timedelta(hours=2)
    as_of = sessions.iloc[8].close.to_pydatetime()
    return sessions, ex_at, conversion_at, payment_at, as_of


def foreign_conversion_lifecycle(
    value,
    *,
    conversion_at,
    policy_at,
    published_amount,
):
    usd = CurrencyReference("USD", "USD", "identity")
    entitlement = replace(
        value.entitlement,
        payment_currencies=(*value.entitlement.payment_currencies, usd),
    )
    election = replace(
        value.election,
        evidence=evidence(f"{value.dividend_id}:election", conversion_at),
        payment_currency="USD",
        selection_kind="explicit",
    )
    conversion = IssuerFxConversion(
        evidence=evidence(f"{value.dividend_id}:conversion", conversion_at),
        from_currency="HKD",
        to_currency="USD",
        rate_text="0.13",
        rate_convention="quote_per_base",
        fixing_at=iso(conversion_at),
        fixing_date=None,
        published_payment_amount=PublishedAmount(
            published_amount,
            "1",
            "share",
            len(published_amount.partition(".")[2]),
            False,
        ),
    )
    policy = replace(
        value.payment_policy,
        evidence=evidence(f"{value.dividend_id}:policy", policy_at),
    )
    return replace(
        value,
        entitlement=entitlement,
        election=election,
        conversion=conversion,
        payment_policy=policy,
        payment=None,
    )


@pytest.mark.parametrize("offset,accepted", [(-1, True), (0, True), (1, False)])
def test_entitlement_terms_may_be_effective_before_or_at_ex(tmp_path, offset, accepted):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        entitlement_effective=ex_at + timedelta(seconds=offset),
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    payload = scenario_payload(case, value, ex_at=ex_at, as_of=as_of)
    if accepted:
        loaded = load_dividend_scenario(payload)
        assert loaded.basis_evidence[0].ex_at == ex_at
    else:
        with pytest.raises(ScenarioValidationError, match="LATE_ENTITLEMENT_UNSUPPORTED"):
            load_dividend_scenario(payload)


def test_complete_hkd_three_phase_scenario_replays_and_publishes_last(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    output = tmp_path / "output"

    result = run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=output,
    )

    assert result["status"] == "complete_as_of"
    assert result["investable"] is result["rankable"] is False
    assert result["market_admission_certified"] is False
    assert result["open_lifecycle_ids"] == []
    assert (output / "execution/manifest.json").exists()
    assert (output / "scenario-manifest.json").exists()
    assert not (output / "standard").exists()
    records = [json.loads(line) for line in (output / "timeline.jsonl").read_text().splitlines()]
    phases = [
        row["qexec_record"]["phase"]
        for row in records
        if row["kind"] in {"dividend_entitlement", "issuer_conversion", "payment"}
    ]
    assert phases == [phase.value for phase in DividendExecutionPhase]
    conversion = next(row for row in records if row["kind"] == "issuer_conversion")
    assert conversion["qexec_record"]["issuer_conversion_audit"] is None
    assert conversion["qexec_record"]["applied_at"] == iso(conversion_at)
    assert next(row for row in records if row["kind"] == "payment")["qexec_record"][
        "applied_at"
    ] == iso(payment_at)
    replayed = replay_dividend_run(output / "execution")
    assert replayed.ledger.cash_balance("HKD") > 0
    state = json.loads((output / "hk-execution-state.json").read_text())
    assert state["schema"] == "quant-hk-dividend-execution-state/v1"
    assert state["journal_sha256"] == replayed.ledger.journal_sha256
    manifest = json.loads((output / "scenario-manifest.json").read_text())
    assert manifest["complete"] is True
    assert set(manifest["files"]) >= {
        "input.json",
        "result.json",
        "orders.csv",
        "timeline.jsonl",
        "hk-execution-state.json",
    }
    assert (
        validate_dividend_scenario_output(
            output, snapshot=case.snapshot, config_path=case.config_path
        )["result"]
        == result
    )


def test_training_selection_ignores_holdout_prices_and_dividend_payload(tmp_path):
    case = create_case(tmp_path)
    prepared = prepare(case.bars, case.calendar, case.config)
    original = scenario_module._select_training_strategy(prepared, case.calendar, case.config)
    changed_bars = case.bars.copy()
    changed_bars.loc[
        changed_bars.date.ge(pd.Timestamp(case.config["test_start"])),
        ["open", "high", "low", "close"],
    ] *= 5
    changed = prepare(changed_bars, case.calendar, case.config)
    altered = scenario_module._select_training_strategy(changed, case.calendar, case.config)
    assert altered == original


def test_hkd_payment_changes_later_whole_lot_purchase_without_changing_selection(tmp_path):
    case = create_case(tmp_path)
    sessions, ex_at, conversion_at, _, _ = event_times(case)
    payment_at = ex_at + timedelta(hours=2)
    as_of = sessions.iloc[6].close.to_pydatetime()
    paid = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
        amount_text="10000.00",
    )
    open_value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=None,
        amount_text="10000.00",
        scheduled_payment_date=(as_of + timedelta(days=30)).date().isoformat(),
    )
    outputs = []
    for name, value in (("paid", paid), ("open", open_value)):
        path = write_scenario(
            tmp_path,
            scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
            f"{name}.json",
        )
        output = tmp_path / name
        result = run_dividend_scenario_files(
            snapshot=case.snapshot,
            config_path=case.config_path,
            scenario_path=path,
            output=output,
        )
        outputs.append((result, pd.read_csv(output / "orders.csv")))
    assert outputs[0][0]["selection"] == outputs[1][0]["selection"]
    paid_buys = outputs[0][1].query("side == 'buy' and status == 'filled'").quantity.sum()
    open_buys = outputs[1][1].query("side == 'buy' and status == 'filled'").quantity.sum()
    assert paid_buys > open_buys


def test_foreign_payment_changes_nav_assets_but_not_spendable_hkd(tmp_path):
    case = create_case(tmp_path)
    sessions, ex_at, conversion_at, payment_at, _ = event_times(case)
    as_of = sessions.iloc[6].close.to_pydatetime()
    rate = pit_fx(currency="USD", available_at=ex_at - timedelta(minutes=30))
    paid = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
        currency="USD",
        amount_text="100.00",
    )
    open_value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=None,
        currency="USD",
        amount_text="100.00",
        scheduled_payment_date=(as_of + timedelta(days=30)).date().isoformat(),
    )
    states = []
    ledgers = []
    order_bytes = []
    for name, value in (("usd-paid", paid), ("usd-open", open_value)):
        path = write_scenario(
            tmp_path,
            scenario_payload(case, value, ex_at=ex_at, as_of=as_of, pit_fx=[rate]),
            f"{name}.json",
        )
        output = tmp_path / name
        run_dividend_scenario_files(
            snapshot=case.snapshot,
            config_path=case.config_path,
            scenario_path=path,
            output=output,
        )
        states.append(json.loads((output / "hk-execution-state.json").read_text()))
        ledgers.append(replay_dividend_run(output / "execution").ledger)
        order_bytes.append((output / "orders.csv").read_bytes())
    assert ledgers[0].cash_balance("USD") > 0
    assert ledgers[1].cash_balance("USD") == 0
    assert states[0]["cash_balance_hkd"] == states[1]["cash_balance_hkd"]
    assert states[0]["available_cash_hkd"] == states[1]["available_cash_hkd"]
    assert order_bytes[0] == order_bytes[1]


def test_unsettled_sale_proceeds_do_not_fund_same_open_buys(tmp_path):
    case = create_case(tmp_path, top_n=1, rebalance_sessions=1)
    specs = scenario_module._instrument_specs(case.config)
    sessions = case.calendar[case.calendar.date.ge(case.config["test_start"])].reset_index(
        drop=True
    )
    first_at = sessions.iloc[0].open.to_pydatetime()
    second_at = sessions.iloc[1].open.to_pydatetime()
    account = HKDailyExecution(
        specs,
        initial_cash=scenario_module.fp(case.config["initial_cash"], 2),
        opened_at=first_at,
        fees=scenario_module.fee_schedule(case.config),
        settlement_days=case.calendar.date.dt.date.tolist(),
        dividend_execution_mode=DividendExecutionMode.SCENARIO_ONLY,
        fx_valuation_mode=FxValuationMode.EVIDENCED_PIT,
    )

    def frame(at, selected):
        rows = []
        for symbol, price in (("00005", 3000), ("00700", 4000)):
            rows.append(
                {
                    "symbol": symbol,
                    "open": price,
                    "signal_date": pd.Timestamp(at.date()) - pd.Timedelta(days=1),
                    "prior_close": price,
                    "prior_turnover_20d": 1000000,
                    "prior_momentum_20d": 2 if symbol == selected else 1,
                    "prior_volatility_20d": 1,
                }
            )
        return pd.DataFrame(rows).set_index("symbol", drop=False)

    orders, costs, signals = [], [], []
    for symbol, price in (("00005", 3000), ("00700", 4000)):
        account.mark(symbol, scenario_module.fp(price), first_at)
    scenario_module._execute_rebalance(
        account=account,
        frame=frame(first_at, "00005"),
        strategy="momentum_20d",
        config=case.config,
        specs=specs,
        at=first_at,
        scenario_id="t2",
        orders=orders,
        costs=costs,
        signals=signals,
    )
    for symbol, price in (("00005", 3000), ("00700", 4000)):
        account.mark(symbol, scenario_module.fp(price), second_at)
    cash_before = account.available_cash()
    scenario_module._execute_rebalance(
        account=account,
        frame=frame(second_at, "00700"),
        strategy="momentum_20d",
        config=case.config,
        specs=specs,
        at=second_at,
        scenario_id="t2",
        orders=orders,
        costs=costs,
        signals=signals,
    )
    second = [item for item in orders if item["timestamp"] == iso(second_at)]
    assert [item["side"] for item in second] == ["sell", "buy"]
    assert second[1]["quantity"] < second[1]["requested_quantity"]
    assert account.pending
    assert account.available_cash() <= cash_before
    pending = sum((amount for _, amount in account.pending), Decimal(0))
    assert account.available_cash() == account.ledger.cash_balance("HKD") - pending


def test_missing_future_payment_policy_preserves_exact_open_receivable(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, _, _, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        payment_at=None,
        scheduled_payment_date=(as_of + timedelta(days=30)).date().isoformat(),
    )
    value = replace(value, election=None, payment_policy=None)
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    result = run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=tmp_path / "policy-open",
    )
    assert result["open_lifecycle_ids"] == [value.dividend_id]
    assert result["dividend_tax_status"] == "unknown"


def test_late_policy_does_not_delay_conversion_that_does_not_use_it(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, _, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=None,
        scheduled_payment_date=(as_of + timedelta(days=30)).date().isoformat(),
    )
    late_policy = replace(
        value.payment_policy,
        evidence=evidence(f"{value.dividend_id}:late-policy", conversion_at + timedelta(days=1)),
    )
    value = replace(value, payment_policy=late_policy)
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    output = tmp_path / "late-policy"
    result = run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=output,
    )
    rows = [json.loads(line) for line in (output / "timeline.jsonl").read_text().splitlines()]
    conversion_rows = [item for item in rows if item["kind"] == "issuer_conversion"]
    assert len(conversion_rows) == 1
    conversion = conversion_rows[0]
    assert conversion["event_time"] == iso(conversion_at)
    assert conversion["qexec_record"]["lifecycle_snapshot"]["payment_policy"] is None
    assert result["dividend_tax_status"] == "unknown"


@pytest.mark.parametrize(
    "published_amount,uses_policy",
    [("0.33333333", False), ("0.33333333333", True)],
)
def test_conversion_uses_late_policy_only_when_qexec_requires_rounding(
    tmp_path, published_amount, uses_policy
):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, _, as_of = event_times(case)
    policy_at = conversion_at + timedelta(days=1)
    base = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=None,
        scheduled_payment_date=(as_of + timedelta(days=30)).date().isoformat(),
    )
    value = foreign_conversion_lifecycle(
        base,
        conversion_at=conversion_at,
        policy_at=policy_at,
        published_amount=published_amount,
    )
    path = write_scenario(
        tmp_path,
        scenario_payload(
            case,
            value,
            ex_at=ex_at,
            as_of=as_of,
            pit_fx=[pit_fx(currency="USD", available_at=ex_at - timedelta(minutes=30))],
        ),
    )
    output = tmp_path / f"conversion-{uses_policy}"
    run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=output,
    )
    rows = [json.loads(line) for line in (output / "timeline.jsonl").read_text().splitlines()]
    conversion = next(item for item in rows if item["kind"] == "issuer_conversion")
    assert conversion["qexec_record"]["applied_at"] == iso(
        policy_at if uses_policy else conversion_at
    )
    snapshot = conversion["qexec_record"]["lifecycle_snapshot"]
    assert (snapshot["payment_policy"] is not None) is uses_policy
    validate_dividend_scenario_output(output, snapshot=case.snapshot, config_path=case.config_path)


def test_nonexact_conversion_without_policy_is_explicitly_blocked(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, _, as_of = event_times(case)
    base = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=None,
        scheduled_payment_date=(as_of + timedelta(days=30)).date().isoformat(),
    )
    value = foreign_conversion_lifecycle(
        base,
        conversion_at=conversion_at,
        policy_at=conversion_at + timedelta(days=1),
        published_amount="0.33333333333",
    )
    value = replace(value, payment_policy=None)
    path = write_scenario(
        tmp_path,
        scenario_payload(
            case,
            value,
            ex_at=ex_at,
            as_of=as_of,
            pit_fx=[pit_fx(currency="USD", available_at=ex_at - timedelta(minutes=30))],
        ),
    )
    with pytest.raises(ScenarioValidationError, match="ROUNDING_POLICY_REQUIRED"):
        run_dividend_scenario_files(
            snapshot=case.snapshot,
            config_path=case.config_path,
            scenario_path=path,
            output=tmp_path / "nonexact-no-policy",
        )


def test_actual_payment_rejects_policy_not_effective_as_of(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    payload = scenario_payload(case, value, ex_at=ex_at, as_of=as_of)
    policy_timing = payload["lifecycles"][0]["lifecycle"]["payment_policy"]["evidence"]["timing"]
    policy_timing["effective_at"] = iso(as_of + timedelta(days=1))
    policy_timing["available_at"] = iso(conversion_at)
    policy_timing["captured_at"] = iso(conversion_at)
    policy_timing["source_published_at"] = iso(conversion_at)
    with pytest.raises(ScenarioValidationError, match="LIFECYCLE_INVALID"):
        load_dividend_scenario(payload)


def test_post_close_payment_is_reflected_in_account_as_of_not_daily_end(tmp_path):
    case = create_case(tmp_path)
    sessions = case.calendar[case.calendar.date.ge(case.config["test_start"])].reset_index(
        drop=True
    )
    first_open = sessions.iloc[0].open.to_pydatetime()
    first_close = sessions.iloc[0].close.to_pydatetime()
    ex_at = first_open + timedelta(hours=3)
    conversion_at = ex_at + timedelta(minutes=30)
    payment_at = first_close + timedelta(hours=2)
    as_of = payment_at + timedelta(hours=1)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    value = replace(
        value,
        payment=replace(
            value.payment,
            withholding_cash_text="100.00",
            net_cash_text="900.00",
        ),
    )
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    output = tmp_path / "post-close-payment"
    result = run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=output,
    )
    daily = pd.read_csv(output / "returns.csv")
    assert result["daily_return_end"] == str(sessions.iloc[0].date.date())
    assert result["account_as_of"] == iso(as_of)
    assert result["price_coverage_end"] == case.config["data_end"]
    assert Decimal(result["metrics"]["as_of_nav_hkd"]) < Decimal(str(daily.nav.iloc[-1]))
    expected = (
        Decimal(result["metrics"]["as_of_nav_hkd"]) / Decimal(case.config["initial_cash"]) - 1
    )
    assert Decimal(result["metrics"]["as_of_total_return"]) == expected


def test_close_and_payment_same_instant_require_explicit_economic_order(tmp_path):
    case = create_case(tmp_path)
    sessions = case.calendar[case.calendar.date.ge(case.config["test_start"])].reset_index(
        drop=True
    )
    at_open = sessions.iloc[0].open.to_pydatetime()
    at_close = sessions.iloc[0].close.to_pydatetime()
    ex_at = at_open + timedelta(hours=3)
    conversion_at = ex_at + timedelta(minutes=30)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=at_close,
    )
    payload = scenario_payload(case, value, ex_at=ex_at, as_of=at_close)
    with pytest.raises(ScenarioValidationError, match="AMBIGUOUS_EVENT_ORDER"):
        build_scenario_timeline(case.calendar, case.config, load_dividend_scenario(payload))
    session_date = str(sessions.iloc[0].date.date())
    payment_id = value.payment.evidence.event_id
    payload["same_instant_order"] = [
        {
            "timestamp": iso(at_close),
            "before_event_id": f"session:{session_date}:close-marks",
            "after_event_id": payment_id,
            "evidence_id": "close-before-payment",
            "source": "synthetic-explicit-order",
        },
        {
            "timestamp": iso(at_close),
            "before_event_id": payment_id,
            "after_event_id": f"session:{session_date}:settlement",
            "evidence_id": "payment-before-settlement",
            "source": "synthetic-explicit-order",
        },
    ]
    ordered = build_scenario_timeline(case.calendar, case.config, load_dividend_scenario(payload))
    at_cutoff = [item.event_id for item in ordered if item.event_time == at_close]
    assert at_cutoff == [
        f"session:{session_date}:close-marks",
        payment_id,
        f"session:{session_date}:settlement",
        "scenario:final-valuation",
    ]


def test_due_payment_requires_exact_not_received_status(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, _, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=None,
        scheduled_payment_date=(as_of - timedelta(days=1)).date().isoformat(),
    )
    payload = scenario_payload(case, value, ex_at=ex_at, as_of=as_of)
    with pytest.raises(ScenarioValidationError, match="PAYMENT_STATUS_UNKNOWN"):
        load_dividend_scenario(payload)
    payload["payment_status"] = [
        {
            "evidence_id": "custody:not-received",
            "dividend_id": value.dividend_id,
            "account_id": "hk-research",
            "status": "not_received",
            "status_at": iso(as_of),
            "available_at": iso(as_of),
            "captured_at": iso(as_of),
            "source": "synthetic-custody-ledger",
        }
    ]
    loaded = load_dividend_scenario(payload)
    assert loaded.payment_status[0].status == "not_received"
    payload["payment_status"][0]["status_at"] = iso(as_of - timedelta(seconds=1))
    with pytest.raises(ScenarioValidationError, match="PAYMENT_STATUS_INVALID"):
        load_dividend_scenario(payload)


def test_same_instant_external_fact_needs_evidenced_total_order(tmp_path):
    case = create_case(tmp_path)
    sessions, _, _, _, as_of = event_times(case)
    ex_at = sessions.iloc[0].open.to_pydatetime()
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        entitlement_effective=ex_at - timedelta(days=1),
        conversion_at=ex_at + timedelta(hours=10),
        payment_at=ex_at + timedelta(hours=11),
    )
    payload = scenario_payload(case, value, ex_at=ex_at, as_of=as_of)
    loaded = load_dividend_scenario(payload)
    with pytest.raises(ScenarioValidationError, match="AMBIGUOUS_EVENT_ORDER"):
        build_scenario_timeline(case.calendar, case.config, loaded)
    session_date = str(sessions.iloc[0].date.date())
    entitlement_id = value.entitlement.evidence.event_id
    payload["same_instant_order"] = [
        {
            "timestamp": iso(ex_at),
            "before_event_id": f"session:{session_date}:open-marks",
            "after_event_id": entitlement_id,
            "evidence_id": "opening-before-ex",
            "source": "synthetic-explicit-order",
        },
        {
            "timestamp": iso(ex_at),
            "before_event_id": entitlement_id,
            "after_event_id": f"session:{session_date}:rebalance",
            "evidence_id": "ex-before-trade",
            "source": "synthetic-explicit-order",
        },
    ]
    ordered = build_scenario_timeline(case.calendar, case.config, load_dividend_scenario(payload))
    at_open = [item.event_id for item in ordered if item.event_time == ex_at]
    assert at_open[:3] == [
        f"session:{session_date}:open-marks",
        entitlement_id,
        f"session:{session_date}:rebalance",
    ]


def test_phase_time_contradiction_is_not_repaired_by_late_cutoff(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, _, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=ex_at - timedelta(hours=1),
        payment_at=payment_at,
    )
    loaded = load_dividend_scenario(scenario_payload(case, value, ex_at=ex_at, as_of=as_of))
    with pytest.raises(ScenarioValidationError, match="PHASE_TIME_CONTRADICTION"):
        build_scenario_timeline(case.calendar, case.config, loaded)


def test_hk_replay_failure_preserves_valid_qexec_artifact(tmp_path, monkeypatch):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    output = tmp_path / "failed-output"

    def fail_replay(first, replay):
        raise ScenarioValidationError("HK_REPLAY_MISMATCH", "injected")

    monkeypatch.setattr(scenario_module, "_verify_replay", fail_replay)
    with pytest.raises(ScenarioValidationError, match="HK_REPLAY_MISMATCH"):
        run_dividend_scenario_files(
            snapshot=case.snapshot,
            config_path=case.config_path,
            scenario_path=path,
            output=output,
        )
    assert (output / "execution/manifest.json").exists()
    assert not (output / "scenario-manifest.json").exists()
    failure = json.loads((output / "FAILED.json").read_text())
    assert failure["qexec_artifact_preserved"] is True
    replay_dividend_run(output / "execution")


def test_candidate_validator_rejects_tamper_even_when_sidecar_hash_is_recomputed(
    tmp_path, monkeypatch
):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    output = tmp_path / "candidate-tamper"
    original = scenario_module.validate_dividend_scenario_output

    def tamper(root, **kwargs):
        orders = root / "orders.csv"
        orders.write_bytes(orders.read_bytes() + b"tampered\n")
        state_path = root / "hk-execution-state.json"
        state = json.loads(state_path.read_text())
        state["orders_sha256"] = scenario_module.sha256(orders)
        state_path.write_bytes(scenario_module._canonical_bytes(state))
        return original(root, **kwargs)

    monkeypatch.setattr(scenario_module, "validate_dividend_scenario_output", tamper)
    with pytest.raises(ScenarioValidationError, match="HK_DISK_REPLAY_MISMATCH"):
        run_dividend_scenario_files(
            snapshot=case.snapshot,
            config_path=case.config_path,
            scenario_path=path,
            output=output,
        )
    assert (output / "execution/manifest.json").exists()
    assert not (output / "scenario-manifest.json").exists()


def test_published_validator_rejects_sidecar_tamper_with_consistent_file_hashes(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    output = tmp_path / "published-tamper"
    run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=output,
    )
    state_path = output / "hk-execution-state.json"
    state = json.loads(state_path.read_text())
    state["journal_sha256"] = "0" * 64
    state_path.write_bytes(scenario_module._canonical_bytes(state))
    result_path = output / "result.json"
    result = json.loads(result_path.read_text())
    result["hk_execution_state_sha256"] = scenario_module.sha256(state_path)
    result_path.write_bytes(scenario_module._canonical_bytes(result))
    manifest_path = output / "scenario-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["hk-execution-state.json"] = scenario_module.sha256(state_path)
    manifest["files"]["result.json"] = scenario_module.sha256(result_path)
    manifest["result_sha256"] = scenario_module.sha256(result_path)
    manifest_path.write_bytes(scenario_module._canonical_bytes(manifest))
    replay_dividend_run(output / "execution")
    with pytest.raises(ScenarioValidationError, match="HK_DISK_REPLAY_MISMATCH"):
        validate_dividend_scenario_output(
            output, snapshot=case.snapshot, config_path=case.config_path
        )


def test_published_validator_rejects_missing_or_modified_file(tmp_path):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    output = tmp_path / "post-publish"
    run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=output,
    )
    (output / "signals.csv").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ScenarioValidationError, match="HK_DISK_REPLAY_MISMATCH"):
        validate_dividend_scenario_output(
            output, snapshot=case.snapshot, config_path=case.config_path
        )
    (output / "signals.csv").unlink()
    with pytest.raises(ScenarioValidationError, match="SCENARIO_ARTIFACT_MISSING"):
        validate_dividend_scenario_output(
            output, snapshot=case.snapshot, config_path=case.config_path
        )


def test_validator_rejects_failure_marker_and_provider_mismatch(tmp_path, monkeypatch):
    case = create_case(tmp_path)
    _, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    path = write_scenario(
        tmp_path,
        scenario_payload(case, value, ex_at=ex_at, as_of=as_of),
    )
    output = tmp_path / "validator-boundaries"
    run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=output,
    )

    failed = output / "FAILED.json"
    failed.write_text('{"status":"failed"}\n', encoding="utf-8")
    with pytest.raises(ScenarioValidationError, match="SCENARIO_RUN_FAILED"):
        validate_dividend_scenario_output(
            output, snapshot=case.snapshot, config_path=case.config_path
        )
    failed.unlink()

    original = scenario_module.load_hk_snapshot

    def wrong_provider(snapshot):
        bars, calendar, manifest = original(snapshot)
        return bars, calendar, {**manifest, "provider": "different-provider"}

    monkeypatch.setattr(scenario_module, "load_hk_snapshot", wrong_provider)
    with pytest.raises(ScenarioValidationError, match="snapshot provider differs"):
        validate_dividend_scenario_output(
            output, snapshot=case.snapshot, config_path=case.config_path
        )


def test_cli_module_is_not_imported_by_default_cli_import():
    source = Path(scenario_module.__file__).with_name("cli.py").read_text(encoding="utf-8")
    assert "from quant_hk_equity.dividend_scenario import" in source
    assert source.index('if args.command == "dividend-scenario"') < source.index(
        "from quant_hk_equity.dividend_scenario import"
    )


def test_dividend_scenario_cli_is_registered_natively():
    completed = subprocess.run(
        [sys.executable, "-m", "quant_hk_equity.cli", "dividend-scenario", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--scenario" in completed.stdout
    assert "--output" in completed.stdout

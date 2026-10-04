from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from quant_data_kit.financial import (
    CurrencyReference,
    DividendEntitlement,
    DividendLifecycle,
    DividendPayment,
    DividendPaymentElection,
    EvidenceTiming,
    PaymentPolicy,
    PhaseEvidence,
    PitFxRate,
    PublishedAmount,
    RoundingPolicy,
)
from quant_data_kit.hong_kong import normalize_hk_bars, sha256


@dataclass
class ScenarioCase:
    config: dict
    config_path: Path
    snapshot: Path
    bars: pd.DataFrame
    calendar: pd.DataFrame


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def create_case(tmp_path: Path, *, top_n: int = 2, rebalance_sessions: int = 5) -> ScenarioCase:
    days = pd.bdate_range("2025-07-02", periods=105)
    instruments = [
        {"symbol": "00005", "name": "SYNTHETIC-A", "lot_size": 100, "stamp_exempt": False},
        {"symbol": "00700", "name": "SYNTHETIC-B", "lot_size": 100, "stamp_exempt": False},
    ]
    config = {
        "schema": "quant-hk-study/v1",
        "provider": "synthetic-software-fixture",
        "data_start": str(days[0].date()),
        "data_end": str(days[99].date()),
        "train_start": str(days[25].date()),
        "test_start": str(days[60].date()),
        "initial_cash": "1000000",
        "rebalance_sessions": rebalance_sessions,
        "top_n": top_n,
        "invested_fraction": "0.80",
        "min_price": 2,
        "min_median_turnover_hkd": 100,
        "candidates": ["momentum_20d", "low_volatility_20d"],
        "universe_scope": "current_watchlist_not_historical_universe",
        "instrument_rules_scope": "constant_board_lot_scenario_not_pit",
        "settlement_calendar_scope": "XHKG_sessions_proxy_not_verified_CCASS_calendar",
        "master_source": "synthetic",
        "master_checked_on": "2026-10-04",
        "instruments": instruments,
        "fees": {
            "valid_from": "2025-01-01",
            "valid_to": "2026-12-31",
            "commission_rate": "0.0003",
            "minimum_commission": "3",
            "platform_fee": "0",
            "stamp_rate": "0.001",
            "sfc_rate": "0.000027",
            "afrc_rate": "0.0000015",
            "trading_rate": "0.0000565",
            "settlement_rate": "0.000042",
            "settlement_minimum": "0",
            "settlement_maximum": None,
            "slippage_rate": "0.0005",
            "source": "synthetic explicit software policy",
        },
    }
    calendar = pd.DataFrame(
        {
            "date": days[:100],
            "open": days[:100].tz_localize("UTC") + pd.Timedelta(hours=1, minutes=30),
            "close": days[:100].tz_localize("UTC") + pd.Timedelta(hours=8),
        }
    )
    frames = []
    for index, item in enumerate(instruments):
        offset = np.arange(100)
        close = 3000 + index * 1000 + offset * (2 if index == 0 else 1)
        frame = pd.DataFrame(
            {
                "date": days[:100],
                "open": close - 0.2,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 100000,
                "amount": 10000000,
            }
        )
        frames.append(normalize_hk_bars(frame, item["symbol"], config["provider"]))
    bars = pd.concat(frames, ignore_index=True)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    bars.to_parquet(snapshot / "bars.parquet", index=False)
    calendar.to_csv(snapshot / "calendar.csv", index=False)
    manifest = {
        "schema": "quant-hk-snapshot/v1",
        "status": "complete",
        "provider": config["provider"],
        "symbols": sorted(bars.symbol.unique()),
        "corporate_actions_complete": False,
        "start": config["data_start"],
        "end": config["data_end"],
        "files": {path.name: sha256(path) for path in snapshot.iterdir()},
    }
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return ScenarioCase(config, config_path, snapshot, bars, calendar)


def evidence(
    event_id: str, effective: datetime, available: datetime | None = None
) -> PhaseEvidence:
    available = available or effective
    return PhaseEvidence(
        event_id=event_id,
        source="synthetic-software-fixture",
        evidence_id=f"evidence:{event_id}",
        timing=EvidenceTiming(
            effective_at=iso(effective),
            available_at=iso(available),
            captured_at=iso(available),
            source_published_at=iso(available),
        ),
    )


def account_quantity(case: ScenarioCase, symbol: str) -> int:
    first = pd.Timestamp(case.config["test_start"])
    price = Decimal(
        str(case.bars[(case.bars.symbol == symbol) & (case.bars.date == first)].open.iloc[0])
    )
    budget = Decimal(case.config["initial_cash"]) * Decimal(case.config["invested_fraction"]) / 2
    lot = next(item["lot_size"] for item in case.config["instruments"] if item["symbol"] == symbol)
    return int(budget / price) // lot * lot


def lifecycle(
    *,
    case: ScenarioCase,
    symbol: str = "00005",
    dividend_id: str = "00005:synthetic:2025",
    amount_text: str = "10.00",
    currency: str = "HKD",
    ex_at: datetime,
    entitlement_effective: datetime | None = None,
    conversion_at: datetime | None = None,
    payment_at: datetime | None = None,
    scheduled_payment_date: str | None = None,
) -> DividendLifecycle:
    entitlement_effective = entitlement_effective or ex_at - timedelta(days=2)
    conversion_at = conversion_at or ex_at + timedelta(hours=1)
    scheduled_payment_date = scheduled_payment_date or (
        payment_at.date().isoformat()
        if payment_at is not None
        else (ex_at + timedelta(days=30)).date().isoformat()
    )
    currency_ref = CurrencyReference(currency, currency, "identity")
    terms = DividendEntitlement(
        evidence=evidence(f"{dividend_id}:entitlement", entitlement_effective),
        approved_amount=PublishedAmount(amount_text, "1", "share", 2, False),
        declared_currency=currency_ref,
        record_date=ex_at.date().isoformat(),
        scheduled_payment_date=scheduled_payment_date,
        payment_currencies=(currency_ref,),
        default_payment_currency=currency,
    )
    policy = PaymentPolicy(
        policy_id=f"{dividend_id}:policy",
        account_id="hk-research",
        certification_status="certified",
        holder_tax_profile_id="synthetic-zero-tax",
        withholding_rule_id="synthetic-zero-withholding",
        rounding=RoundingPolicy("aggregate_account", 2, "ROUND_HALF_UP", "synthetic-rounding"),
        evidence=evidence(f"{dividend_id}:policy", conversion_at),
    )
    election = DividendPaymentElection(
        evidence=evidence(f"{dividend_id}:election", conversion_at),
        account_id="hk-research",
        account_policy_id=policy.policy_id,
        payment_currency=currency,
        selection_kind="default",
    )
    payment = None
    if payment_at is not None:
        gross = Decimal(account_quantity(case, symbol)) * Decimal(amount_text)
        gross_text = f"{gross:.2f}"
        payment = DividendPayment(
            evidence=evidence(f"{dividend_id}:payment", payment_at),
            account_id="hk-research",
            payment_currency=currency,
            policy_id=policy.policy_id,
            gross_cash_text=gross_text,
            withholding_cash_text="0",
            deductions=(),
            rounding_adjustment_text="0",
            net_cash_text=gross_text,
        )
    return DividendLifecycle(
        dividend_id=dividend_id,
        instrument_id=symbol,
        entitlement=terms,
        election=election,
        payment_policy=policy,
        payment=payment,
    )


def scenario_payload(
    case: ScenarioCase,
    lifecycle_value: DividendLifecycle,
    *,
    ex_at: datetime,
    as_of: datetime,
    pit_fx: list[dict] | None = None,
    payment_status: list[dict] | None = None,
    same_instant_order: list[dict] | None = None,
) -> dict:
    lifecycle_json = lifecycle_value.to_json().encode("utf-8")
    return {
        "schema": "quant-hk-dividend-scenario/v1",
        "scenario_id": f"scenario:{lifecycle_value.dividend_id}",
        "study_config_sha256": sha256(case.config_path),
        "snapshot_manifest_sha256": sha256(case.snapshot / "manifest.json"),
        "as_of": iso(as_of),
        "execution_mode": "scenario_only",
        "lifecycles": [
            {
                "lifecycle": lifecycle_value.to_dict(),
                "source_record_sha256": hashlib.sha256(lifecycle_json).hexdigest(),
                "source_reference": "synthetic://scenario-fixture",
            }
        ],
        "basis_evidence": [
            {
                "account_id": "hk-research",
                "dividend_id": lifecycle_value.dividend_id,
                "instrument_id": lifecycle_value.instrument_id,
                "ex_at": iso(ex_at),
                "available_at": iso(ex_at - timedelta(days=1)),
                "captured_at": iso(ex_at),
                "evidence_id": f"basis:{lifecycle_value.dividend_id}",
                "evidence_source": "synthetic-position-replay",
                "certification_ref": None,
            }
        ],
        "pit_fx": pit_fx or [],
        "payment_status": payment_status or [],
        "same_instant_order": same_instant_order or [],
    }


def pit_fx(*, currency: str, available_at: datetime, rate: str = "7.8") -> dict:
    return PitFxRate(
        event_id=f"fx:{currency}:HKD:{iso(available_at)}",
        base_currency=currency,
        quote_currency="HKD",
        rate_text=rate,
        rate_convention="quote_per_base",
        observed_at=iso(available_at - timedelta(minutes=1)),
        available_at=iso(available_at),
        captured_at=iso(available_at),
        source="synthetic-software-fixture",
        evidence_id=f"evidence:fx:{currency}",
    ).to_dict()


def write_scenario(tmp_path: Path, payload: dict, name: str = "scenario.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path

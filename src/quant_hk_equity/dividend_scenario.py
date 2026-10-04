"""Software-only HK dividend scenarios with evidenced timing and replay."""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.metadata
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd
from quant_data_kit import AssetClass, FixedPoint, InstrumentSpec
from quant_data_kit.exceptions import ValidationError
from quant_data_kit.financial import DividendLifecycle, PitFxRate
from quant_data_kit.financial.calendars import CalendarBook, PurposeCalendar
from quant_data_kit.financial.common import utc
from quant_data_kit.financial.lifecycle import select_universe
from quant_data_kit.financial.status import permission_asof
from quant_data_kit.hong_kong import load_hk_snapshot, sha256
from quant_execution import (
    DividendEntitlementBasis,
    DividendExecutionMode,
    DividendExecutionPhase,
    DividendExecutionRequest,
    FxValuationMode,
    export_dividend_run,
    replay_dividend_run,
)
from quant_execution.contracts import Side
from quant_execution.hong_kong import HKDailyExecution

from quant_hk_equity.research import (
    code_revision,
    fee_schedule,
    fp,
    metrics,
    prepare,
    select,
    simulate,
    validate_config,
)

SCENARIO_SCHEMA = "quant-hk-dividend-scenario/v1"
RESULT_SCHEMA = "quant-hk-dividend-scenario-result/v1"
MANIFEST_SCHEMA = "quant-hk-dividend-scenario-manifest/v1"
EXECUTION_STATE_SCHEMA = "quant-hk-dividend-execution-state/v1"
QEXEC_COMMIT = "62a75a4bfacb445cc0809b8e1b283dbe4312050a"
QDK_COMMIT = "fd788b2956a10490aa00c399b717ec796ae371b1"
DEPENDENCY_COMMITS = {
    "quant-execution": QEXEC_COMMIT,
    "quant-data-kit": QDK_COMMIT,
    "quant-factors": "4ff2ab39f1557c6c1247c1406a007fbd923196fd",
    "quant-lab": "8b87aa176d1b0eaacbe918593b83bbec6d1baa33",
}
DEPENDENCY_MODULES = {
    "quant-execution": "quant_execution",
    "quant-data-kit": "quant_data_kit",
    "quant-factors": "quant_factors",
    "quant-lab": "quant_lab",
}


class ScenarioValidationError(ValueError):
    """Stable, fail-closed scenario validation error."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class _ConversionPolicyRequired(Exception):
    def __init__(self, dividend_id: str):
        self.dividend_id = dividend_id
        super().__init__(dividend_id)


@dataclass(frozen=True)
class LifecycleEnvelope:
    lifecycle: DividendLifecycle
    source_record_sha256: str
    source_reference: str


@dataclass(frozen=True)
class BasisEvidence:
    account_id: str
    dividend_id: str
    instrument_id: str
    ex_at: datetime
    available_at: datetime
    captured_at: datetime
    evidence_id: str
    evidence_source: str
    certification_ref: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "dividend_id": self.dividend_id,
            "instrument_id": self.instrument_id,
            "ex_at": _timestamp(self.ex_at),
            "available_at": _timestamp(self.available_at),
            "captured_at": _timestamp(self.captured_at),
            "evidence_id": self.evidence_id,
            "evidence_source": self.evidence_source,
            "certification_ref": self.certification_ref,
        }


@dataclass(frozen=True)
class PaymentReceiptStatusEvidence:
    evidence_id: str
    dividend_id: str
    account_id: str
    status: str
    status_at: datetime
    available_at: datetime
    captured_at: datetime
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "dividend_id": self.dividend_id,
            "account_id": self.account_id,
            "status": self.status,
            "status_at": _timestamp(self.status_at),
            "available_at": _timestamp(self.available_at),
            "captured_at": _timestamp(self.captured_at),
            "source": self.source,
        }


@dataclass(frozen=True)
class SameInstantOrder:
    timestamp: datetime
    before_event_id: str
    after_event_id: str
    evidence_id: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "timestamp": _timestamp(self.timestamp),
            "before_event_id": self.before_event_id,
            "after_event_id": self.after_event_id,
            "evidence_id": self.evidence_id,
            "source": self.source,
        }


@dataclass(frozen=True)
class HKDividendScenarioInput:
    scenario_id: str
    study_config_sha256: str
    snapshot_manifest_sha256: str
    as_of: datetime
    lifecycles: tuple[LifecycleEnvelope, ...]
    basis_evidence: tuple[BasisEvidence, ...]
    pit_fx: tuple[PitFxRate, ...]
    payment_status: tuple[PaymentReceiptStatusEvidence, ...]
    same_instant_order: tuple[SameInstantOrder, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCENARIO_SCHEMA,
            "scenario_id": self.scenario_id,
            "study_config_sha256": self.study_config_sha256,
            "snapshot_manifest_sha256": self.snapshot_manifest_sha256,
            "as_of": _timestamp(self.as_of),
            "execution_mode": "scenario_only",
            "lifecycles": [
                {
                    "lifecycle": item.lifecycle.to_dict(),
                    "source_record_sha256": item.source_record_sha256,
                    "source_reference": item.source_reference,
                }
                for item in self.lifecycles
            ],
            "basis_evidence": [item.to_dict() for item in self.basis_evidence],
            "pit_fx": [item.to_dict() for item in self.pit_fx],
            "payment_status": [item.to_dict() for item in self.payment_status],
            "same_instant_order": [item.to_dict() for item in self.same_instant_order],
        }


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    event_time: datetime
    kind: str
    source_identity: str
    payload: object = None

    def public(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "event_time": _timestamp(self.event_time),
            "kind": self.kind,
            "source_identity": self.source_identity,
        }


@dataclass
class ScenarioExecution:
    account: HKDailyExecution
    returns: pd.DataFrame
    orders: pd.DataFrame
    costs: pd.DataFrame
    positions: pd.DataFrame
    signals: pd.DataFrame
    timeline: list[dict[str, object]]
    summary: dict[str, object]
    applied_phases: dict[str, tuple[str, ...]]
    final_snapshot: object


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenarioValidationError("SCHEMA_INVALID", f"{context} must be an object")
    return value


def _keys(value: Mapping[str, Any], required: set[str], context: str) -> None:
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        raise ScenarioValidationError(
            "SCHEMA_INVALID", f"{context} keys differ: missing={missing}, extra={extra}"
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioValidationError("SCHEMA_INVALID", f"{field} must be non-empty text")
    return value.strip()


def _hash(value: object, field: str) -> str:
    text = _text(value, field).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ScenarioValidationError("SCHEMA_INVALID", f"{field} must be SHA-256")
    return text


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ScenarioValidationError("TIMING_UNRESOLVED", f"{field} must be an exact UTC time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ScenarioValidationError(
            "TIMING_UNRESOLVED", f"{field} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != pd.Timedelta(0):
        raise ScenarioValidationError("TIMING_UNRESOLVED", f"{field} must use UTC")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fact_times(fact: object) -> tuple[datetime, datetime]:
    evidence = getattr(fact, "evidence", None)
    if evidence is None:
        raise ScenarioValidationError("POLICY_EVIDENCE_MISSING", "phase fact lacks evidence")
    return utc(evidence.timing.effective_at).to_pydatetime(), utc(
        evidence.timing.available_at
    ).to_pydatetime()


def _phase_facts(lifecycle: DividendLifecycle, phase: DividendExecutionPhase) -> list[object]:
    if phase is DividendExecutionPhase.ENTITLEMENT:
        values = [lifecycle.entitlement]
        if lifecycle.payment_policy is not None:
            values.append(lifecycle.payment_policy)
        return values
    if phase is DividendExecutionPhase.ISSUER_CONVERSION:
        values = [lifecycle.election]
        if lifecycle.conversion is not None:
            values.append(lifecycle.conversion)
        return [item for item in values if item is not None]
    return [
        item
        for item in (
            lifecycle.election,
            lifecycle.conversion,
            lifecycle.payment_policy,
            lifecycle.payment,
        )
        if item is not None
    ]


def _phase_applied_at(lifecycle: DividendLifecycle, phase: DividendExecutionPhase) -> datetime:
    facts = _phase_facts(lifecycle, phase)
    if not facts:
        raise ScenarioValidationError("PHASE_DEPENDENCY_UNRESOLVED", f"{phase.value} has no facts")
    points = [point for fact in facts for point in _fact_times(fact)]
    return max(points)


def _known_at(fact: object | None, cutoff: datetime) -> bool:
    if fact is None:
        return False
    effective, available = _fact_times(fact)
    return effective <= cutoff and available <= cutoff


def _phase_prefix(
    lifecycle: DividendLifecycle,
    phase: DividendExecutionPhase,
    cutoff: datetime,
) -> DividendLifecycle:
    proposal = lifecycle.proposal if _known_at(lifecycle.proposal, cutoff) else None
    policy = lifecycle.payment_policy if _known_at(lifecycle.payment_policy, cutoff) else None
    if phase is DividendExecutionPhase.ENTITLEMENT:
        return replace(
            lifecycle,
            proposal=proposal,
            election=None,
            conversion=None,
            payment_policy=policy,
            payment=None,
        )
    if phase is DividendExecutionPhase.ISSUER_CONVERSION:
        return replace(
            lifecycle,
            proposal=proposal,
            payment_policy=policy,
            payment=None,
        )
    return replace(lifecycle, proposal=proposal, payment_policy=policy)


def _load_basis(value: object) -> BasisEvidence:
    item = _mapping(value, "basis evidence")
    _keys(
        item,
        {
            "account_id",
            "dividend_id",
            "instrument_id",
            "ex_at",
            "available_at",
            "captured_at",
            "evidence_id",
            "evidence_source",
            "certification_ref",
        },
        "basis evidence",
    )
    basis = BasisEvidence(
        account_id=_text(item["account_id"], "account_id"),
        dividend_id=_text(item["dividend_id"], "dividend_id"),
        instrument_id=_text(item["instrument_id"], "instrument_id"),
        ex_at=_time(item["ex_at"], "ex_at"),
        available_at=_time(item["available_at"], "basis.available_at"),
        captured_at=_time(item["captured_at"], "basis.captured_at"),
        evidence_id=_text(item["evidence_id"], "basis.evidence_id"),
        evidence_source=_text(item["evidence_source"], "basis.evidence_source"),
        certification_ref=(
            _text(item["certification_ref"], "certification_ref")
            if item["certification_ref"] is not None
            else None
        ),
    )
    if basis.account_id != "hk-research":
        raise ScenarioValidationError("IDENTITY_MISMATCH", "basis account must be hk-research")
    if basis.available_at > basis.ex_at or basis.available_at > basis.captured_at:
        raise ScenarioValidationError("LATE_ENTITLEMENT_UNSUPPORTED", basis.dividend_id)
    return basis


def _load_payment_status(value: object, as_of: datetime) -> PaymentReceiptStatusEvidence:
    item = _mapping(value, "payment status")
    _keys(
        item,
        {
            "evidence_id",
            "dividend_id",
            "account_id",
            "status",
            "status_at",
            "available_at",
            "captured_at",
            "source",
        },
        "payment status",
    )
    status = PaymentReceiptStatusEvidence(
        evidence_id=_text(item["evidence_id"], "payment_status.evidence_id"),
        dividend_id=_text(item["dividend_id"], "payment_status.dividend_id"),
        account_id=_text(item["account_id"], "payment_status.account_id"),
        status=_text(item["status"], "payment_status.status"),
        status_at=_time(item["status_at"], "payment_status.status_at"),
        available_at=_time(item["available_at"], "payment_status.available_at"),
        captured_at=_time(item["captured_at"], "payment_status.captured_at"),
        source=_text(item["source"], "payment_status.source"),
    )
    if (
        status.account_id != "hk-research"
        or status.status != "not_received"
        or status.status_at != as_of
        or status.captured_at != as_of
        or status.available_at > status.captured_at
    ):
        raise ScenarioValidationError(
            "PAYMENT_STATUS_INVALID",
            "status must prove not_received for this account at exact as_of",
        )
    return status


def _load_same_instant(value: object) -> SameInstantOrder:
    item = _mapping(value, "same instant order")
    _keys(
        item,
        {"timestamp", "before_event_id", "after_event_id", "evidence_id", "source"},
        "same instant order",
    )
    result = SameInstantOrder(
        timestamp=_time(item["timestamp"], "same_instant_order.timestamp"),
        before_event_id=_text(item["before_event_id"], "before_event_id"),
        after_event_id=_text(item["after_event_id"], "after_event_id"),
        evidence_id=_text(item["evidence_id"], "same_instant_order.evidence_id"),
        source=_text(item["source"], "same_instant_order.source"),
    )
    if result.before_event_id == result.after_event_id:
        raise ScenarioValidationError("AMBIGUOUS_EVENT_ORDER", "self edge is invalid")
    return result


def load_dividend_scenario(value: object) -> HKDividendScenarioInput:
    payload = _mapping(value, "scenario")
    _keys(
        payload,
        {
            "schema",
            "scenario_id",
            "study_config_sha256",
            "snapshot_manifest_sha256",
            "as_of",
            "execution_mode",
            "lifecycles",
            "basis_evidence",
            "pit_fx",
            "payment_status",
            "same_instant_order",
        },
        "scenario",
    )
    if payload["schema"] != SCENARIO_SCHEMA or payload["execution_mode"] != "scenario_only":
        raise ScenarioValidationError("SCHEMA_INVALID", "unsupported scenario schema or mode")
    for field in ("lifecycles", "basis_evidence", "pit_fx", "payment_status", "same_instant_order"):
        if not isinstance(payload[field], list):
            raise ScenarioValidationError("SCHEMA_INVALID", f"{field} must be an array")
    as_of = _time(payload["as_of"], "as_of")
    envelopes = []
    for raw in payload["lifecycles"]:
        item = _mapping(raw, "lifecycle envelope")
        _keys(
            item,
            {"lifecycle", "source_record_sha256", "source_reference"},
            "lifecycle envelope",
        )
        try:
            lifecycle = DividendLifecycle.from_dict(item["lifecycle"])
        except (TypeError, ValueError) as exc:
            raise ScenarioValidationError("LIFECYCLE_INVALID", str(exc)) from exc
        envelopes.append(
            LifecycleEnvelope(
                lifecycle=lifecycle,
                source_record_sha256=_hash(item["source_record_sha256"], "source_record_sha256"),
                source_reference=_text(item["source_reference"], "source_reference"),
            )
        )
    bases = tuple(_load_basis(item) for item in payload["basis_evidence"])
    statuses = tuple(_load_payment_status(item, as_of) for item in payload["payment_status"])
    orders = tuple(_load_same_instant(item) for item in payload["same_instant_order"])
    try:
        rates = tuple(PitFxRate.from_dict(item) for item in payload["pit_fx"])
    except (TypeError, ValueError) as exc:
        raise ScenarioValidationError("PIT_FX_INVALID", str(exc)) from exc
    result = HKDividendScenarioInput(
        scenario_id=_text(payload["scenario_id"], "scenario_id"),
        study_config_sha256=_hash(payload["study_config_sha256"], "study_config_sha256"),
        snapshot_manifest_sha256=_hash(
            payload["snapshot_manifest_sha256"], "snapshot_manifest_sha256"
        ),
        as_of=as_of,
        lifecycles=tuple(envelopes),
        basis_evidence=bases,
        pit_fx=rates,
        payment_status=statuses,
        same_instant_order=orders,
    )
    _validate_scenario_identities(result)
    return result


def _all_lifecycle_facts(lifecycle: DividendLifecycle) -> list[object]:
    values = [
        lifecycle.proposal,
        lifecycle.entitlement,
        lifecycle.election,
        lifecycle.conversion,
        lifecycle.payment,
        lifecycle.payment_policy,
    ]
    return [item for item in values if item is not None]


def _validate_scenario_identities(scenario: HKDividendScenarioInput) -> None:
    lifecycle_ids = [item.lifecycle.dividend_id for item in scenario.lifecycles]
    if len(lifecycle_ids) != len(set(lifecycle_ids)):
        raise ScenarioValidationError("DUPLICATE_EVENT_ID", "duplicate dividend_id")
    lifecycle_by_id = {item.lifecycle.dividend_id: item.lifecycle for item in scenario.lifecycles}
    basis_by_id = {item.dividend_id: item for item in scenario.basis_evidence}
    if len(basis_by_id) != len(scenario.basis_evidence) or set(basis_by_id) != set(lifecycle_by_id):
        raise ScenarioValidationError(
            "ENTITLEMENT_BASIS_IDENTITY_MISMATCH", "one basis is required per lifecycle"
        )
    event_ids: list[str] = []
    for lifecycle in lifecycle_by_id.values():
        basis = basis_by_id[lifecycle.dividend_id]
        if basis.instrument_id != lifecycle.instrument_id:
            raise ScenarioValidationError(
                "ENTITLEMENT_BASIS_IDENTITY_MISMATCH", lifecycle.dividend_id
            )
        for fact in _all_lifecycle_facts(lifecycle):
            evidence = getattr(fact, "evidence", None)
            if evidence is not None:
                event_ids.append(evidence.event_id)
                if utc(evidence.timing.available_at).to_pydatetime() > scenario.as_of:
                    raise ScenarioValidationError("FACT_NOT_KNOWN_AS_OF", evidence.event_id)
        policy = lifecycle.payment_policy
        if policy is not None:
            if policy.evidence is None:
                raise ScenarioValidationError("POLICY_EVIDENCE_MISSING", lifecycle.dividend_id)
            if policy.account_id != "hk-research":
                raise ScenarioValidationError("IDENTITY_MISMATCH", policy.policy_id)
        entitlement_effective, entitlement_available = _fact_times(lifecycle.entitlement)
        if entitlement_effective > basis.ex_at or entitlement_available > basis.ex_at:
            raise ScenarioValidationError("LATE_ENTITLEMENT_UNSUPPORTED", lifecycle.dividend_id)
        if basis.captured_at > scenario.as_of:
            raise ScenarioValidationError("FACT_NOT_KNOWN_AS_OF", basis.evidence_id)
        selected = lifecycle.election.payment_currency if lifecycle.election is not None else None
        declared = lifecycle.entitlement.declared_currency.calculation_currency
        if selected is not None and selected != declared and lifecycle.conversion is None:
            raise ScenarioValidationError("ISSUER_CONVERSION_REQUIRED", lifecycle.dividend_id)
        if lifecycle.payment is not None:
            payment_effective, payment_available = _fact_times(lifecycle.payment)
            if payment_effective > payment_available:
                raise ScenarioValidationError("FUTURE_PAYMENT_EFFECTIVE", lifecycle.dividend_id)
            for fact in _phase_facts(lifecycle, DividendExecutionPhase.PAYMENT):
                effective, available = _fact_times(fact)
                if effective > scenario.as_of or available > scenario.as_of:
                    raise ScenarioValidationError(
                        "PAYMENT_PREREQUISITE_NOT_EFFECTIVE",
                        fact.evidence.event_id,
                    )
    event_ids.extend(rate.event_id for rate in scenario.pit_fx)
    if len(event_ids) != len(set(event_ids)):
        raise ScenarioValidationError("DUPLICATE_EVENT_ID", "external event IDs must be global")
    if any(utc(rate.available_at).to_pydatetime() > scenario.as_of for rate in scenario.pit_fx):
        raise ScenarioValidationError("FACT_NOT_KNOWN_AS_OF", "PIT FX after as_of")
    status_ids = [item.evidence_id for item in scenario.payment_status]
    status_dividends = [item.dividend_id for item in scenario.payment_status]
    if len(status_ids) != len(set(status_ids)) or len(status_dividends) != len(
        set(status_dividends)
    ):
        raise ScenarioValidationError("DUPLICATE_EVENT_ID", "duplicate payment status")
    if not set(status_dividends) <= set(lifecycle_by_id):
        raise ScenarioValidationError("IDENTITY_MISMATCH", "unknown payment status dividend")
    statuses = {item.dividend_id: item for item in scenario.payment_status}
    for lifecycle in lifecycle_by_id.values():
        scheduled = date.fromisoformat(lifecycle.entitlement.scheduled_payment_date)
        if (
            lifecycle.payment is None
            and scheduled <= scenario.as_of.date()
            and lifecycle.dividend_id not in statuses
        ):
            raise ScenarioValidationError("PAYMENT_STATUS_UNKNOWN", lifecycle.dividend_id)


def _instrument_specs(config: Mapping[str, Any]) -> dict[str, InstrumentSpec]:
    assumption_start = pd.Timestamp(config["data_start"], tz="UTC").to_pydatetime()
    return {
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


def _expected_phase_events(
    scenario: HKDividendScenarioInput,
    opening: datetime,
    conversion_policy_required: frozenset[str] = frozenset(),
) -> list[TimelineEvent]:
    bases = {item.dividend_id: item for item in scenario.basis_evidence}
    events: list[TimelineEvent] = []
    for envelope in scenario.lifecycles:
        lifecycle = envelope.lifecycle
        basis = bases[lifecycle.dividend_id]
        if basis.ex_at < opening:
            raise ScenarioValidationError(
                "PRE_ACCOUNT_ENTITLEMENT_UNSUPPORTED", lifecycle.dividend_id
            )
        if basis.ex_at > scenario.as_of:
            raise ScenarioValidationError(
                "ENTITLEMENT_AFTER_AS_OF", "future entitlements are outside scenario v1"
            )
        events.append(
            TimelineEvent(
                event_id=lifecycle.entitlement.evidence.event_id,
                event_time=basis.ex_at,
                kind="dividend_entitlement",
                source_identity=lifecycle.dividend_id,
                payload=(lifecycle, basis, DividendExecutionPhase.ENTITLEMENT),
            )
        )
        parent_at = basis.ex_at
        if lifecycle.election is not None:
            conversion_at = _phase_applied_at(lifecycle, DividendExecutionPhase.ISSUER_CONVERSION)
            if lifecycle.dividend_id in conversion_policy_required:
                if lifecycle.payment_policy is None:
                    raise ScenarioValidationError("ROUNDING_POLICY_REQUIRED", lifecycle.dividend_id)
                conversion_at = max(conversion_at, *_fact_times(lifecycle.payment_policy))
            if conversion_at < parent_at:
                raise ScenarioValidationError(
                    "PHASE_TIME_CONTRADICTION", f"{lifecycle.dividend_id}:issuer_conversion"
                )
            if conversion_at <= scenario.as_of:
                phase_event = lifecycle.conversion or lifecycle.election
                events.append(
                    TimelineEvent(
                        event_id=phase_event.evidence.event_id,
                        event_time=conversion_at,
                        kind="issuer_conversion",
                        source_identity=lifecycle.dividend_id,
                        payload=(lifecycle, None, DividendExecutionPhase.ISSUER_CONVERSION),
                    )
                )
            parent_at = conversion_at
        if lifecycle.payment is not None:
            payment_at = _phase_applied_at(lifecycle, DividendExecutionPhase.PAYMENT)
            if lifecycle.election is None or payment_at < parent_at:
                raise ScenarioValidationError(
                    "PHASE_TIME_CONTRADICTION", f"{lifecycle.dividend_id}:payment"
                )
            if payment_at <= scenario.as_of:
                events.append(
                    TimelineEvent(
                        event_id=lifecycle.payment.evidence.event_id,
                        event_time=payment_at,
                        kind="payment",
                        source_identity=lifecycle.dividend_id,
                        payload=(lifecycle, None, DividendExecutionPhase.PAYMENT),
                    )
                )
    return events


def _unique_group_order(
    group: list[TimelineEvent],
    explicit: tuple[SameInstantOrder, ...],
) -> list[TimelineEvent]:
    by_id = {item.event_id: item for item in group}
    edges: set[tuple[str, str]] = set()
    kinds = {item.kind: item.event_id for item in group}
    if "open_marks" in kinds and "rebalance_trigger" in kinds:
        edges.add((kinds["open_marks"], kinds["rebalance_trigger"]))
    if "close_marks" in kinds and "settlement" in kinds:
        edges.add((kinds["close_marks"], kinds["settlement"]))
    final = kinds.get("final_valuation")
    if final is not None:
        edges.update((item.event_id, final) for item in group if item.event_id != final)
    phase_rank = {"dividend_entitlement": 0, "issuer_conversion": 1, "payment": 2}
    for left in group:
        for right in group:
            if (
                left.source_identity == right.source_identity
                and left.kind in phase_rank
                and right.kind in phase_rank
                and phase_rank[left.kind] < phase_rank[right.kind]
            ):
                edges.add((left.event_id, right.event_id))
    for order in explicit:
        if order.timestamp != group[0].event_time:
            continue
        if order.before_event_id not in by_id or order.after_event_id not in by_id:
            raise ScenarioValidationError(
                "AMBIGUOUS_EVENT_ORDER", f"ordering evidence {order.evidence_id} has unknown event"
            )
        edges.add((order.before_event_id, order.after_event_id))
    incoming = {event_id: 0 for event_id in by_id}
    outgoing = {event_id: set() for event_id in by_id}
    for before, after in edges:
        if after not in outgoing[before]:
            outgoing[before].add(after)
            incoming[after] += 1
    ordered: list[TimelineEvent] = []
    remaining = set(by_id)
    while remaining:
        ready = sorted(event_id for event_id in remaining if incoming[event_id] == 0)
        if len(ready) != 1:
            raise ScenarioValidationError(
                "AMBIGUOUS_EVENT_ORDER",
                f"{_timestamp(group[0].event_time)} unresolved={sorted(remaining)}",
            )
        current = ready[0]
        remaining.remove(current)
        ordered.append(by_id[current])
        for after in outgoing[current]:
            incoming[after] -= 1
    return ordered


def build_scenario_timeline(
    calendar: pd.DataFrame,
    config: Mapping[str, Any],
    scenario: HKDividendScenarioInput,
    *,
    conversion_policy_required: frozenset[str] = frozenset(),
) -> tuple[TimelineEvent, ...]:
    sessions = calendar.loc[calendar.date.between(config["test_start"], config["data_end"])]
    if sessions.empty:
        raise ScenarioValidationError("PRICE_COVERAGE_MISSING", "empty holdout calendar")
    opening = sessions.iloc[0].open.to_pydatetime()
    last_close = sessions.iloc[-1].close.to_pydatetime()
    if scenario.as_of < sessions.iloc[0].close.to_pydatetime() or scenario.as_of > last_close:
        raise ScenarioValidationError(
            "PRICE_COVERAGE_MISSING", "as_of must include one holdout close and not exceed coverage"
        )
    events: list[TimelineEvent] = []
    for index, session in enumerate(sessions.itertuples()):
        session_date = str(session.date.date())
        at_open = session.open.to_pydatetime()
        at_close = session.close.to_pydatetime()
        if at_open <= scenario.as_of:
            events.append(
                TimelineEvent(
                    f"session:{session_date}:open-marks",
                    at_open,
                    "open_marks",
                    session_date,
                    session_date,
                )
            )
            if index % config["rebalance_sessions"] == 0:
                events.append(
                    TimelineEvent(
                        f"session:{session_date}:rebalance",
                        at_open,
                        "rebalance_trigger",
                        session_date,
                        session_date,
                    )
                )
        if at_close <= scenario.as_of:
            events.extend(
                (
                    TimelineEvent(
                        f"session:{session_date}:close-marks",
                        at_close,
                        "close_marks",
                        session_date,
                        session_date,
                    ),
                    TimelineEvent(
                        f"session:{session_date}:settlement",
                        at_close,
                        "settlement",
                        session_date,
                        session_date,
                    ),
                )
            )
    events.extend(_expected_phase_events(scenario, opening, conversion_policy_required))
    events.extend(
        TimelineEvent(
            event_id=rate.event_id,
            event_time=utc(rate.available_at).to_pydatetime(),
            kind="pit_fx",
            source_identity=rate.evidence_id,
            payload=rate,
        )
        for rate in scenario.pit_fx
    )
    events.append(
        TimelineEvent(
            "scenario:final-valuation",
            scenario.as_of,
            "final_valuation",
            scenario.scenario_id,
        )
    )
    identifiers = [item.event_id for item in events]
    if len(identifiers) != len(set(identifiers)):
        raise ScenarioValidationError("DUPLICATE_EVENT_ID", "timeline event IDs collide")
    groups: dict[datetime, list[TimelineEvent]] = {}
    for event in events:
        groups.setdefault(event.event_time, []).append(event)
    evidence_times = {item.timestamp for item in scenario.same_instant_order}
    if not evidence_times <= set(groups):
        raise ScenarioValidationError(
            "AMBIGUOUS_EVENT_ORDER", "ordering evidence timestamp has no events"
        )
    ordered: list[TimelineEvent] = []
    for event_time in sorted(groups):
        group = groups[event_time]
        ordered.extend(
            group if len(group) == 1 else _unique_group_order(group, scenario.same_instant_order)
        )
    return tuple(ordered)


def _settlement_days(
    config: Mapping[str, Any], calendar: pd.DataFrame, at: datetime, session_date: pd.Timestamp
) -> list[date]:
    financial = config.get("financial")
    if not financial:
        return calendar.date.dt.date.tolist()
    purpose_book = CalendarBook([PurposeCalendar(**item) for item in financial["calendars"]])
    settlement = purpose_book.asof(
        financial["settlement_calendar_id"], "settlement", at, session_date
    )
    return pd.DatetimeIndex(settlement.open_days).date.tolist()


def _runtime_state(account: HKDailyExecution) -> dict[str, object]:
    executed = []
    for order_id, (signature, fill, charges) in account.executed.items():
        symbol, quantity, side, price, at = signature
        signature_payload = {
            "symbol": symbol,
            "quantity": quantity,
            "side": side.value,
            "price": {"units": price.units, "scale": price.scale},
            "at": _timestamp(at),
        }
        charge_payload = {
            key: {"units": value.units, "scale": value.scale}
            for key, value in sorted(charges.items())
        }
        executed.append(
            {
                "order_id": order_id,
                "signature_sha256": _sha_bytes(_canonical_bytes(signature_payload)),
                "fill_id": fill.fill_id,
                "charges_sha256": _sha_bytes(_canonical_bytes(charge_payload)),
            }
        )
    return {
        "last_settlement_day": (
            account.last_settlement_day.isoformat()
            if account.last_settlement_day is not None
            else None
        ),
        "pending_sale_proceeds": [
            {"settlement_date": due.isoformat(), "amount_hkd": str(amount)}
            for due, amount in account.pending
        ],
        "executed_orders": executed,
        "cash_balance_hkd": str(account.ledger.cash_balance("HKD")),
        "available_cash_hkd": str(account.available_cash()),
        "journal_sha256": account.ledger.journal_sha256,
    }


def _state_sha(account: HKDailyExecution) -> str:
    return _sha_bytes(_canonical_bytes(_runtime_state(account)))


def _selected_symbols(
    frame: pd.DataFrame,
    strategy: str,
    config: Mapping[str, Any],
    at: datetime,
) -> list[str]:
    selected = select(frame.reset_index(drop=True), strategy, config)
    financial = config.get("financial")
    if financial:
        eligible = select_universe(
            pd.DataFrame(financial["lifecycle"]),
            at,
            at,
            universe_id=financial.get("universe_id"),
        )
        selected = [symbol for symbol in selected if symbol in eligible.eligible]
    return selected


def _execute_rebalance(
    *,
    account: HKDailyExecution,
    frame: pd.DataFrame,
    strategy: str,
    config: Mapping[str, Any],
    specs: Mapping[str, InstrumentSpec],
    at: datetime,
    scenario_id: str,
    orders: list[dict[str, object]],
    costs: list[dict[str, object]],
    signals: list[dict[str, object]],
) -> tuple[Decimal, list[str]]:
    selected = _selected_symbols(frame, strategy, config, at)
    opening_snapshot = account.ledger.snapshot(at)
    opening_nav = opening_snapshot.nav.to_decimal()
    target: dict[str, int] = {}
    session_date = at.date()
    for symbol, row in frame.iterrows():
        budget = (
            opening_nav * Decimal(str(config["invested_fraction"])) / len(selected)
            if symbol in selected
            else Decimal(0)
        )
        lot = int(specs[symbol].metadata["lot_size"])
        target[symbol] = int(budget / Decimal(str(row.open))) // lot * lot
        signals.append(
            {
                "date": session_date.isoformat(),
                "symbol": symbol,
                "signal_date": str(row.signal_date.date()),
                "selected": symbol in selected,
                "target_quantity": target[symbol],
            }
        )
    generated: list[str] = []
    total_cost = Decimal(0)
    holding = opening_snapshot.positions
    financial = config.get("financial")
    for side in (Side.SELL, Side.BUY):
        for symbol in sorted(specs):
            held = int(holding.get(symbol, FixedPoint(0, 0)).to_decimal())
            delta = target[symbol] - held
            if (side is Side.BUY and delta <= 0) or (side is Side.SELL and delta >= 0):
                continue
            row = frame.loc[symbol]
            requested = abs(delta)
            quantity = requested
            if financial:
                permission = permission_asof(pd.DataFrame(financial["status"]), symbol, at, at)
                allowed = permission.buy if side is Side.BUY else permission.sell
                if allowed != "tradable":
                    orders.append(
                        {
                            "timestamp": _timestamp(at),
                            "strategy": strategy,
                            "symbol": symbol,
                            "side": side.value,
                            "quantity": 0,
                            "requested_quantity": requested,
                            "status": f"blocked_{allowed}",
                            "reason": permission.reason,
                        }
                    )
                    continue
            if side is Side.BUY:
                quantity = min(
                    quantity,
                    account.affordable_quantity(
                        symbol, fp(row.open), account.available_cash(), session_date
                    ),
                )
            status = "filled" if quantity else "rejected_settled_cash"
            order_id = f"{scenario_id}:{strategy}:{session_date}:{symbol}:{side.value}"
            order = {
                "timestamp": _timestamp(at),
                "strategy": strategy,
                "symbol": symbol,
                "side": side.value,
                "quantity": quantity,
                "requested_quantity": requested,
                "target_weight": (
                    str(Decimal(str(config["invested_fraction"])) / len(selected))
                    if symbol in selected
                    else "0"
                ),
                "order_type": "daily_open_scenario",
                "status": status,
                "order_id": order_id,
            }
            orders.append(order)
            generated.append(order_id)
            if not quantity:
                continue
            _, charges = account.execute(
                order_id=order_id,
                symbol=symbol,
                quantity=quantity,
                side=side,
                price=fp(row.open),
                at=at,
            )
            charge_values = {key: value.to_decimal() for key, value in charges.items()}
            charge_total = sum(charge_values.values(), Decimal(0))
            total_cost += charge_total
            costs.append(
                {
                    "date": session_date.isoformat(),
                    "strategy": strategy,
                    "symbol": symbol,
                    **{key: str(value) for key, value in charge_values.items()},
                    "market_impact": "0",
                    "borrow_cost": "0",
                    "total_cost": str(charge_total),
                }
            )
    return total_cost, generated


def _empty_frame(rows: list[dict[str, object]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=columns)


def _execute_holdout(
    *,
    prepared: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    config: Mapping[str, Any],
    scenario: HKDividendScenarioInput,
    strategy: str,
    timeline: tuple[TimelineEvent, ...],
) -> ScenarioExecution:
    sessions = calendar.loc[calendar.date.between(config["test_start"], config["data_end"])]
    opening = sessions.iloc[0].open.to_pydatetime()
    specs = _instrument_specs(config)
    account = HKDailyExecution(
        specs,
        initial_cash=fp(config["initial_cash"], 2),
        opened_at=opening,
        fees=fee_schedule(config),
        settlement_days=calendar.date.dt.date.tolist(),
        dividend_execution_mode=DividendExecutionMode.SCENARIO_ONLY,
        fx_valuation_mode=FxValuationMode.EVIDENCED_PIT,
    )
    session_rows = {str(row.date.date()): row for row in sessions.itertuples()}
    frames = {
        str(day.date()): prepared[prepared.date.eq(day)].set_index("symbol", drop=False)
        for day in sessions.date
    }
    returns_rows: list[dict[str, object]] = []
    order_rows: list[dict[str, object]] = []
    cost_rows: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    signal_rows: list[dict[str, object]] = []
    timeline_rows: list[dict[str, object]] = []
    day_costs: dict[str, Decimal] = {}
    previous_nav = Decimal(str(config["initial_cash"]))
    applied: dict[str, list[str]] = {}
    final_snapshot = None
    for event in timeline:
        before = _state_sha(account)
        generated: list[str] = []
        qexec_record: dict[str, object] | None = None
        if event.kind == "pit_fx":
            record = account.ledger.observe_pit_fx(event.payload)
            qexec_record = record.to_dict()
        elif event.kind in {"dividend_entitlement", "issuer_conversion", "payment"}:
            lifecycle, basis_evidence, phase = event.payload
            basis = None
            if phase is DividendExecutionPhase.ENTITLEMENT:
                snapshot = account.ledger.snapshot(event.event_time)
                quantity = snapshot.positions.get(lifecycle.instrument_id, FixedPoint(0, 0))
                basis = DividendEntitlementBasis(
                    account_id=basis_evidence.account_id,
                    dividend_id=basis_evidence.dividend_id,
                    instrument_id=basis_evidence.instrument_id,
                    ex_at=basis_evidence.ex_at,
                    entitled_quantity=quantity,
                    available_at=basis_evidence.available_at,
                    captured_at=basis_evidence.captured_at,
                    evidence_id=basis_evidence.evidence_id,
                    evidence_source=basis_evidence.evidence_source,
                    certification_ref=basis_evidence.certification_ref,
                )
            prefix = _phase_prefix(lifecycle, phase, event.event_time)
            try:
                record = account.ledger.apply_dividend_lifecycle(
                    DividendExecutionRequest(
                        lifecycle=prefix,
                        phase=phase,
                        cutoff=event.event_time,
                        entitlement_basis=basis,
                    )
                )
            except ValidationError as exc:
                if (
                    phase is DividendExecutionPhase.ISSUER_CONVERSION
                    and str(exc) == "ROUNDING_POLICY_REQUIRED"
                ):
                    raise _ConversionPolicyRequired(lifecycle.dividend_id) from exc
                raise
            if record.applied_at != event.event_time:
                raise ScenarioValidationError(
                    "PHASE_APPLIED_AT_MISMATCH",
                    f"{event.event_id}: scheduled={_timestamp(event.event_time)} "
                    f"actual={_timestamp(record.applied_at)}",
                )
            applied.setdefault(lifecycle.dividend_id, []).append(phase.value)
            qexec_record = record.to_dict()
        elif event.kind in {"open_marks", "close_marks"}:
            frame = frames[event.source_identity]
            column = "open" if event.kind == "open_marks" else "close"
            for symbol in sorted(specs):
                account.mark(symbol, fp(frame.loc[symbol, column]), event.event_time)
            if event.kind == "close_marks":
                snapshot = account.ledger.snapshot(event.event_time)
                nav = snapshot.nav.to_decimal()
                costs = day_costs.get(event.source_identity, Decimal(0))
                returns_rows.append(
                    {
                        "date": event.source_identity,
                        "strategy": strategy,
                        "gross_return": str((nav + costs) / previous_nav - 1),
                        "net_return": str(nav / previous_nav - 1),
                        "nav": str(nav),
                        "cash_hkd": str(account.ledger.cash_balance("HKD")),
                        "available_cash_hkd": str(account.available_cash()),
                        "cost_hkd": str(costs),
                    }
                )
                for symbol, quantity in snapshot.positions.items():
                    market_value = quantity.to_decimal() * Decimal(str(frame.loc[symbol, "close"]))
                    position_rows.append(
                        {
                            "date": event.source_identity,
                            "strategy": strategy,
                            "symbol": symbol,
                            "quantity": int(quantity.to_decimal()),
                            "market_value": str(market_value),
                            "weight": str(market_value / nav),
                            "side": "long",
                        }
                    )
                previous_nav = nav
        elif event.kind == "rebalance_trigger":
            session = session_rows[event.source_identity]
            account.settlement_days = _settlement_days(
                config, calendar, event.event_time, session.date
            )
            cost, generated = _execute_rebalance(
                account=account,
                frame=frames[event.source_identity],
                strategy=strategy,
                config=config,
                specs=specs,
                at=event.event_time,
                scenario_id=scenario.scenario_id,
                orders=order_rows,
                costs=cost_rows,
                signals=signal_rows,
            )
            day_costs[event.source_identity] = (
                day_costs.get(event.source_identity, Decimal(0)) + cost
            )
        elif event.kind == "settlement":
            account.settle_end_of_day(date.fromisoformat(event.source_identity))
        elif event.kind == "final_valuation":
            valuation = account.ledger.record_dividend_valuation(as_of=event.event_time)
            qexec_record = valuation.to_dict()
            final_snapshot = account.ledger.snapshot(event.event_time)
        else:
            raise ScenarioValidationError("SCHEMA_INVALID", f"unknown event kind {event.kind}")
        after = _state_sha(account)
        timeline_rows.append(
            {
                **event.public(),
                "before_state_sha256": before,
                "after_state_sha256": after,
                "generated_order_ids": generated,
                "qexec_record": qexec_record,
            }
        )
    if final_snapshot is None:
        raise ScenarioValidationError("VALUATION_MISSING", "final valuation was not recorded")
    returns = _empty_frame(
        returns_rows,
        [
            "date",
            "strategy",
            "gross_return",
            "net_return",
            "nav",
            "cash_hkd",
            "available_cash_hkd",
            "cost_hkd",
        ],
    )
    metric_input = returns.copy()
    for column in ("net_return", "nav", "cost_hkd"):
        metric_input[column] = pd.to_numeric(metric_input[column])
    summary = metrics(metric_input, float(config["initial_cash"]))
    summary.update(
        {
            "fees_and_slippage_hkd": str(
                sum((Decimal(str(item["total_cost"])) for item in cost_rows), Decimal(0))
            ),
            "filled_orders": sum(item["status"] == "filled" for item in order_rows),
            "return_basis": "raw_price_signals_plus_evidenced_dividend_account",
            "as_of_nav_hkd": str(final_snapshot.nav.to_decimal()),
            "as_of_total_return": str(
                final_snapshot.nav.to_decimal() / Decimal(str(config["initial_cash"])) - 1
            ),
            "daily_return_start": str(returns.date.iloc[0]),
            "daily_return_end": str(returns.date.iloc[-1]),
            "investable": False,
        }
    )
    return ScenarioExecution(
        account=account,
        returns=returns,
        orders=_empty_frame(
            order_rows,
            [
                "timestamp",
                "strategy",
                "symbol",
                "side",
                "quantity",
                "requested_quantity",
                "target_weight",
                "order_type",
                "status",
                "order_id",
            ],
        ),
        costs=_empty_frame(cost_rows, ["date", "strategy", "symbol", "total_cost"]),
        positions=_empty_frame(
            position_rows,
            ["date", "strategy", "symbol", "quantity", "market_value", "weight", "side"],
        ),
        signals=_empty_frame(
            signal_rows, ["date", "symbol", "signal_date", "selected", "target_quantity"]
        ),
        timeline=timeline_rows,
        summary=summary,
        applied_phases={key: tuple(value) for key, value in applied.items()},
        final_snapshot=final_snapshot,
    )


def _execute_planned_holdout(
    *,
    prepared: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    config: Mapping[str, Any],
    scenario: HKDividendScenarioInput,
    strategy: str,
) -> tuple[ScenarioExecution, tuple[TimelineEvent, ...]]:
    """Use QExec's public precision decision to schedule only necessary policy facts."""

    policy_required: set[str] = set()
    while True:
        timeline = build_scenario_timeline(
            calendar,
            config,
            scenario,
            conversion_policy_required=frozenset(policy_required),
        )
        try:
            return (
                _execute_holdout(
                    prepared=prepared,
                    bars=bars,
                    calendar=calendar,
                    config=config,
                    scenario=scenario,
                    strategy=strategy,
                    timeline=timeline,
                ),
                timeline,
            )
        except _ConversionPolicyRequired as exc:
            if exc.dividend_id in policy_required:
                raise ScenarioValidationError("ROUNDING_POLICY_REQUIRED", exc.dividend_id) from exc
            lifecycle = next(
                item.lifecycle
                for item in scenario.lifecycles
                if item.lifecycle.dividend_id == exc.dividend_id
            )
            if lifecycle.payment_policy is None:
                raise ScenarioValidationError("ROUNDING_POLICY_REQUIRED", exc.dividend_id) from exc
            policy_required.add(exc.dividend_id)


def _frame_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, lineterminator="\n").encode("utf-8")


def _timeline_bytes(rows: list[dict[str, object]]) -> bytes:
    return b"".join(_canonical_bytes(item) for item in rows)


def _execution_state(
    execution: ScenarioExecution,
    *,
    orders_sha256: str,
    timeline_sha256: str,
) -> dict[str, object]:
    state = _runtime_state(execution.account)
    pending = sum(
        (Decimal(item["amount_hkd"]) for item in state["pending_sale_proceeds"]),
        Decimal(0),
    )
    if Decimal(state["cash_balance_hkd"]) - pending != Decimal(state["available_cash_hkd"]):
        raise ScenarioValidationError("HK_AVAILABLE_CASH_MISMATCH", "cash-pending invariant failed")
    return {
        "schema": EXECUTION_STATE_SCHEMA,
        **state,
        "orders_sha256": orders_sha256,
        "timeline_sha256": timeline_sha256,
    }


def _verify_replay(first: ScenarioExecution, replay: ScenarioExecution) -> None:
    for name in ("returns", "orders", "costs", "positions", "signals"):
        if _frame_bytes(getattr(first, name)) != _frame_bytes(getattr(replay, name)):
            raise ScenarioValidationError("HK_REPLAY_MISMATCH", f"{name} changed")
    if _timeline_bytes(first.timeline) != _timeline_bytes(replay.timeline):
        raise ScenarioValidationError("HK_REPLAY_MISMATCH", "timeline changed")
    if _canonical_bytes(_runtime_state(first.account)) != _canonical_bytes(
        _runtime_state(replay.account)
    ):
        raise ScenarioValidationError("HK_REPLAY_MISMATCH", "HK execution state changed")
    if first.summary != replay.summary or first.applied_phases != replay.applied_phases:
        raise ScenarioValidationError("HK_REPLAY_MISMATCH", "result facts changed")


def _result_payload(
    *,
    execution: ScenarioExecution,
    scenario: HKDividendScenarioInput,
    config: Mapping[str, Any],
    selection: Mapping[str, Any],
    dependency_commits: Mapping[str, str],
    input_sha256: str,
    execution_manifest_sha256: str,
    state_sha256: str,
    timeline_sha256: str,
) -> dict[str, object]:
    paid = {
        dividend_id
        for dividend_id, phases in execution.applied_phases.items()
        if DividendExecutionPhase.PAYMENT.value in phases
    }
    entitled = {
        dividend_id
        for dividend_id, phases in execution.applied_phases.items()
        if DividendExecutionPhase.ENTITLEMENT.value in phases
    }
    open_ids = sorted(entitled - paid)
    status_by_dividend = {item.dividend_id: item.evidence_id for item in scenario.payment_status}
    exposure = execution.account.ledger.dividend_exposure(as_of=scenario.as_of)
    return {
        "schema": RESULT_SCHEMA,
        "status": "complete_as_of",
        "scenario_id": scenario.scenario_id,
        "as_of": _timestamp(scenario.as_of),
        "selection": dict(selection),
        "daily_return_start": execution.summary["daily_return_start"],
        "daily_return_end": execution.summary["daily_return_end"],
        "account_as_of": _timestamp(scenario.as_of),
        "price_coverage_end": config["data_end"],
        "signal_return_basis": "raw_price",
        "investable": False,
        "rankable": False,
        "market_admission_certified": False,
        "input_sha256": input_sha256,
        "hk_code_revision": code_revision(),
        "dependency_commits": dict(dependency_commits),
        "qexec_commit": QEXEC_COMMIT,
        "qdk_commit": QDK_COMMIT,
        "execution_manifest_sha256": execution_manifest_sha256,
        "hk_execution_state_sha256": state_sha256,
        "timeline_sha256": timeline_sha256,
        "open_lifecycle_ids": open_ids,
        "open_payment_status_evidence_ids": [
            status_by_dividend[item] for item in open_ids if item in status_by_dividend
        ],
        "dividend_tax_status": exposure.tax_status,
        "metrics": execution.summary,
        "blockers": [],
    }


def _manifest_payload(
    output: Path,
    *,
    input_sha256: str,
    result_sha256: str,
    execution_manifest_sha256: str,
) -> dict[str, object]:
    names = [
        "input.json",
        "result.json",
        "returns.csv",
        "orders.csv",
        "costs.csv",
        "positions.csv",
        "signals.csv",
        "timeline.jsonl",
        "hk-execution-state.json",
    ]
    return {
        "schema": MANIFEST_SCHEMA,
        "complete": True,
        "scenario_input_sha256": input_sha256,
        "result_sha256": result_sha256,
        "execution_manifest_sha256": execution_manifest_sha256,
        "files": {name: sha256(output / name) for name in sorted(names)},
    }


def _select_training_strategy(
    prepared: pd.DataFrame,
    calendar: pd.DataFrame,
    config: Mapping[str, Any],
) -> dict[str, object]:
    train_end = (pd.Timestamp(config["test_start"]) - pd.Timedelta(days=1)).date().isoformat()
    training: dict[str, dict[str, object]] = {}
    for candidate in config["candidates"]:
        training[candidate] = simulate(
            prepared,
            calendar,
            config,
            strategy=candidate,
            start=config["train_start"],
            end=train_end,
        )[1]
    selected = min(config["candidates"], key=lambda key: (-training[key]["sharpe_zero_rf"], key))
    return {
        "selected": selected,
        "rule": "maximum training Sharpe; alphabetical tie break",
        "train_start": config["train_start"],
        "train_end": train_end,
        "return_basis": "raw_price",
        "candidates": training,
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _write_failure(
    output: Path,
    *,
    stage: str,
    exc: Exception,
    input_sha256: str | None,
    scenario_id: str | None,
) -> None:
    manifest = output / "execution" / "manifest.json"
    payload = {
        "schema": "quant-hk-dividend-scenario-failure/v1",
        "status": "failed",
        "scenario_id": scenario_id,
        "stage": stage,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "input_sha256": input_sha256,
        "qexec_artifact_preserved": manifest.exists(),
        "execution_manifest_sha256": sha256(manifest) if manifest.exists() else None,
    }
    _atomic_write(output / "FAILED.json", _canonical_bytes(payload))


def _path_from_file_url(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        raise ScenarioValidationError(
            "DEPENDENCY_IDENTITY_UNVERIFIED", f"unsupported editable URL {value}"
        )
    raw = unquote(parsed.path)
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw).resolve()


def _git_identity(root: Path, name: str) -> str:
    if not (root / ".git").exists():
        raise ScenarioValidationError(
            "DEPENDENCY_IDENTITY_UNVERIFIED", f"{name} editable root lacks .git"
        )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode or status.returncode or status.stdout.strip():
        raise ScenarioValidationError(
            "DEPENDENCY_IDENTITY_UNVERIFIED", f"{name} editable source is missing or dirty"
        )
    return head.stdout.strip()


def _dependency_commit(name: str, module_name: str) -> str:
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ScenarioValidationError(
            "DEPENDENCY_IDENTITY_UNVERIFIED", f"{name} distribution metadata is missing"
        ) from exc
    direct_text = distribution.read_text("direct_url.json")
    if not direct_text:
        raise ScenarioValidationError(
            "DEPENDENCY_IDENTITY_UNVERIFIED", f"{name} direct_url.json is missing"
        )
    payload = json.loads(direct_text)
    module_path = Path(importlib.import_module(module_name).__file__).resolve()
    vcs = payload.get("vcs_info")
    if isinstance(vcs, Mapping) and isinstance(vcs.get("commit_id"), str):
        distribution_root = Path(distribution.locate_file("")).resolve()
        if not module_path.is_relative_to(distribution_root):
            raise ScenarioValidationError(
                "DEPENDENCY_IDENTITY_UNVERIFIED",
                f"{name} imported from {module_path}, outside {distribution_root}",
            )
        return vcs["commit_id"]
    if payload.get("dir_info", {}).get("editable") is True:
        root = _path_from_file_url(payload.get("url", ""))
        if not module_path.is_relative_to(root):
            raise ScenarioValidationError(
                "DEPENDENCY_IDENTITY_UNVERIFIED",
                f"{name} imported from {module_path}, outside editable root {root}",
            )
        return _git_identity(root, name)
    raise ScenarioValidationError(
        "DEPENDENCY_IDENTITY_UNVERIFIED", f"{name} lacks a verifiable VCS identity"
    )


def _verify_dependency_stack() -> dict[str, str]:
    lock = Path(__file__).resolve().parents[2] / "stack.dividend-scenario.lock"
    if not lock.is_file():
        raise ScenarioValidationError("DEPENDENCY_IDENTITY_UNVERIFIED", "scenario lock is missing")
    lock_text = lock.read_text(encoding="utf-8")
    identities = {}
    for name, expected in DEPENDENCY_COMMITS.items():
        if f"{name} @ git+https://github.com/PureSaber/{name}.git@{expected}" not in lock_text:
            raise ScenarioValidationError(
                "DEPENDENCY_IDENTITY_MISMATCH", f"scenario lock does not pin {name}@{expected}"
            )
        actual = _dependency_commit(name, DEPENDENCY_MODULES[name])
        if actual != expected:
            raise ScenarioValidationError(
                "DEPENDENCY_IDENTITY_MISMATCH", f"{name} expected {expected}, got {actual}"
            )
        identities[name] = actual
    identities["stack.dividend-scenario.lock"] = sha256(lock)
    return identities


def validate_dividend_scenario_output(
    output: Path,
    *,
    snapshot: Path,
    config_path: Path,
    require_manifest: bool = True,
) -> dict[str, object]:
    """Read back, replay, and validate a complete HK scenario directory."""

    if (output / "FAILED.json").exists():
        raise ScenarioValidationError("SCENARIO_RUN_FAILED", "FAILED.json is present")
    required = [
        "input.json",
        "result.json",
        "returns.csv",
        "orders.csv",
        "costs.csv",
        "positions.csv",
        "signals.csv",
        "timeline.jsonl",
        "hk-execution-state.json",
        "execution/manifest.json",
    ]
    missing = [name for name in required if not (output / name).is_file()]
    if missing:
        raise ScenarioValidationError("SCENARIO_ARTIFACT_MISSING", str(missing))
    raw_input = json.loads((output / "input.json").read_text(encoding="utf-8"))
    scenario = load_dividend_scenario(raw_input)
    normalized_input = _canonical_bytes(scenario.to_dict())
    if (output / "input.json").read_bytes() != normalized_input:
        raise ScenarioValidationError("SCENARIO_INPUT_NOT_CANONICAL", "input bytes changed")
    input_sha256 = _sha_bytes(normalized_input)
    if sha256(config_path) != scenario.study_config_sha256:
        raise ScenarioValidationError("INPUT_HASH_MISMATCH", "study config hash differs")
    if sha256(snapshot / "manifest.json") != scenario.snapshot_manifest_sha256:
        raise ScenarioValidationError("INPUT_HASH_MISMATCH", "snapshot manifest hash differs")
    dependency_commits = _verify_dependency_stack()
    config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))
    if config.get("schema") == "quant-hk-study/v2" and config["financial"]["actions"]:
        raise ScenarioValidationError(
            "LEGACY_ACTIONS_CONFLICT", "scenario requires financial.actions=[]"
        )
    bars, calendar, manifest = load_hk_snapshot(snapshot)
    if manifest["start"] != config["data_start"] or manifest["end"] != config["data_end"]:
        raise ScenarioValidationError("PRICE_COVERAGE_MISSING", "snapshot range differs")
    if manifest["provider"] != config["provider"]:
        raise ScenarioValidationError("INPUT_HASH_MISMATCH", "snapshot provider differs")
    raw_config = copy.deepcopy(config)
    if raw_config.get("financial"):
        raw_config["financial"]["actions"] = []
    prepared = prepare(bars, calendar, raw_config)
    selection = _select_training_strategy(prepared, calendar, raw_config)
    replayed_hk, _ = _execute_planned_holdout(
        prepared=prepared,
        bars=bars,
        calendar=calendar,
        config=config,
        scenario=scenario,
        strategy=selection["selected"],
    )
    for name in ("returns", "orders", "costs", "positions", "signals"):
        if (output / f"{name}.csv").read_bytes() != _frame_bytes(getattr(replayed_hk, name)):
            raise ScenarioValidationError("HK_DISK_REPLAY_MISMATCH", f"{name}.csv")
    timeline_bytes = _timeline_bytes(replayed_hk.timeline)
    if (output / "timeline.jsonl").read_bytes() != timeline_bytes:
        raise ScenarioValidationError("HK_DISK_REPLAY_MISMATCH", "timeline.jsonl")
    qexec_replay = replay_dividend_run(output / "execution")
    if qexec_replay.ledger.journal_sha256 != replayed_hk.account.ledger.journal_sha256:
        raise ScenarioValidationError(
            "HK_QEXEC_REPLAY_MISMATCH", "QExec and HK journal hashes differ"
        )
    state = _execution_state(
        replayed_hk,
        orders_sha256=sha256(output / "orders.csv"),
        timeline_sha256=sha256(output / "timeline.jsonl"),
    )
    state_bytes = _canonical_bytes(state)
    if (output / "hk-execution-state.json").read_bytes() != state_bytes:
        raise ScenarioValidationError("HK_DISK_REPLAY_MISMATCH", "hk-execution-state.json")
    execution_manifest_sha = sha256(output / "execution" / "manifest.json")
    expected_result = _result_payload(
        execution=replayed_hk,
        scenario=scenario,
        config=config,
        selection=selection,
        dependency_commits=dependency_commits,
        input_sha256=input_sha256,
        execution_manifest_sha256=execution_manifest_sha,
        state_sha256=_sha_bytes(state_bytes),
        timeline_sha256=_sha_bytes(timeline_bytes),
    )
    result_bytes = _canonical_bytes(expected_result)
    if (output / "result.json").read_bytes() != result_bytes:
        raise ScenarioValidationError("HK_DISK_REPLAY_MISMATCH", "result.json")
    candidate = _manifest_payload(
        output,
        input_sha256=input_sha256,
        result_sha256=_sha_bytes(result_bytes),
        execution_manifest_sha256=execution_manifest_sha,
    )
    manifest_path = output / "scenario-manifest.json"
    if require_manifest:
        if not manifest_path.is_file():
            raise ScenarioValidationError("SCENARIO_ARTIFACT_MISSING", "scenario-manifest.json")
        if manifest_path.read_bytes() != _canonical_bytes(candidate):
            raise ScenarioValidationError("HK_MANIFEST_MISMATCH", "top manifest changed")
    return {"result": expected_result, "manifest": candidate}


def run_dividend_scenario_files(
    *,
    snapshot: Path,
    config_path: Path,
    scenario_path: Path,
    output: Path,
) -> dict[str, object]:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    stage = "load_input"
    input_sha256 = None
    scenario_id = None
    try:
        config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))
        if config.get("schema") == "quant-hk-study/v2" and config["financial"]["actions"]:
            raise ScenarioValidationError(
                "LEGACY_ACTIONS_CONFLICT", "scenario requires financial.actions=[]"
            )
        raw = json.loads(scenario_path.read_text(encoding="utf-8"))
        scenario = load_dividend_scenario(raw)
        scenario_id = scenario.scenario_id
        normalized_input = _canonical_bytes(scenario.to_dict())
        input_sha256 = _sha_bytes(normalized_input)
        (output / "input.json").write_bytes(normalized_input)
        if sha256(config_path) != scenario.study_config_sha256:
            raise ScenarioValidationError("INPUT_HASH_MISMATCH", "study config hash differs")
        manifest_path = snapshot / "manifest.json"
        if sha256(manifest_path) != scenario.snapshot_manifest_sha256:
            raise ScenarioValidationError("INPUT_HASH_MISMATCH", "snapshot manifest hash differs")
        bars, calendar, manifest = load_hk_snapshot(snapshot)
        if manifest["start"] != config["data_start"] or manifest["end"] != config["data_end"]:
            raise ScenarioValidationError("PRICE_COVERAGE_MISSING", "snapshot range differs")
        if manifest["provider"] != config["provider"]:
            raise ScenarioValidationError("INPUT_HASH_MISMATCH", "snapshot provider differs")
        configured = {item["symbol"] for item in config["instruments"]}
        if any(item.lifecycle.instrument_id not in configured for item in scenario.lifecycles):
            raise ScenarioValidationError("IDENTITY_MISMATCH", "dividend instrument not configured")
        dependency_commits = _verify_dependency_stack()

        stage = "training_selection"
        raw_config = copy.deepcopy(config)
        if raw_config.get("financial"):
            raw_config["financial"]["actions"] = []
        prepared = prepare(bars, calendar, raw_config)
        selection = _select_training_strategy(prepared, calendar, raw_config)

        stage = "timeline_and_execute"
        first, timeline = _execute_planned_holdout(
            prepared=prepared,
            bars=bars,
            calendar=calendar,
            config=config,
            scenario=scenario,
            strategy=selection["selected"],
        )

        stage = "qexec_export"
        export_dividend_run(first.account.ledger, output / "execution")
        execution_manifest = output / "execution" / "manifest.json"
        execution_manifest_sha = sha256(execution_manifest)

        stage = "hk_replay"
        replay = _execute_holdout(
            prepared=prepared,
            bars=bars,
            calendar=calendar,
            config=config,
            scenario=scenario,
            strategy=selection["selected"],
            timeline=timeline,
        )
        _verify_replay(first, replay)

        stage = "sidecars"
        frame_files = {
            "returns.csv": _frame_bytes(first.returns),
            "orders.csv": _frame_bytes(first.orders),
            "costs.csv": _frame_bytes(first.costs),
            "positions.csv": _frame_bytes(first.positions),
            "signals.csv": _frame_bytes(first.signals),
        }
        for name, content in frame_files.items():
            (output / name).write_bytes(content)
        timeline_bytes = _timeline_bytes(first.timeline)
        (output / "timeline.jsonl").write_bytes(timeline_bytes)
        state = _execution_state(
            first,
            orders_sha256=_sha_bytes(frame_files["orders.csv"]),
            timeline_sha256=_sha_bytes(timeline_bytes),
        )
        state_bytes = _canonical_bytes(state)
        (output / "hk-execution-state.json").write_bytes(state_bytes)
        result = _result_payload(
            execution=first,
            scenario=scenario,
            config=config,
            selection=selection,
            dependency_commits=dependency_commits,
            input_sha256=input_sha256,
            execution_manifest_sha256=execution_manifest_sha,
            state_sha256=_sha_bytes(state_bytes),
            timeline_sha256=_sha_bytes(timeline_bytes),
        )
        result_bytes = _canonical_bytes(result)
        (output / "result.json").write_bytes(result_bytes)
        stage = "candidate_validation"
        validated = validate_dividend_scenario_output(
            output,
            snapshot=snapshot,
            config_path=config_path,
            require_manifest=False,
        )
        if validated["result"] != result:
            raise ScenarioValidationError("HK_DISK_REPLAY_MISMATCH", "candidate result changed")
        manifest_payload = validated["manifest"]
        stage = "publish_manifest"
        _atomic_write(output / "scenario-manifest.json", _canonical_bytes(manifest_payload))
        return result
    except Exception as exc:
        _write_failure(
            output,
            stage=stage,
            exc=exc,
            input_sha256=input_sha256,
            scenario_id=scenario_id,
        )
        raise


__all__ = [
    "HKDividendScenarioInput",
    "PaymentReceiptStatusEvidence",
    "ScenarioValidationError",
    "TimelineEvent",
    "build_scenario_timeline",
    "load_dividend_scenario",
    "run_dividend_scenario_files",
    "validate_dividend_scenario_output",
]

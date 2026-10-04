from __future__ import annotations

import copy
import importlib.metadata
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from quant_execution import DividendExecutionPhase

import quant_hk_equity.dividend_scenario as scenario_module
from quant_hk_equity.dividend_scenario import ScenarioValidationError
from scenario_tests.helpers import (
    create_case,
    evidence,
    iso,
    lifecycle,
    pit_fx,
    scenario_payload,
    write_scenario,
)

REAL_VERIFY_DEPENDENCY_STACK = scenario_module._verify_dependency_stack


@pytest.fixture(autouse=True)
def explicit_source_identity_fixture(monkeypatch):
    identities = {
        **scenario_module.DEPENDENCY_COMMITS,
        "stack.dividend-scenario.lock": "f" * 64,
    }
    monkeypatch.setattr(scenario_module, "_verify_dependency_stack", lambda: identities)


def event_times(case):
    sessions = case.calendar[case.calendar.date.ge(case.config["test_start"])].reset_index(
        drop=True
    )
    ex_at = sessions.iloc[0].close.to_pydatetime() + timedelta(hours=1)
    conversion_at = ex_at + timedelta(hours=1)
    payment_at = ex_at + timedelta(hours=2)
    as_of = sessions.iloc[8].close.to_pydatetime()
    return sessions, ex_at, conversion_at, payment_at, as_of


def v2_financial(case, *, status="unknown"):
    known = "2025-01-01T00:00:00Z"
    symbols = [item["symbol"] for item in case.config["instruments"]]
    return {
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
                "open_days": [str(value.date()) for value in case.calendar.date],
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
                "evidence_id": f"listing:{symbol}",
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
                "buy_status": status,
                "sell_status": status,
                "reason": "explicit-test-status",
                "source": "synthetic",
                "evidence_id": f"status:{symbol}",
            }
            for symbol in symbols
        ],
    }


def loaded_scenario(tmp_path):
    case = create_case(tmp_path)
    sessions, ex_at, conversion_at, payment_at, as_of = event_times(case)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    payload = scenario_payload(case, value, ex_at=ex_at, as_of=as_of)
    return (
        case,
        sessions,
        ex_at,
        conversion_at,
        payment_at,
        as_of,
        value,
        payload,
        scenario_module.load_dividend_scenario(payload),
    )


def test_schema_primitive_guards_and_phase_helpers(tmp_path):
    with pytest.raises(ScenarioValidationError, match="SCHEMA_INVALID"):
        scenario_module._mapping([], "test")
    with pytest.raises(ScenarioValidationError, match="SCHEMA_INVALID"):
        scenario_module._keys({"extra": 1}, {"required"}, "test")
    with pytest.raises(ScenarioValidationError, match="SCHEMA_INVALID"):
        scenario_module._text("", "test")
    with pytest.raises(ScenarioValidationError, match="SCHEMA_INVALID"):
        scenario_module._hash("g" * 64, "test")
    with pytest.raises(ScenarioValidationError, match="TIMING_UNRESOLVED"):
        scenario_module._time(1, "test")
    with pytest.raises(ScenarioValidationError, match="TIMING_UNRESOLVED"):
        scenario_module._time("not-a-time", "test")
    with pytest.raises(ScenarioValidationError, match="TIMING_UNRESOLVED"):
        scenario_module._time("2026-01-01T00:00:00", "test")
    with pytest.raises(ScenarioValidationError, match="POLICY_EVIDENCE_MISSING"):
        scenario_module._fact_times(object())

    *_, value, _, _ = loaded_scenario(tmp_path)
    without_policy = replace(value, payment=None, payment_policy=None)
    assert scenario_module._phase_facts(without_policy, DividendExecutionPhase.ENTITLEMENT) == [
        value.entitlement
    ]
    assert value.payment_policy in scenario_module._phase_facts(
        value, DividendExecutionPhase.ENTITLEMENT
    )
    assert value.election in scenario_module._phase_facts(
        value, DividendExecutionPhase.ISSUER_CONVERSION
    )
    with pytest.raises(ScenarioValidationError, match="PHASE_DEPENDENCY_UNRESOLVED"):
        scenario_module._phase_applied_at(
            replace(value, election=None, conversion=None, payment=None),
            DividendExecutionPhase.ISSUER_CONVERSION,
        )


def test_loader_rejects_malformed_envelopes_and_evidence(tmp_path):
    *_, as_of, _, base, _ = loaded_scenario(tmp_path)

    malformed = copy.deepcopy(base)
    malformed["unexpected"] = True
    with pytest.raises(ScenarioValidationError, match="SCHEMA_INVALID"):
        scenario_module.load_dividend_scenario(malformed)

    for field, value in (
        ("schema", "unsupported"),
        ("execution_mode", "legacy"),
        ("lifecycles", {}),
    ):
        malformed = copy.deepcopy(base)
        malformed[field] = value
        with pytest.raises(ScenarioValidationError):
            scenario_module.load_dividend_scenario(malformed)

    malformed = copy.deepcopy(base)
    malformed["lifecycles"][0]["source_reference"] = ""
    with pytest.raises(ScenarioValidationError, match="SCHEMA_INVALID"):
        scenario_module.load_dividend_scenario(malformed)
    malformed = copy.deepcopy(base)
    malformed["lifecycles"][0]["source_record_sha256"] = "bad"
    with pytest.raises(ScenarioValidationError, match="SCHEMA_INVALID"):
        scenario_module.load_dividend_scenario(malformed)

    malformed = copy.deepcopy(base)
    malformed["basis_evidence"][0]["account_id"] = "other-account"
    with pytest.raises(ScenarioValidationError, match="IDENTITY_MISMATCH"):
        scenario_module.load_dividend_scenario(malformed)
    malformed = copy.deepcopy(base)
    malformed["basis_evidence"][0]["available_at"] = iso(as_of + timedelta(seconds=1))
    with pytest.raises(ScenarioValidationError, match="LATE_ENTITLEMENT_UNSUPPORTED"):
        scenario_module.load_dividend_scenario(malformed)

    malformed = copy.deepcopy(base)
    malformed["pit_fx"] = [{"bad": "record"}]
    with pytest.raises(ScenarioValidationError, match="PIT_FX_INVALID"):
        scenario_module.load_dividend_scenario(malformed)
    malformed = copy.deepcopy(base)
    malformed["same_instant_order"] = [
        {
            "timestamp": base["as_of"],
            "before_event_id": "same",
            "after_event_id": "same",
            "evidence_id": "self",
            "source": "test",
        }
    ]
    with pytest.raises(ScenarioValidationError, match="AMBIGUOUS_EVENT_ORDER"):
        scenario_module.load_dividend_scenario(malformed)


def test_identity_validation_rejects_cross_record_conflicts(tmp_path):
    _, _, _, _, payment_at, as_of, _, base, loaded = loaded_scenario(tmp_path)

    malformed = copy.deepcopy(base)
    malformed["lifecycles"].append(copy.deepcopy(malformed["lifecycles"][0]))
    with pytest.raises(ScenarioValidationError, match="DUPLICATE_EVENT_ID"):
        scenario_module.load_dividend_scenario(malformed)
    malformed = copy.deepcopy(base)
    malformed["basis_evidence"] = []
    with pytest.raises(ScenarioValidationError, match="ENTITLEMENT_BASIS_IDENTITY_MISMATCH"):
        scenario_module.load_dividend_scenario(malformed)
    malformed = copy.deepcopy(base)
    malformed["basis_evidence"][0]["instrument_id"] = "00700"
    with pytest.raises(ScenarioValidationError, match="ENTITLEMENT_BASIS_IDENTITY_MISMATCH"):
        scenario_module.load_dividend_scenario(malformed)

    envelope = loaded.lifecycles[0]
    future_timing = replace(
        envelope.lifecycle.entitlement.evidence.timing,
        available_at=iso(as_of + timedelta(seconds=1)),
        captured_at=iso(as_of + timedelta(seconds=1)),
    )
    future_entitlement = replace(
        envelope.lifecycle.entitlement,
        evidence=replace(envelope.lifecycle.entitlement.evidence, timing=future_timing),
    )
    with pytest.raises(ScenarioValidationError, match="FACT_NOT_KNOWN_AS_OF"):
        scenario_module._validate_scenario_identities(
            replace(
                loaded,
                lifecycles=(
                    replace(
                        envelope,
                        lifecycle=replace(envelope.lifecycle, entitlement=future_entitlement),
                    ),
                ),
            )
        )

    no_policy_evidence = replace(
        envelope.lifecycle.payment_policy,
        certification_status="unverified",
        holder_tax_profile_id=None,
        withholding_rule_id=None,
        rounding=None,
        evidence=None,
    )
    with pytest.raises(ScenarioValidationError, match="POLICY_EVIDENCE_MISSING"):
        scenario_module._validate_scenario_identities(
            replace(
                loaded,
                lifecycles=(
                    replace(
                        envelope,
                        lifecycle=replace(
                            envelope.lifecycle,
                            payment=None,
                            payment_policy=no_policy_evidence,
                        ),
                    ),
                ),
            )
        )
    wrong_account_policy = replace(envelope.lifecycle.payment_policy, account_id="other")
    with pytest.raises(ScenarioValidationError, match="IDENTITY_MISMATCH"):
        scenario_module._validate_scenario_identities(
            replace(
                loaded,
                lifecycles=(
                    replace(
                        envelope,
                        lifecycle=replace(
                            envelope.lifecycle,
                            election=None,
                            payment=None,
                            payment_policy=wrong_account_policy,
                        ),
                    ),
                ),
            )
        )

    future_basis = replace(loaded.basis_evidence[0], captured_at=as_of + timedelta(seconds=1))
    with pytest.raises(ScenarioValidationError, match="FACT_NOT_KNOWN_AS_OF"):
        scenario_module._validate_scenario_identities(
            replace(loaded, basis_evidence=(future_basis,))
        )

    issuer_conversion_required = copy.deepcopy(envelope.lifecycle)
    usd_election = copy.deepcopy(issuer_conversion_required.election)
    object.__setattr__(usd_election, "payment_currency", "USD")
    object.__setattr__(issuer_conversion_required, "election", usd_election)
    object.__setattr__(issuer_conversion_required, "conversion", None)
    with pytest.raises(ScenarioValidationError, match="ISSUER_CONVERSION_REQUIRED"):
        scenario_module._validate_scenario_identities(
            replace(
                loaded,
                lifecycles=(
                    replace(
                        envelope,
                        lifecycle=issuer_conversion_required,
                    ),
                ),
            )
        )

    payment_timing = replace(
        envelope.lifecycle.payment.evidence.timing,
        effective_at=iso(payment_at + timedelta(seconds=1)),
        available_at=iso(payment_at),
    )
    invalid_payment = copy.deepcopy(envelope.lifecycle.payment)
    object.__setattr__(
        invalid_payment,
        "evidence",
        replace(envelope.lifecycle.payment.evidence, timing=payment_timing),
    )
    with pytest.raises(ScenarioValidationError, match="FUTURE_PAYMENT_EFFECTIVE"):
        scenario_module._validate_scenario_identities(
            replace(
                loaded,
                lifecycles=(
                    replace(
                        envelope,
                        lifecycle=replace(envelope.lifecycle, payment=invalid_payment),
                    ),
                ),
            )
        )

    future_policy_lifecycle = copy.deepcopy(envelope.lifecycle)
    future_policy = replace(
        future_policy_lifecycle.payment_policy,
        evidence=evidence(
            "future-effective-payment-policy",
            as_of + timedelta(days=1),
            payment_at,
        ),
    )
    object.__setattr__(future_policy_lifecycle, "payment_policy", future_policy)
    with pytest.raises(ScenarioValidationError, match="PAYMENT_PREREQUISITE_NOT_EFFECTIVE"):
        scenario_module._validate_scenario_identities(
            replace(
                loaded,
                lifecycles=(replace(envelope, lifecycle=future_policy_lifecycle),),
            )
        )

    duplicate_fx = replace(
        loaded,
        pit_fx=(
            scenario_module.PitFxRate.from_dict(pit_fx(currency="USD", available_at=payment_at)),
            scenario_module.PitFxRate.from_dict(pit_fx(currency="USD", available_at=payment_at)),
        ),
    )
    with pytest.raises(ScenarioValidationError, match="DUPLICATE_EVENT_ID"):
        scenario_module._validate_scenario_identities(duplicate_fx)
    future_fx = scenario_module.PitFxRate.from_dict(
        pit_fx(currency="USD", available_at=as_of + timedelta(seconds=1))
    )
    with pytest.raises(ScenarioValidationError, match="FACT_NOT_KNOWN_AS_OF"):
        scenario_module._validate_scenario_identities(replace(loaded, pit_fx=(future_fx,)))


def test_payment_status_identity_and_serialization_branches(tmp_path):
    case, _, ex_at, conversion_at, _, as_of, _, _, _ = loaded_scenario(tmp_path)
    open_value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=None,
        scheduled_payment_date=(as_of - timedelta(days=1)).date().isoformat(),
    )
    status = {
        "evidence_id": "custody:not-received",
        "dividend_id": open_value.dividend_id,
        "account_id": "hk-research",
        "status": "not_received",
        "status_at": iso(as_of),
        "available_at": iso(as_of),
        "captured_at": iso(as_of),
        "source": "test",
    }
    base = scenario_payload(
        case,
        open_value,
        ex_at=ex_at,
        as_of=as_of,
        payment_status=[status],
    )
    loaded = scenario_module.load_dividend_scenario(base)
    assert loaded.payment_status[0].to_dict() == status

    duplicate = copy.deepcopy(base)
    duplicate["payment_status"].append(copy.deepcopy(status))
    with pytest.raises(ScenarioValidationError, match="DUPLICATE_EVENT_ID"):
        scenario_module.load_dividend_scenario(duplicate)
    unknown = copy.deepcopy(base)
    unknown["payment_status"][0]["dividend_id"] = "unknown"
    with pytest.raises(ScenarioValidationError, match="IDENTITY_MISMATCH"):
        scenario_module.load_dividend_scenario(unknown)


def test_timeline_guards_and_skipped_future_phases(tmp_path):
    case, sessions, ex_at, _, _, as_of, _, _, loaded = loaded_scenario(tmp_path)
    with pytest.raises(ScenarioValidationError, match="PRICE_COVERAGE_MISSING"):
        scenario_module.build_scenario_timeline(case.calendar.iloc[0:0], case.config, loaded)
    with pytest.raises(ScenarioValidationError, match="PRICE_COVERAGE_MISSING"):
        scenario_module.build_scenario_timeline(
            case.calendar,
            case.config,
            replace(loaded, as_of=sessions.iloc[0].open.to_pydatetime()),
        )

    opening = sessions.iloc[0].open.to_pydatetime()
    with pytest.raises(ScenarioValidationError, match="PRE_ACCOUNT_ENTITLEMENT_UNSUPPORTED"):
        scenario_module.build_scenario_timeline(
            case.calendar,
            case.config,
            replace(
                loaded,
                basis_evidence=(
                    replace(loaded.basis_evidence[0], ex_at=opening - timedelta(seconds=1)),
                ),
            ),
        )
    with pytest.raises(ScenarioValidationError, match="ENTITLEMENT_AFTER_AS_OF"):
        scenario_module.build_scenario_timeline(
            case.calendar,
            case.config,
            replace(
                loaded,
                basis_evidence=(
                    replace(loaded.basis_evidence[0], ex_at=as_of + timedelta(seconds=1)),
                ),
            ),
        )

    envelope = loaded.lifecycles[0]
    without_election = copy.deepcopy(envelope.lifecycle)
    object.__setattr__(without_election, "election", None)
    with pytest.raises(ScenarioValidationError, match="PHASE_TIME_CONTRADICTION"):
        scenario_module.build_scenario_timeline(
            case.calendar,
            case.config,
            replace(loaded, lifecycles=(replace(envelope, lifecycle=without_election),)),
        )

    future_at = as_of + timedelta(days=1)
    future_lifecycle = replace(
        envelope.lifecycle,
        election=replace(
            envelope.lifecycle.election,
            evidence=evidence("future-election", future_at),
        ),
        payment_policy=replace(
            envelope.lifecycle.payment_policy,
            evidence=evidence("future-policy", future_at),
        ),
        payment=replace(
            envelope.lifecycle.payment,
            evidence=evidence("future-payment", future_at + timedelta(hours=1)),
        ),
    )
    timeline = scenario_module.build_scenario_timeline(
        case.calendar,
        case.config,
        replace(loaded, lifecycles=(replace(envelope, lifecycle=future_lifecycle),)),
    )
    assert not {"issuer_conversion", "payment"} & {item.kind for item in timeline}

    collision_rate = replace(
        scenario_module.PitFxRate.from_dict(
            pit_fx(currency="USD", available_at=ex_at + timedelta(minutes=1))
        ),
        event_id="scenario:final-valuation",
    )
    with pytest.raises(ScenarioValidationError, match="DUPLICATE_EVENT_ID"):
        scenario_module.build_scenario_timeline(
            case.calendar,
            case.config,
            replace(loaded, pit_fx=(collision_rate,)),
        )

    order = scenario_module.SameInstantOrder(
        timestamp=as_of - timedelta(seconds=1),
        before_event_id="a",
        after_event_id="b",
        evidence_id="orphan-order",
        source="test",
    )
    assert order.to_dict()["evidence_id"] == "orphan-order"
    with pytest.raises(ScenarioValidationError, match="AMBIGUOUS_EVENT_ORDER"):
        scenario_module.build_scenario_timeline(
            case.calendar,
            case.config,
            replace(loaded, same_instant_order=(order,)),
        )


def test_same_instant_order_unknown_and_duplicate_internal_edge(tmp_path):
    case, sessions, _, _, _, _, _, _, loaded = loaded_scenario(tmp_path)
    opening = sessions.iloc[0].open.to_pydatetime()
    session_date = str(sessions.iloc[0].date.date())
    unknown = scenario_module.SameInstantOrder(
        timestamp=opening,
        before_event_id="unknown",
        after_event_id=f"session:{session_date}:rebalance",
        evidence_id="unknown-edge",
        source="test",
    )
    with pytest.raises(ScenarioValidationError, match="AMBIGUOUS_EVENT_ORDER"):
        scenario_module.build_scenario_timeline(
            case.calendar,
            case.config,
            replace(loaded, same_instant_order=(unknown,)),
        )
    duplicate_internal = replace(
        unknown,
        before_event_id=f"session:{session_date}:open-marks",
        evidence_id="duplicate-internal-edge",
    )
    timeline = scenario_module.build_scenario_timeline(
        case.calendar,
        case.config,
        replace(loaded, same_instant_order=(duplicate_internal,)),
    )
    assert timeline[0].kind == "open_marks"


def test_dependency_identity_fail_closed_branches(tmp_path, monkeypatch):
    with pytest.raises(ScenarioValidationError, match="unsupported editable URL"):
        scenario_module._path_from_file_url("https://example.invalid/repo")
    assert scenario_module._path_from_file_url(tmp_path.as_uri()) == tmp_path.resolve()

    without_git = tmp_path / "without-git"
    without_git.mkdir()
    with pytest.raises(ScenarioValidationError, match="lacks .git"):
        scenario_module._git_identity(without_git, "dependency")
    git_root = tmp_path / "git-root"
    (git_root / ".git").mkdir(parents=True)
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="abc\n"),
            SimpleNamespace(returncode=0, stdout="dirty\n"),
        ]
    )
    monkeypatch.setattr(scenario_module.subprocess, "run", lambda *args, **kwargs: next(responses))
    with pytest.raises(ScenarioValidationError, match="missing or dirty"):
        scenario_module._git_identity(git_root, "dependency")
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="abc\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )
    monkeypatch.setattr(scenario_module.subprocess, "run", lambda *args, **kwargs: next(responses))
    assert scenario_module._git_identity(git_root, "dependency") == "abc"


def test_dependency_direct_url_identity_branches(tmp_path, monkeypatch):
    root = tmp_path / "distribution"
    root.mkdir()
    inside = root / "package.py"
    inside.write_text("", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")

    class Distribution:
        def __init__(self, payload):
            self.payload = payload

        def read_text(self, name):
            assert name == "direct_url.json"
            return self.payload

        def locate_file(self, name):
            assert name == ""
            return root

    def configure(payload, module_path=inside):
        monkeypatch.setattr(
            scenario_module.importlib.metadata,
            "distribution",
            lambda name: Distribution(payload),
        )
        monkeypatch.setattr(
            scenario_module.importlib,
            "import_module",
            lambda name: SimpleNamespace(__file__=str(module_path)),
        )

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(scenario_module.importlib.metadata, "distribution", missing)
    with pytest.raises(ScenarioValidationError, match="metadata is missing"):
        scenario_module._dependency_commit("dependency", "module")
    configure(None)
    with pytest.raises(ScenarioValidationError, match="direct_url.json is missing"):
        scenario_module._dependency_commit("dependency", "module")
    configure(json.dumps({"vcs_info": {"commit_id": "abc"}}))
    assert scenario_module._dependency_commit("dependency", "module") == "abc"
    configure(json.dumps({"vcs_info": {"commit_id": "abc"}}), outside)
    with pytest.raises(ScenarioValidationError, match="outside"):
        scenario_module._dependency_commit("dependency", "module")
    configure(json.dumps({"url": root.as_uri(), "dir_info": {"editable": True}}), inside)
    monkeypatch.setattr(scenario_module, "_git_identity", lambda path, name: "editable-head")
    assert scenario_module._dependency_commit("dependency", "module") == "editable-head"
    configure(json.dumps({"url": root.as_uri(), "dir_info": {"editable": True}}), outside)
    with pytest.raises(ScenarioValidationError, match="outside editable root"):
        scenario_module._dependency_commit("dependency", "module")
    configure(json.dumps({"url": root.as_uri()}), inside)
    with pytest.raises(ScenarioValidationError, match="verifiable VCS identity"):
        scenario_module._dependency_commit("dependency", "module")


def test_stack_lock_requires_exact_runtime_commits(monkeypatch):
    monkeypatch.setattr(
        scenario_module,
        "DEPENDENCY_COMMITS",
        {"quant-execution": "a" * 40},
    )
    with pytest.raises(ScenarioValidationError, match="scenario lock does not pin"):
        REAL_VERIFY_DEPENDENCY_STACK()

    expected = "62a75a4bfacb445cc0809b8e1b283dbe4312050a"
    monkeypatch.setattr(
        scenario_module,
        "DEPENDENCY_COMMITS",
        {"quant-execution": expected},
    )
    monkeypatch.setattr(scenario_module, "_dependency_commit", lambda name, module: "0" * 40)
    with pytest.raises(ScenarioValidationError, match="expected"):
        REAL_VERIFY_DEPENDENCY_STACK()
    monkeypatch.setattr(scenario_module, "_dependency_commit", lambda name, module: expected)
    identities = REAL_VERIFY_DEPENDENCY_STACK()
    assert identities["quant-execution"] == expected
    assert len(identities["stack.dividend-scenario.lock"]) == 64


def test_file_runner_rejects_existing_output_and_hash_mismatch(tmp_path):
    case, _, ex_at, conversion_at, payment_at, as_of, _, payload, _ = loaded_scenario(tmp_path)
    value = lifecycle(
        case=case,
        ex_at=ex_at,
        conversion_at=conversion_at,
        payment_at=payment_at,
    )
    path = write_scenario(tmp_path, payload)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        scenario_module.run_dividend_scenario_files(
            snapshot=case.snapshot,
            config_path=case.config_path,
            scenario_path=path,
            output=existing,
        )

    bad_config = copy.deepcopy(payload)
    bad_config["study_config_sha256"] = "0" * 64
    bad_config_path = write_scenario(tmp_path, bad_config, "bad-config.json")
    with pytest.raises(ScenarioValidationError, match="INPUT_HASH_MISMATCH"):
        scenario_module.run_dividend_scenario_files(
            snapshot=case.snapshot,
            config_path=case.config_path,
            scenario_path=bad_config_path,
            output=tmp_path / "bad-config-output",
        )
    bad_snapshot = scenario_payload(case, value, ex_at=ex_at, as_of=as_of)
    bad_snapshot["snapshot_manifest_sha256"] = "0" * 64
    bad_snapshot_path = write_scenario(tmp_path, bad_snapshot, "bad-snapshot.json")
    with pytest.raises(ScenarioValidationError, match="INPUT_HASH_MISMATCH"):
        scenario_module.run_dividend_scenario_files(
            snapshot=case.snapshot,
            config_path=case.config_path,
            scenario_path=bad_snapshot_path,
            output=tmp_path / "bad-snapshot-output",
        )


def test_v2_evidence_controls_universe_calendar_and_order_permission(tmp_path):
    case = create_case(tmp_path)
    case.config["schema"] = "quant-hk-study/v2"
    case.config["settlement_calendar_scope"] = "evidenced_purpose_calendar"
    case.config["financial"] = v2_financial(case)
    case.config_path.write_text(json.dumps(case.config), encoding="utf-8")
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
        "v2-scenario.json",
    )
    output = tmp_path / "v2-output"
    result = scenario_module.run_dividend_scenario_files(
        snapshot=case.snapshot,
        config_path=case.config_path,
        scenario_path=path,
        output=output,
    )
    orders = pd.read_csv(output / "orders.csv")
    assert result["status"] == "complete_as_of"
    assert not orders.empty
    assert orders.status.eq("blocked_unknown").all()

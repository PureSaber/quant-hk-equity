"""Command line entry point. No credentials or order transmission."""

import argparse
import json
from pathlib import Path

from quant_data_kit.hong_kong import capture_hk_snapshot, load_hk_snapshot

from quant_hk_equity.research import prepare, run_study, validate_config


def main():
    parser = argparse.ArgumentParser(description="Hong Kong daily research")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "preflight", "run"):
        item = sub.add_parser(name)
        item.add_argument("--config", required=True, type=Path)
        item.add_argument("--snapshot", required=True, type=Path)
        if name == "run":
            item.add_argument("--output", required=True, type=Path)
    scenario = sub.add_parser("dividend-scenario")
    scenario.add_argument("--config", required=True, type=Path)
    scenario.add_argument("--snapshot", required=True, type=Path)
    scenario.add_argument("--scenario", required=True, type=Path)
    scenario.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "dividend-scenario":
        # Keep the new QExec surface out of the frozen default environment.
        from quant_hk_equity.dividend_scenario import run_dividend_scenario_files

        result = run_dividend_scenario_files(
            snapshot=args.snapshot,
            config_path=args.config,
            scenario_path=args.scenario,
            output=args.output,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    config = validate_config(json.loads(args.config.read_text(encoding="utf-8")))
    if args.command == "fetch":
        result = capture_hk_snapshot(
            args.snapshot,
            [i["symbol"] for i in config["instruments"]],
            config["data_start"],
            config["data_end"],
            provider=config["provider"],
        )
    elif args.command == "preflight":
        bars, calendar, manifest = load_hk_snapshot(args.snapshot)
        prepared = prepare(bars, calendar, config)
        result = {
            "software_preflight": "pass",
            "rows": len(prepared),
            "symbols": len(config["instruments"]),
            "investable": False,
            "corporate_actions_complete": manifest["corporate_actions_complete"],
        }
    else:
        result = run_study(args.snapshot, args.config, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

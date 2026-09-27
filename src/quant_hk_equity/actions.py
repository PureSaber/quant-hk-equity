"""Cross-market action replay on the shared HK account; distinct from price-only signals."""

from quant_data_kit.financial.actions import ActionTerms
from quant_data_kit.financial.common import number, utc


def replay_actions(account, records, *, through):
    """All targets must be registered on account. Records include explicit elections.

    Invoke chronologically alongside trades/marks, not after completing a price-only
    backtest. This does not certify the source's action coverage or total-return signal.
    """
    actions = [ActionTerms(**row) for row in records]
    for action in sorted(
        actions, key=lambda x: (max(utc(x.effective_at), utc(x.available_at)), x.event_id)
    ):
        at = max(utc(action.effective_at), utc(action.available_at))
        if at <= utc(through):
            if action.currency != "HKD":
                raise ValueError("HK action adapter requires HKD")
            if (
                action.kind == "rights_exercise"
                and f"financial-action:{action.event_id}" not in account.ledger._event_fingerprints
            ):
                required = number(action.election_quantity) * number(action.cash_per_unit)
                if required > account.available_cash():
                    raise ValueError("rights election exceeds settled HK cash")
            account.ledger.apply_corporate_action(action, at=at)
    return account.ledger.snapshot()

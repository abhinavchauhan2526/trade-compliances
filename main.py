"""Check proposed trades against portfolio rules and save their decisions.

The program reads trades from CSV, loads portfolio data from JSON, checks each
trade in rule order, and writes a CSV containing each decision and its reason.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence


TRADE_COLUMNS = (
    "portfolio_id",
    "security_id",
    "action",
    "quantity",
    "price",
    "security_type",
)
DECISION_COLUMNS = ("decision", "reason")
INVALID_TRADE_REASON = "Invalid trade data."


@dataclass(frozen=True)
class Holding:
    """A portfolio position and its reference market price."""

    security_id: str
    quantity: Decimal
    market_price: Decimal


@dataclass(frozen=True)
class Portfolio:
    """Reference data needed to evaluate trades for one portfolio."""

    cash_balance: Decimal
    restricted_securities: frozenset[str]
    max_position_pct: Decimal
    current_holdings: tuple[Holding, ...]


@dataclass(frozen=True)
class Trade:
    """A validated trade request with numeric values stored as Decimals."""

    portfolio_id: str
    security_id: str
    action: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class TradeResult:
    """The decision and human-readable explanation for one trade."""

    decision: str
    reason: str


def _decimal_value(value: object, field_name: str) -> Decimal:
    """Convert a value to a finite Decimal or raise a field-specific error.

    Decimal arithmetic keeps cash, price, quantity, and threshold comparisons
    precise, including cases where a trade lands exactly on a limit.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} must be a finite number.") from None
    if not number.is_finite():
        raise ValueError(f"{field_name} must be a finite number.")
    return number


def _portfolio_from_data(portfolio_id: str, data: object) -> Portfolio:
    """Validate one portfolio JSON record and convert it to typed data.

    Raises:
        ValueError: If a required field is missing or has an invalid value.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Portfolio {portfolio_id!r} must be a JSON object.")

    try:
        cash = _decimal_value(data["cash_balance"], "cash_balance")
        max_position_pct = _decimal_value(
            data["max_position_pct"], "max_position_pct"
        )
        restricted_data = data["restricted_securities"]
        holdings_data = data["current_holdings"]
    except KeyError as error:
        raise ValueError(
            f"Portfolio {portfolio_id!r} is missing {error.args[0]!r}."
        ) from None

    if cash < 0:
        raise ValueError(f"Portfolio {portfolio_id!r} has negative cash_balance.")
    if not 0 <= max_position_pct <= 1:
        raise ValueError(
            f"Portfolio {portfolio_id!r} max_position_pct must be between 0 and 1."
        )
    if not isinstance(restricted_data, list) or not all(
        isinstance(security_id, str) and security_id
        for security_id in restricted_data
    ):
        raise ValueError(
            f"Portfolio {portfolio_id!r} restricted_securities must be a list "
            "of non-empty security IDs."
        )
    if not isinstance(holdings_data, list):
        raise ValueError(
            f"Portfolio {portfolio_id!r} current_holdings must be a list."
        )

    holdings: list[Holding] = []
    for index, item in enumerate(holdings_data, start=1):
        if not isinstance(item, dict):
            raise ValueError(
                f"Portfolio {portfolio_id!r} holding {index} must be a JSON object."
            )
        try:
            security_id = item["security_id"]
            quantity = _decimal_value(item["quantity"], "holding quantity")
            market_price = _decimal_value(item["market_price"], "market_price")
        except KeyError as error:
            raise ValueError(
                f"Portfolio {portfolio_id!r} holding {index} is missing "
                f"{error.args[0]!r}."
            ) from None
        if not isinstance(security_id, str) or not security_id:
            raise ValueError(
                f"Portfolio {portfolio_id!r} holding {index} has an invalid "
                "security_id."
            )
        if quantity < 0 or market_price <= 0:
            raise ValueError(
                f"Portfolio {portfolio_id!r} holding {index} must have a "
                "non-negative quantity and positive market_price."
            )
        holdings.append(Holding(security_id, quantity, market_price))

    return Portfolio(
        cash_balance=cash,
        restricted_securities=frozenset(restricted_data),
        max_position_pct=max_position_pct,
        current_holdings=tuple(holdings),
    )


def load_portfolios(path: Path) -> dict[str, Portfolio]:
    """Load and validate portfolio reference data from a JSON file.

    The JSON object is keyed by portfolio ID. Each value contains cash,
    restricted securities, the concentration cap, and current holdings.
    """
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("Portfolio reference data must be a JSON object.")
    portfolios: dict[str, Portfolio] = {}
    for portfolio_id, portfolio_data in data.items():
        if not isinstance(portfolio_id, str) or not portfolio_id:
            raise ValueError("Portfolio IDs must be non-empty strings.")
        portfolios[portfolio_id] = _portfolio_from_data(
            portfolio_id, portfolio_data
        )
    return portfolios


def _parse_trade(row: Mapping[str, object]) -> tuple[Trade | None, TradeResult | None]:
    """Run Step 1: validate a CSV trade before portfolio checks are performed.

    Invalid trade values return a rejected result instead of raising, so the
    caller can include the rejection alongside other trade decisions.
    """
    try:
        quantity = _decimal_value(row.get("quantity"), "quantity")
        price = _decimal_value(row.get("price"), "price")
        action = row.get("action")
        security_id = row.get("security_id")
        portfolio_id = row.get("portfolio_id")
    except ValueError:
        return None, TradeResult("Rejected", INVALID_TRADE_REASON)

    if quantity <= 0 or price <= 0:
        return None, TradeResult("Rejected", INVALID_TRADE_REASON)
    if action not in ("Buy", "Sell"):
        return None, TradeResult("Rejected", INVALID_TRADE_REASON)
    if not isinstance(security_id, str) or not security_id:
        return None, TradeResult("Rejected", INVALID_TRADE_REASON)
    if not isinstance(portfolio_id, str) or not portfolio_id:
        return None, TradeResult("Rejected", INVALID_TRADE_REASON)

    return Trade(portfolio_id, security_id, action, quantity, price), None


def evaluate_trade(trade: Trade, portfolio: Portfolio) -> TradeResult:
    """Apply the compliance rules in order and return the first applicable result.

    Trade fields are validated in Step 1 before this function is called.
    This function then checks, in order:
    2. The security is not restricted.
    3. For sells, enough shares are held.
    4. For buys, enough cash is available.
    5. For affordable buys, position concentration is within the limit.
    """
    # Step 2: restricted securities cannot be traded.
    if trade.security_id in portfolio.restricted_securities:
        return TradeResult(
            "Rejected", "Security is restricted for this portfolio."
        )

    # Add together any separate entries for this security.
    target_quantity = Decimal(0)
    for holding in portfolio.current_holdings:
        if holding.security_id == trade.security_id:
            target_quantity += holding.quantity

    # Step 3: a sell is allowed only when enough shares are held.
    if trade.action == "Sell":
        if target_quantity < trade.quantity:
            return TradeResult("Rejected", "Insufficient holdings to sell.")
        return TradeResult("Approved", "Trade passed all compliance checks.")

    # Step 4: a buy must fit within the available cash.
    cost = trade.quantity * trade.price
    if cost > portfolio.cash_balance:
        return TradeResult(
            "Rejected", "Insufficient cash for this buy order."
        )

    # Step 5: work out the portfolio values after this buy.
    post_trade_cash = portfolio.cash_balance - cost
    post_trade_quantity = target_quantity + trade.quantity
    post_trade_security_value = post_trade_quantity * trade.price

    value_of_other_holdings = Decimal(0)
    for holding in portfolio.current_holdings:
        if holding.security_id != trade.security_id:
            value_of_other_holdings += holding.quantity * holding.market_price

    total_portfolio_value_after = (
        post_trade_cash + post_trade_security_value + value_of_other_holdings
    )

    # Cross-multiplication avoids rounding when the position is exactly at the cap.
    if post_trade_security_value > (
        portfolio.max_position_pct * total_portfolio_value_after
    ):
        return TradeResult(
            "Needs Manual Sign-off",
            "Exceeds maximum position concentration — requires approval.",
        )

    return TradeResult("Approved", "Trade passed all compliance checks.")


def process_trades(
    rows: Sequence[Mapping[str, object]],
    portfolios: Mapping[str, Portfolio],
) -> list[dict[str, object]]:
    """Evaluate trade rows and return their original fields plus decisions.

    CSV row numbers include the header, so the first trade is reported as row 2.
    A valid trade referring to an unknown portfolio raises ValueError.
    """
    results: list[dict[str, object]] = []
    for row_number, row in enumerate(rows, start=2):
        # Invalid trade fields are reported before looking up portfolio data.
        trade, validation_result = _parse_trade(row)
        if validation_result is not None:
            result = validation_result
        else:
            assert trade is not None
            try:
                portfolio = portfolios[trade.portfolio_id]
            except KeyError:
                raise ValueError(
                    f"Trade row {row_number} references unknown portfolio "
                    f"{trade.portfolio_id!r}."
                ) from None
            result = evaluate_trade(trade, portfolio)

        # Keep the original trade columns and append the decision fields.
        results.append(
            {
                **{column: row.get(column, "") for column in TRADE_COLUMNS},
                "decision": result.decision,
                "reason": result.reason,
            }
        )
    return results


def read_trades(path: Path) -> list[dict[str, str]]:
    """Read trade records from CSV after checking the required header columns."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing_columns = set(TRADE_COLUMNS) - set(reader.fieldnames or ())
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise ValueError(f"Trade CSV is missing required columns: {missing}.")
        return list(reader)


def write_results(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    """Write trade fields and decisions to a UTF-8 CSV file."""
    fieldnames = [*TRADE_COLUMNS, *DECISION_COLUMNS]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line parser with paths to the sample files as defaults."""
    project_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Check proposed portfolio trades for compliance."
    )
    parser.add_argument(
        "--trades",
        type=Path,
        default=project_dir / "trades_test.csv",
        help="Input trade CSV (default: trades_test.csv).",
    )
    parser.add_argument(
        "--portfolios",
        type=Path,
        default=project_dir / "portfolios.json",
        help="Portfolio reference JSON (default: portfolios.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=project_dir / "results.csv",
        help="Output decision CSV (default: results.csv).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checker, report input errors, and return a process exit code."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        portfolios = load_portfolios(args.portfolios)
        trades = read_trades(args.trades)
        results = process_trades(trades, portfolios)
        write_results(args.output, results)
    except (OSError, json.JSONDecodeError, csv.Error, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    print(f"Checked {len(results)} trades. Results written to {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from main import (
    Holding,
    Portfolio,
    Trade,
    _parse_trade,
    evaluate_trade,
    load_portfolios,
    process_trades,
    read_trades,
)


def portfolio(
    cash="100",
    cap="1",
    restricted=(),
    holdings=(),
):
    return Portfolio(
        cash_balance=Decimal(cash),
        restricted_securities=frozenset(restricted),
        max_position_pct=Decimal(cap),
        current_holdings=tuple(holdings),
    )


def trade(action="Buy", security_id="AAPL", quantity="1", price="10"):
    return Trade(
        portfolio_id="P1",
        security_id=security_id,
        action=action,
        quantity=Decimal(quantity),
        price=Decimal(price),
    )


class TradeValidationTests(unittest.TestCase):
    def test_invalid_quantity_price_and_action_are_rejected(self):
        invalid_rows = [
            {"portfolio_id": "P1", "security_id": "AAPL", "action": "Buy",
             "quantity": "0", "price": "10"},
            {"portfolio_id": "P1", "security_id": "AAPL", "action": "Buy",
             "quantity": "-1", "price": "10"},
            {"portfolio_id": "P1", "security_id": "AAPL", "action": "Buy",
             "quantity": "one", "price": "10"},
            {"portfolio_id": "P1", "security_id": "AAPL", "action": "Buy",
             "quantity": "1", "price": "0"},
            {"portfolio_id": "P1", "security_id": "AAPL", "action": "Buy",
             "quantity": "1", "price": "NaN"},
            {"portfolio_id": "P1", "security_id": "AAPL", "action": "Hold",
             "quantity": "1", "price": "10"},
        ]
        for row in invalid_rows:
            with self.subTest(row=row):
                parsed_trade, result = _parse_trade(row)
                self.assertIsNone(parsed_trade)
                self.assertEqual(result.decision, "Rejected")
                self.assertEqual(result.reason, "Invalid trade data.")

    def test_invalid_trade_does_not_reach_concentration_check(self):
        rows = [{
            "portfolio_id": "P1",
            "security_id": "BLOCKED",
            "action": "Buy",
            "quantity": "-1",
            "price": "10",
        }]
        results = process_trades(
            rows,
            {
                "P1": portfolio(
                    cap="0",
                    holdings=(Holding("OTHER", Decimal("10"), Decimal("10")),),
                )
            },
        )
        self.assertEqual(results[0]["reason"], "Invalid trade data.")

    def test_restricted_security_is_rejected(self):
        result = evaluate_trade(
            trade(security_id="BLOCKED"),
            portfolio(restricted=("BLOCKED",)),
        )
        self.assertEqual(result.decision, "Rejected")
        self.assertEqual(
            result.reason, "Security is restricted for this portfolio."
        )

    def test_restriction_check_precedes_insufficient_sell_holdings(self):
        result = evaluate_trade(
            trade(action="Sell", security_id="BLOCKED", quantity="5"),
            portfolio(restricted=("BLOCKED",)),
        )
        self.assertEqual(result.reason, "Security is restricted for this portfolio.")


class SellRuleTests(unittest.TestCase):
    def test_holding_exactly_quantity_is_sufficient(self):
        result = evaluate_trade(
            trade(action="Sell", quantity="5"),
            portfolio(holdings=(Holding("AAPL", Decimal("5"), Decimal("10")),)),
        )
        self.assertEqual(result.decision, "Approved")

    def test_insufficient_holdings_are_rejected(self):
        result = evaluate_trade(
            trade(action="Sell", quantity="5"),
            portfolio(holdings=(Holding("AAPL", Decimal("4"), Decimal("10")),)),
        )
        self.assertEqual(result.decision, "Rejected")
        self.assertEqual(result.reason, "Insufficient holdings to sell.")

    def test_sell_does_not_run_concentration_check(self):
        result = evaluate_trade(
            trade(action="Sell", quantity="1"),
            portfolio(
                cap="0",
                holdings=(Holding("AAPL", Decimal("1"), Decimal("10")),),
            ),
        )
        self.assertEqual(result.decision, "Approved")


class BuyRuleTests(unittest.TestCase):
    def test_buy_costing_exactly_cash_balance_is_affordable(self):
        result = evaluate_trade(
            trade(quantity="10", price="10"),
            portfolio(cash="100"),
        )
        self.assertEqual(result.decision, "Approved")

    def test_buy_exceeding_cash_is_rejected(self):
        result = evaluate_trade(
            trade(quantity="10.01", price="10"),
            portfolio(cash="100"),
        )
        self.assertEqual(result.decision, "Rejected")
        self.assertEqual(result.reason, "Insufficient cash for this buy order.")

    def test_affordability_check_precedes_concentration_check(self):
        result = evaluate_trade(
            trade(quantity="11", price="10"),
            portfolio(
                cash="100",
                cap="0",
                holdings=(Holding("OTHER", Decimal("10"), Decimal("10")),),
            ),
        )
        self.assertEqual(result.reason, "Insufficient cash for this buy order.")

    def test_exact_concentration_cap_is_approved(self):
        result = evaluate_trade(
            trade(quantity="10", price="10"),
            portfolio(
                cash="100",
                cap="0.5",
                holdings=(Holding("OTHER", Decimal("10"), Decimal("10")),),
            ),
        )
        self.assertEqual(result.decision, "Approved")

    def test_concentration_above_cap_needs_manual_signoff(self):
        result = evaluate_trade(
            trade(quantity="10", price="10"),
            portfolio(
                cash="100",
                cap="0.49",
                holdings=(Holding("OTHER", Decimal("10"), Decimal("10")),),
            ),
        )
        self.assertEqual(result.decision, "Needs Manual Sign-off")
        self.assertEqual(
            result.reason,
            "Exceeds maximum position concentration — requires approval.",
        )

    def test_concentration_uses_trade_price_for_target_and_stored_price_for_others(self):
        result = evaluate_trade(
            trade(quantity="2", price="10"),
            portfolio(
                cash="1000",
                cap="0.025",
                holdings=(
                    Holding("AAPL", Decimal("1"), Decimal("700")),
                    Holding("OTHER", Decimal("10"), Decimal("20")),
                ),
            ),
        )
        self.assertEqual(result.decision, "Approved")

    def test_existing_target_quantity_is_included_in_concentration(self):
        result = evaluate_trade(
            trade(quantity="2", price="10"),
            portfolio(
                cash="20",
                cap="0.5",
                holdings=(
                    Holding("AAPL", Decimal("2"), Decimal("99")),
                    Holding("OTHER", Decimal("2"), Decimal("10")),
                ),
            ),
        )
        self.assertEqual(result.decision, "Needs Manual Sign-off")

    def test_duplicate_holdings_are_aggregated(self):
        result = evaluate_trade(
            trade(action="Sell", quantity="5"),
            portfolio(
                holdings=(
                    Holding("AAPL", Decimal("2"), Decimal("10")),
                    Holding("AAPL", Decimal("3"), Decimal("11")),
                ),
            ),
        )
        self.assertEqual(result.decision, "Approved")


class InputOutputTests(unittest.TestCase):
    def test_unknown_portfolio_is_reported_for_valid_trade(self):
        with self.assertRaisesRegex(ValueError, "unknown portfolio"):
            process_trades(
                [{
                    "portfolio_id": "MISSING",
                    "security_id": "AAPL",
                    "action": "Buy",
                    "quantity": "1",
                    "price": "10",
                }],
                {},
            )

    def test_portfolio_reference_file_is_loaded(self):
        sample_path = Path(__file__).with_name("portfolios.json")
        loaded = load_portfolios(sample_path)
        self.assertIn("POR001", loaded)
        self.assertEqual(loaded["POR001"].cash_balance, Decimal("10000"))

    def test_csv_cli_processes_trades_and_writes_decisions(self):
        project_dir = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            trades_path = temp_path / "trades.csv"
            portfolios_path = temp_path / "portfolios.json"
            output_path = temp_path / "results.csv"

            with trades_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=[
                        "portfolio_id", "security_id", "action",
                        "quantity", "price", "security_type",
                    ],
                )
                writer.writeheader()
                writer.writerow({
                    "portfolio_id": "P1", "security_id": "AAPL",
                    "action": "Buy", "quantity": "1", "price": "10",
                    "security_type": "Equity",
                })
                writer.writerow({
                    "portfolio_id": "P1", "security_id": "AAPL",
                    "action": "Sell", "quantity": "1", "price": "10",
                    "security_type": "Equity",
                })
            portfolios_path.write_text(
                json.dumps({
                    "P1": {
                        "cash_balance": 100,
                        "restricted_securities": [],
                        "max_position_pct": 1,
                        "current_holdings": [
                            {"security_id": "AAPL", "quantity": 1, "market_price": 10}
                        ],
                    }
                }),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(project_dir / "main.py"),
                    "--trades", str(trades_path),
                    "--portfolios", str(portfolios_path),
                    "--output", str(output_path),
                ],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            with output_path.open(newline="", encoding="utf-8") as file:
                output = list(csv.DictReader(file))
            self.assertEqual(
                [row["decision"] for row in output], ["Approved", "Approved"]
            )
            self.assertTrue(all(row["reason"] for row in output))

    def test_read_trades_rejects_missing_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trades.csv"
            path.write_text("portfolio_id,security_id\nP1,AAPL\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required columns"):
                read_trades(path)


if __name__ == "__main__":
    unittest.main()

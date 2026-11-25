# Portfolio Trade Compliance Checker

A small command-line Python application that reviews proposed portfolio trades
and labels each one **Approved**, **Rejected**, or **Needs Manual Sign-off**.
It checks trade validity, restricted securities, sellable holdings, buy
affordability, and buy-order position concentration.

> This is a sample compliance checker for learning and demonstration. It is not
> investment, legal, or regulatory advice and should not be used to execute
> real trades without appropriate review and controls.

## Requirements

- Python 3.10 or later
- No third-party packages

## Run the checker

Open a terminal in this project folder and run:

```powershell
python main.py
```

By default, the checker reads:

- `trades_test.csv` — proposed trades
- `portfolios.json` — portfolio reference data

It writes `results.csv` with the original trade columns and two added columns:
`decision` and `reason`. Each run replaces the previous output file.

You can specify other input and output files:

```powershell
python main.py --trades proposed_trades.csv --portfolios portfolios.json --output decisions.csv
```

See `python main.py --help` for all command-line options.

## Input formats

### Trade CSV

The CSV must include these columns:

| Column | Description |
| --- | --- |
| `portfolio_id` | ID used to find the portfolio reference data |
| `security_id` | Security being traded |
| `action` | `Buy` or `Sell` |
| `quantity` | Positive trade quantity |
| `price` | Positive per-unit trade price |
| `security_type` | Security type, such as Equity, Bond, or ETF |

### Portfolio JSON

The JSON object is keyed by portfolio ID. Each portfolio has a cash balance,
restricted security list, concentration limit, and holdings. For example:

```json
{
  "PORTFOLIO1": {
    "cash_balance": 10000,
    "restricted_securities": ["RESTRICTED1"],
    "max_position_pct": 0.25,
    "current_holdings": [
      {
        "security_id": "ABC",
        "quantity": 10,
        "market_price": 150
      }
    ]
  }
}
```

`max_position_pct` is a fraction between 0 and 1; for example, `0.25` means
25%. Holdings have their own `market_price`, which is used to value securities
other than the one being bought.

## Rules

Checks are applied in this order:

1. Quantity and price must be positive, and action must be `Buy` or `Sell`.
2. Restricted securities are rejected.
3. A sell requires sufficient existing holdings.
4. A buy must not cost more than the portfolio's cash balance.
5. An affordable buy that exceeds the maximum position concentration needs
   manual sign-off.
6. Otherwise, the trade is approved.

Equality at the cash, holdings, and concentration limits passes. Sells do not
go through the buy concentration check. Detailed rule definitions are in
[`trade_compliance_requirements.md`](trade_compliance_requirements.md).

## Run tests

From the project folder:

```powershell
python -m unittest -v
```

The test suite covers the compliance rules, boundary conditions, input
validation, CSV output, and command-line execution.

## Project files

- `main.py` — command-line application and compliance checks
- `test_main.py` — automated tests
- `trades_test.csv` — sample proposed trades
- `portfolios.json` — sample portfolio reference data
- `trade_compliance_requirements.md` — detailed requirements and usage notes

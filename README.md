# Tastytrade OTOCO Bot (Terminal UI)

Terminal-based CLI and UI for building OCC symbols, showing PDH/PDL pivots, and placing OTOCO orders on tastytrade using the unofficial `tastytrade` Python SDK.

## Setup

1. Create a `.env` in the project root with:

```
TT_USERNAME=your_username
TT_PASSWORD=your_password
TT_ACCOUNT_NUMBER=your_account_number
UNDERLYING=SPY
TARGET_DELTA=0.35
DTE_DAYS=7
TP_PCT=0.40
SL_PCT=0.30
# OCC_SYMBOL=AAPL  250214C00240000  # optional
```

2. Install dependencies:

```
pip install -r requirements.txt
```

## Usage

- Build OCC symbol:

```
python tt_otoco_cli.py build-occ --symbol AAPL --cp call --expiry 2025-02-14 --strike 240
```

- Show levels and signal (auto-uses tastytrade SDK for price/history if installed):

```
python tt_otoco_cli.py signal --symbol SPY
```

- Place an OTOCO order:

```
python tt_otoco_cli.py place-otoco --occ "AAPL  250214C00240000" --qty 1 --entry 1.00 --tp 1.40 --sl 0.70
```

- Interactive terminal UI:

```
python tt_otoco_cli.py ui
```

Notes:
- This uses the unofficial `tastytrade` SDK (`pip install tastytrade`).
- Market data and auth will fall back gracefully if the SDK is missing, but placing orders requires a valid authenticated session.
from __future__ import annotations

import os
import sys
import json
import asyncio
from dataclasses import dataclass
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional, Tuple

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.theme import Theme
from rich.prompt import Prompt, Confirm
from dotenv import load_dotenv
import requests

# tastytrade SDK (unofficial)
try:
    from tastytrade import Session as TTSession
    from tastytrade import Account as TTAccount
    from tastytrade.market_data import get_history, HistoryGranularity, HistoryPeriod
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote
except Exception as _e:
    TTSession = None  # type: ignore
    TTAccount = None  # type: ignore
    get_history = None  # type: ignore
    HistoryGranularity = None  # type: ignore
    HistoryPeriod = None  # type: ignore
    DXLinkStreamer = None  # type: ignore
    Quote = None  # type: ignore

app = typer.Typer(add_completion=False, help="Styled tastytrade OTOCO bot")

console = Console(theme=Theme({
    "info": "cyan",
    "ok": "green",
    "warn": "yellow",
    "err": "bold red",
}))


# -------------------- Config & helpers --------------------

def load_env() -> None:
    load_dotenv()


def env_str(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(key)
    return v if v not in (None, "") else default


def env_decimal(key: str, default: float) -> Decimal:
    v = os.getenv(key)
    return Decimal(str(default)) if v in (None, "") else Decimal(v)


@dataclass
class BotConfig:
    username: str
    password: str
    account: str
    underlying: str = "SPY"
    target_delta: Decimal = Decimal("0.35")
    dte_days: int = 7
    tp_pct: Decimal = Decimal("0.40")
    sl_pct: Decimal = Decimal("0.30")
    occ_symbol: Optional[str] = None

    @staticmethod
    def from_env() -> "BotConfig":
        load_env()
        username = env_str("TT_USERNAME")
        password = env_str("TT_PASSWORD")
        account = env_str("TT_ACCOUNT_NUMBER")
        if not username or not password or not account:
            console.print("[err]Missing TT_USERNAME, TT_PASSWORD, or TT_ACCOUNT_NUMBER in .env[/err]")
            raise typer.Exit(1)
        return BotConfig(
            username=username,
            password=password,
            account=account,
            underlying=env_str("UNDERLYING", "SPY"),
            target_delta=env_decimal("TARGET_DELTA", 0.35),
            dte_days=int(env_str("DTE_DAYS", "7")),
            tp_pct=env_decimal("TP_PCT", 0.40),
            sl_pct=env_decimal("SL_PCT", 0.30),
            occ_symbol=env_str("OCC_SYMBOL"),
        )


# -------------------- Market data using tastytrade SDK --------------------

def get_previous_day_hlc(symbol: str, session: Optional[TTSession] = None) -> Tuple[Decimal, Decimal, Decimal]:
    """Fetch previous day high/low/close using tastytrade SDK.
    Fallbacks to static values if SDK not available or on error.
    """
    if TTSession is None or get_history is None or HistoryGranularity is None or HistoryPeriod is None or session is None:
        return Decimal("100"), Decimal("98"), Decimal("99")

    try:
        # Fetch 5 days of daily candles and pick the last completed day (yesterday)
        candles = get_history(
            session,
            symbol,
            period=HistoryPeriod.DAY_5,
            granularity=HistoryGranularity.DAY,
        )
        # candles is List[Candle] where candle.ends_at is datetime
        # Filter to strictly before today (handle timezone)
        today = date.today()
        daily = [c for c in candles if (c.ends_at.date() < today)]  # type: ignore[attr-defined]
        if not daily:
            c = candles[-2]
            return Decimal(str(c.high)), Decimal(str(c.low)), Decimal(str(c.close))  # type: ignore[attr-defined]
        c = daily[-1]
        return Decimal(str(c.high)), Decimal(str(c.low)), Decimal(str(c.close))  # type: ignore[attr-defined]
    except Exception as e:
        console.print(f"[warn]Failed to fetch history via tastytrade SDK: {e}[/warn]")
        return Decimal("100"), Decimal("98"), Decimal("99")


def get_last_quote(symbol: str, session: Optional[TTSession]) -> Optional[Decimal]:
    if DXLinkStreamer is None or Quote is None or session is None:
        return None
    try:
        async def _once() -> Optional[Decimal]:
            async with DXLinkStreamer(session) as streamer:
                await streamer.subscribe(Quote, [symbol])
                evt = await streamer.get_event(Quote)
                # Use mid if available else last known ask/bid
                if getattr(evt, "ask_price", None) and getattr(evt, "bid_price", None):
                    ask = Decimal(str(evt.ask_price))
                    bid = Decimal(str(evt.bid_price))
                    if ask > 0 and bid > 0:
                        return (ask + bid) / Decimal("2")
                if getattr(evt, "price", None):
                    return Decimal(str(evt.price))
                return None
        return asyncio.run(_once())
    except Exception as e:
        console.print(f"[warn]Quote fetch failed: {e}[/warn]")
        return None


def classic_pivots(high: Decimal, low: Decimal, close: Decimal) -> dict:
    p = (high + low + close) / Decimal("3")
    r1 = (Decimal("2") * p) - low
    s1 = (Decimal("2") * p) - high
    r2 = p + (high - low)
    s2 = p - (high - low)
    return {"P": p, "R1": r1, "S1": s1, "R2": r2, "S2": s2}


def signal_from_pdh_pdl(current_price: Decimal, pdh: Decimal, pdl: Decimal, piv: dict) -> str:
    near_pivot = abs(current_price - piv["P"]) <= (piv["P"] * Decimal("0.002"))
    if current_price > pdh and near_pivot:
        return "breakout_up"
    if current_price < pdl and near_pivot:
        return "breakout_down"
    return "none"


# -------------------- OCC helpers --------------------

def build_occ_symbol(symbol: str, expiry_yyyy_mm_dd: str, call_put: str, strike: Decimal) -> str:
    """Very basic OCC formatter. For robust building, query instrument endpoints.
    OCC example: "AAPL  250214C00240000"""
    dt = datetime.strptime(expiry_yyyy_mm_dd, "%Y-%m-%d")
    yy = dt.strftime("%y")
    mm = dt.strftime("%m")
    dd = dt.strftime("%d")
    cp = "C" if call_put.lower().startswith("c") else "P"
    strike_int = int(Decimal(str(strike)) * 1000)
    strike_str = f"{strike_int:08d}"
    padded = f"{symbol:<6}"
    return f"{padded}{yy}{mm}{dd}{cp}{strike_str}"


# -------------------- Tastytrade HTTP using SDK session --------------------

class TastySession:
    """Session wrapper bridging tastytrade SDK and direct HTTP calls.
    Uses SDK for auth; exposes a requests.Session with cookies for raw endpoints.
    """

    def __init__(self, username: str, password: str, base_url: str = "https://api.tastytrade.com") -> None:
        self.base = base_url.rstrip("/")
        self.tt: Optional[TTSession] = None
        self.rs: Optional[requests.Session] = None
        if TTSession is None:
            console.print("[warn]tastytrade SDK not installed; HTTP calls will fail.[/warn]")
            return
        try:
            self.tt = TTSession(username, password)
            # tastytrade SDK uses underlying requests.Session for sync
            self.rs = getattr(self.tt, "session", None) or requests.Session()
        except Exception as e:
            console.print(f"[err]Login failed: {e}[/err]")
            console.print("[err]Install tastytrade (`pip install tastytrade`) and verify credentials.[/err]")
            self.tt = None
            self.rs = None

    def authenticated(self) -> bool:
        if self.tt is None or TTAccount is None:
            return False
        try:
            # Light auth check
            _ = TTAccount.get(self.tt)
            return True
        except Exception:
            return False

    def post(self, path: str, json_payload: dict) -> requests.Response:
        if self.rs is None:
            raise RuntimeError("No authenticated HTTP session available")
        url = f"{self.base}{path}"
        r = self.rs.post(url, json=json_payload, timeout=20)
        r.raise_for_status()
        return r


def otoco_payload_open_long_occ(underlying: str, occ: str, qty: int, entry: Decimal, tp: Decimal, sl: Decimal) -> dict:
    return {
        "type": "OTOCO",
        "trigger-order": {
            "time-in-force": "Day",
            "order-type": "Limit",
            "underlying-symbol": underlying,
            "price": float(entry),
            "price-effect": "Debit",
            "legs": [
                {
                    "instrument-type": "Equity Option",
                    "symbol": occ,
                    "quantity": qty,
                    "action": "Buy to Open",
                }
            ],
        },
        "orders": [
            {
                "time-in-force": "Day",
                "order-type": "Limit",
                "underlying-symbol": underlying,
                "price-effect": "Credit",
                "price": float(tp),
                "legs": [
                    {
                        "instrument-type": "Equity Option",
                        "symbol": occ,
                        "quantity": qty,
                        "action": "Sell to Close",
                    }
                ],
            },
            {
                "time-in-force": "Day",
                "order-type": "Stop",
                "underlying-symbol": underlying,
                "stop-trigger": float(sl),
                "legs": [
                    {
                        "instrument-type": "Equity Option",
                        "symbol": occ,
                        "quantity": qty,
                        "action": "Sell to Close",
                    }
                ],
            },
        ],
    }


# -------------------- CLI Commands --------------------

@app.command()
def build_occ(
    symbol: str = typer.Option(..., help="Underlying symbol, e.g., AAPL"),
    cp: str = typer.Option("call", help="call or put"),
    expiry: str = typer.Option(..., help="YYYY-MM-DD"),
    strike: float = typer.Option(..., help="Strike price, e.g., 240"),
):
    """Build a basic OCC symbol string."""
    occ = build_occ_symbol(symbol.upper(), expiry, cp, Decimal(str(strike)))
    console.print(Panel.fit(occ, title="OCC", style="ok"))


@app.command()
def signal(
    symbol: str = typer.Option("SPY", help="Underlying symbol"),
    price: float = typer.Option(0.0, help="Current price to test signal; 0 to derive from close"),
):
    """Show PDH/PDL, pivots, and a simple breakout signal."""
    cfg = None
    session = None
    try:
        cfg = BotConfig.from_env()
        session = TastySession(cfg.username, cfg.password).tt
    except Exception:
        pass

    pdh, pdl, pc = get_previous_day_hlc(symbol, session)
    piv = classic_pivots(pdh, pdl, pc)
    if price > 0:
        current = Decimal(str(price))
    else:
        q = get_last_quote(symbol, session)
        current = q if q is not None else pc

    t = Table(title=f"{symbol} Prior-Day Levels", show_edge=False)
    t.add_column("Metric", style="info")
    t.add_column("Value", style="ok")
    t.add_row("PDH", f"{pdh}")
    t.add_row("PDL", f"{pdl}")
    for k in ["P", "R1", "S1", "R2", "S2"]:
        t.add_row(k, f"{piv[k]:.2f}")
    console.print(t)

    sig = signal_from_pdh_pdl(current, pdh, pdl, piv)
    console.print(Panel(f"Price={current} → Signal: [bold]{sig}[/bold]", title="Signal", style="warn" if sig=="none" else "ok"))


@app.command("place-otoco")
def place_otoco(
    occ: str = typer.Option(..., help="OCC option symbol"),
    qty: int = typer.Option(1, min=1),
    entry: float = typer.Option(..., help="Entry limit premium"),
    tp: float = typer.Option(..., help="Take-profit limit premium"),
    sl: float = typer.Option(..., help="Stop-loss trigger premium"),
):
    """Submit an OTOCO for an option OCC symbol (buy-to-open + OCO exits)."""
    cfg = BotConfig.from_env()
    sess = TastySession(cfg.username, cfg.password)
    if not sess.authenticated():
        console.print("[err]Not authenticated. Install and use tastytrade SDK or verify credentials.[/err]")
        raise typer.Exit(1)
    payload = otoco_payload_open_long_occ(
        cfg.underlying, occ, qty, Decimal(str(entry)), Decimal(str(tp)), Decimal(str(sl))
    )
    console.print(Panel.fit(json.dumps(payload, indent=2), title="Submitting OTOCO", style="info"))
    try:
        r = sess.post(f"/accounts/{cfg.account}/complex-orders", payload)
        console.print(Panel.fit(r.text, title="Response", style="ok"))
    except Exception as e:
        console.print(f"[err]Order submission failed: {e}[/err]")
        raise typer.Exit(1)


@app.command()
def run(
    symbol: str = typer.Option("SPY"),
    qty: int = typer.Option(1, min=1),
    entry: float = typer.Option(0.0, help="Override entry price; 0 to compute from mid (placeholder)"),
):
    """Auto flow: PDH/PDL breakout → build OTOCO using TP_PCT/SL_PCT on entry premium.
    OCC must be provided via .env (OCC_SYMBOL) or use build-occ.
    """
    cfg = BotConfig.from_env()
    occ = cfg.occ_symbol
    if not occ:
        console.print("[err]Set OCC_SYMBOL in .env or use build-occ and place-otoco.[/err]")
        raise typer.Exit(1)

    tt = TastySession(cfg.username, cfg.password)
    if not tt.authenticated():
        console.print("[err]Not authenticated. Install and use tastytrade SDK or add auth flow.[/err]")
        raise typer.Exit(1)

    pdh, pdl, pc = get_previous_day_hlc(symbol, tt.tt)
    piv = classic_pivots(pdh, pdl, pc)
    q = get_last_quote(symbol, tt.tt)
    current = q if q is not None else pc
    sig = signal_from_pdh_pdl(current, pdh, pdl, piv)
    if sig == "none":
        console.print("[warn]No breakout signal. Exiting.[/warn]")
        raise typer.Exit(0)

    base_entry = Decimal(str(entry)) if entry > 0 else Decimal("1.00")
    tp = (base_entry * (Decimal("1") + cfg.tp_pct)).quantize(Decimal("0.01"))
    sl = (base_entry * (Decimal("1") - cfg.sl_pct)).quantize(Decimal("0.01"))

    payload = otoco_payload_open_long_occ(symbol, occ, qty, base_entry, tp, sl)
    console.print(Panel.fit(json.dumps(payload, indent=2), title=f"{symbol} {sig} OTOCO", style="info"))
    try:
        r = tt.post(f"/accounts/{cfg.account}/complex-orders", payload)
        console.print(Panel.fit(r.text, title="Response", style="ok"))
    except Exception as e:
        console.print(f"[err]Order submission failed: {e}[/err]")
        raise typer.Exit(1)


@app.command()
def ui():
    """Interactive terminal UI for signal + OTOCO placement."""
    cfg = BotConfig.from_env()
    sess = TastySession(cfg.username, cfg.password)
    if not sess.authenticated():
        console.print("[err]Login failed. Check your credentials and tastytrade SDK installation.[/err]")
        raise typer.Exit(1)

    symbol = cfg.underlying
    pdh, pdl, pc = get_previous_day_hlc(symbol, sess.tt)
    piv = classic_pivots(pdh, pdl, pc)
    q = get_last_quote(symbol, sess.tt)
    current = q if q is not None else pc

    # Render dashboard
    t = Table(title=f"{symbol} Prior-Day Levels", show_edge=False)
    t.add_column("Metric", style="info")
    t.add_column("Value", style="ok")
    t.add_row("PDH", f"{pdh}")
    t.add_row("PDL", f"{pdl}")
    for k in ["P", "R1", "S1", "R2", "S2"]:
        t.add_row(k, f"{piv[k]:.2f}")

    sig = signal_from_pdh_pdl(current, pdh, pdl, piv)

    console.print(t)
    console.print(Panel(f"Current={current} → Signal: [bold]{sig}[/bold]", title="Signal", style="warn" if sig=="none" else "ok"))

    # Menu
    while True:
        action = Prompt.ask("Select action", choices=["build-occ", "place-otoco", "refresh", "exit"], default="refresh")
        if action == "refresh":
            pdh, pdl, pc = get_previous_day_hlc(symbol, sess.tt)
            piv = classic_pivots(pdh, pdl, pc)
            q = get_last_quote(symbol, sess.tt)
            current = q if q is not None else pc
            console.print(t)
            console.print(Panel(f"Current={current} → Signal: [bold]{sig}[/bold]", title="Signal", style="warn" if sig=="none" else "ok"))
        elif action == "build-occ":
            sym = Prompt.ask("Underlying", default=symbol)
            cp = Prompt.ask("Call or Put", choices=["call", "put"], default="call")
            expiry = Prompt.ask("Expiry (YYYY-MM-DD)")
            strike = Prompt.ask("Strike", default="0")
            try:
                occ = build_occ_symbol(sym.upper(), expiry, cp, Decimal(str(strike)))
                console.print(Panel.fit(occ, title="OCC", style="ok"))
            except Exception as e:
                console.print(f"[err]OCC build failed: {e}[/err]")
        elif action == "place-otoco":
            occ = cfg.occ_symbol or Prompt.ask("OCC symbol")
            qty = int(Prompt.ask("Qty", default="1"))
            entry = Decimal(Prompt.ask("Entry premium", default="1.00"))
            tp = (entry * (Decimal("1") + cfg.tp_pct)).quantize(Decimal("0.01"))
            sl = (entry * (Decimal("1") - cfg.sl_pct)).quantize(Decimal("0.01"))
            console.print(Panel.fit(
                f"Underlying={symbol} OCC={occ} Qty={qty}\nEntry={entry} TP={tp} SL={sl}",
                title="Confirm OTOCO",
                style="info",
            ))
            if Confirm.ask("Submit order?", default=False):
                payload = otoco_payload_open_long_occ(symbol, occ, qty, entry, tp, sl)
                console.print(Panel.fit(json.dumps(payload, indent=2), title="Submitting OTOCO", style="info"))
                try:
                    r = sess.post(f"/accounts/{cfg.account}/complex-orders", payload)
                    console.print(Panel.fit(r.text, title="Response", style="ok"))
                except Exception as e:
                    console.print(f"[err]Order submission failed: {e}[/err]")
        else:
            break


if __name__ == "__main__":
    app()
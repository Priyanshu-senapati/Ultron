"""Stocks collector — yfinance for Sensex / Nifty / individual tickers.

yfinance is synchronous; we call it from ``run_in_executor`` in the
service. Returns a list of dicts, one per ticker, with the most recent
close + day change. Includes a tiny "insight" line for the headline
index so the HUD has something quotable.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger("ultron.dailydata.stocks")


def _fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def fetch_quotes(tickers: Iterable[str]) -> list[dict[str, Any]]:
    """Return one row per ticker with last close + day change."""
    try:
        import yfinance as yf  # type: ignore[import]
    except ImportError:
        return []
    out: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d", interval="1d", auto_adjust=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yfinance failed for %s: %s", ticker, exc)
            continue
        if hist is None or hist.empty:
            continue
        try:
            last = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) >= 2 else None
            close = float(last["Close"])
            prev_close = float(prev["Close"]) if prev is not None else close
            change = close - prev_close
            change_pct = (change / prev_close * 100.0) if prev_close else 0.0
            out.append({
                "ticker": ticker,
                "close": round(close, 2),
                "prev_close": round(prev_close, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 3),
                "change_pct_label": _fmt_pct(change_pct),
            })
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("yfinance parse failed for %s: %s", ticker, exc)
            continue
    return out


def insight_line(rows: list[dict[str, Any]]) -> str:
    """One short line summarising the day's tape. Headline = first row."""
    if not rows:
        return "no market data available"
    head = rows[0]
    pct = head["change_pct"]
    direction = "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"
    label = head["ticker"].lstrip("^")
    base = f"{label} {head['close']:.0f} {direction} {_fmt_pct(pct)}"
    if len(rows) <= 1:
        return base
    gainers = [r for r in rows[1:] if r["change_pct"] > 0]
    losers = [r for r in rows[1:] if r["change_pct"] < 0]
    parts = [base]
    if gainers:
        parts.append(f"{len(gainers)} up")
    if losers:
        parts.append(f"{len(losers)} down")
    return " | ".join(parts)

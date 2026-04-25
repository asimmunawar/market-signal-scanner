from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

try:
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.trend import MACD
except Exception:  # pragma: no cover - optional dependency fallback
    RSIIndicator = None
    StochasticOscillator = None
    MACD = None


TRADING_DAYS = 252


def compute_signals(ticker: str, prices: pd.DataFrame, fundamentals: dict[str, Any] | None = None) -> dict[str, Any]:
    frame = prices.copy().sort_index()
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    high = pd.to_numeric(frame.get("High", close), errors="coerce").reindex(close.index).ffill()
    low = pd.to_numeric(frame.get("Low", close), errors="coerce").reindex(close.index).ffill()
    volume = pd.to_numeric(frame.get("Volume", pd.Series(index=frame.index, dtype=float)), errors="coerce").reindex(close.index)
    returns = close.pct_change()

    latest_price = last_value(close)
    signals: dict[str, Any] = {
        "ticker": ticker,
        "entity_name": entity_name(ticker, fundamentals or {}),
        "yahoo_finance_url": yahoo_finance_url(ticker),
        "google_finance_url": google_finance_url(ticker, fundamentals or {}),
        "tradingview_url": tradingview_url(ticker),
        "asset_type": "crypto" if ticker.endswith("-USD") else "equity_or_etf",
        "last_price": latest_price,
        "return_1d": period_return(close, 1),
        "return_5d": period_return(close, 5),
        "return_1m": period_return(close, 21),
        "return_3m": period_return(close, 63),
        "return_6m": period_return(close, 126),
        "return_1y": period_return(close, 252),
        "volatility_annual": annualized_volatility(returns),
        "downside_volatility": downside_volatility(returns),
        "max_drawdown": max_drawdown(close),
        "sharpe_like": sharpe_like(returns),
        "roc_20": period_return(close, 20),
    }

    for window in (20, 50, 100, 200):
        sma = close.rolling(window).mean()
        signals[f"sma_{window}"] = last_value(sma)
        signals[f"price_vs_sma_{window}"] = pct_diff(latest_price, last_value(sma))

    for window in (20, 50):
        ema = close.ewm(span=window, adjust=False).mean()
        signals[f"ema_{window}"] = last_value(ema)
        signals[f"price_vs_ema_{window}"] = pct_diff(latest_price, last_value(ema))

    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    signals["golden_cross"] = bool(last_value(sma50) > last_value(sma200)) if valid(last_value(sma50), last_value(sma200)) else False
    signals["death_cross"] = bool(last_value(sma50) < last_value(sma200)) if valid(last_value(sma50), last_value(sma200)) else False

    signals["rsi_14"] = last_value(ta_rsi(close, 14))
    macd_line, macd_signal, macd_hist = ta_macd(close)
    signals["macd"] = last_value(macd_line)
    signals["macd_signal"] = last_value(macd_signal)
    signals["macd_hist"] = last_value(macd_hist)
    signals["macd_bullish"] = bool(last_value(macd_line) > last_value(macd_signal)) if valid(last_value(macd_line), last_value(macd_signal)) else False

    stoch_k, stoch_d = ta_stochastic(close, high, low)
    signals["stoch_k"] = last_value(stoch_k)
    signals["stoch_d"] = last_value(stoch_d)

    signals["avg_volume_20d"] = last_value(volume.rolling(20).mean())
    signals["volume_spike"] = safe_div(last_value(volume), signals["avg_volume_20d"])

    if fundamentals:
        signals.update(extract_fundamentals(fundamentals))

    return normalize_nan(signals)


def entity_name(ticker: str, info: dict[str, Any]) -> str:
    for key in ("longName", "shortName", "displayName", "name"):
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if ticker.endswith("-USD"):
        return ticker.replace("-USD", "")
    return ticker


def yahoo_finance_url(ticker: str) -> str:
    return f"https://finance.yahoo.com/quote/{ticker}"


def google_finance_url(ticker: str, info: dict[str, Any]) -> str:
    if ticker.endswith("-USD"):
        return f"https://www.google.com/finance/quote/{ticker}"
    exchange = google_exchange_code(info.get("exchange") or info.get("fullExchangeName"))
    return f"https://www.google.com/finance/quote/{ticker}:{exchange}" if exchange else f"https://www.google.com/search?q={ticker}+stock+chart"


def google_exchange_code(exchange: Any) -> str:
    text = str(exchange or "").upper()
    if "NASDAQ" in text or text in {"NMS", "NGM", "NCM"}:
        return "NASDAQ"
    if "NYSE" in text or text in {"NYQ", "NYS"}:
        return "NYSE"
    if "AMEX" in text or "NYSE AMERICAN" in text or text == "ASE":
        return "NYSEAMERICAN"
    return "NASDAQ"


def tradingview_url(ticker: str) -> str:
    if ticker.endswith("-USD"):
        symbol = ticker.replace("-USD", "USD")
        return f"https://www.tradingview.com/symbols/{symbol}/"
    return f"https://www.tradingview.com/symbols/{ticker}/"


def extract_fundamentals(info: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "market_cap": "marketCap",
        "trailing_pe": "trailingPE",
        "forward_pe": "forwardPE",
        "peg_ratio": "pegRatio",
        "price_to_book": "priceToBook",
        "revenue_growth": "revenueGrowth",
        "earnings_growth": "earningsGrowth",
        "profit_margin": "profitMargins",
        "debt_to_equity": "debtToEquity",
        "free_cash_flow": "freeCashflow",
        "dividend_yield": "dividendYield",
        "analyst_recommendation": "recommendationKey",
        "analyst_mean_rating": "recommendationMean",
    }
    return {out_key: clean_scalar(info.get(in_key)) for out_key, in_key in keys.items()}


def period_return(close: pd.Series, days: int) -> float:
    if len(close) <= days:
        return math.nan
    return pct_diff(close.iloc[-1], close.iloc[-days - 1])


def annualized_volatility(returns: pd.Series) -> float:
    value = returns.dropna().std() * math.sqrt(TRADING_DAYS)
    return float(value) if pd.notna(value) else math.nan


def downside_volatility(returns: pd.Series) -> float:
    downside = returns[returns < 0].dropna()
    if downside.empty:
        return math.nan
    return float(downside.std() * math.sqrt(TRADING_DAYS))


def max_drawdown(close: pd.Series) -> float:
    running_max = close.cummax()
    drawdowns = close / running_max - 1
    return float(drawdowns.min()) if not drawdowns.empty else math.nan


def sharpe_like(returns: pd.Series) -> float:
    daily = returns.dropna()
    if daily.empty or daily.std() == 0:
        return math.nan
    return float((daily.mean() / daily.std()) * math.sqrt(TRADING_DAYS))


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def ta_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    if RSIIndicator is not None:
        try:
            return RSIIndicator(close=close, window=window).rsi()
        except Exception:
            pass
    return rsi(close, window)


def macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    line = fast - slow
    signal = line.ewm(span=9, adjust=False).mean()
    hist = line - signal
    return line, signal, hist


def ta_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    if MACD is not None:
        try:
            indicator = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
            return indicator.macd(), indicator.macd_signal(), indicator.macd_diff()
        except Exception:
            pass
    return macd(close)


def stochastic(close: pd.Series, high: pd.Series, low: pd.Series, window: int = 14) -> tuple[pd.Series, pd.Series]:
    lowest_low = low.rolling(window).min()
    highest_high = high.rolling(window).max()
    k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    d = k.rolling(3).mean()
    return k, d


def ta_stochastic(close: pd.Series, high: pd.Series, low: pd.Series, window: int = 14) -> tuple[pd.Series, pd.Series]:
    if StochasticOscillator is not None:
        try:
            indicator = StochasticOscillator(high=high, low=low, close=close, window=window, smooth_window=3)
            return indicator.stoch(), indicator.stoch_signal()
        except Exception:
            pass
    return stochastic(close, high, low, window)


def last_value(series: pd.Series) -> float:
    clean = series.dropna()
    if clean.empty:
        return math.nan
    return clean.iloc[-1].item() if hasattr(clean.iloc[-1], "item") else float(clean.iloc[-1])


def pct_diff(a: float, b: float) -> float:
    if not valid(a, b) or b == 0:
        return math.nan
    return float((a / b) - 1)


def safe_div(a: float, b: float) -> float:
    if not valid(a, b) or b == 0:
        return math.nan
    return float(a / b)


def valid(*values: float) -> bool:
    return all(v is not None and pd.notna(v) and np.isfinite(v) for v in values)


def clean_scalar(value: Any) -> Any:
    if value is None:
        return math.nan
    if isinstance(value, (int, float, str, bool)):
        return value
    try:
        return float(value)
    except Exception:
        return math.nan


def normalize_nan(signals: dict[str, Any]) -> dict[str, Any]:
    normalized = {}
    for key, value in signals.items():
        if isinstance(value, (np.floating, np.integer)):
            value = value.item()
        normalized[key] = value
    return normalized

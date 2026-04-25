from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def score_universe(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame

    scored = frame.copy()
    components = scored.apply(score_row, axis=1, result_type="expand")
    scored = pd.concat([scored, components], axis=1)
    scored["score"] = scored[
        ["trend_score", "momentum_score", "risk_penalty", "valuation_score", "quality_score"]
    ].sum(axis=1)
    scored["score"] = scored["score"].clip(-100, 100).round(2)
    scored["recommendation"] = scored["score"].apply(recommendation)
    return scored.sort_values("score", ascending=False)


def score_row(row: pd.Series) -> dict[str, float]:
    trend = trend_score(row)
    momentum = momentum_score(row)
    risk = risk_penalty(row)
    valuation = valuation_score(row)
    quality = quality_score(row)
    return {
        "trend_score": round(trend, 2),
        "momentum_score": round(momentum, 2),
        "risk_penalty": round(risk, 2),
        "valuation_score": round(valuation, 2),
        "quality_score": round(quality, 2),
    }


def trend_score(row: pd.Series) -> float:
    score = 0.0
    for col, weight in (("price_vs_sma_50", 10), ("price_vs_sma_200", 14), ("price_vs_ema_20", 6)):
        value = num(row.get(col))
        if value is not None:
            score += np.clip(value / 0.10, -1, 1) * weight
    if row.get("golden_cross") is True:
        score += 8
    if row.get("death_cross") is True:
        score -= 8
    value = num(row.get("return_6m"))
    if value is not None:
        score += np.clip(value / 0.30, -1, 1) * 7
    return float(np.clip(score, -35, 35))


def momentum_score(row: pd.Series) -> float:
    score = 0.0
    rsi = num(row.get("rsi_14"))
    if rsi is not None:
        if 45 <= rsi <= 65:
            score += 8
        elif 30 <= rsi < 45:
            score += 3
        elif 65 < rsi <= 72:
            score += 2
        elif rsi > 72:
            score -= min(18, (rsi - 72) * 0.9 + 6)
        elif rsi < 30:
            score -= min(12, (30 - rsi) * 0.5 + 3)

    for col, weight, scale in (("return_1m", 6, 0.10), ("return_3m", 7, 0.20), ("roc_20", 5, 0.10)):
        value = num(row.get(col))
        if value is not None:
            score += np.clip(value / scale, -1, 1) * weight

    if row.get("macd_bullish") is True:
        score += 5
    else:
        score -= 3

    stoch_k = num(row.get("stoch_k"))
    if stoch_k is not None and stoch_k > 90:
        score -= 4
    return float(np.clip(score, -30, 30))


def risk_penalty(row: pd.Series) -> float:
    penalty = 0.0
    vol = num(row.get("volatility_annual"))
    if vol is not None:
        if vol > 0.75:
            penalty -= 20
        elif vol > 0.45:
            penalty -= 12
        elif vol > 0.30:
            penalty -= 6
        elif vol < 0.18:
            penalty += 3

    drawdown = num(row.get("max_drawdown"))
    if drawdown is not None:
        if drawdown < -0.60:
            penalty -= 20
        elif drawdown < -0.40:
            penalty -= 12
        elif drawdown < -0.25:
            penalty -= 6

    sharpe = num(row.get("sharpe_like"))
    if sharpe is not None:
        penalty += np.clip(sharpe / 2, -1, 1) * 7
    return float(np.clip(penalty, -30, 10))


def valuation_score(row: pd.Series) -> float:
    if row.get("asset_type") == "crypto":
        return 0.0

    score = 0.0
    pe = first_num(row.get("forward_pe"), row.get("trailing_pe"))
    if pe is not None:
        if 0 < pe <= 18:
            score += 10
        elif pe <= 30:
            score += 4
        elif pe <= 50:
            score -= 4
        else:
            score -= 10

    peg = num(row.get("peg_ratio"))
    if peg is not None:
        if 0 < peg <= 1.2:
            score += 7
        elif peg <= 2.5:
            score += 2
        elif peg > 3:
            score -= 6

    price_to_book = num(row.get("price_to_book"))
    if price_to_book is not None and price_to_book > 12:
        score -= 4
    return float(np.clip(score, -18, 18))


def quality_score(row: pd.Series) -> float:
    if row.get("asset_type") == "crypto":
        return 0.0

    score = 0.0
    for col, weight in (("revenue_growth", 5), ("earnings_growth", 6), ("profit_margin", 6)):
        value = num(row.get(col))
        if value is not None:
            score += np.clip(value / 0.25, -1, 1) * weight

    debt = num(row.get("debt_to_equity"))
    if debt is not None:
        if debt < 80:
            score += 3
        elif debt > 200:
            score -= 6

    fcf = num(row.get("free_cash_flow"))
    if fcf is not None:
        score += 4 if fcf > 0 else -4

    recommendation = str(row.get("analyst_recommendation") or "").lower()
    if recommendation in {"strong_buy", "buy"}:
        score += 4
    elif recommendation in {"sell", "strong_sell"}:
        score -= 5
    return float(np.clip(score, -17, 17))


def recommendation(score: float) -> str:
    if score >= 60:
        return "Strong Buy"
    if score >= 30:
        return "Buy"
    if score <= -60:
        return "Strong Sell"
    if score <= -30:
        return "Sell"
    return "Hold"


def num(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def first_num(*values: Any) -> float | None:
    for value in values:
        numeric = num(value)
        if numeric is not None:
            return numeric
    return None

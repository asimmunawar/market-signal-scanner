from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd


def write_outputs(scored: pd.DataFrame, output_dir: str | Path) -> Path:
    out = Path(output_dir) / "scans" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    scored.to_csv(out / "ranked_signals.csv", index=False)
    scored[scored["recommendation"].isin(["Strong Buy", "Buy"])].head(20).to_csv(
        out / "top_buy_candidates.csv", index=False
    )
    scored[scored["recommendation"].isin(["Strong Sell", "Sell"])].sort_values("score").head(20).to_csv(
        out / "top_sell_candidates.csv", index=False
    )
    (out / "portfolio_report.md").write_text(build_report(scored), encoding="utf-8")
    return out


def build_report(scored: pd.DataFrame) -> str:
    if scored.empty:
        generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z%z")
        return f"# Portfolio Scanner Report\n\nGenerated: {generated}\n\nNo valid ticker data was available.\n\nThis is not financial advice.\n"

    buys = scored[scored["recommendation"].isin(["Strong Buy", "Buy"])].head(20)
    sells = scored[scored["recommendation"].isin(["Strong Sell", "Sell"])].sort_values("score").head(20)
    hold_count = int((scored["recommendation"] == "Hold").sum())

    lines = [
        "# Portfolio Scanner Report",
        "",
        f"Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z%z')}",
        "",
        "This report provides analytical market signals only. It is not financial advice, and it does not guarantee future returns.",
        "",
        "## Executive Summary",
        "",
        f"- Universe analyzed: {len(scored)} tickers",
        f"- Buy-rated assets: {len(buys)}",
        f"- Sell/avoid-rated assets: {len(sells)}",
        f"- Hold-rated assets: {hold_count}",
        f"- Median score: {scored['score'].median():.2f}",
        "",
        "## Market Regime Summary",
        "",
        market_regime_summary(scored),
        "",
        "## Top Buy Candidates",
        "",
        table_for_report(buys),
        "",
        "## Top Sell/Avoid Candidates",
        "",
        table_for_report(sells.sort_values("score")),
        "",
        "## Risk Warnings",
        "",
        "- High scores can reverse quickly during volatility shocks, earnings surprises, macro events, liquidity stress, or crypto market dislocations.",
        "- Missing fundamentals are left blank and are not fabricated. Crypto assets skip equity-style fundamentals.",
        "- Scores combine backward-looking technicals and available fundamentals; they are not forecasts.",
        "",
        "## Per-Ticker Explanations",
        "",
    ]

    focus = pd.concat([buys.head(10), sells.sort_values("score").head(10)]).drop_duplicates(subset=["ticker"])
    for _, row in focus.iterrows():
        lines.extend(explain_row(row))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def market_regime_summary(scored: pd.DataFrame) -> str:
    above_200 = scored["price_vs_sma_200"].dropna().gt(0).mean()
    median_vol = scored["volatility_annual"].dropna().median()
    median_drawdown = scored["max_drawdown"].dropna().median()
    breadth = "constructive" if above_200 >= 0.6 else "mixed" if above_200 >= 0.4 else "weak"
    vol_text = "low" if median_vol < 0.20 else "moderate" if median_vol < 0.35 else "elevated"
    return (
        f"Market breadth looks {breadth}: {above_200:.0%} of analyzed assets are above their 200-day SMA. "
        f"Median annualized volatility is {vol_text} at {median_vol:.1%}, and median max drawdown is {median_drawdown:.1%}."
    )


def table_for_report(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "No tickers met this bucket."
    cols = ["ticker", "entity_name", "score", "recommendation", "return_3m", "rsi_14", "volatility_annual", "max_drawdown"]
    cols = [col for col in cols if col in frame.columns]
    table = frame[cols].copy()
    for col in ["return_3m", "volatility_annual", "max_drawdown"]:
        table[col] = table[col].map(lambda x: "" if pd.isna(x) else f"{x:.1%}")
    table["rsi_14"] = table["rsi_14"].map(lambda x: "" if pd.isna(x) else f"{x:.1f}")
    return markdown_table(table)


def markdown_table(frame: pd.DataFrame) -> str:
    headers = [str(col) for col in frame.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for _, row in frame.iterrows():
        values = [str(row[col]) for col in frame.columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def explain_row(row: pd.Series) -> list[str]:
    positives, negatives = signal_lists(row)
    reasoning = plain_reasoning(row, positives, negatives)
    return [
        f"### {row['ticker']} - {row.get('entity_name', row['ticker'])}",
        "",
        f"- Score: {row['score']:.2f}",
        f"- Recommendation: {row['recommendation']}",
        f"- Strongest positive signals: {', '.join(positives) if positives else 'None prominent'}",
        f"- Strongest negative signals: {', '.join(negatives) if negatives else 'None prominent'}",
        f"- Reasoning: {reasoning}",
    ]


def signal_lists(row: pd.Series) -> tuple[list[str], list[str]]:
    positives: list[str] = []
    negatives: list[str] = []

    if positive(row, "price_vs_sma_200"):
        positives.append("price above 200-day trend")
    if positive(row, "price_vs_sma_50"):
        positives.append("price above 50-day trend")
    if row.get("golden_cross") is True:
        positives.append("golden cross")
    if positive(row, "return_3m"):
        positives.append("positive 3-month momentum")
    if positive(row, "sharpe_like"):
        positives.append("favorable risk-adjusted return")
    if positive(row, "revenue_growth"):
        positives.append("revenue growth")
    if positive(row, "profit_margin"):
        positives.append("positive profit margin")

    if negative(row, "price_vs_sma_200"):
        negatives.append("price below 200-day trend")
    if row.get("death_cross") is True:
        negatives.append("death cross")
    if negative(row, "return_3m"):
        negatives.append("negative 3-month momentum")
    if value(row, "rsi_14") and value(row, "rsi_14") > 72:
        negatives.append("overbought RSI")
    if value(row, "volatility_annual") and value(row, "volatility_annual") > 0.45:
        negatives.append("high volatility")
    if value(row, "max_drawdown") and value(row, "max_drawdown") < -0.35:
        negatives.append("severe drawdown")
    if value(row, "forward_pe") and value(row, "forward_pe") > 50:
        negatives.append("expensive forward P/E")

    return positives[:5], negatives[:5]


def plain_reasoning(row: pd.Series, positives: list[str], negatives: list[str]) -> str:
    if row["score"] >= 30:
        return "The asset ranks well because positive trend, momentum, quality, or valuation inputs outweigh the risk penalties currently observed."
    if row["score"] <= -30:
        return "The asset ranks poorly because weak trend, poor momentum, valuation pressure, or risk penalties dominate the available positive inputs."
    if positives and negatives:
        return "The asset has offsetting evidence, so the model keeps it in a neutral range until the signal balance improves."
    return "The available data does not create a strong directional edge in this scoring model."


def value(row: pd.Series, column: str) -> float | None:
    raw = row.get(column)
    if pd.isna(raw):
        return None
    return float(raw)


def positive(row: pd.Series, column: str) -> bool:
    val = value(row, column)
    return val is not None and val > 0


def negative(row: pd.Series, column: str) -> bool:
    val = value(row, column)
    return val is not None and val < 0

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from tqdm import tqdm

from market_signal_scanner.config_loader import BacktestConfig
from market_signal_scanner.indicators import compute_signals
from market_signal_scanner.scorer import score_universe


LOGGER = logging.getLogger(__name__)
MIN_HISTORY_BARS = 220


@dataclass
class BacktestResult:
    output_dir: Path
    summary: dict[str, Any]
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    holdings: pd.DataFrame
    rebalance_scores: pd.DataFrame


def run_backtest(prices: dict[str, pd.DataFrame], config: BacktestConfig, output_base: str | Path) -> BacktestResult:
    output_dir = Path(output_base) / "backtests" / datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    close = build_close_matrix(prices)
    close = filter_backtest_window(close, config)
    if close.empty:
        raise ValueError("No price data available in the requested backtest window")

    rebalance_dates = scheduled_dates(close.index, config.rebalance_frequency)
    contribution_dates = set(scheduled_dates(close.index, config.contribution_frequency))
    LOGGER.info("Backtest window has %d price dates and %d rebalance dates", len(close.index), len(rebalance_dates))

    cash = float(config.initial_cash)
    total_contributions = float(config.initial_cash)
    shares: dict[str, float] = {}
    latest_targets = pd.DataFrame()

    equity_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []

    rebalance_set = set(rebalance_dates)
    cost_rate = config.transaction_cost_bps / 10000
    slippage_rate = config.slippage_bps / 10000

    for date in tqdm(close.index, desc="backtest"):
        prices_today = close.loc[date]

        if date in contribution_dates and date != close.index[0] and config.contribution_amount > 0:
            cash += config.contribution_amount
            total_contributions += config.contribution_amount

        if date in rebalance_set:
            scored = score_as_of(date, prices, config)
            if not scored.empty:
                scored = scored.copy()
                scored.insert(0, "date", date)
                score_rows.extend(scored.to_dict("records"))
                latest_targets = select_targets(scored, config)
            else:
                latest_targets = pd.DataFrame()
            cash, trade_batch = rebalance_portfolio(
                date=date,
                cash=cash,
                shares=shares,
                prices_today=prices_today,
                targets=latest_targets,
                cost_rate=cost_rate,
                slippage_rate=slippage_rate,
            )
            trade_rows.extend(trade_batch)

        portfolio_value = portfolio_market_value(cash, shares, prices_today)
        equity_rows.append(
            {
                "date": date,
                "portfolio_value": portfolio_value,
                "cash": cash,
                "invested_value": portfolio_value - cash,
                "total_contributions": total_contributions,
                "net_profit": portfolio_value - total_contributions,
                "positions": len([ticker for ticker, qty in shares.items() if qty > 0]),
            }
        )
        holding_rows.extend(snapshot_holdings(date, shares, prices_today, portfolio_value, latest_targets))

    equity_curve = pd.DataFrame(equity_rows)
    trades = pd.DataFrame(trade_rows)
    holdings = pd.DataFrame(holding_rows)
    rebalance_scores = pd.DataFrame(score_rows)
    benchmark = benchmark_curve(close, config, equity_curve)
    if not benchmark.empty:
        equity_curve = equity_curve.merge(benchmark, on="date", how="left")

    summary = summarize_backtest(equity_curve, trades, config)
    write_backtest_outputs(output_dir, summary, equity_curve, trades, holdings, rebalance_scores, config)
    return BacktestResult(output_dir, summary, equity_curve, trades, holdings, rebalance_scores)


def build_close_matrix(prices: dict[str, pd.DataFrame]) -> pd.DataFrame:
    series = {}
    for ticker, frame in prices.items():
        if "Close" not in frame.columns:
            continue
        close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
        close.index = pd.to_datetime(close.index).tz_localize(None)
        series[ticker] = close
    matrix = pd.DataFrame(series).sort_index().ffill()
    return matrix.dropna(how="all")


def filter_backtest_window(close: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    start = pd.Timestamp(config.start_date)
    end = pd.Timestamp(config.end_date) if config.end_date else close.index.max()
    return close.loc[(close.index >= start) & (close.index <= end)].copy()


def scheduled_dates(index: pd.DatetimeIndex, frequency: str) -> list[pd.Timestamp]:
    frequency = frequency.lower()
    if frequency in {"daily", "day", "d"}:
        return list(index)
    rule = "W" if frequency in {"weekly", "week", "w"} else "M"
    frame = pd.DataFrame(index=index)
    frame["date"] = index
    return list(frame.groupby(index.to_period(rule))["date"].first())


def score_as_of(date: pd.Timestamp, prices: dict[str, pd.DataFrame], config: BacktestConfig) -> pd.DataFrame:
    rows = []
    for ticker, frame in prices.items():
        hist = frame.copy()
        hist.index = pd.to_datetime(hist.index).tz_localize(None)
        hist = hist.loc[hist.index <= date]
        if len(hist.dropna(subset=["Close"])) < MIN_HISTORY_BARS:
            continue
        try:
            rows.append(compute_signals(ticker, hist, {}))
        except Exception as exc:
            LOGGER.debug("Signal failure for %s on %s: %s", ticker, date.date(), exc)
    if not rows:
        return pd.DataFrame()
    return score_universe(pd.DataFrame(rows))


def select_targets(scored: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    candidates = scored[scored["score"] >= config.min_score_to_buy].copy()
    if candidates.empty:
        return candidates
    return candidates.sort_values("score", ascending=False).head(config.max_positions)


def rebalance_portfolio(
    date: pd.Timestamp,
    cash: float,
    shares: dict[str, float],
    prices_today: pd.Series,
    targets: pd.DataFrame,
    cost_rate: float,
    slippage_rate: float,
) -> tuple[float, list[dict[str, Any]]]:
    trades: list[dict[str, Any]] = []
    target_tickers = set(targets["ticker"]) if not targets.empty else set()

    for ticker, qty in list(shares.items()):
        price = safe_price(prices_today, ticker)
        if qty <= 0 or price is None:
            continue
        if ticker not in target_tickers:
            exec_price = price * (1 - slippage_rate)
            notional = qty * exec_price
            cost = notional * cost_rate
            cash += notional - cost
            shares[ticker] = 0.0
            trades.append(trade_row(date, ticker, "SELL", qty, exec_price, notional, cost, "removed_from_targets"))

    portfolio_value = portfolio_market_value(cash, shares, prices_today)
    if not target_tickers:
        return cash, trades

    target_value = portfolio_value / len(target_tickers)
    target_scores = targets.set_index("ticker")["score"].to_dict()

    for ticker in sorted(target_tickers):
        price = safe_price(prices_today, ticker)
        if price is None:
            continue
        current_qty = shares.get(ticker, 0.0)
        current_value = current_qty * price
        delta_value = target_value - current_value
        if abs(delta_value) < 1:
            continue
        if delta_value < 0 and current_qty > 0:
            exec_price = price * (1 - slippage_rate)
            qty = min(current_qty, abs(delta_value) / exec_price)
            notional = qty * exec_price
            cost = notional * cost_rate
            cash += notional - cost
            shares[ticker] = current_qty - qty
            trades.append(trade_row(date, ticker, "SELL", qty, exec_price, notional, cost, f"rebalance_score_{target_scores.get(ticker):.2f}"))
        elif delta_value > 0 and cash > 0:
            exec_price = price * (1 + slippage_rate)
            spend = min(delta_value, cash / (1 + cost_rate))
            if spend <= 0:
                continue
            cost = spend * cost_rate
            qty = spend / exec_price
            cash -= spend + cost
            shares[ticker] = current_qty + qty
            trades.append(trade_row(date, ticker, "BUY", qty, exec_price, spend, cost, f"target_score_{target_scores.get(ticker):.2f}"))

    return cash, trades


def portfolio_market_value(cash: float, shares: dict[str, float], prices_today: pd.Series) -> float:
    value = cash
    for ticker, qty in shares.items():
        price = safe_price(prices_today, ticker)
        if qty > 0 and price is not None:
            value += qty * price
    return float(value)


def safe_price(prices_today: pd.Series, ticker: str) -> float | None:
    if ticker not in prices_today.index:
        return None
    value = prices_today[ticker]
    if pd.isna(value) or value <= 0:
        return None
    return float(value)


def trade_row(date: pd.Timestamp, ticker: str, action: str, shares: float, price: float, notional: float, cost: float, reason: str) -> dict[str, Any]:
    return {
        "date": date,
        "ticker": ticker,
        "action": action,
        "shares": shares,
        "price": price,
        "notional": notional,
        "transaction_cost": cost,
        "reason": reason,
    }


def snapshot_holdings(date: pd.Timestamp, shares: dict[str, float], prices_today: pd.Series, portfolio_value: float, targets: pd.DataFrame) -> list[dict[str, Any]]:
    target_scores = targets.set_index("ticker")["score"].to_dict() if not targets.empty else {}
    rows = []
    for ticker, qty in shares.items():
        price = safe_price(prices_today, ticker)
        if qty <= 0 or price is None:
            continue
        market_value = qty * price
        rows.append(
            {
                "date": date,
                "ticker": ticker,
                "shares": qty,
                "price": price,
                "market_value": market_value,
                "weight": market_value / portfolio_value if portfolio_value else np.nan,
                "latest_score": target_scores.get(ticker, np.nan),
            }
        )
    return rows


def benchmark_curve(close: pd.DataFrame, config: BacktestConfig, equity_curve: pd.DataFrame) -> pd.DataFrame:
    if config.benchmark not in close.columns:
        return pd.DataFrame()

    dates = pd.to_datetime(equity_curve["date"])
    prices = close[config.benchmark].reindex(dates).ffill()
    if prices.dropna().empty:
        return pd.DataFrame()

    shares = 0.0
    cash = 0.0
    previous_contributions = 0.0
    rows = []
    for _, row in equity_curve.iterrows():
        date = pd.Timestamp(row["date"])
        price = prices.loc[date]
        if pd.isna(price) or price <= 0:
            rows.append({"date": date, "benchmark_value": np.nan})
            continue
        contribution = float(row["total_contributions"]) - previous_contributions
        previous_contributions = float(row["total_contributions"])
        cash += max(0.0, contribution)
        if cash > 0:
            shares += cash / float(price)
            cash = 0.0
        rows.append({"date": date, "benchmark_value": shares * float(price) + cash})
    result = pd.DataFrame(rows)
    result["benchmark_net_profit"] = result["benchmark_value"] - equity_curve["total_contributions"].values
    return result


def summarize_backtest(equity_curve: pd.DataFrame, trades: pd.DataFrame, config: BacktestConfig) -> dict[str, Any]:
    start_value = float(equity_curve["portfolio_value"].iloc[0])
    end_value = float(equity_curve["portfolio_value"].iloc[-1])
    total_contributions = float(equity_curve["total_contributions"].iloc[-1])
    returns = equity_curve["portfolio_value"].pct_change().dropna()
    days = max(1, (pd.Timestamp(equity_curve["date"].iloc[-1]) - pd.Timestamp(equity_curve["date"].iloc[0])).days)
    years = days / 365.25
    total_return = end_value / start_value - 1 if start_value else np.nan
    cagr = (end_value / start_value) ** (1 / years) - 1 if start_value > 0 and years > 0 else np.nan
    running_max = equity_curve["portfolio_value"].cummax()
    drawdown = equity_curve["portfolio_value"] / running_max - 1
    sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if len(returns) > 2 and returns.std() else np.nan
    summary = {
        "start_date": equity_curve["date"].iloc[0],
        "end_date": equity_curve["date"].iloc[-1],
        "initial_cash": config.initial_cash,
        "total_contributions": total_contributions,
        "final_value": end_value,
        "net_profit": end_value - total_contributions,
        "total_return_on_initial_cash": total_return,
        "return_on_contributed_capital": end_value / total_contributions - 1 if total_contributions else np.nan,
        "cagr_on_initial_cash": cagr,
        "max_drawdown": float(drawdown.min()),
        "annualized_volatility": float(returns.std() * np.sqrt(252)) if len(returns) > 2 else np.nan,
        "sharpe_like": float(sharpe) if pd.notna(sharpe) else np.nan,
        "trade_count": int(len(trades)),
        "buy_count": int((trades["action"] == "BUY").sum()) if not trades.empty else 0,
        "sell_count": int((trades["action"] == "SELL").sum()) if not trades.empty else 0,
        "rebalance_frequency": config.rebalance_frequency,
        "contribution_frequency": config.contribution_frequency,
        "max_positions": config.max_positions,
    }
    if "benchmark_value" in equity_curve.columns and equity_curve["benchmark_value"].notna().any():
        bench_end = equity_curve["benchmark_value"].dropna().iloc[-1]
        summary["benchmark"] = config.benchmark
        summary["benchmark_return_on_contributed_capital"] = bench_end / total_contributions - 1 if total_contributions else np.nan
        summary["excess_return_vs_benchmark"] = summary["return_on_contributed_capital"] - summary["benchmark_return_on_contributed_capital"]
    return summary


def write_backtest_outputs(
    output_dir: Path,
    summary: dict[str, Any],
    equity_curve: pd.DataFrame,
    trades: pd.DataFrame,
    holdings: pd.DataFrame,
    rebalance_scores: pd.DataFrame,
    config: BacktestConfig,
) -> None:
    pd.DataFrame([summary]).to_csv(output_dir / "backtest_summary.csv", index=False)
    equity_curve.to_csv(output_dir / "backtest_equity_curve.csv", index=False)
    trades.to_csv(output_dir / "backtest_trades.csv", index=False)
    holdings.to_csv(output_dir / "backtest_holdings.csv", index=False)
    rebalance_scores.to_csv(output_dir / "backtest_rebalance_scores.csv", index=False)
    (output_dir / "backtest_report.md").write_text(build_backtest_report(summary, trades, config), encoding="utf-8")


def build_backtest_report(summary: dict[str, Any], trades: pd.DataFrame, config: BacktestConfig) -> str:
    lines = [
        "# Backtest Report",
        "",
        "This report is a historical simulation of scanner rules. It is not financial advice and does not guarantee future returns.",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {format_value(value)}")
    lines.extend(
        [
            "",
            "## Assumptions",
            "",
            f"- Rebalance frequency: {config.rebalance_frequency}",
            f"- Contribution frequency: {config.contribution_frequency}",
            f"- Contribution amount: {config.contribution_amount}",
            f"- Maximum positions: {config.max_positions}",
            f"- Minimum score to buy: {config.min_score_to_buy}",
            f"- Transaction cost: {config.transaction_cost_bps} bps",
            f"- Slippage: {config.slippage_bps} bps",
            "- Fundamentals are excluded to avoid look-ahead bias from current fundamental data.",
            "",
            "## Largest Trades",
            "",
        ]
    )
    if trades.empty:
        lines.append("No trades were generated.")
    else:
        largest = trades.sort_values("notional", ascending=False).head(20).copy()
        lines.append(markdown_table(largest[["date", "ticker", "action", "shares", "price", "notional", "transaction_cost", "reason"]]))
    return "\n".join(lines).rstrip() + "\n"


def format_value(value: Any) -> str:
    if isinstance(value, float):
        if abs(value) < 5:
            return f"{value:.2%}"
        return f"{value:,.2f}"
    return str(value)


def markdown_table(frame: pd.DataFrame) -> str:
    table = frame.copy()
    for column in table.columns:
        table[column] = table[column].map(format_cell)
    headers = [str(col) for col in table.columns]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for _, row in table.iterrows():
        lines.append("| " + " | ".join(str(row[col]) for col in table.columns) + " |")
    return "\n".join(lines)


def format_cell(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        return f"{value:,.4f}"
    return str(value)

from __future__ import annotations

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from market_signal_scanner.agent_researcher import run_agent_research
from market_signal_scanner.backtester import run_backtest
from market_signal_scanner.charting import ChartOptions, generate_chart_report
from market_signal_scanner.config_loader import load_config, resolve_ticker_universe
from market_signal_scanner.data_fetcher import Cache, fetch_fundamentals, fetch_price_history
from market_signal_scanner.indicators import compute_signals
from market_signal_scanner.news_summary import run_news_summary
from market_signal_scanner.trend_catcher import run_trend_catcher
from market_signal_scanner.reporter import write_outputs
from market_signal_scanner.scorer import score_universe


LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan markets and backtest scanner rules.")
    parser.add_argument("command", nargs="?", choices=["scan", "backtest", "chart", "news", "agent", "trend-catcher", "oracle"], default="scan", help="Run a current scan, historical backtest, ticker chart, news summary, research agent, or Trend Catcher market scan.")
    parser.add_argument("--config", default="config/config.yaml", help="Path to YAML configuration file.")
    parser.add_argument("--output", default="./output", help="Base output directory.")
    parser.add_argument("--skip-fundamentals", action="store_true", help="Skip fundamentals for this run.")
    parser.add_argument("--fast-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--ticker", help="Ticker to chart, summarize, or research, for example AAPL or BTC-USD.")
    parser.add_argument("--query", default="", help="Natural-language question for agent mode.")
    parser.add_argument("--period", help="Chart/download period override, for example 6mo, 2y, 5y.")
    parser.add_argument("--interval", help="Chart/download interval override, for example 1d, 1h, 1wk.")
    parser.add_argument("--chart-type", default="candle", choices=["candle", "line"], help="Chart style for price panel.")
    parser.add_argument("--lookback", type=int, default=180, help="Number of most recent bars to display.")
    parser.add_argument("--ma", default="20,50,100,200", help="Comma-separated SMA windows to overlay.")
    parser.add_argument("--no-support-resistance", action="store_true", help="Hide support/resistance pivot levels.")
    parser.add_argument("--no-bollinger", action="store_true", help="Hide Bollinger bands.")
    parser.add_argument("--no-volume", action="store_true", help="Hide volume panel.")
    parser.add_argument("--no-rsi", action="store_true", help="Hide RSI panel.")
    parser.add_argument("--no-macd", action="store_true", help="Hide MACD panel.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    if args.command == "backtest":
        return run_backtest_command(config, args)
    if args.command == "chart":
        return run_chart_command(config, args)
    if args.command == "news":
        return run_news_command(config, args)
    if args.command == "agent":
        return run_agent_command(config, args)
    if args.command in {"trend-catcher", "oracle"}:
        return run_trend_catcher_command(config, args)
    return run_scan_command(config, args)


def run_scan_command(config, args: argparse.Namespace) -> int:
    tickers = resolve_ticker_universe(config)
    LOGGER.info("Resolved %d unique tickers", len(tickers))
    if not tickers:
        LOGGER.error("No tickers found in config or enabled groups")
        return 2

    cache = Cache(config.runtime.cache_dir)
    prices = fetch_price_history(
        tickers,
        cache,
        config.runtime.refresh_prices_hours,
        period=config.runtime.price_period,
        interval=config.runtime.price_interval,
    )
    valid_tickers = [ticker for ticker in tickers if ticker in prices]
    LOGGER.info("Fetched usable price history for %d/%d tickers", len(valid_tickers), len(tickers))

    skip_fundamentals = args.skip_fundamentals or args.fast_mode or config.runtime.skip_fundamentals
    fundamentals = {}
    if skip_fundamentals:
        LOGGER.info("Skipping fundamentals")
    else:
        fundamentals = fetch_fundamentals(
            valid_tickers,
            cache,
            refresh_days=config.runtime.refresh_fundamentals_days,
            workers=config.runtime.workers,
        )

    rows = []
    with ThreadPoolExecutor(max_workers=config.runtime.workers) as executor:
        futures = {
            executor.submit(compute_signals, ticker, prices[ticker], fundamentals.get(ticker, {})): ticker
            for ticker in valid_tickers
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="signals"):
            ticker = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:
                LOGGER.warning("Skipping %s after signal failure: %s", ticker, exc)

    signals = pd.DataFrame(rows)
    if signals.empty:
        LOGGER.error("No signal rows were generated")
        return 3

    signals = apply_market_cap_filter(signals, config.limits.min_market_cap)
    scored = score_universe(signals)
    run_output_dir = write_outputs(scored, Path(args.output))

    LOGGER.info("Wrote scan outputs to %s", run_output_dir.resolve())
    return 0


def run_backtest_command(config, args: argparse.Namespace) -> int:
    tickers = resolve_ticker_universe(config)
    if config.backtest.benchmark and config.backtest.benchmark not in tickers:
        tickers.append(config.backtest.benchmark)
    LOGGER.info("Resolved %d unique tickers for backtest", len(tickers))
    if not tickers:
        LOGGER.error("No tickers found in config or enabled groups")
        return 2

    cache = Cache(config.runtime.cache_dir)
    prices = fetch_price_history(
        tickers,
        cache,
        config.runtime.refresh_prices_hours,
        period=config.backtest.price_period,
        interval=config.backtest.price_interval,
    )
    if not prices:
        LOGGER.error("No usable price history was fetched for backtest")
        return 3

    result = run_backtest(prices, config.backtest, args.output)
    LOGGER.info("Wrote backtest outputs to %s", result.output_dir.resolve())
    LOGGER.info("Final value: %.2f | Net profit: %.2f", result.summary["final_value"], result.summary["net_profit"])
    return 0


def run_chart_command(config, args: argparse.Namespace) -> int:
    if not args.ticker:
        LOGGER.error("--ticker is required for chart mode")
        return 2

    ticker = args.ticker.strip().upper()
    period = args.period or config.runtime.price_period
    interval = args.interval or config.runtime.price_interval
    cache = Cache(config.runtime.cache_dir)
    prices = fetch_price_history(
        [ticker],
        cache,
        config.runtime.refresh_prices_hours,
        period=period,
        interval=interval,
    )
    if ticker not in prices:
        LOGGER.error("No usable price history was fetched for %s", ticker)
        return 3

    options = ChartOptions(
        ticker=ticker,
        chart_type=args.chart_type,
        lookback=max(30, args.lookback),
        moving_averages=parse_moving_averages(args.ma),
        show_support_resistance=not args.no_support_resistance,
        show_bollinger=not args.no_bollinger,
        show_volume=not args.no_volume,
        show_rsi=not args.no_rsi,
        show_macd=not args.no_macd,
    )
    result = generate_chart_report(prices[ticker], options, args.output)
    LOGGER.info("Wrote chart outputs to %s", result.output_dir.resolve())
    LOGGER.info("Chart: %s", result.chart_path.resolve())
    LOGGER.info("Report: %s", result.report_path.resolve())
    return 0


def run_news_command(config, args: argparse.Namespace) -> int:
    if not args.ticker:
        LOGGER.error("--ticker is required for news mode")
        return 2
    result = run_news_summary(args.ticker, config, args.output)
    LOGGER.info("Wrote news summary outputs to %s", result.output_dir.resolve())
    LOGGER.info("Report: %s", result.report_path.resolve())
    LOGGER.info("Sources: %s", result.sources_path.resolve())
    return 0


def run_agent_command(config, args: argparse.Namespace) -> int:
    if not args.ticker and not args.query:
        LOGGER.error("--ticker or --query is required for agent mode")
        return 2

    def progress(kind: str, message: str) -> None:
        LOGGER.info("agent %s: %s", kind, message)

    result = run_agent_research(args.query, args.ticker or "", config, args.output, progress=progress)
    LOGGER.info("Wrote agent outputs to %s", result.output_dir.resolve())
    LOGGER.info("Report: %s", result.report_path.resolve())
    LOGGER.info("Sources: %s", result.evidence_path.resolve())
    return 0


def run_trend_catcher_command(config, args: argparse.Namespace) -> int:
    def progress(kind: str, message: str) -> None:
        LOGGER.info("trend-catcher %s: %s", kind, message)

    result = run_trend_catcher(config, args.output, progress=progress)
    LOGGER.info("Wrote Trend Catcher outputs to %s", result.output_dir.resolve())
    LOGGER.info("Report: %s", result.report_path.resolve())
    LOGGER.info("Sources: %s", result.sources_path.resolve())
    LOGGER.info("Log: %s", result.log_path.resolve())
    return 0


def parse_moving_averages(raw: str) -> tuple[int, ...]:
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            window = int(part)
        except ValueError:
            LOGGER.warning("Ignoring invalid moving average window: %s", part)
            continue
        if window > 1:
            values.append(window)
    return tuple(values) or (20, 50, 100, 200)


def apply_market_cap_filter(frame: pd.DataFrame, min_market_cap: int) -> pd.DataFrame:
    if min_market_cap <= 0 or "market_cap" not in frame.columns:
        return frame

    market_cap = pd.to_numeric(frame["market_cap"], errors="coerce")
    keep = market_cap.isna() | (market_cap >= min_market_cap) | (frame["asset_type"] == "crypto")
    removed = len(frame) - int(keep.sum())
    if removed:
        LOGGER.info("Filtered out %d tickers below min_market_cap=%d", removed, min_market_cap)
    return frame.loc[keep].copy()


if __name__ == "__main__":
    raise SystemExit(main())

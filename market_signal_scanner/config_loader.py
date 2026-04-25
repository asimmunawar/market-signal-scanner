from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


LOGGER = logging.getLogger(__name__)


DEFAULT_CRYPTO_TOP = [
    "BTC-USD",
    "ETH-USD",
    "BNB-USD",
    "SOL-USD",
    "XRP-USD",
    "ADA-USD",
    "DOGE-USD",
    "AVAX-USD",
    "DOT-USD",
    "LINK-USD",
]


@dataclass(frozen=True)
class LimitsConfig:
    max_tickers: int = 500
    min_market_cap: int = 0


@dataclass(frozen=True)
class RuntimeConfig:
    skip_fundamentals: bool = False
    workers: int = 5
    cache_dir: str = "./cache"
    price_interval: str = "1d"
    price_period: str = "2y"
    refresh_prices_hours: int = 12
    refresh_fundamentals_days: int = 7


@dataclass(frozen=True)
class BacktestConfig:
    enabled: bool = False
    start_date: str = "2020-01-01"
    end_date: str | None = None
    initial_cash: float = 10000.0
    contribution_amount: float = 0.0
    contribution_frequency: str = "monthly"
    rebalance_frequency: str = "weekly"
    max_positions: int = 10
    min_score_to_buy: float = 30.0
    sell_below_score: float = 0.0
    transaction_cost_bps: float = 5.0
    slippage_bps: float = 10.0
    benchmark: str = "SPY"
    price_interval: str = "1d"
    price_period: str = "max"


@dataclass(frozen=True)
class AgentConfig:
    provider: str = "ollama"
    model: str = "gpt-oss:120b"
    base_url: str = "http://127.0.0.1:11434"
    temperature: float = 0.2
    timeout_seconds: int = 180
    max_news_items: int = 12
    news_lookback_days: int = 21
    include_fundamentals: bool = True


@dataclass(frozen=True)
class ScannerConfig:
    tickers: list[str] = field(default_factory=list)
    groups: dict[str, bool] = field(default_factory=dict)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


def load_config(path: str | Path) -> ScannerConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    limits = raw.get("limits") or {}
    runtime = raw.get("runtime") or {}
    backtest = raw.get("backtest") or {}
    agent = raw.get("agent") or {}

    return ScannerConfig(
        tickers=[normalize_ticker(t) for t in raw.get("tickers", []) if t],
        groups={str(k): bool(v) for k, v in (raw.get("groups") or {}).items()},
        limits=LimitsConfig(
            max_tickers=max(1, int(limits.get("max_tickers", 500))),
            min_market_cap=max(0, int(limits.get("min_market_cap", 0))),
        ),
        runtime=RuntimeConfig(
            skip_fundamentals=bool(runtime.get("skip_fundamentals", runtime.get("fast_mode", False))),
            workers=max(1, int(runtime.get("workers", 5))),
            cache_dir=str(runtime.get("cache_dir", "./cache")),
            price_interval=str(runtime.get("price_interval", "1d")).strip(),
            price_period=str(runtime.get("price_period", "2y")).strip(),
            refresh_prices_hours=max(1, int(runtime.get("refresh_prices_hours", 12))),
            refresh_fundamentals_days=max(1, int(runtime.get("refresh_fundamentals_days", 7))),
        ),
        backtest=BacktestConfig(
            enabled=bool(backtest.get("enabled", False)),
            start_date=str(backtest.get("start_date", "2020-01-01")),
            end_date=backtest.get("end_date"),
            initial_cash=float(backtest.get("initial_cash", 10000)),
            contribution_amount=float(backtest.get("contribution_amount", 0)),
            contribution_frequency=str(backtest.get("contribution_frequency", "monthly")).strip().lower(),
            rebalance_frequency=str(backtest.get("rebalance_frequency", "weekly")).strip().lower(),
            max_positions=max(1, int(backtest.get("max_positions", 10))),
            min_score_to_buy=float(backtest.get("min_score_to_buy", 30)),
            sell_below_score=float(backtest.get("sell_below_score", 0)),
            transaction_cost_bps=max(0.0, float(backtest.get("transaction_cost_bps", 5))),
            slippage_bps=max(0.0, float(backtest.get("slippage_bps", 10))),
            benchmark=str(backtest.get("benchmark", "SPY")).strip().upper(),
            price_interval=str(backtest.get("price_interval", runtime.get("price_interval", "1d"))).strip(),
            price_period=str(backtest.get("price_period", "max")).strip(),
        ),
        agent=AgentConfig(
            provider=str(agent.get("provider", "ollama")).strip().lower(),
            model=str(agent.get("model", "gpt-oss:120b")).strip(),
            base_url=str(agent.get("base_url", "http://127.0.0.1:11434")).rstrip("/"),
            temperature=float(agent.get("temperature", 0.2)),
            timeout_seconds=max(10, int(agent.get("timeout_seconds", 180))),
            max_news_items=max(1, int(agent.get("max_news_items", 12))),
            news_lookback_days=max(1, int(agent.get("news_lookback_days", 21))),
            include_fundamentals=bool(agent.get("include_fundamentals", True)),
        ),
    )


def resolve_ticker_universe(config: ScannerConfig) -> list[str]:
    tickers = list(config.tickers)

    for group, enabled in config.groups.items():
        if not enabled:
            continue
        try:
            tickers.extend(fetch_group_tickers(group))
        except Exception as exc:
            LOGGER.warning("Could not expand group %s: %s", group, exc)

    deduped = deduplicate_tickers(tickers)
    limited = deduped[: config.limits.max_tickers]
    if len(deduped) > len(limited):
        LOGGER.info("Ticker universe truncated from %d to max_tickers=%d", len(deduped), len(limited))
    return limited


def fetch_group_tickers(group: str) -> list[str]:
    group_key = group.lower().strip()
    if group_key == "sp500":
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        return [normalize_ticker(t) for t in tables[0]["Symbol"].dropna().astype(str)]
    if group_key == "nasdaq100":
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        for table in tables:
            columns = {str(col).lower(): col for col in table.columns}
            symbol_col = next((col for name, col in columns.items() if "ticker" in name or "symbol" in name), None)
            if symbol_col is not None:
                return [normalize_ticker(t) for t in table[symbol_col].dropna().astype(str)]
        return []
    if group_key == "dow":
        tables = pd.read_html("https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average")
        for table in tables:
            if "Symbol" in table.columns:
                return [normalize_ticker(t) for t in table["Symbol"].dropna().astype(str)]
        return []
    if group_key == "crypto_top":
        return DEFAULT_CRYPTO_TOP
    LOGGER.warning("Unknown group %s; skipping", group)
    return []


def normalize_ticker(ticker: Any) -> str:
    return str(ticker).strip().upper().replace(".", "-")


def deduplicate_tickers(tickers: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ticker in tickers:
        normalized = normalize_ticker(ticker)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result

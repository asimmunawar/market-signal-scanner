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


DEFAULT_AGENT_SUGGESTED_QUESTIONS = [
    "Where should I invest $100 today for the long term, and why?",
    "What are the most attractive risk/reward opportunities in the market right now?",
    "What market risks should I pay attention to this week?",
    "Which sectors look strongest right now, and what is driving them?",
    "Which stocks or ETFs should I avoid right now, and why?",
    "What changed in markets today that could matter for long-term investors?",
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
class NewsSummaryConfig:
    provider: str = "ollama"
    model: str = "gpt-oss:120b"
    base_url: str = "http://127.0.0.1:11434"
    temperature: float = 0.2
    timeout_seconds: int = 180
    max_news_items: int = 12
    news_lookback_days: int = 21
    news_sources: dict[str, bool] = field(default_factory=lambda: {
        "yfinance_news": True,
        "yahoo_rss": True,
        "google_news": True,
    })
    include_fundamentals: bool = True


@dataclass(frozen=True)
class AgentConfig:
    provider: str = "ollama"
    model: str = "gpt-oss:120b"
    base_url: str = "http://127.0.0.1:11434"
    temperature: float = 0.2
    timeout_seconds: int = 240
    max_iterations: int = 4
    max_search_queries: int = 5
    search_results_per_query: int = 6
    pages_per_search: int = 3
    max_page_chars: int = 6000
    search_region: str = "us-en"
    include_market_data: bool = True
    suggested_questions: list[str] = field(default_factory=lambda: list(DEFAULT_AGENT_SUGGESTED_QUESTIONS))


@dataclass(frozen=True)
class OracleConfig:
    provider: str = "ollama"
    model: str = "gpt-oss:120b"
    base_url: str = "http://127.0.0.1:11434"
    temperature: float = 0.15
    timeout_seconds: int = 300
    max_iterations: int = 4
    max_search_queries: int = 8
    search_results_per_query: int = 6
    pages_per_search: int = 4
    max_page_chars: int = 7000
    search_region: str = "us-en"
    alert_threshold: int = 70
    source_lookback_hours: int = 48
    require_source_dates: bool = True
    pulse_enabled: bool = True
    pulse_use_baseline_tickers: bool = False
    pulse_include_config_tickers: bool = False
    pulse_tickers: list[str] = field(default_factory=lambda: [
        "SPY", "QQQ", "IWM", "DIA",
        "XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLP", "XLU", "XLB", "XLRE",
        "SMH", "ARKK", "TLT", "HYG", "GLD", "SLV", "USO", "UUP",
        "BTC-USD", "ETH-USD",
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    ])
    pulse_period: str = "5d"
    pulse_interval: str = "15m"
    pulse_min_abs_move_pct: float = 1.5
    pulse_min_volume_ratio: float = 1.8
    pulse_max_rows: int = 40


@dataclass(frozen=True)
class UIConfig:
    theme: str = "green"


@dataclass(frozen=True)
class ScannerConfig:
    tickers: list[str] = field(default_factory=list)
    groups: dict[str, bool] = field(default_factory=dict)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    news_summary: NewsSummaryConfig = field(default_factory=NewsSummaryConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    oracle: OracleConfig = field(default_factory=OracleConfig)
    ui: UIConfig = field(default_factory=UIConfig)


def load_config(path: str | Path) -> ScannerConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    limits = raw.get("limits") or {}
    runtime = raw.get("runtime") or {}
    backtest = raw.get("backtest") or {}
    news_summary = raw.get("news_summary") or {}
    news_sources = news_summary.get("news_sources") or news_summary.get("sources") or {}
    agent = raw.get("agent") or {}
    oracle = raw.get("oracle") or {}
    ui = raw.get("ui") or {}
    agent_suggested_questions = agent.get("suggested_questions", DEFAULT_AGENT_SUGGESTED_QUESTIONS)
    if not isinstance(agent_suggested_questions, list):
        agent_suggested_questions = DEFAULT_AGENT_SUGGESTED_QUESTIONS
    oracle_pulse_tickers = oracle.get("pulse_tickers")

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
        news_summary=NewsSummaryConfig(
            provider=str(news_summary.get("provider", "ollama")).strip().lower(),
            model=str(news_summary.get("model", "gpt-oss:120b")).strip(),
            base_url=str(news_summary.get("base_url", "http://127.0.0.1:11434")).rstrip("/"),
            temperature=float(news_summary.get("temperature", 0.2)),
            timeout_seconds=max(10, int(news_summary.get("timeout_seconds", 180))),
            max_news_items=max(1, int(news_summary.get("max_news_items", 12))),
            news_lookback_days=max(1, int(news_summary.get("news_lookback_days", 21))),
            news_sources={
                "yfinance_news": bool(news_sources.get("yfinance_news", True)),
                "yahoo_rss": bool(news_sources.get("yahoo_rss", True)),
                "google_news": bool(news_sources.get("google_news", True)),
            },
            include_fundamentals=bool(news_summary.get("include_fundamentals", True)),
        ),
        agent=AgentConfig(
            provider=str(agent.get("provider", news_summary.get("provider", "ollama"))).strip().lower(),
            model=str(agent.get("model", news_summary.get("model", "gpt-oss:120b"))).strip(),
            base_url=str(agent.get("base_url", news_summary.get("base_url", "http://127.0.0.1:11434"))).rstrip("/"),
            temperature=float(agent.get("temperature", news_summary.get("temperature", 0.2))),
            timeout_seconds=max(10, int(agent.get("timeout_seconds", 240))),
            max_iterations=max(1, int(agent.get("max_iterations", 4))),
            max_search_queries=max(1, int(agent.get("max_search_queries", 5))),
            search_results_per_query=max(1, int(agent.get("search_results_per_query", 6))),
            pages_per_search=max(0, int(agent.get("pages_per_search", 3))),
            max_page_chars=max(500, int(agent.get("max_page_chars", 6000))),
            search_region=str(agent.get("search_region", "us-en")).strip(),
            include_market_data=bool(agent.get("include_market_data", True)),
            suggested_questions=[
                str(question).strip()
                for question in agent_suggested_questions
                if str(question).strip()
            ],
        ),
        oracle=OracleConfig(
            provider=str(oracle.get("provider", agent.get("provider", news_summary.get("provider", "ollama")))).strip().lower(),
            model=str(oracle.get("model", agent.get("model", news_summary.get("model", "gpt-oss:120b")))).strip(),
            base_url=str(oracle.get("base_url", agent.get("base_url", news_summary.get("base_url", "http://127.0.0.1:11434")))).rstrip("/"),
            temperature=float(oracle.get("temperature", 0.15)),
            timeout_seconds=max(10, int(oracle.get("timeout_seconds", 300))),
            max_iterations=max(1, int(oracle.get("max_iterations", 4))),
            max_search_queries=max(1, int(oracle.get("max_search_queries", 8))),
            search_results_per_query=max(1, int(oracle.get("search_results_per_query", 6))),
            pages_per_search=max(0, int(oracle.get("pages_per_search", 4))),
            max_page_chars=max(500, int(oracle.get("max_page_chars", 7000))),
            search_region=str(oracle.get("search_region", "us-en")).strip(),
            alert_threshold=max(0, min(100, int(oracle.get("alert_threshold", 70)))),
            source_lookback_hours=max(1, int(oracle.get("source_lookback_hours", 48))),
            require_source_dates=bool(oracle.get("require_source_dates", True)),
            pulse_enabled=bool(oracle.get("pulse_enabled", True)),
            pulse_use_baseline_tickers=bool(oracle.get("pulse_use_baseline_tickers", False)),
            pulse_include_config_tickers=bool(oracle.get("pulse_include_config_tickers", False)),
            pulse_tickers=(
                [normalize_ticker(t) for t in oracle_pulse_tickers if t]
                if isinstance(oracle_pulse_tickers, list)
                else OracleConfig().pulse_tickers
            ),
            pulse_period=str(oracle.get("pulse_period", "5d")).strip(),
            pulse_interval=str(oracle.get("pulse_interval", "15m")).strip(),
            pulse_min_abs_move_pct=max(0.0, float(oracle.get("pulse_min_abs_move_pct", 1.5))),
            pulse_min_volume_ratio=max(0.0, float(oracle.get("pulse_min_volume_ratio", 1.8))),
            pulse_max_rows=max(1, int(oracle.get("pulse_max_rows", 40))),
        ),
        ui=UIConfig(
            theme=str(ui.get("theme", "green")).strip().lower(),
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

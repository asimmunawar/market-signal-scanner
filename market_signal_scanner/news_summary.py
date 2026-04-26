from __future__ import annotations

import csv
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

import pandas as pd
import requests
import yfinance as yf

from market_signal_scanner.config_loader import NewsSummaryConfig, ScannerConfig
from market_signal_scanner.data_fetcher import Cache, fetch_fundamentals, fetch_price_history, safe_name
from market_signal_scanner.indicators import compute_signals
from market_signal_scanner.scorer import score_universe


LOGGER = logging.getLogger(__name__)


@dataclass
class ResearchSource:
    title: str
    url: str
    publisher: str = ""
    published: str = ""
    summary: str = ""
    source_type: str = "news"


@dataclass
class NewsSummaryResult:
    output_dir: Path
    report_path: Path
    sources_path: Path
    context_path: Path


@dataclass
class NewsSummaryState:
    ticker: str
    config: ScannerConfig
    output_base: Path
    price_signals: dict[str, Any] = field(default_factory=dict)
    fundamentals: dict[str, Any] = field(default_factory=dict)
    entity_name: str = ""
    sources: list[ResearchSource] = field(default_factory=list)
    llm_report: str = ""
    llm_error: str = ""
    output_dir: Path | None = None
    report_path: Path | None = None
    sources_path: Path | None = None
    context_path: Path | None = None


class NewsSummaryPipeline:
    """Small deterministic news-summary pipeline."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, Callable[[NewsSummaryState], NewsSummaryState]]] = [
            ("market_data", collect_market_data),
            ("news_search", collect_news),
            ("llm_analysis", run_llm_analysis),
            ("report_writer", write_news_outputs),
        ]

    def run(self, state: NewsSummaryState) -> NewsSummaryState:
        for name, node in self.nodes:
            LOGGER.info("News summary step: %s", name)
            state = node(state)
        return state


def run_news_summary(ticker: str, config: ScannerConfig, output_base: str | Path) -> NewsSummaryResult:
    normalized = ticker.strip().upper()
    if not normalized:
        raise ValueError("ticker is required")
    state = NewsSummaryState(ticker=normalized, config=config, output_base=Path(output_base))
    completed = NewsSummaryPipeline().run(state)
    if not completed.output_dir or not completed.report_path or not completed.sources_path or not completed.context_path:
        raise RuntimeError("news summary did not produce all expected outputs")
    return NewsSummaryResult(
        output_dir=completed.output_dir,
        report_path=completed.report_path,
        sources_path=completed.sources_path,
        context_path=completed.context_path,
    )


def collect_market_data(state: NewsSummaryState) -> NewsSummaryState:
    cache = Cache(state.config.runtime.cache_dir)
    prices = fetch_price_history(
        [state.ticker],
        cache,
        state.config.runtime.refresh_prices_hours,
        period=state.config.runtime.price_period,
        interval=state.config.runtime.price_interval,
    )
    fundamentals: dict[str, Any] = {}
    if state.config.news_summary.include_fundamentals and not state.ticker.endswith("-USD"):
        fundamentals = fetch_fundamentals(
            [state.ticker],
            cache,
            refresh_days=state.config.runtime.refresh_fundamentals_days,
            workers=1,
        ).get(state.ticker, {})
    state.fundamentals = fundamentals
    state.entity_name = str(fundamentals.get("longName") or fundamentals.get("shortName") or state.ticker)

    if state.ticker in prices:
        scored = score_universe(pd.DataFrame([compute_signals(state.ticker, prices[state.ticker], fundamentals)]))
        if not scored.empty:
            state.price_signals = scored.iloc[0].to_dict()
    else:
        LOGGER.warning("No price history available for %s; continuing with news/fundamentals only", state.ticker)
    return state


def collect_news(state: NewsSummaryState) -> NewsSummaryState:
    sources: list[ResearchSource] = []
    enabled = state.config.news_summary.news_sources
    if enabled.get("yfinance_news", True):
        sources.extend(fetch_yfinance_news(state.ticker, state.config.news_summary.max_news_items))
    if enabled.get("yahoo_rss", True):
        sources.extend(fetch_yahoo_rss(state.ticker, state.config.news_summary.max_news_items))
    if enabled.get("google_news", True):
        sources.extend(fetch_google_news_rss(state.ticker, state.entity_name, state.config.news_summary))
    state.sources = dedupe_sources(sources)[: state.config.news_summary.max_news_items]
    return state


def fetch_yfinance_news(ticker: str, limit: int) -> list[ResearchSource]:
    try:
        items = yf.Ticker(ticker).news or []
    except Exception as exc:
        LOGGER.warning("Could not fetch yfinance news for %s: %s", ticker, exc)
        return []
    sources: list[ResearchSource] = []
    for item in items[:limit]:
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = str(content.get("title") or item.get("title") or "").strip()
        url = str(content.get("canonicalUrl", {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else content.get("link") or item.get("link") or "").strip()
        publisher = str(content.get("provider", {}).get("displayName") if isinstance(content.get("provider"), dict) else content.get("publisher") or "").strip()
        summary = strip_html(str(content.get("summary") or content.get("description") or ""))
        published = parse_yfinance_time(content.get("pubDate") or content.get("providerPublishTime") or item.get("providerPublishTime"))
        if title and url:
            sources.append(ResearchSource(title=title, url=url, publisher=publisher, published=published, summary=summary, source_type="yfinance_news"))
    return sources


def fetch_yahoo_rss(ticker: str, limit: int) -> list[ResearchSource]:
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(ticker)}&region=US&lang=en-US"
    return fetch_rss(url, "yahoo_rss", limit)


def fetch_google_news_rss(ticker: str, entity_name: str, news_config: NewsSummaryConfig) -> list[ResearchSource]:
    query_name = entity_name if entity_name and entity_name != ticker else ticker
    query = f'{query_name} {ticker} stock OR earnings OR analyst OR forecast when:{news_config.news_lookback_days}d'
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    return fetch_rss(url, "google_news", news_config.max_news_items)


def fetch_rss(url: str, source_type: str, limit: int) -> list[ResearchSource]:
    try:
        response = requests.get(url, timeout=15, headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"})
        response.raise_for_status()
    except Exception as exc:
        LOGGER.warning("RSS fetch failed for %s: %s", source_type, exc)
        return []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        LOGGER.warning("RSS parse failed for %s: %s", source_type, exc)
        return []
    sources: list[ResearchSource] = []
    for item in root.findall(".//item")[:limit]:
        title = text_at(item, "title")
        link = text_at(item, "link")
        publisher = text_at(item, "source")
        published = text_at(item, "pubDate")
        summary = strip_html(text_at(item, "description"))
        if title and link:
            sources.append(ResearchSource(title=title, url=link, publisher=publisher, published=published, summary=summary, source_type=source_type))
    return sources


def run_llm_analysis(state: NewsSummaryState) -> NewsSummaryState:
    prompt = build_prompt(state)
    if state.config.news_summary.provider != "ollama":
        state.llm_error = f"Unsupported provider configured: {state.config.news_summary.provider}"
        state.llm_report = fallback_analysis(state)
        return state
    try:
        state.llm_report = call_ollama(state.config.news_summary, prompt)
    except Exception as exc:
        LOGGER.warning("LLM analysis failed for %s: %s", state.ticker, exc)
        state.llm_error = str(exc)
        state.llm_report = fallback_analysis(state)
    return state


def call_ollama(news_config: NewsSummaryConfig, prompt: str) -> str:
    payload = {
        "model": news_config.model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": news_config.temperature},
    }
    response = requests.post(
        f"{news_config.base_url}/api/generate",
        json=payload,
        timeout=news_config.timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    text = str(data.get("response") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response")
    return text


def build_prompt(state: NewsSummaryState) -> str:
    signals = compact_signals(state.price_signals)
    fundamentals = compact_fundamentals(state.fundamentals)
    sources = "\n".join(
        f"{index}. {source.title} | {source.publisher or 'unknown'} | {source.published or 'undated'} | {source.url}\n   {source.summary[:350]}"
        for index, source in enumerate(state.sources, start=1)
    ) or "No recent news sources were found."
    return f"""You are a cautious financial news summarizer inside market-signal-scanner.

Ticker: {state.ticker}
Entity name: {state.entity_name or state.ticker}

Technical/scoring signals:
{json.dumps(signals, indent=2, default=str)}

Fundamental snapshot:
{json.dumps(fundamentals, indent=2, default=str)}

Recent source list:
{sources}

Write a source-grounded research memo in Markdown with these sections:
1. Verdict
2. Buy Case
3. Sell / Avoid Case
4. Short-Term Outlook
5. Long-Term Outlook
6. Catalysts To Watch
7. Key Risks
8. Source Notes

Rules:
- Do not fabricate facts, numbers, news, ratings, or price targets.
- Treat predictions as scenarios with uncertainty, not guarantees.
- Mention if the available sources are thin, stale, or inconclusive.
- Tie claims back to the provided technical signals, fundamentals, or news titles.
- Include a final disclaimer that this is analytical research, not financial advice.
"""


def compact_signals(signals: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "ticker", "entity_name", "close", "score", "recommendation", "trend_score",
        "momentum_score", "risk_penalty", "valuation_score", "quality_score",
        "return_5d", "return_1m", "return_3m", "return_6m", "return_1y",
        "volatility", "max_drawdown", "rsi_14", "macd", "macd_signal",
        "price_vs_sma_50", "price_vs_sma_200", "volume_spike",
    ]
    return {key: signals.get(key) for key in keys if key in signals and pd.notna(signals.get(key))}


def compact_fundamentals(fundamentals: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "longName", "sector", "industry", "marketCap", "trailingPE", "forwardPE",
        "pegRatio", "priceToBook", "revenueGrowth", "earningsGrowth",
        "profitMargins", "debtToEquity", "freeCashflow", "dividendYield",
        "recommendationKey", "targetMeanPrice",
    ]
    return {key: fundamentals.get(key) for key in keys if fundamentals.get(key) is not None}


def fallback_analysis(state: NewsSummaryState) -> str:
    signals = compact_signals(state.price_signals)
    score = signals.get("score", "N/A")
    recommendation = signals.get("recommendation", "Unknown")
    positives, negatives = signal_bullets(signals)
    source_note = "Recent news was found." if state.sources else "No recent news sources were found from the configured free feeds."
    llm_note = f"\n\nLLM note: {state.llm_error}" if state.llm_error else ""
    return f"""## Verdict

{state.ticker} currently shows a scanner recommendation of **{recommendation}** with score **{score}** based on available technical/fundamental signals. {source_note}

## Buy Case

{format_bullets(positives) or "- No strong positive signals were available in the fetched data."}

## Sell / Avoid Case

{format_bullets(negatives) or "- No strong negative signals were available in the fetched data."}

## Short-Term Outlook

Short-term direction should be treated as uncertain. Use the latest trend, RSI, MACD, volatility, and news catalysts above as scenario inputs rather than a forecast.

## Long-Term Outlook

Long-term attractiveness depends on whether fundamentals, competitive position, growth, valuation, and risk remain supportive. The app did not fabricate missing long-term estimates.

## Catalysts To Watch

- Earnings, guidance, analyst revisions, macro data, sector news, and unusually large price/volume moves.

## Key Risks

- Free data can be delayed, missing, or rate-limited.
- News feeds may miss important filings or paywalled reporting.
- Historical signals and current summaries do not guarantee future returns.

## Source Notes

{format_source_notes(state.sources)}

This is analytical research, not financial advice.{llm_note}
"""


def signal_bullets(signals: dict[str, Any]) -> tuple[list[str], list[str]]:
    positives: list[str] = []
    negatives: list[str] = []
    score = to_float(signals.get("score"))
    rsi_value = to_float(signals.get("rsi_14"))
    drawdown = to_float(signals.get("max_drawdown"))
    volatility = to_float(signals.get("volatility"))
    price_vs_sma_200 = to_float(signals.get("price_vs_sma_200"))
    return_3m = to_float(signals.get("return_3m"))
    if score is not None and score >= 30:
        positives.append(f"Scanner score is constructive at {score:.1f}.")
    if score is not None and score <= -30:
        negatives.append(f"Scanner score is weak at {score:.1f}.")
    if price_vs_sma_200 is not None and price_vs_sma_200 > 0:
        positives.append(f"Price is above its 200-bar SMA by {price_vs_sma_200:.1%}.")
    if price_vs_sma_200 is not None and price_vs_sma_200 < 0:
        negatives.append(f"Price is below its 200-bar SMA by {abs(price_vs_sma_200):.1%}.")
    if return_3m is not None and return_3m > 0:
        positives.append(f"Three-month return is positive at {return_3m:.1%}.")
    if return_3m is not None and return_3m < 0:
        negatives.append(f"Three-month return is negative at {return_3m:.1%}.")
    if rsi_value is not None and rsi_value >= 70:
        negatives.append(f"RSI is overbought at {rsi_value:.1f}.")
    if drawdown is not None and drawdown <= -0.25:
        negatives.append(f"Max drawdown is severe at {drawdown:.1%}.")
    if volatility is not None and volatility >= 0.6:
        negatives.append(f"Annualized volatility is elevated at {volatility:.1%}.")
    return positives, negatives


def write_news_outputs(state: NewsSummaryState) -> NewsSummaryState:
    output_dir = state.output_base / "news" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{safe_name(state.ticker)}"
    output_dir.mkdir(parents=True, exist_ok=True)
    sources_path = output_dir / f"{safe_name(state.ticker)}_sources.csv"
    context_path = output_dir / f"{safe_name(state.ticker)}_news_context.json"
    report_path = output_dir / "news_summary.md"

    with sources_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["title", "publisher", "published", "source_type", "url", "summary"])
        writer.writeheader()
        for source in state.sources:
            writer.writerow(source.__dict__)

    context = {
        "ticker": state.ticker,
        "entity_name": state.entity_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "news_summary_config": {
            "provider": state.config.news_summary.provider,
            "model": state.config.news_summary.model,
            "base_url": state.config.news_summary.base_url,
            "max_news_items": state.config.news_summary.max_news_items,
            "news_lookback_days": state.config.news_summary.news_lookback_days,
            "news_sources": state.config.news_summary.news_sources,
        },
        "signals": compact_signals(state.price_signals),
        "fundamentals": compact_fundamentals(state.fundamentals),
        "llm_error": state.llm_error,
        "sources": [source.__dict__ for source in state.sources],
    }
    context_path.write_text(json.dumps(context, indent=2, default=str), encoding="utf-8")
    report_path.write_text(build_full_report(state), encoding="utf-8")
    state.output_dir = output_dir
    state.sources_path = sources_path
    state.context_path = context_path
    state.report_path = report_path
    return state


def build_full_report(state: NewsSummaryState) -> str:
    source_count = len(state.sources)
    llm_status = "completed" if not state.llm_error else f"fallback used: {state.llm_error}"
    return f"""# News Summary: {state.ticker}

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Entity: {state.entity_name or state.ticker}

Flow: market data -> configured news sources -> LLM summary -> report writer

LLM: `{state.config.news_summary.provider}` / `{state.config.news_summary.model}` ({llm_status})

Sources reviewed: {source_count}

{state.llm_report.strip()}

## Source Links

{format_source_notes(state.sources)}
"""


def format_source_notes(sources: list[ResearchSource]) -> str:
    if not sources:
        return "- No source links captured."
    return "\n".join(
        f"- [{source.title}]({source.url})"
        + (f" - {source.publisher}" if source.publisher else "")
        + (f" ({source.published})" if source.published else "")
        for source in sources
    )


def format_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def dedupe_sources(sources: list[ResearchSource]) -> list[ResearchSource]:
    deduped: list[ResearchSource] = []
    seen: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    for source in sources:
        key = normalize_url(source.url) or source.title.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if source.published:
            parsed = parse_datetime(source.published)
            if parsed and parsed < cutoff:
                continue
        deduped.append(source)
    return deduped


def text_at(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    return child.text.strip() if child is not None and child.text else ""


def strip_html(value: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", value or "")
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def parse_yfinance_time(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")
    return str(value)


def parse_datetime(value: str) -> datetime | None:
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize_url(url: str) -> str:
    return url.split("?")[0].strip().lower()


def to_float(value: Any) -> float | None:
    try:
        result = float(value)
        return result if pd.notna(result) else None
    except (TypeError, ValueError):
        return None

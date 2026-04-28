from __future__ import annotations

import csv
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, TypedDict
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from market_signal_scanner.config_loader import AgentConfig, ScannerConfig
from market_signal_scanner.data_fetcher import Cache, fetch_fundamentals, fetch_price_history, safe_name
from market_signal_scanner.indicators import compute_signals
from market_signal_scanner.news_summary import compact_fundamentals, compact_signals
from market_signal_scanner.prompt_loader import load_prompt
from market_signal_scanner.scorer import score_universe


LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[str, str], None]
HTTP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) market-signal-scanner/1.0",
}


@dataclass
class AgentEvidence:
    title: str
    url: str
    snippet: str = ""
    query: str = ""
    content: str = ""
    summary: str = ""
    source_type: str = "duckduckgo"
    published_at: str = ""
    fetched_at: str = ""
    freshness_status: str = "unknown"


@dataclass
class AgentResult:
    output_dir: Path
    report_path: Path
    evidence_path: Path
    context_path: Path
    log_path: Path
    log_json_path: Path
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PageFetchResult:
    text: str = ""
    reason: str = ""
    status_code: int | None = None
    content_type: str = ""
    published_at: str = ""


class AgentState(TypedDict, total=False):
    query: str
    ticker: str
    entity_name: str
    config: ScannerConfig
    output_base: Path
    searches: list[str]
    search_index: int
    evidence: list[AgentEvidence]
    signals: dict[str, Any]
    fundamentals: dict[str, Any]
    report: str
    llm_error: str
    events: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]


class LinkTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = {key: value or "" for key, value in attrs}
        href = attr_map.get("href", "")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            title = clean_text(" ".join(self._text))
            if title:
                self.links.append((title, self._href))
            self._href = ""
            self._text = []


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            text = clean_text(data)
            if text:
                self.parts.append(text)


class MetadataExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.meta: list[str] = []
        self.json_ld: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self._json_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            name = (attr_map.get("name") or attr_map.get("property") or "").lower()
            if name in {"description", "og:title", "og:description", "twitter:title", "twitter:description", "article:published_time", "author"}:
                value = clean_text(attr_map.get("content", ""))
                if value:
                    self.meta.append(value)
            return
        if tag == "script" and attr_map.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._json_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            raw = "".join(self._json_parts).strip()
            if raw:
                self.json_ld.append(raw)
            self._json_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_json_ld:
            self._json_parts.append(data)


class AgentRunner:
    def __init__(self, progress: ProgressCallback | None = None) -> None:
        self.external_progress = progress or (lambda _kind, _message: None)
        self.events: list[dict[str, Any]] = []

    def progress(self, kind: str, message: str, **details: Any) -> None:
        event = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "message": message,
            "details": details,
        }
        self.events.append(event)
        self.external_progress(kind, message)

    def run(self, query: str, ticker: str, config: ScannerConfig, output_base: str | Path) -> AgentResult:
        state: AgentState = {
            "query": query.strip() or ticker.strip(),
            "ticker": ticker.strip().upper(),
            "config": config,
            "output_base": Path(output_base),
            "search_index": 0,
            "searches": [],
            "evidence": [],
            "signals": {},
            "fundamentals": {},
            "llm_error": "",
            "events": self.events,
            "llm_calls": [],
        }
        final_state = self._run_graph(state)
        final_state["events"] = self.events
        return write_agent_outputs(final_state)

    def _run_graph(self, state: AgentState) -> AgentState:
        try:
            from langgraph.graph import END, StateGraph
        except Exception as exc:
            LOGGER.warning("LangGraph unavailable; using sequential ReAct runner: %s", exc)
            return self._run_sequential(state)

        graph = StateGraph(AgentState)
        graph.add_node("observe_market", self.observe_market)
        graph.add_node("plan", self.plan)
        graph.add_node("search", self.search)
        graph.add_node("read", self.read)
        graph.add_node("synthesize", self.synthesize)
        graph.set_entry_point("observe_market")
        graph.add_edge("observe_market", "plan")
        graph.add_edge("plan", "search")
        graph.add_edge("search", "read")
        graph.add_conditional_edges("read", self.should_continue, {"continue": "search", "finish": "synthesize"})
        graph.add_edge("synthesize", END)
        self.progress("thought", "Built LangGraph ReAct workflow: observe -> plan -> search/read loop -> synthesize.")
        return graph.compile().invoke(state)

    def _run_sequential(self, state: AgentState) -> AgentState:
        state = self.observe_market(state)
        state = self.plan(state)
        while self.should_continue(state) == "continue":
            state = self.search(state)
            state = self.read(state)
        state = self.synthesize(state)
        return state

    def observe_market(self, state: AgentState) -> AgentState:
        config = state["config"]
        ticker = state.get("ticker", "")
        self.progress("thought", "Collecting local market data and fundamentals for context.")
        if not ticker or not config.agent.include_market_data:
            state["entity_name"] = ticker
            return state

        cache = Cache(config.runtime.cache_dir)
        fundamentals: dict[str, Any] = {}
        if not ticker.endswith("-USD"):
            fundamentals = fetch_fundamentals([ticker], cache, config.runtime.refresh_fundamentals_days, workers=1).get(ticker, {})
        state["fundamentals"] = fundamentals
        state["entity_name"] = str(fundamentals.get("longName") or fundamentals.get("shortName") or ticker)

        prices = fetch_price_history(
            [ticker],
            cache,
            config.runtime.refresh_prices_hours,
            period=config.runtime.price_period,
            interval=config.runtime.price_interval,
        )
        if ticker in prices:
            scored = score_universe(pd.DataFrame([compute_signals(ticker, prices[ticker], fundamentals)]))
            if not scored.empty:
                state["signals"] = scored.iloc[0].to_dict()
        self.progress("observation", f"Market context ready for {state.get('entity_name') or ticker}.")
        return state

    def plan(self, state: AgentState) -> AgentState:
        config = state["config"]
        prompt = load_prompt("agent_planner.md").format(
            query=state["query"],
            ticker=state.get("ticker") or "not provided",
            entity_name=state.get("entity_name") or state.get("ticker") or "unknown",
            current_date=current_datetime_text(),
            max_queries=config.agent.max_search_queries,
        )
        self.progress("thought", "Asking the LLM to plan current-news searches.")
        planned = default_search_queries(state)
        try:
            response = call_logged_ollama(config.agent, prompt, state, "agent_planner")
            parsed = json.loads(extract_json_object(response))
            queries = [str(item).strip() for item in parsed.get("queries", []) if str(item).strip()]
            if queries:
                planned = queries[: config.agent.max_search_queries]
            if parsed.get("reasoning"):
                self.progress("thought", str(parsed["reasoning"]), planned_queries=planned)
        except Exception as exc:
            state["llm_error"] = str(exc)
            self.progress("observation", f"Planner fallback used: {exc}", fallback_queries=planned)
        state["searches"] = planned
        self.progress("action", f"Planned {len(planned)} search queries.", queries=planned)
        return state

    def search(self, state: AgentState) -> AgentState:
        config = state["config"]
        index = int(state.get("search_index", 0))
        searches = state.get("searches", [])
        if index >= len(searches):
            return state
        query = searches[index]
        self.progress("action", f"Searching web/news sources: {query}", query=query)
        results, note = search_web(
            query,
            config.agent.search_results_per_query,
            config.agent.search_region,
            ticker=state.get("ticker", ""),
        )
        evidence = state.get("evidence", [])
        evidence.extend(results)
        state["evidence"] = dedupe_evidence(evidence)
        state["search_index"] = index + 1
        detail = f" {note}" if note else ""
        self.progress(
            "observation",
            f"Found {len(results)} source link(s).{detail}{format_found_links(results)}",
            query=query,
            search_note=note,
            results=[source_event_payload(item) for item in results],
        )
        return state

    def read(self, state: AgentState) -> AgentState:
        config = state["config"]
        evidence = state.get("evidence", [])
        unread = [item for item in evidence if not item.content][: config.agent.pages_per_search]
        if not unread:
            return state
        for item in unread:
            self.progress("action", f"Fetching source page: {item.title[:90]}", title=item.title, url=item.url)
            fetched = fetch_page_text(item.url, config.agent.max_page_chars)
            item.content = fetched.text
            item.fetched_at = current_datetime_text()
            if fetched.published_at and not item.published_at:
                item.published_at = fetched.published_at
            mark_evidence_freshness(item, max_age_hours=24 * 30, require_source_date=False)
            if item.content:
                source_note = f" ({fetched.reason})" if fetched.reason else ""
                self.progress(
                    "observation",
                    f"Captured {len(item.content):,} characters from source{source_note}. Summarizing for the research question.",
                    title=item.title,
                    url=item.url,
                    fetch_result=fetched.__dict__,
                    captured_chars=len(item.content),
                )
                item.summary = summarize_source(item, state)
                self.progress("observation", f"Source summary ready: {short_preview(item.summary, 260)}", title=item.title, url=item.url, summary=item.summary)
            else:
                reason = fetched.reason or "no readable text was extracted"
                self.progress("observation", f"Source page could not be parsed: {reason}. Keeping search snippet.", title=item.title, url=item.url, fetch_result=fetched.__dict__)
        return state

    def should_continue(self, state: AgentState) -> str:
        config = state["config"]
        index = int(state.get("search_index", 0))
        enough_sources = len([item for item in state.get("evidence", []) if item.content or item.snippet]) >= config.agent.max_search_queries
        if index >= min(len(state.get("searches", [])), config.agent.max_iterations):
            return "finish"
        return "finish" if enough_sources and index >= 2 else "continue"

    def synthesize(self, state: AgentState) -> AgentState:
        config = state["config"]
        if not has_agent_evidence(state.get("evidence", [])):
            state["llm_error"] = "No external source evidence was fetched; skipped LLM synthesis to avoid unsourced conclusions."
            self.progress("observation", state["llm_error"])
            state["report"] = insufficient_agent_report(state)
            self.progress("observation", "Agent research report completed with insufficient source evidence.", report_chars=len(state.get("report", "")))
            return state
        evidence_text = format_evidence(state.get("evidence", []))
        prompt = load_prompt("agent_synthesis.md").format(
            query=state["query"],
            ticker=state.get("ticker") or "not provided",
            entity_name=state.get("entity_name") or state.get("ticker") or "unknown",
            current_date=current_datetime_text(),
            signals_json=json.dumps(compact_signals(state.get("signals", {})), indent=2, default=str),
            fundamentals_json=json.dumps(compact_fundamentals(state.get("fundamentals", {})), indent=2, default=str),
            evidence_text=evidence_text,
            title=state.get("ticker") or state["query"],
        )
        self.progress("thought", "Synthesizing evidence into a comprehensive investment memo.", evidence_count=len(state.get("evidence", [])))
        try:
            state["report"] = call_logged_ollama(config.agent, prompt, state, "agent_synthesis")
        except Exception as exc:
            state["llm_error"] = str(exc)
            self.progress("observation", f"Synthesis fallback used: {exc}", error=str(exc))
            state["report"] = fallback_report(state)
        self.progress("observation", "Agent research report completed.", report_chars=len(state.get("report", "")))
        return state


def run_agent_research(
    query: str,
    ticker: str,
    config: ScannerConfig,
    output_base: str | Path,
    progress: ProgressCallback | None = None,
) -> AgentResult:
    return AgentRunner(progress=progress).run(query=query, ticker=ticker, config=config, output_base=output_base)


def answer_followup(question: str, context: dict[str, Any], config: ScannerConfig) -> str:
    evidence = [AgentEvidence(**item) for item in context.get("evidence", [])]
    if not has_agent_evidence(evidence):
        return (
            "I do not have fetched external source evidence for this session, so I cannot answer that with a "
            "source-grounded market view. Re-run the Agent when internet/search sources are available, or provide "
            "specific source text to analyze. I will not use the local model's prior knowledge to invent an answer."
        )
    prompt = load_prompt("agent_followup.md").format(
        current_date=current_datetime_text(),
        query=context.get("query", ""),
        ticker=context.get("ticker", ""),
        entity_name=context.get("entity_name", ""),
        report=context.get("report", ""),
        evidence_text=format_evidence(evidence),
        chat_history=format_chat_history(context.get("chat", [])),
        question=question,
    )
    try:
        return call_logged_ollama(config.agent, prompt, context, "agent_followup")
    except Exception as exc:
        return f"Could not reach the configured LLM for this follow-up: `{exc}`.\n\nThe gathered report and sources are still available in the agent output folder."


def call_ollama(agent_config: AgentConfig, prompt: str) -> str:
    if agent_config.provider != "ollama":
        raise RuntimeError(f"Unsupported agent provider configured: {agent_config.provider}")
    payload = {
        "model": agent_config.model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": agent_config.temperature},
    }
    last_empty = False
    for attempt in range(2):
        response = requests.post(
            f"{agent_config.base_url}/api/generate",
            json=payload,
            timeout=agent_config.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        text = str(data.get("response") or "").strip()
        if text:
            return text
        last_empty = True
        time.sleep(0.8 + attempt)
    if last_empty:
        raise RuntimeError("Ollama returned an empty response after retry")
    raise RuntimeError("Ollama returned an empty response")


def call_logged_ollama(agent_config: Any, prompt: str, state: dict[str, Any], call_name: str) -> str:
    record = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "name": call_name,
        "provider": agent_config.provider,
        "model": agent_config.model,
        "base_url": agent_config.base_url,
        "prompt": prompt,
        "response": "",
        "error": "",
    }
    try:
        response = call_ollama(agent_config, prompt)
        record["response"] = response
        return response
    except Exception as exc:
        record["error"] = str(exc)
        raise
    finally:
        state.setdefault("llm_calls", []).append(record)


def summarize_source(item: AgentEvidence, state: AgentState) -> str:
    config = state["config"]
    prompt = load_prompt("agent_source_summary.md").format(
        query=state.get("query", ""),
        ticker=state.get("ticker", ""),
        entity_name=state.get("entity_name") or state.get("ticker") or "",
        current_date=current_datetime_text(),
        title=item.title,
        url=item.url,
        published_at=item.published_at or "unknown",
        fetched_at=item.fetched_at or "unknown",
        freshness_status=item.freshness_status or "unknown",
        page_text=item.content[: config.agent.max_page_chars],
    )
    try:
        return call_logged_ollama(config.agent, prompt, state, "agent_source_summary")
    except Exception as exc:
        LOGGER.warning("Source summary failed for %s: %s", item.url, exc)
        return extractive_source_summary(item.content)


def default_search_queries(state: AgentState) -> list[str]:
    ticker = state.get("ticker", "")
    entity = state.get("entity_name") or ticker
    base = f"{entity} {ticker}".strip()
    request = state.get("query", "")
    return [
        f"{base} latest stock news earnings guidance analyst sentiment",
        f"{base} stock valuation risks catalysts macro sector outlook",
        f"{base} geopolitical supply chain regulation market perception",
        f"{request} finance stock market news",
    ]


def search_web(query: str, limit: int, region: str, ticker: str = "", recent_days: int | None = None) -> tuple[list[AgentEvidence], str]:
    duck_results, duck_error = search_duckduckgo(query, limit, region)
    if duck_results:
        return duck_results, "Source: DuckDuckGo HTML."

    rss_results = search_news_rss(query, limit, recent_days=recent_days)
    if rss_results:
        note = "DuckDuckGo returned no usable links; used Google News RSS fallback."
        if duck_error:
            note = f"DuckDuckGo failed: {duck_error}; used Google News RSS fallback."
        return rss_results, note

    ticker_results = search_yfinance_news(ticker, limit) if ticker else []
    if ticker_results:
        note = "DuckDuckGo and RSS returned no usable links; used yfinance ticker news fallback."
        if duck_error:
            note = f"DuckDuckGo failed: {duck_error}; RSS returned no links; used yfinance ticker news fallback."
        return ticker_results, note

    if duck_error:
        return [], f"DuckDuckGo failed: {duck_error}; RSS and yfinance fallbacks also returned no links."
    return [], "DuckDuckGo, RSS, and yfinance fallbacks returned no usable links."


def search_duckduckgo(query: str, limit: int, region: str) -> tuple[list[AgentEvidence], str]:
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}&kl={quote_plus(region)}"
    try:
        response = requests.get(url, timeout=20, headers=HTTP_HEADERS)
        response.raise_for_status()
    except Exception as exc:
        LOGGER.warning("DuckDuckGo search failed for %s: %s", query, exc)
        return [], short_error(exc)
    parser = LinkTextParser()
    parser.feed(response.text)
    results: list[AgentEvidence] = []
    for title, href in parser.links:
        clean_url = normalize_duckduckgo_url(href)
        if not clean_url or "duckduckgo.com" in urlparse(clean_url).netloc:
            continue
        if title.lower() in {"cached", "similar", "more"}:
            continue
        results.append(AgentEvidence(title=title, url=clean_url, query=query, source_type="duckduckgo"))
        if len(results) >= limit:
            break
    return results, ""


def search_news_rss(query: str, limit: int, recent_days: int | None = None) -> list[AgentEvidence]:
    search_query = f"{query} when:{recent_days}d" if recent_days else query
    url = f"https://news.google.com/rss/search?q={quote_plus(search_query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        response = requests.get(url, timeout=20, headers={"Accept": "application/rss+xml, application/xml, text/xml, */*", "User-Agent": HTTP_HEADERS["User-Agent"]})
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as exc:
        LOGGER.warning("Google News RSS fallback failed for %s: %s", query, exc)
        return []
    results: list[AgentEvidence] = []
    for item in root.findall(".//item")[:limit]:
        title = text_at(item, "title")
        link = text_at(item, "link")
        summary = clean_text(text_at(item, "description"))
        published_at = normalize_datetime_text(text_at(item, "pubDate"))
        if title and link:
            results.append(AgentEvidence(title=title, url=link, query=query, snippet=summary, source_type="google_news_rss", published_at=published_at))
    return results


def search_yfinance_news(ticker: str, limit: int) -> list[AgentEvidence]:
    try:
        items = yf.Ticker(ticker).news or []
    except Exception as exc:
        LOGGER.warning("yfinance news fallback failed for %s: %s", ticker, exc)
        return []
    results: list[AgentEvidence] = []
    for item in items[:limit]:
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = str(content.get("title") or item.get("title") or "").strip()
        url = str(
            content.get("canonicalUrl", {}).get("url")
            if isinstance(content.get("canonicalUrl"), dict)
            else content.get("link") or item.get("link") or ""
        ).strip()
        summary = clean_text(str(content.get("summary") or content.get("description") or ""))
        published_at = normalize_datetime_text(content.get("pubDate") or content.get("providerPublishTime") or item.get("providerPublishTime"))
        if title and url:
            results.append(AgentEvidence(title=title, url=url, query=f"{ticker} yfinance news", snippet=summary, source_type="yfinance_news", published_at=published_at))
    return results


def source_event_payload(item: AgentEvidence) -> dict[str, str]:
    return {
        "title": item.title,
        "url": item.url,
        "source_type": item.source_type,
        "query": item.query,
        "snippet": item.snippet,
        "published_at": item.published_at,
        "fetched_at": item.fetched_at,
        "freshness_status": item.freshness_status,
    }


def normalize_duckduckgo_url(href: str) -> str:
    href = unescape(href)
    parsed = urlparse(href)
    if parsed.query:
        values = parse_qs(parsed.query)
        if values.get("uddg"):
            return unquote(values["uddg"][0])
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return ""


def fetch_page_text(url: str, max_chars: int) -> PageFetchResult:
    try:
        response = requests.get(url, timeout=15, headers=HTTP_HEADERS)
        content_type = response.headers.get("content-type", "")
        status_code = response.status_code
        header_published_at = normalize_datetime_text(response.headers.get("last-modified", ""))
        if status_code in {401, 403, 429}:
            return PageFetchResult(reason=f"HTTP {status_code}; source likely blocked automated access", status_code=status_code, content_type=content_type)
        if status_code >= 400:
            return PageFetchResult(reason=f"HTTP {status_code}", status_code=status_code, content_type=content_type)
        if "pdf" in content_type.lower():
            return PageFetchResult(reason="PDF content is not parsed by this lightweight reader", status_code=status_code, content_type=content_type)
        if "text/plain" in content_type.lower():
            text = clean_text(response.text)[:max_chars]
            return PageFetchResult(text=text, reason="plain text", status_code=status_code, content_type=content_type, published_at=header_published_at)

        visible_text = extract_visible_text_bs4(response.text)
        metadata_text = extract_metadata_text_bs4(response.text)
        page_published_at = extract_published_at_bs4(response.text) or header_published_at
        extraction_method = "Beautiful Soup"
        if not visible_text and not metadata_text:
            parser = TextExtractor()
            parser.feed(response.text)
            visible_text = clean_text(" ".join(parser.parts))
            metadata_text = extract_metadata_text(response.text)
            extraction_method = "HTMLParser fallback"
        combined = clean_text(f"{metadata_text} {visible_text}")
        if len(combined) < 180:
            return PageFetchResult(
                reason=f"only {len(combined)} readable characters after {extraction_method} cleanup; page may require JavaScript, consent, or login",
                status_code=status_code,
                content_type=content_type,
            )
        reason = f"{extraction_method} visible text"
        if metadata_text and len(visible_text) < 500:
            reason = f"{extraction_method} metadata/JSON-LD fallback plus visible text"
        return PageFetchResult(text=combined[:max_chars], reason=reason, status_code=status_code, content_type=content_type, published_at=page_published_at)
    except Exception as exc:
        LOGGER.debug("Could not fetch page %s: %s", url, exc)
        return PageFetchResult(reason=short_error(exc))


def write_agent_outputs(state: AgentState) -> AgentResult:
    ticker_part = safe_name(state.get("ticker") or "QUERY")
    output_dir = Path(state["output_base"]) / "agents" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{ticker_part}"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "agent_report.md"
    evidence_path = output_dir / "agent_sources.csv"
    context_path = output_dir / "agent_context.json"
    log_path = output_dir / "agent_log.md"
    log_json_path = output_dir / "agent_log.json"

    evidence = state.get("evidence", [])
    with evidence_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["title", "url", "query", "snippet", "summary", "source_type", "published_at", "fetched_at", "freshness_status", "content"])
        writer.writeheader()
        for item in evidence:
            writer.writerow({
                "title": item.title,
                "url": item.url,
                "query": item.query,
                "snippet": item.snippet,
                "summary": item.summary,
                "source_type": item.source_type,
                "published_at": item.published_at,
                "fetched_at": item.fetched_at,
                "freshness_status": item.freshness_status,
                "content": item.content,
            })

    report = state.get("report", "")
    report = with_report_timestamp(report, "Agent Research", current_datetime_text())
    if state.get("llm_error"):
        report += f"\n\n## Agent Runtime Note\n\nLLM fallback/error: `{state['llm_error']}`\n"
    report_path.write_text(report, encoding="utf-8")
    context = {
        "query": state.get("query", ""),
        "ticker": state.get("ticker", ""),
        "entity_name": state.get("entity_name", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "agent_config": {
            "provider": state["config"].agent.provider,
            "model": state["config"].agent.model,
            "base_url": state["config"].agent.base_url,
            "max_iterations": state["config"].agent.max_iterations,
            "max_search_queries": state["config"].agent.max_search_queries,
            "search_results_per_query": state["config"].agent.search_results_per_query,
            "pages_per_search": state["config"].agent.pages_per_search,
            "search_region": state["config"].agent.search_region,
        },
        "signals": compact_signals(state.get("signals", {})),
        "fundamentals": compact_fundamentals(state.get("fundamentals", {})),
        "report": report,
        "evidence": [item.__dict__ for item in evidence],
        "events": state.get("events", []),
        "llm_calls": state.get("llm_calls", []),
        "chat": [],
    }
    context_path.write_text(json.dumps(context, indent=2, default=str), encoding="utf-8")
    write_agent_log_files(log_path, log_json_path, state, output_dir)
    return AgentResult(
        output_dir=output_dir,
        report_path=report_path,
        evidence_path=evidence_path,
        context_path=context_path,
        log_path=log_path,
        log_json_path=log_json_path,
        events=state.get("events", []),
    )


def with_report_timestamp(report: str, fallback_title: str, generated_at: str) -> str:
    lines = report.strip().splitlines()
    stamp = ["", f"Generated: {generated_at}", ""]
    if lines and lines[0].startswith("# "):
        return "\n".join([lines[0], *stamp, *lines[1:]]).rstrip() + "\n"
    return "\n".join([f"# {fallback_title}", *stamp, report.strip()]).rstrip() + "\n"


def write_agent_log_files(log_path: Path, log_json_path: Path, state: AgentState, output_dir: Path) -> None:
    events = state.get("events", [])
    payload = {
        "query": state.get("query", ""),
        "ticker": state.get("ticker", ""),
        "entity_name": state.get("entity_name", ""),
        "output_dir": str(output_dir),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "events": events,
        "llm_calls": state.get("llm_calls", []),
        "evidence": [item.__dict__ for item in state.get("evidence", [])],
        "llm_error": state.get("llm_error", ""),
    }
    log_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log_path.write_text(format_agent_log_markdown(payload), encoding="utf-8")


def append_agent_log_event(output_dir: str | Path, event: dict[str, Any]) -> None:
    output_path = Path(output_dir)
    log_json_path = output_path / "agent_log.json"
    log_path = output_path / "agent_log.md"
    if not log_json_path.exists():
        return
    payload = json.loads(log_json_path.read_text(encoding="utf-8"))
    payload.setdefault("events", []).append(event)
    log_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log_path.write_text(format_agent_log_markdown(payload), encoding="utf-8")


def sync_agent_log_llm_calls(output_dir: str | Path, llm_calls: list[dict[str, Any]]) -> None:
    output_path = Path(output_dir)
    log_json_path = output_path / "agent_log.json"
    log_path = output_path / "agent_log.md"
    if not log_json_path.exists():
        return
    payload = json.loads(log_json_path.read_text(encoding="utf-8"))
    payload["llm_calls"] = llm_calls
    log_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log_path.write_text(format_agent_log_markdown(payload), encoding="utf-8")


def format_agent_log_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Agent Log: {payload.get('ticker') or payload.get('query') or 'Research'}",
        "",
        f"Generated: {payload.get('generated_at', '')}",
        f"Query: {payload.get('query', '')}",
        f"Ticker: {payload.get('ticker', '')}",
        f"Entity: {payload.get('entity_name', '')}",
        f"Output: `{payload.get('output_dir', '')}`",
        "",
        "## Timeline",
        "",
    ]
    for index, event in enumerate(payload.get("events", []), start=1):
        lines.extend(format_log_event(index, event))
    llm_calls = payload.get("llm_calls", [])
    lines.extend(["", "## LLM Prompts And Responses", ""])
    if not llm_calls:
        lines.append("- No LLM calls captured.")
    for index, call in enumerate(llm_calls, start=1):
        lines.extend(format_llm_call(index, call))
    evidence = payload.get("evidence", [])
    lines.extend(["", "## Evidence Snapshot", ""])
    if not evidence:
        lines.append("- No evidence captured.")
    for index, item in enumerate(evidence, start=1):
        lines.append(f"### {index}. {item.get('title', 'Untitled')}")
        lines.append("")
        lines.append(f"- URL: {item.get('url', '')}")
        lines.append(f"- Source type: {item.get('source_type', '')}")
        lines.append(f"- Published: {item.get('published_at') or 'unknown'}")
        lines.append(f"- Fetched: {item.get('fetched_at') or 'unknown'}")
        lines.append(f"- Freshness: {item.get('freshness_status') or 'unknown'}")
        lines.append(f"- Search query: {item.get('query', '')}")
        if item.get("snippet"):
            lines.append(f"- Snippet: {item.get('snippet')}")
        if item.get("summary"):
            lines.append("")
            lines.append("Summary:")
            lines.append("")
            lines.append(str(item.get("summary")))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_log_event(index: int, event: dict[str, Any]) -> list[str]:
    lines = [
        f"### {index}. {event.get('kind', 'event')} - {event.get('created_at', '')}",
        "",
        str(event.get("message", "")),
        "",
    ]
    details = event.get("details") or {}
    if details:
        lines.append("Details:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(details, indent=2, default=str))
        lines.append("```")
        lines.append("")
    return lines


def format_llm_call(index: int, call: dict[str, Any]) -> list[str]:
    lines = [
        f"### {index}. {call.get('name', 'llm_call')} - {call.get('created_at', '')}",
        "",
        f"- Provider: {call.get('provider', '')}",
        f"- Model: {call.get('model', '')}",
        f"- Base URL: `{call.get('base_url', '')}`",
        "",
        "Prompt:",
        "",
        "````text",
        str(call.get("prompt", "")),
        "````",
        "",
    ]
    if call.get("response"):
        lines.extend([
            "Response:",
            "",
            "````text",
            str(call.get("response", "")),
            "````",
            "",
        ])
    if call.get("error"):
        lines.extend([
            "Error:",
            "",
            "````text",
            str(call.get("error", "")),
            "````",
            "",
        ])
    return lines


def fallback_report(state: AgentState) -> str:
    evidence = state.get("evidence", [])
    source_lines = "\n".join(f"- [{index}] [{item.title}]({item.url})" for index, item in enumerate(evidence, start=1)) or "- No sources were captured."
    signals = compact_signals(state.get("signals", {}))
    recommendation = signals.get("recommendation", "Unknown")
    score = signals.get("score", "N/A")
    return f"""# Agent Research: {state.get('ticker') or state.get('query')}

## Bottom Line

The configured LLM was unavailable, so this is a conservative fallback. The scanner score is **{score}** with recommendation **{recommendation}** where local market data was available.

## Buy Case

- Review the captured sources for current catalysts before acting.
- Positive technical or fundamental context should be confirmed against fresh filings and primary sources.

## Sell / Avoid Case

- The app could not complete LLM synthesis, so qualitative risks may be incomplete.
- Free web sources may omit paywalled reporting, filings, or breaking news.

## Short-Term Outlook

Short-term direction remains uncertain without a completed LLM synthesis. Use the source list and latest price action as inputs, not guarantees.

## Long-Term Outlook

Long-term attractiveness depends on durable fundamentals, valuation, competitive position, and risk. Missing data was not fabricated.

## Source Notes

{source_lines}

This is analytical research, not financial advice.
"""


def has_agent_evidence(evidence: list[AgentEvidence]) -> bool:
    return any(item.url and (item.summary or item.snippet or item.content or item.title) for item in evidence)


def insufficient_agent_report(state: AgentState) -> str:
    signals = compact_signals(state.get("signals", {}))
    recommendation = signals.get("recommendation", "Unknown")
    score = signals.get("score", "N/A")
    return f"""# Agent Research: {state.get('ticker') or state.get('query')}

## Bottom Line

Insufficient external source evidence was fetched, so the Agent did **not** ask the local LLM to produce a buy/sell thesis.

Local scanner context, if available, shows recommendation **{recommendation}** with score **{score}**, but this is not a source-grounded current-news conclusion.

## Buy Case

- Not provided. No fetched sources support a current buy thesis.

## Sell / Avoid Case

- Not provided. No fetched sources support a current sell or avoid thesis.

## Short-Term Outlook

Unknown from this run. The app will not infer breaking catalysts, sentiment, or global-event effects from the local model's prior knowledge.

## Long-Term Outlook

Unknown from this run. Long-term analysis requires fetched filings, current reporting, or user-provided source material.

## Scenario Map

- Bull: Not assessed due to missing source evidence.
- Base: Not assessed due to missing source evidence.
- Bear: Not assessed due to missing source evidence.

## What Would Change The View

- Re-run after internet/search access is available.
- Provide specific filings, articles, transcripts, or other source text for the Agent to analyze.

## Source Notes

- No usable external source evidence was captured.

This is analytical research, not financial advice.
"""


def format_evidence(evidence: list[AgentEvidence]) -> str:
    if not evidence:
        return "No evidence gathered."
    blocks = []
    for index, item in enumerate(evidence, start=1):
        text = item.summary or item.snippet or extractive_source_summary(item.content)
        blocks.append(
            f"[{index}] {item.title}\nURL: {item.url}\nSource type: {item.source_type}\nPublished: {item.published_at or 'unknown'}\nFetched: {item.fetched_at or 'unknown'}\nFreshness: {item.freshness_status or 'unknown'}\nSearch query: {item.query}\nEvidence summary:\n{text[:1800]}"
        )
    return "\n\n".join(blocks)


def format_chat_history(chat: list[dict[str, str]]) -> str:
    if not chat:
        return "No follow-up chat yet."
    return "\n".join(f"{item.get('role', 'user')}: {item.get('content', '')}" for item in chat[-12:])


def dedupe_evidence(evidence: list[AgentEvidence]) -> list[AgentEvidence]:
    seen: set[str] = set()
    result: list[AgentEvidence] = []
    for item in evidence:
        key = item.url.split("?")[0].rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def extract_json_object(text: str) -> str:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("LLM did not return a JSON object")
    return match.group(0)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def extractive_source_summary(text: str, max_chars: int = 900) -> str:
    clean = clean_text(text)
    if not clean:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    useful = [sentence for sentence in sentences if len(sentence) > 40][:8]
    summary = " ".join(useful) if useful else clean
    return summary[:max_chars]


def extract_visible_text_bs4(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe", "form", "nav", "footer", "header"]):
        tag.decompose()

    main = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = main.find_all(["h1", "h2", "h3", "p", "li", "blockquote"])
    if blocks:
        text = " ".join(block.get_text(" ", strip=True) for block in blocks)
    else:
        text = main.get_text(" ", strip=True)
    return clean_text(text)


def extract_metadata_text_bs4(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string)

    for selector in (
        {"name": "description"},
        {"property": "og:title"},
        {"property": "og:description"},
        {"name": "twitter:title"},
        {"name": "twitter:description"},
        {"property": "article:published_time"},
        {"name": "author"},
    ):
        tag = soup.find("meta", attrs=selector)
        if tag and tag.get("content"):
            parts.append(str(tag["content"]))

    for script in soup.find_all("script", attrs={"type": "application/ld+json"})[:3]:
        raw = script.string or script.get_text(" ", strip=True)
        if raw:
            parts.extend(json_ld_text(raw))
    return clean_text(" ".join(parts))


def extract_published_at_bs4(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    selectors = (
        {"property": "article:published_time"},
        {"property": "article:modified_time"},
        {"name": "date"},
        {"name": "pubdate"},
        {"name": "publishdate"},
        {"name": "timestamp"},
        {"itemprop": "datePublished"},
        {"itemprop": "dateModified"},
    )
    for selector in selectors:
        tag = soup.find("meta", attrs=selector)
        if tag and tag.get("content"):
            parsed = normalize_datetime_text(str(tag["content"]))
            if parsed:
                return parsed
    time_tag = soup.find("time")
    if time_tag:
        parsed = normalize_datetime_text(str(time_tag.get("datetime") or time_tag.get_text(" ", strip=True)))
        if parsed:
            return parsed
    for script in soup.find_all("script", attrs={"type": "application/ld+json"})[:6]:
        raw = script.string or script.get_text(" ", strip=True)
        parsed = extract_published_at_json_ld(raw)
        if parsed:
            return parsed
    return ""


def extract_published_at_json_ld(raw: str) -> str:
    try:
        data = json.loads(raw)
    except Exception:
        return ""
    items = data if isinstance(data, list) else [data]
    queue = list(items)
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            for key in ("datePublished", "dateModified", "uploadDate"):
                parsed = normalize_datetime_text(item.get(key))
                if parsed:
                    return parsed
            queue.extend(value for value in item.values() if isinstance(value, (dict, list)))
        elif isinstance(item, list):
            queue.extend(item)
    return ""


def extract_metadata_text(html: str) -> str:
    parser = MetadataExtractor()
    try:
        parser.feed(html)
    except Exception:
        return ""
    parts: list[str] = []
    title = clean_text(" ".join(parser.title_parts))
    if title:
        parts.append(title)
    parts.extend(parser.meta[:8])
    for raw in parser.json_ld[:3]:
        parts.extend(json_ld_text(raw))
    return clean_text(" ".join(parts))


def json_ld_text(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except Exception:
        return []
    items = data if isinstance(data, list) else [data]
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("headline", "name", "description", "articleBody", "datePublished", "author"):
            value = item.get(key)
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, dict):
                nested = value.get("name")
                if isinstance(nested, str):
                    parts.append(nested)
            elif isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                        parts.append(entry["name"])
                    elif isinstance(entry, str):
                        parts.append(entry)
    return parts


def format_found_links(results: list[AgentEvidence]) -> str:
    if not results:
        return ""
    lines = []
    for index, item in enumerate(results[:5], start=1):
        host = urlparse(item.url).netloc.replace("www.", "")
        lines.append(f"{index}. {item.title} ({host or item.source_type})")
    return "\n" + "\n".join(lines)


def short_preview(text: str, limit: int = 220) -> str:
    clean = clean_text(text)
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


def text_at(item: ET.Element, tag: str) -> str:
    child = item.find(tag)
    return child.text.strip() if child is not None and child.text else ""


def short_error(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) <= 220 else text[:217] + "..."


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def current_datetime_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z%z")


def normalize_datetime_text(value: Any) -> str:
    parsed = parse_source_datetime(value)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds") if parsed else ""


def parse_source_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = clean_text(str(value))
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    relative = parse_relative_datetime(text)
    return relative


def parse_relative_datetime(text: str) -> datetime | None:
    match = re.search(r"\b(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks)\s+ago\b", text, flags=re.I)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("minute"):
        delta = timedelta(minutes=amount)
    elif unit.startswith("hour"):
        delta = timedelta(hours=amount)
    elif unit.startswith("day"):
        delta = timedelta(days=amount)
    else:
        delta = timedelta(weeks=amount)
    return now_utc() - delta


def mark_evidence_freshness(item: AgentEvidence, max_age_hours: int, require_source_date: bool = False) -> AgentEvidence:
    item.fetched_at = item.fetched_at or current_datetime_text()
    published = parse_source_datetime(item.published_at)
    if not published:
        item.freshness_status = "undated_rejected" if require_source_date else "undated"
        return item
    age_hours = (now_utc() - published.astimezone(timezone.utc)).total_seconds() / 3600
    if age_hours < -2:
        item.freshness_status = "future_timestamp_rejected"
    elif age_hours <= max_age_hours:
        item.freshness_status = f"fresh_{age_hours:.1f}h_old"
    else:
        item.freshness_status = f"stale_{age_hours:.1f}h_old"
    return item


def is_fresh_evidence(item: AgentEvidence, max_age_hours: int, require_source_date: bool = False) -> bool:
    mark_evidence_freshness(item, max_age_hours=max_age_hours, require_source_date=require_source_date)
    return item.freshness_status.startswith("fresh_") or (item.freshness_status == "undated" and not require_source_date)

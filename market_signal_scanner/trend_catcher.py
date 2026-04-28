from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypedDict

import pandas as pd
import yfinance as yf

from market_signal_scanner.agent_researcher import (
    AgentEvidence,
    call_ollama,
    call_logged_ollama,
    current_datetime_text,
    fetch_page_text,
    format_agent_log_markdown,
    format_evidence,
    has_agent_evidence,
    is_fresh_evidence,
    mark_evidence_freshness,
    search_duckduckgo,
    search_news_rss,
    short_preview,
)
from market_signal_scanner.config_loader import ScannerConfig
from market_signal_scanner.prompt_loader import load_prompt


LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[str, str], None]


@dataclass
class TrendCatcherResult:
    output_dir: Path
    report_path: Path
    sources_path: Path
    pulse_path: Path
    context_path: Path
    log_path: Path
    log_json_path: Path
    report: str
    sources: list[AgentEvidence] = field(default_factory=list)
    market_pulse: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class TrendCatcherState(TypedDict, total=False):
    config: ScannerConfig
    output_base: Path
    queries: list[str]
    query_index: int
    evidence: list[AgentEvidence]
    market_pulse: list[dict[str, Any]]
    report: str
    events: list[dict[str, Any]]
    llm_calls: list[dict[str, Any]]
    llm_error: str
    generated_at: str


class TrendCatcherRunner:
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

    def run(self, config: ScannerConfig, output_base: str | Path) -> TrendCatcherResult:
        state: TrendCatcherState = {
            "config": config,
            "output_base": Path(output_base),
            "queries": [],
            "query_index": 0,
            "evidence": [],
            "market_pulse": [],
            "events": self.events,
            "llm_calls": [],
            "llm_error": "",
            "generated_at": current_datetime_text(),
        }
        final_state = self._run_graph(state)
        final_state["events"] = self.events
        return write_trend_catcher_outputs(final_state)

    def _run_graph(self, state: TrendCatcherState) -> TrendCatcherState:
        try:
            from langgraph.graph import END, StateGraph
        except Exception as exc:
            LOGGER.warning("LangGraph unavailable; using sequential Trend Catcher runner: %s", exc)
            return self._run_sequential(state)

        graph = StateGraph(TrendCatcherState)
        graph.add_node("plan", self.plan)
        graph.add_node("search", self.search)
        graph.add_node("read", self.read)
        graph.add_node("pulse", self.pulse)
        graph.add_node("synthesize", self.synthesize)
        graph.set_entry_point("plan")
        graph.add_edge("plan", "search")
        graph.add_edge("search", "read")
        graph.add_conditional_edges("read", self.should_continue, {"continue": "search", "finish": "pulse"})
        graph.add_edge("pulse", "synthesize")
        graph.add_edge("synthesize", END)
        self.progress("thought", "Built Trend Catcher LangGraph workflow: plan -> search/read loop -> discovered-ticker pulse -> alert synthesis.")
        return graph.compile().invoke(state)

    def _run_sequential(self, state: TrendCatcherState) -> TrendCatcherState:
        state = self.plan(state)
        while self.should_continue(state) == "continue":
            state = self.search(state)
            state = self.read(state)
        state = self.pulse(state)
        state = self.synthesize(state)
        return state

    def pulse(self, state: TrendCatcherState) -> TrendCatcherState:
        config = state["config"]
        if not config.oracle.pulse_enabled:
            self.progress("observation", "Market pulse disabled in config.")
            return state
        discovered_tickers = discover_pulse_tickers(state, self.progress)
        baseline_tickers = config.oracle.pulse_tickers if config.oracle.pulse_use_baseline_tickers else []
        config_tickers = config.tickers if config.oracle.pulse_include_config_tickers else []
        tickers = dedupe_tickers([*discovered_tickers, *baseline_tickers, *config_tickers])
        if not tickers:
            self.progress("observation", "No tickers were discovered from Trend Catcher evidence, so intraday pulse verification was skipped.")
            return state
        self.progress(
            "action",
            f"Verifying discovered trend with intraday market pulse for {len(tickers)} ticker(s).",
            tickers=tickers,
            interval=config.oracle.pulse_interval,
            period=config.oracle.pulse_period,
        )
        pulse_rows = collect_market_pulse(config, tickers)
        state["market_pulse"] = pulse_rows
        if pulse_rows:
            self.progress(
                "observation",
                f"Market pulse found {len(pulse_rows)} notable intraday mover(s).",
                market_pulse=pulse_rows,
            )
        else:
            self.progress("observation", "Market pulse did not find notable intraday movers.")
        return state

    def plan(self, state: TrendCatcherState) -> TrendCatcherState:
        config = state["config"]
        prompt = render_trend_catcher_prompt(
            "trend_catcher_planner.md",
            max_queries=config.oracle.max_search_queries,
        )
        fallback = default_trend_catcher_queries()
        self.progress("thought", "Planning broad market disruption searches.")
        try:
            response = call_logged_ollama(config.oracle, prompt, state, "trend_catcher_planner")
            parsed = json.loads(extract_json(response))
            queries = [str(item).strip() for item in parsed.get("queries", []) if str(item).strip()]
            state["queries"] = (queries or fallback)[: config.oracle.max_search_queries]
            if parsed.get("reasoning"):
                self.progress("thought", str(parsed["reasoning"]), queries=state["queries"])
        except Exception as exc:
            state["llm_error"] = str(exc)
            state["queries"] = fallback[: config.oracle.max_search_queries]
            self.progress("observation", f"Trend Catcher planner fallback used: {exc}", queries=state["queries"])
        self.progress("action", f"Trend Catcher planned {len(state['queries'])} searches.", queries=state["queries"])
        return state

    def search(self, state: TrendCatcherState) -> TrendCatcherState:
        config = state["config"]
        index = int(state.get("query_index", 0))
        queries = state.get("queries", [])
        if index >= len(queries):
            return state
        query = queries[index]
        self.progress("action", f"Scanning market news: {query}", query=query)
        results, note = search_recent_trend_catcher_sources(query, config)
        for item in results:
            mark_evidence_freshness(item, config.oracle.source_lookback_hours, config.oracle.require_source_dates)
        evidence = state.get("evidence", [])
        evidence.extend(results)
        state["evidence"] = dedupe_by_url(evidence)
        state["query_index"] = index + 1
        self.progress(
            "observation",
            f"Trend Catcher found {len(results)} source link(s). {note}",
            query=query,
            note=note,
            results=[item.__dict__ for item in results],
        )
        return state

    def read(self, state: TrendCatcherState) -> TrendCatcherState:
        config = state["config"]
        unread = sorted(
            [item for item in state.get("evidence", []) if not item.content],
            key=lambda item: 0 if item.published_at else 1,
        )[: config.oracle.pages_per_search]
        for item in unread:
            self.progress("action", f"Reading market source: {item.title[:90]}", title=item.title, url=item.url)
            fetched = fetch_page_text(item.url, config.oracle.max_page_chars)
            item.content = fetched.text
            item.fetched_at = current_datetime_text()
            if fetched.published_at and not item.published_at:
                item.published_at = fetched.published_at
            mark_evidence_freshness(item, config.oracle.source_lookback_hours, config.oracle.require_source_dates)
            if not is_fresh_evidence(item, config.oracle.source_lookback_hours, config.oracle.require_source_dates):
                self.progress(
                    "observation",
                    f"Ignoring source outside freshness window: {item.freshness_status}.",
                    title=item.title,
                    url=item.url,
                    published_at=item.published_at,
                    fetched_at=item.fetched_at,
                    freshness_status=item.freshness_status,
                )
                continue
            if item.content:
                self.progress("observation", f"Captured {len(item.content):,} characters ({fetched.reason}). Summarizing market impact.", title=item.title, url=item.url, fetch_result=fetched.__dict__)
                item.summary = summarize_trend_catcher_source(item, state)
                self.progress("observation", f"Market-impact summary ready: {short_preview(item.summary, 260)}", title=item.title, url=item.url, summary=item.summary)
            else:
                self.progress("observation", f"Could not parse source: {fetched.reason}. Keeping snippet.", title=item.title, url=item.url, fetch_result=fetched.__dict__)
        before_count = len(state.get("evidence", []))
        state["evidence"] = filter_recent_trend_catcher_evidence(state.get("evidence", []), config)
        removed_count = before_count - len(state["evidence"])
        if removed_count:
            self.progress(
                "observation",
                f"Ignored {removed_count} stale or undated source(s) outside the Trend Catcher freshness window.",
                source_lookback_hours=config.oracle.source_lookback_hours,
                require_source_dates=config.oracle.require_source_dates,
            )
        return state

    def should_continue(self, state: TrendCatcherState) -> str:
        config = state["config"]
        index = int(state.get("query_index", 0))
        if index >= min(len(state.get("queries", [])), config.oracle.max_iterations):
            return "finish"
        return "continue"

    def synthesize(self, state: TrendCatcherState) -> TrendCatcherState:
        config = state["config"]
        if not has_agent_evidence(state.get("evidence", [])):
            state["llm_error"] = "No external source evidence was fetched; skipped Trend Catcher LLM synthesis to avoid unsourced conclusions."
            self.progress("observation", state["llm_error"])
            state["report"] = insufficient_trend_catcher_report(state)
            self.progress("observation", "Trend Catcher scan completed with insufficient source evidence.", report_chars=len(state.get("report", "")))
            return state
        prompt = render_trend_catcher_prompt(
            "trend_catcher_synthesis.md",
            alert_threshold=config.oracle.alert_threshold,
            source_lookback_hours=config.oracle.source_lookback_hours,
            market_pulse_text=format_market_pulse(state.get("market_pulse", [])),
            evidence_text=format_evidence(state.get("evidence", [])),
        )
        self.progress("thought", "Synthesizing Trend Catcher alert threshold decision.", evidence_count=len(state.get("evidence", [])))
        try:
            state["report"] = call_logged_ollama(config.oracle, prompt, state, "trend_catcher_synthesis")
        except Exception as exc:
            state["llm_error"] = str(exc)
            state["report"] = fallback_trend_catcher_report(state)
            self.progress("observation", f"Trend Catcher synthesis fallback used: {exc}", error=str(exc))
        self.progress("observation", "Trend Catcher scan completed.", report_chars=len(state.get("report", "")))
        return state


def run_trend_catcher(config: ScannerConfig, output_base: str | Path, progress: ProgressCallback | None = None) -> TrendCatcherResult:
    return TrendCatcherRunner(progress=progress).run(config, output_base)


def render_trend_catcher_prompt(name: str, **values: Any) -> str:
    values.setdefault("current_date", current_datetime_text())
    return load_prompt(name).format(**values)


def search_recent_trend_catcher_sources(query: str, config: ScannerConfig) -> tuple[list[AgentEvidence], str]:
    recent_days = max(1, int((config.oracle.source_lookback_hours + 23) // 24))
    rss_results = search_news_rss(query, config.oracle.search_results_per_query, recent_days=recent_days)
    fresh_rss = [
        item
        for item in rss_results
        if is_fresh_evidence(item, config.oracle.source_lookback_hours, require_source_date=True)
    ]
    notes = [f"Google News RSS returned {len(fresh_rss)} fresh timestamped link(s)."]

    results = fresh_rss[: config.oracle.search_results_per_query]
    if len(results) >= config.oracle.search_results_per_query:
        return results, " ".join(notes)

    duck_results, duck_error = search_duckduckgo(
        query,
        config.oracle.search_results_per_query - len(results),
        config.oracle.search_region,
    )
    if duck_results:
        notes.append(f"DuckDuckGo added {len(duck_results)} supplemental web link(s); these still need page-level timestamps.")
        results.extend(duck_results)
    elif duck_error:
        notes.append(f"DuckDuckGo fallback failed: {duck_error}.")

    return dedupe_by_url(results), " ".join(notes)


def summarize_trend_catcher_source(item: AgentEvidence, state: TrendCatcherState) -> str:
    config = state["config"]
    prompt = render_trend_catcher_prompt(
        "trend_catcher_source_summary.md",
        source_lookback_hours=config.oracle.source_lookback_hours,
        title=item.title,
        url=item.url,
        published_at=item.published_at or "unknown",
        fetched_at=item.fetched_at or "unknown",
        freshness_status=item.freshness_status or "unknown",
        page_text=item.content[: config.oracle.max_page_chars],
    )
    try:
        return call_logged_ollama(config.oracle, prompt, state, "trend_catcher_source_summary")
    except Exception as exc:
        LOGGER.warning("Trend Catcher source summary failed for %s: %s", item.url, exc)
        return item.snippet or item.content[:900]


def write_trend_catcher_outputs(state: TrendCatcherState) -> TrendCatcherResult:
    output_dir = Path(state["output_base"]) / "trend-catcher" / datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "trend_catcher_report.md"
    sources_path = output_dir / "trend_catcher_sources.csv"
    pulse_path = output_dir / "trend_catcher_market_pulse.csv"
    context_path = output_dir / "trend_catcher_context.json"
    log_path = output_dir / "trend_catcher_log.md"
    log_json_path = output_dir / "trend_catcher_log.json"
    evidence = state.get("evidence", [])
    report = state.get("report", "")
    report = with_trend_catcher_timestamp(report, state)
    if state.get("llm_error"):
        report += f"\n\n## Trend Catcher Runtime Note\n\nLLM fallback/error: `{state['llm_error']}`\n"
    report_path.write_text(report, encoding="utf-8")
    with sources_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["title", "url", "query", "snippet", "summary", "source_type", "published_at", "fetched_at", "freshness_status", "content"])
        writer.writeheader()
        for item in evidence:
            writer.writerow(item.__dict__)
    market_pulse = state.get("market_pulse", [])
    with pulse_path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "ticker",
            "last_price",
            "period_return_pct",
            "latest_bar_return_pct",
            "day_return_pct",
            "volume_ratio",
            "last_volume",
            "avg_volume",
            "bars",
            "reason",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in market_pulse:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    payload = {
        "generated_at": current_datetime_text(),
        "report": report,
        "evidence": [item.__dict__ for item in evidence],
        "market_pulse": market_pulse,
        "events": state.get("events", []),
        "llm_calls": state.get("llm_calls", []),
        "llm_error": state.get("llm_error", ""),
    }
    context_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log_payload = {"query": "Trend Catcher market disruption scan", "ticker": "TREND-CATCHER", "entity_name": "Market", "output_dir": str(output_dir), **payload}
    log_json_path.write_text(json.dumps(log_payload, indent=2, default=str), encoding="utf-8")
    log_path.write_text(format_agent_log_markdown(log_payload), encoding="utf-8")
    return TrendCatcherResult(output_dir, report_path, sources_path, pulse_path, context_path, log_path, log_json_path, report, evidence, market_pulse, state.get("events", []))


def with_trend_catcher_timestamp(report: str, state: TrendCatcherState) -> str:
    generated_at = state.get("generated_at") or current_datetime_text()
    source_window = state["config"].oracle.source_lookback_hours
    lines = report.strip().splitlines()
    stamp = [
        "",
        f"Generated: {generated_at}",
        f"Freshness window: sources must be timestamped within the last {source_window} hours.",
        "",
    ]
    if lines and lines[0].startswith("# "):
        return "\n".join([lines[0], *stamp, *lines[1:]]).rstrip() + "\n"
    return "\n".join(["# Trend Catcher Report", *stamp, report.strip()]).rstrip() + "\n"


def append_trend_catcher_log_event(output_dir: str | Path, event: dict[str, Any]) -> None:
    output_path = Path(output_dir)
    log_json_path = output_path / "trend_catcher_log.json"
    log_path = output_path / "trend_catcher_log.md"
    if not log_json_path.exists():
        return
    payload = json.loads(log_json_path.read_text(encoding="utf-8"))
    payload.setdefault("events", []).append(event)
    log_json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log_path.write_text(format_agent_log_markdown(payload), encoding="utf-8")


def default_trend_catcher_queries() -> list[str]:
    return [
        "latest breaking market movers past 24 hours unusual volume surging plunging stocks crypto commodities",
        "latest markets right now past 24 hours stocks crypto commodities investor attention",
        "latest market movers today catalyst news most active stocks crypto commodities",
        "latest sector rotation today risk on risk off unusual investor attention",
        "viral stock market narrative today retail traders institutional flows most active tickers",
        "latest sudden market repricing today policy regulation legal ruling supply shock macro data",
        "early buy signal today breakout momentum unusual options volume latest news",
        "early sell warning today breakdown risk warning credit stress liquidity shock latest news",
    ]


def collect_market_pulse(config: ScannerConfig, tickers_override: list[str] | None = None) -> list[dict[str, Any]]:
    if tickers_override is not None:
        tickers = dedupe_tickers(tickers_override)
    else:
        extra_tickers = config.tickers if config.oracle.pulse_include_config_tickers else []
        baseline_tickers = config.oracle.pulse_tickers if config.oracle.pulse_use_baseline_tickers else []
        tickers = dedupe_tickers([*baseline_tickers, *extra_tickers])
    if not tickers:
        return []
    try:
        data = yf.download(
            tickers=tickers,
            period=config.oracle.pulse_period,
            interval=config.oracle.pulse_interval,
            group_by="ticker",
            auto_adjust=True,
            prepost=True,
            threads=True,
            progress=False,
        )
    except Exception as exc:
        LOGGER.warning("Trend Catcher market pulse download failed: %s", exc)
        return []
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        frame = extract_ticker_frame(data, ticker, len(tickers) == 1)
        row = score_pulse_ticker(ticker, frame, config)
        if row:
            rows.append(row)
    rows.sort(key=lambda item: (abs(float(item.get("period_return_pct") or 0)), float(item.get("volume_ratio") or 0)), reverse=True)
    return rows[: config.oracle.pulse_max_rows]


def extract_ticker_frame(data: pd.DataFrame, ticker: str, single_ticker: bool) -> pd.DataFrame:
    if data is None or data.empty:
        return pd.DataFrame()
    if single_ticker and not isinstance(data.columns, pd.MultiIndex):
        return data.copy()
    if isinstance(data.columns, pd.MultiIndex):
        if ticker in data.columns.get_level_values(0):
            return data[ticker].copy()
        if ticker in data.columns.get_level_values(-1):
            return data.xs(ticker, axis=1, level=-1).copy()
    return pd.DataFrame()


def score_pulse_ticker(ticker: str, frame: pd.DataFrame, config: ScannerConfig) -> dict[str, Any] | None:
    if frame.empty or "Close" not in frame.columns:
        return None
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if len(close) < 3:
        return None
    volume = pd.to_numeric(frame["Volume"], errors="coerce").dropna() if "Volume" in frame.columns else pd.Series(dtype=float)
    last_price = float(close.iloc[-1])
    period_return_pct = pct_change(float(close.iloc[0]), last_price)
    latest_bar_return_pct = pct_change(float(close.iloc[-2]), last_price)
    day_return_pct = compute_latest_day_return(close)
    last_volume = float(volume.iloc[-1]) if not volume.empty else 0.0
    avg_volume = float(volume.iloc[:-1].tail(20).mean()) if len(volume) > 2 else 0.0
    volume_ratio = (last_volume / avg_volume) if avg_volume > 0 else 0.0
    reasons: list[str] = []
    if abs(period_return_pct) >= config.oracle.pulse_min_abs_move_pct:
        reasons.append(f"{period_return_pct:+.2f}% over {config.oracle.pulse_period}")
    if abs(day_return_pct) >= config.oracle.pulse_min_abs_move_pct:
        reasons.append(f"{day_return_pct:+.2f}% latest session")
    if abs(latest_bar_return_pct) >= max(0.4, config.oracle.pulse_min_abs_move_pct / 2):
        reasons.append(f"{latest_bar_return_pct:+.2f}% latest bar")
    if volume_ratio >= config.oracle.pulse_min_volume_ratio:
        reasons.append(f"{volume_ratio:.1f}x recent volume")
    if not reasons:
        return None
    return {
        "ticker": ticker,
        "last_price": round(last_price, 4),
        "period_return_pct": round(period_return_pct, 3),
        "latest_bar_return_pct": round(latest_bar_return_pct, 3),
        "day_return_pct": round(day_return_pct, 3),
        "volume_ratio": round(volume_ratio, 3) if volume_ratio else "",
        "last_volume": int(last_volume) if last_volume else "",
        "avg_volume": int(avg_volume) if avg_volume else "",
        "bars": int(len(close)),
        "reason": "; ".join(reasons),
    }


def compute_latest_day_return(close: pd.Series) -> float:
    if not isinstance(close.index, pd.DatetimeIndex):
        return pct_change(float(close.iloc[-2]), float(close.iloc[-1]))
    last_day = close.index[-1].date()
    day_values = close[close.index.date == last_day]
    if len(day_values) >= 2:
        return pct_change(float(day_values.iloc[0]), float(day_values.iloc[-1]))
    return pct_change(float(close.iloc[-2]), float(close.iloc[-1]))


def pct_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return ((end / start) - 1.0) * 100.0


def format_market_pulse(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No notable intraday price/volume moves crossed the configured pulse thresholds."
    lines = []
    for index, row in enumerate(rows, 1):
        lines.append(
            f"[P{index}] {row.get('ticker')}: last {row.get('last_price')}, "
            f"period {row.get('period_return_pct')}%, day {row.get('day_return_pct')}%, "
            f"latest bar {row.get('latest_bar_return_pct')}%, volume ratio {row.get('volume_ratio') or 'n/a'}; "
            f"{row.get('reason')}"
        )
    return "\n".join(lines)


def discover_pulse_tickers(state: TrendCatcherState, progress: ProgressCallback | None = None) -> list[str]:
    config = state["config"]
    evidence = state.get("evidence", [])
    if not evidence:
        return []
    evidence_text = format_evidence(evidence)[:12000]
    prompt = render_trend_catcher_prompt("trend_catcher_ticker_extraction.md", evidence_text=evidence_text)
    try:
        response = call_logged_ollama(config.oracle, prompt, state, "trend_catcher_ticker_extraction")
        parsed = json.loads(extract_json(response))
        tickers = normalize_discovered_tickers(parsed.get("tickers", []))
        if progress and tickers:
            progress("observation", f"Trend Catcher discovered {len(tickers)} ticker(s) from evidence for pulse verification: {', '.join(tickers)}")
        if parsed.get("reasoning") and progress:
            progress("thought", str(parsed["reasoning"]))
        return tickers[: config.oracle.pulse_max_rows]
    except Exception as exc:
        tickers = regex_discover_tickers(evidence_text)
        if progress:
            progress("observation", f"Ticker extraction fallback used: {exc}. Found {len(tickers)} ticker(s).")
        return tickers[: config.oracle.pulse_max_rows]


def normalize_discovered_tickers(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    tickers: list[str] = []
    for value in values:
        ticker = str(value).strip().upper()
        if is_plausible_ticker(ticker):
            tickers.append(ticker)
    return dedupe_tickers(tickers)


def regex_discover_tickers(text: str) -> list[str]:
    import re
    candidates = re.findall(r"(?:\$|\bNASDAQ:\s*|\bNYSE:\s*|\bAMEX:\s*)([A-Z][A-Z0-9.-]{0,9})\b|\(([A-Z][A-Z0-9.-]{0,9})\)", text)
    tickers: list[str] = []
    for first, second in candidates:
        ticker = (first or second).strip().upper()
        if is_plausible_ticker(ticker):
            tickers.append(ticker)
    return dedupe_tickers(tickers)


def is_plausible_ticker(ticker: str) -> bool:
    if not ticker or len(ticker) > 12:
        return False
    blocked = {
        "CEO", "CFO", "FDA", "SEC", "USA", "US", "AI", "EV", "ETF", "IPO", "GDP", "CPI", "PPI",
        "Q1", "Q2", "Q3", "Q4", "FY", "EPS", "PE", "CNBC", "NYSE", "NASDAQ", "AMEX",
    }
    if ticker in blocked:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
    return all(char in allowed for char in ticker)


def dedupe_tickers(tickers: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ticker in tickers:
        value = str(ticker).strip().upper()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def dedupe_by_url(items: list[AgentEvidence]) -> list[AgentEvidence]:
    seen: set[str] = set()
    result: list[AgentEvidence] = []
    for item in items:
        key = item.url.split("?")[0].rstrip("/").lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def filter_recent_trend_catcher_evidence(items: list[AgentEvidence], config: ScannerConfig) -> list[AgentEvidence]:
    fresh: list[AgentEvidence] = []
    for item in items:
        if is_fresh_evidence(
            item,
            max_age_hours=config.oracle.source_lookback_hours,
            require_source_date=config.oracle.require_source_dates,
        ):
            fresh.append(item)
    return fresh


def extract_json(text: str) -> str:
    import re
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("LLM did not return a JSON object")
    return match.group(0)


def fallback_trend_catcher_report(state: TrendCatcherState) -> str:
    evidence = state.get("evidence", [])
    lines = "\n".join(f"- [{i}] [{item.title}]({item.url})" for i, item in enumerate(evidence[:5], 1)) or "- No sources captured."
    return f"""# Trend Catcher: Needs Manual Review

Trend Catcher could not complete LLM synthesis, so no high-confidence alert decision was produced.

## Attention Verdict

- **Action now:** Do not trade from this run; review the captured sources manually before acting.
- **Trade posture:** No trade yet.
- **Why now:** The scanner found possible source links but could not complete the synthesis step.
- **Do not:** Do not treat this fallback as a buy or sell signal.
- **Invalidation:** Re-run successfully with fresh source summaries and market pulse confirmation.

## What Was Checked

{lines}

This is analytical research, not financial advice.
"""


def insufficient_trend_catcher_report(state: TrendCatcherState) -> str:
    evidence = state.get("evidence", [])
    attempted = len(evidence)
    source_note = (
        f"{attempted} source link(s) survived the freshness filter but none had usable text or snippets."
        if attempted
        else "No fresh timestamped source links survived the configured freshness filter."
    )
    return f"""# Trend Catcher: Unable To Assess

Trend Catcher did not fetch usable external market/news evidence, so it did **not** ask the local LLM to identify trends, catalysts, buy signals, or sell warnings.

## What Was Checked

- Broad market search flow was attempted.
- {source_note}
- Google News RSS is tried first for timestamped recent news; web search links are only supplemental unless page metadata proves they are fresh.

## Attention Verdict

No alert is produced from this run. This means **insufficient evidence**, not "nothing is happening."

## What To Do Next

- Check internet access and configured sources.
- Re-run Trend Catcher when search/news access is available.

This is analytical research, not financial advice.
"""


from __future__ import annotations

import csv
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from market_signal_scanner.agent_researcher import (
    AgentEvidence,
    append_agent_log_event,
    answer_followup,
    call_logged_ollama,
    current_datetime_text,
    dedupe_evidence,
    extractive_source_summary,
    fetch_page_text,
    run_agent_research,
    search_web,
    sync_agent_log_llm_calls,
)
from market_signal_scanner.charting import ChartOptions, build_interactive_chart_payload
from market_signal_scanner.config_loader import load_config
from market_signal_scanner.data_fetcher import Cache, fetch_price_history, validate_price_frame
from market_signal_scanner.llm_utils import clean_llm_response, extract_json_object
from market_signal_scanner.trend_catcher import append_trend_catcher_log_event, run_trend_catcher


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
OUTPUT_ROOT = PROJECT_ROOT / "output"
WEB_ROOT = PROJECT_ROOT / "market_signal_scanner" / "web"
CLI_SCRIPT = PROJECT_ROOT / "market-signal-scanner.py"
LOGGER = logging.getLogger(__name__)


class SaveConfigRequest(BaseModel):
    text: str


class TickerDiscoveryRequest(BaseModel):
    query: str
    max_results: int = 18


class TickerConfigUpdateRequest(BaseModel):
    tickers: list[str]


class JobRequest(BaseModel):
    command: str
    ticker: Optional[str] = None
    period: Optional[str] = None
    interval: Optional[str] = None
    chart_type: str = "candle"
    lookback: int = 180
    moving_averages: str = "20,50,100,200"
    skip_fundamentals: bool = False
    no_support_resistance: bool = False
    no_bollinger: bool = False
    no_volume: bool = False
    no_rsi: bool = False
    no_macd: bool = False


class AgentStartRequest(BaseModel):
    ticker: Optional[str] = None
    query: str = ""


class AgentQuestionRequest(BaseModel):
    question: str


class LlmDiagnosticRequest(BaseModel):
    kind: str


@dataclass
class Job:
    id: str
    command: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    returncode: Optional[int] = None
    output_dir: Optional[str] = None
    logs: str = ""
    error: Optional[str] = None
    process: Optional[subprocess.Popen[str]] = field(default=None, repr=False, compare=False)


class SessionCancelled(RuntimeError):
    pass


app = FastAPI(title="market-signal-scanner GUI")
jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
agent_sessions: dict[str, dict[str, Any]] = {}
agent_lock = threading.Lock()
trend_catcher_sessions: dict[str, dict[str, Any]] = {}
trend_catcher_lock = threading.Lock()
managed_ollama_process: Optional[subprocess.Popen[str]] = None
llm_lock = threading.Lock()


THEME_TICKER_CANDIDATES: dict[str, list[dict[str, str]]] = {
    "water": [
        {"ticker": "AWK", "name": "American Water Works", "type": "EQUITY", "reason": "Largest publicly traded U.S. water utility; often used as a direct water-infrastructure watchlist name."},
        {"ticker": "XYL", "name": "Xylem Inc.", "type": "EQUITY", "reason": "Water technology and measurement company tied to infrastructure, utilities, and industrial water demand."},
        {"ticker": "WTRG", "name": "Essential Utilities", "type": "EQUITY", "reason": "Regulated water and wastewater utility with defensive infrastructure exposure."},
        {"ticker": "AWR", "name": "American States Water", "type": "EQUITY", "reason": "Long-running regulated water utility often tracked by income and defensive investors."},
        {"ticker": "CWT", "name": "California Water Service", "type": "EQUITY", "reason": "Regional regulated water utility; useful for comparing water-utility valuations and risk."},
        {"ticker": "PHO", "name": "Invesco Water Resources ETF", "type": "ETF", "reason": "ETF basket for U.S. water infrastructure, equipment, and utility exposure."},
        {"ticker": "FIW", "name": "First Trust Water ETF", "type": "ETF", "reason": "Water ETF basket that can be easier to track than choosing one company."},
        {"ticker": "CGW", "name": "Invesco S&P Global Water ETF", "type": "ETF", "reason": "Global water ETF for broader water infrastructure exposure."},
    ],
    "energy": [
        {"ticker": "XOM", "name": "Exxon Mobil", "type": "EQUITY", "reason": "Large integrated energy company; useful benchmark for oil and gas exposure."},
        {"ticker": "CVX", "name": "Chevron", "type": "EQUITY", "reason": "Large integrated energy company with dividend focus and commodity sensitivity."},
        {"ticker": "COP", "name": "ConocoPhillips", "type": "EQUITY", "reason": "Major exploration and production name tied closely to oil and gas prices."},
        {"ticker": "SLB", "name": "SLB", "type": "EQUITY", "reason": "Oilfield services leader; can benefit when producers increase drilling and service spending."},
        {"ticker": "EOG", "name": "EOG Resources", "type": "EQUITY", "reason": "Large U.S. shale producer often watched for efficient oil and gas operations."},
        {"ticker": "XLE", "name": "Energy Select Sector SPDR Fund", "type": "ETF", "reason": "Broad U.S. energy-sector ETF; simpler than picking one producer."},
        {"ticker": "VDE", "name": "Vanguard Energy ETF", "type": "ETF", "reason": "Low-cost diversified energy ETF for sector-level tracking."},
    ],
    "dividend": [
        {"ticker": "SCHD", "name": "Schwab U.S. Dividend Equity ETF", "type": "ETF", "reason": "Popular dividend-quality ETF; useful core watchlist name for income-focused investors."},
        {"ticker": "VIG", "name": "Vanguard Dividend Appreciation ETF", "type": "ETF", "reason": "Dividend-growth ETF focused on companies with a history of raising dividends."},
        {"ticker": "DGRO", "name": "iShares Core Dividend Growth ETF", "type": "ETF", "reason": "Broad dividend-growth ETF; useful for comparing income plus growth exposure."},
        {"ticker": "VYM", "name": "Vanguard High Dividend Yield ETF", "type": "ETF", "reason": "High-dividend-yield ETF for diversified income exposure."},
        {"ticker": "HDV", "name": "iShares Core High Dividend ETF", "type": "ETF", "reason": "High-dividend ETF with quality screens; useful for income-oriented watchlists."},
        {"ticker": "NOBL", "name": "ProShares S&P 500 Dividend Aristocrats ETF", "type": "ETF", "reason": "Tracks companies with long dividend-increase histories; useful for dividend consistency research."},
        {"ticker": "KO", "name": "Coca-Cola", "type": "EQUITY", "reason": "Classic dividend compounder candidate with durable consumer brand exposure."},
        {"ticker": "PEP", "name": "PepsiCo", "type": "EQUITY", "reason": "Consumer staples dividend-growth name with snacks and beverages exposure."},
        {"ticker": "PG", "name": "Procter & Gamble", "type": "EQUITY", "reason": "Defensive consumer staples dividend name often watched for stability."},
        {"ticker": "JNJ", "name": "Johnson & Johnson", "type": "EQUITY", "reason": "Healthcare dividend name with long dividend history; useful for defensive income research."},
        {"ticker": "ABBV", "name": "AbbVie", "type": "EQUITY", "reason": "Large pharma dividend stock; income appeal but requires pipeline and patent-risk review."},
        {"ticker": "MCD", "name": "McDonald's", "type": "EQUITY", "reason": "Dividend-growth consumer name with global franchise exposure."},
        {"ticker": "XOM", "name": "Exxon Mobil", "type": "EQUITY", "reason": "Energy dividend name; attractive to income investors but sensitive to commodity cycles."},
        {"ticker": "CVX", "name": "Chevron", "type": "EQUITY", "reason": "Large energy dividend stock; useful for income plus energy-cycle exposure."},
    ],
    "income": [
        {"ticker": "SCHD", "name": "Schwab U.S. Dividend Equity ETF", "type": "ETF", "reason": "Dividend-quality ETF commonly used by income-focused investors."},
        {"ticker": "VYM", "name": "Vanguard High Dividend Yield ETF", "type": "ETF", "reason": "Diversified high-dividend ETF; useful for income comparison."},
        {"ticker": "HDV", "name": "iShares Core High Dividend ETF", "type": "ETF", "reason": "High-dividend ETF with quality screens for income-oriented research."},
        {"ticker": "JEPI", "name": "JPMorgan Equity Premium Income ETF", "type": "ETF", "reason": "Options-income ETF; high distributions but different risk/return profile than normal dividend stocks."},
        {"ticker": "JEPQ", "name": "JPMorgan Nasdaq Equity Premium Income ETF", "type": "ETF", "reason": "Nasdaq-focused options-income ETF; income appeal with tech-market exposure."},
        {"ticker": "O", "name": "Realty Income", "type": "EQUITY", "reason": "Monthly dividend REIT; useful income watchlist name but rate sensitivity matters."},
        {"ticker": "ADC", "name": "Agree Realty", "type": "EQUITY", "reason": "Net lease REIT with dividend focus; useful comparison to Realty Income."},
        {"ticker": "T", "name": "AT&T", "type": "EQUITY", "reason": "High-yield telecom stock; requires debt, growth, and payout sustainability review."},
        {"ticker": "VZ", "name": "Verizon", "type": "EQUITY", "reason": "Telecom dividend stock; income appeal but slow growth and debt sensitivity matter."},
    ],
    "reit": [
        {"ticker": "VNQ", "name": "Vanguard Real Estate ETF", "type": "ETF", "reason": "Broad REIT ETF; useful for tracking real estate income exposure."},
        {"ticker": "XLRE", "name": "Real Estate Select Sector SPDR Fund", "type": "ETF", "reason": "Sector ETF for U.S. real estate equities."},
        {"ticker": "O", "name": "Realty Income", "type": "EQUITY", "reason": "Monthly dividend net lease REIT; popular income watchlist name."},
        {"ticker": "ADC", "name": "Agree Realty", "type": "EQUITY", "reason": "Net lease REIT with dividend focus and retail-property exposure."},
        {"ticker": "PLD", "name": "Prologis", "type": "EQUITY", "reason": "Industrial/logistics REIT; lower yield but high-quality real estate exposure."},
        {"ticker": "AMT", "name": "American Tower", "type": "EQUITY", "reason": "Tower REIT tied to communications infrastructure; rate sensitivity matters."},
    ],
    "solar": [
        {"ticker": "FSLR", "name": "First Solar", "type": "EQUITY", "reason": "Major U.S. solar manufacturer; often sensitive to clean-energy policy and demand."},
        {"ticker": "ENPH", "name": "Enphase Energy", "type": "EQUITY", "reason": "Solar inverter and home-energy technology name; high growth but can be volatile."},
        {"ticker": "SEDG", "name": "SolarEdge Technologies", "type": "EQUITY", "reason": "Solar inverter company; useful for tracking solar-cycle risk and recovery."},
        {"ticker": "TAN", "name": "Invesco Solar ETF", "type": "ETF", "reason": "Solar ETF basket for diversified exposure to the solar theme."},
    ],
    "nuclear": [
        {"ticker": "CEG", "name": "Constellation Energy", "type": "EQUITY", "reason": "Large nuclear power operator; often tracked for clean baseload power demand."},
        {"ticker": "CCJ", "name": "Cameco", "type": "EQUITY", "reason": "Major uranium producer; tied to nuclear fuel demand and uranium prices."},
        {"ticker": "UEC", "name": "Uranium Energy", "type": "EQUITY", "reason": "Uranium miner/developer; speculative way to track uranium-cycle interest."},
        {"ticker": "URA", "name": "Global X Uranium ETF", "type": "ETF", "reason": "Uranium and nuclear-fuel ETF; diversified way to monitor the theme."},
        {"ticker": "URNM", "name": "Sprott Uranium Miners ETF", "type": "ETF", "reason": "Uranium miners ETF; higher-volatility basket for the nuclear-fuel theme."},
    ],
    "semiconductor": [
        {"ticker": "NVDA", "name": "NVIDIA", "type": "EQUITY", "reason": "AI accelerator leader; central ticker for AI infrastructure sentiment."},
        {"ticker": "AMD", "name": "Advanced Micro Devices", "type": "EQUITY", "reason": "CPU/GPU competitor watched for AI, data center, and PC cycles."},
        {"ticker": "AVGO", "name": "Broadcom", "type": "EQUITY", "reason": "Semiconductor and infrastructure software name tied to AI networking and custom silicon."},
        {"ticker": "TSM", "name": "Taiwan Semiconductor", "type": "EQUITY", "reason": "Leading chip foundry; important read-through for global semiconductor demand."},
        {"ticker": "SMH", "name": "VanEck Semiconductor ETF", "type": "ETF", "reason": "Semiconductor ETF; useful diversified benchmark for the chip theme."},
        {"ticker": "SOXX", "name": "iShares Semiconductor ETF", "type": "ETF", "reason": "Broad semiconductor ETF for tracking the sector without single-stock concentration."},
    ],
    "ai": [
        {"ticker": "NVDA", "name": "NVIDIA", "type": "EQUITY", "reason": "Core AI infrastructure name; often drives AI-market sentiment."},
        {"ticker": "MSFT", "name": "Microsoft", "type": "EQUITY", "reason": "Large AI platform and cloud name with enterprise distribution."},
        {"ticker": "GOOGL", "name": "Alphabet", "type": "EQUITY", "reason": "AI model, search, cloud, and advertising exposure in one large-cap name."},
        {"ticker": "AMZN", "name": "Amazon", "type": "EQUITY", "reason": "AWS cloud and AI infrastructure exposure plus large consumer platform."},
        {"ticker": "META", "name": "Meta Platforms", "type": "EQUITY", "reason": "AI-driven advertising, recommendation systems, and open model investments."},
        {"ticker": "BOTZ", "name": "Global X Robotics & Artificial Intelligence ETF", "type": "ETF", "reason": "ETF basket for AI and robotics exposure."},
    ],
    "cybersecurity": [
        {"ticker": "CRWD", "name": "CrowdStrike", "type": "EQUITY", "reason": "Endpoint and cloud security leader; high-growth cybersecurity benchmark."},
        {"ticker": "PANW", "name": "Palo Alto Networks", "type": "EQUITY", "reason": "Large cybersecurity platform company with broad enterprise exposure."},
        {"ticker": "ZS", "name": "Zscaler", "type": "EQUITY", "reason": "Zero-trust and cloud security name; often volatile but theme-relevant."},
        {"ticker": "HACK", "name": "Amplify Cybersecurity ETF", "type": "ETF", "reason": "Cybersecurity ETF basket for diversified theme tracking."},
        {"ticker": "CIBR", "name": "First Trust Nasdaq Cybersecurity ETF", "type": "ETF", "reason": "Cybersecurity ETF with broad public-company exposure."},
    ],
}


THEME_COLORS: dict[str, dict[str, str]] = {
    "green": {
        "bg": "#f4f6f8",
        "panel": "#ffffff",
        "ink": "#17212b",
        "muted": "#667085",
        "line": "#d9e0e7",
        "code-bg": "#f8fafc",
        "code-border": "#d6dee7",
        "code-ink": "#182230",
        "accent": "#0f766e",
        "accent-strong": "#115e59",
        "accent-rgb": "15, 118, 110",
        "accent-soft": "rgba(15, 118, 110, 0.08)",
        "accent-border": "rgba(15, 118, 110, 0.18)",
        "accent-border-strong": "rgba(15, 118, 110, 0.45)",
        "danger": "#b42318",
        "warning": "#b54708",
        "selection-bg": "#c7f0ea",
        "selection-ink": "#101820",
        "sidebar-bg": "#101820",
        "sidebar-ink": "#ffffff",
        "sidebar-muted": "#aab5c0",
        "sidebar-line": "rgba(214, 222, 231, 0.18)",
        "sidebar-active-bg": "rgba(45, 212, 191, 0.12)",
        "sidebar-active-border": "rgba(45, 212, 191, 0.28)",
        "chat-bg": "#e8eee9",
        "chat-head-bg": "#075e54",
        "chat-outgoing-bg": "#dcf8c6",
        "shadow": "0 12px 30px rgba(15, 23, 42, 0.08)",
    },
    "blue": {
        "bg": "#f5f7fb",
        "panel": "#ffffff",
        "ink": "#172033",
        "muted": "#64748b",
        "line": "#d8e1ee",
        "code-bg": "#f8fbff",
        "code-border": "#d5e0ef",
        "code-ink": "#172033",
        "accent": "#2563eb",
        "accent-strong": "#1d4ed8",
        "accent-rgb": "37, 99, 235",
        "accent-soft": "rgba(37, 99, 235, 0.08)",
        "accent-border": "rgba(37, 99, 235, 0.18)",
        "accent-border-strong": "rgba(37, 99, 235, 0.45)",
        "danger": "#b42318",
        "warning": "#b54708",
        "selection-bg": "#dbeafe",
        "selection-ink": "#0f172a",
        "sidebar-bg": "#13233f",
        "sidebar-ink": "#ffffff",
        "sidebar-muted": "#bfdbfe",
        "sidebar-line": "rgba(191, 219, 254, 0.18)",
        "sidebar-active-bg": "rgba(59, 130, 246, 0.18)",
        "sidebar-active-border": "rgba(59, 130, 246, 0.38)",
        "chat-bg": "#eaf2ff",
        "chat-head-bg": "#1d4ed8",
        "chat-outgoing-bg": "#dbeafe",
        "shadow": "0 12px 30px rgba(30, 64, 175, 0.08)",
    },
    "slate": {
        "bg": "#f6f7f9",
        "panel": "#ffffff",
        "ink": "#111827",
        "muted": "#64748b",
        "line": "#d8dee8",
        "code-bg": "#f8fafc",
        "code-border": "#d7dee8",
        "code-ink": "#111827",
        "accent": "#475569",
        "accent-strong": "#334155",
        "accent-rgb": "71, 85, 105",
        "accent-soft": "rgba(71, 85, 105, 0.08)",
        "accent-border": "rgba(71, 85, 105, 0.18)",
        "accent-border-strong": "rgba(71, 85, 105, 0.45)",
        "danger": "#b42318",
        "warning": "#b54708",
        "selection-bg": "#e2e8f0",
        "selection-ink": "#0f172a",
        "sidebar-bg": "#172033",
        "sidebar-ink": "#ffffff",
        "sidebar-muted": "#cbd5e1",
        "sidebar-line": "rgba(203, 213, 225, 0.18)",
        "sidebar-active-bg": "rgba(148, 163, 184, 0.16)",
        "sidebar-active-border": "rgba(148, 163, 184, 0.35)",
        "chat-bg": "#eef2f6",
        "chat-head-bg": "#334155",
        "chat-outgoing-bg": "#e2e8f0",
        "shadow": "0 12px 30px rgba(15, 23, 42, 0.08)",
    },
    "indigo": {
        "bg": "#f7f7fc",
        "panel": "#ffffff",
        "ink": "#1e1b2e",
        "muted": "#6b7280",
        "line": "#dddff0",
        "code-bg": "#fafaff",
        "code-border": "#dcdef0",
        "code-ink": "#1e1b2e",
        "accent": "#4f46e5",
        "accent-strong": "#4338ca",
        "accent-rgb": "79, 70, 229",
        "accent-soft": "rgba(79, 70, 229, 0.08)",
        "accent-border": "rgba(79, 70, 229, 0.18)",
        "accent-border-strong": "rgba(79, 70, 229, 0.45)",
        "danger": "#b42318",
        "warning": "#b54708",
        "selection-bg": "#e0e7ff",
        "selection-ink": "#1e1b4b",
        "sidebar-bg": "#1e1b4b",
        "sidebar-ink": "#ffffff",
        "sidebar-muted": "#c7d2fe",
        "sidebar-line": "rgba(199, 210, 254, 0.18)",
        "sidebar-active-bg": "rgba(129, 140, 248, 0.18)",
        "sidebar-active-border": "rgba(129, 140, 248, 0.38)",
        "chat-bg": "#eef0ff",
        "chat-head-bg": "#4338ca",
        "chat-outgoing-bg": "#e0e7ff",
        "shadow": "0 12px 30px rgba(67, 56, 202, 0.08)",
    },
    "red": {
        "bg": "#f8f6f6",
        "panel": "#ffffff",
        "ink": "#241a1a",
        "muted": "#706565",
        "line": "#eadada",
        "code-bg": "#fffafa",
        "code-border": "#eadada",
        "code-ink": "#241a1a",
        "accent": "#dc2626",
        "accent-strong": "#b91c1c",
        "accent-rgb": "220, 38, 38",
        "accent-soft": "rgba(220, 38, 38, 0.08)",
        "accent-border": "rgba(220, 38, 38, 0.18)",
        "accent-border-strong": "rgba(220, 38, 38, 0.45)",
        "danger": "#b42318",
        "warning": "#b54708",
        "selection-bg": "#fee2e2",
        "selection-ink": "#450a0a",
        "sidebar-bg": "#3b1717",
        "sidebar-ink": "#ffffff",
        "sidebar-muted": "#fecaca",
        "sidebar-line": "rgba(254, 202, 202, 0.18)",
        "sidebar-active-bg": "rgba(248, 113, 113, 0.18)",
        "sidebar-active-border": "rgba(248, 113, 113, 0.38)",
        "chat-bg": "#fff0f0",
        "chat-head-bg": "#b91c1c",
        "chat-outgoing-bg": "#fee2e2",
        "shadow": "0 12px 30px rgba(153, 27, 27, 0.08)",
    },
    "gold": {
        "bg": "#f8f7f2",
        "panel": "#ffffff",
        "ink": "#241f18",
        "muted": "#71695c",
        "line": "#e6dfd0",
        "code-bg": "#fffdf7",
        "code-border": "#e6dfd0",
        "code-ink": "#241f18",
        "accent": "#b7791f",
        "accent-strong": "#92400e",
        "accent-rgb": "183, 121, 31",
        "accent-soft": "rgba(183, 121, 31, 0.09)",
        "accent-border": "rgba(183, 121, 31, 0.20)",
        "accent-border-strong": "rgba(183, 121, 31, 0.45)",
        "danger": "#b42318",
        "warning": "#b54708",
        "selection-bg": "#fef3c7",
        "selection-ink": "#422006",
        "sidebar-bg": "#372710",
        "sidebar-ink": "#ffffff",
        "sidebar-muted": "#fde68a",
        "sidebar-line": "rgba(253, 230, 138, 0.18)",
        "sidebar-active-bg": "rgba(245, 158, 11, 0.18)",
        "sidebar-active-border": "rgba(245, 158, 11, 0.38)",
        "chat-bg": "#f8f1df",
        "chat-head-bg": "#92400e",
        "chat-outgoing-bg": "#fef3c7",
        "shadow": "0 12px 30px rgba(146, 64, 14, 0.08)",
    },
    "teal": {
        "bg": "#f3f8fa",
        "panel": "#ffffff",
        "ink": "#14232b",
        "muted": "#637782",
        "line": "#d5e5ea",
        "code-bg": "#f8fcfd",
        "code-border": "#d5e5ea",
        "code-ink": "#14232b",
        "accent": "#0891b2",
        "accent-strong": "#0e7490",
        "accent-rgb": "8, 145, 178",
        "accent-soft": "rgba(8, 145, 178, 0.08)",
        "accent-border": "rgba(8, 145, 178, 0.18)",
        "accent-border-strong": "rgba(8, 145, 178, 0.45)",
        "danger": "#b42318",
        "warning": "#b54708",
        "selection-bg": "#cffafe",
        "selection-ink": "#083344",
        "sidebar-bg": "#083344",
        "sidebar-ink": "#ffffff",
        "sidebar-muted": "#a5f3fc",
        "sidebar-line": "rgba(165, 243, 252, 0.18)",
        "sidebar-active-bg": "rgba(34, 211, 238, 0.16)",
        "sidebar-active-border": "rgba(34, 211, 238, 0.36)",
        "chat-bg": "#e6f6f8",
        "chat-head-bg": "#0e7490",
        "chat-outgoing-bg": "#cffafe",
        "shadow": "0 12px 30px rgba(14, 116, 144, 0.08)",
    },
}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")


@app.get("/api/ui/theme.css")
def ui_theme_css() -> Response:
    try:
        theme_name = load_config(CONFIG_PATH).ui.theme
    except Exception:
        theme_name = "green"
    colors = THEME_COLORS.get(theme_name, THEME_COLORS["green"])
    declarations = "\n".join(f"  --{name}: {value};" for name, value in colors.items())
    return Response(
        content=f":root {{\n{declarations}\n}}\n",
        media_type="text/css",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.post("/api/shutdown")
def shutdown_server() -> dict[str, Any]:
    threading.Timer(0.5, stop_process).start()
    return {"ok": True, "message": "Server shutdown requested."}


@app.get("/api/llm/status")
def llm_status() -> dict[str, Any]:
    return get_llm_status()


@app.post("/api/llm/start")
def start_llm() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    if config.news_summary.provider != "ollama":
        raise HTTPException(status_code=400, detail="Start is only supported for local Ollama provider")

    status = get_llm_status()
    if status["server_running"]:
        return status | {"ok": True, "message": "Ollama is already running."}

    global managed_ollama_process
    already_starting = False
    with llm_lock:
        if managed_ollama_process and managed_ollama_process.poll() is None:
            already_starting = True
        else:
            try:
                devnull = open(os.devnull, "w", encoding="utf-8")
                managed_ollama_process = subprocess.Popen(
                    ["ollama", "serve"],
                    cwd=PROJECT_ROOT,
                    text=True,
                    stdout=devnull,
                    stderr=devnull,
                )
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="Ollama command not found. Install Ollama first.")
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Could not start Ollama: {exc}")

    if already_starting:
        return get_llm_status() | {"ok": True, "message": "Ollama is starting."}

    for _ in range(15):
        time.sleep(0.4)
        status = get_llm_status()
        if status["server_running"]:
            return status | {"ok": True, "message": "Ollama started."}
    return get_llm_status() | {"ok": False, "message": "Ollama start was requested, but the server is not reachable yet."}


@app.post("/api/llm/stop")
def stop_llm() -> dict[str, Any]:
    global managed_ollama_process
    with llm_lock:
        if not managed_ollama_process or managed_ollama_process.poll() is not None:
            should_stop = False
        else:
            should_stop = True
            process = managed_ollama_process
    if not should_stop:
        return get_llm_status() | {
            "ok": False,
            "message": "This app did not start the running Ollama server. Stop it outside the app if needed.",
        }
    with llm_lock:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        managed_ollama_process = None
    return get_llm_status() | {"ok": True, "message": "Ollama stopped."}


@app.post("/api/llm/diagnostic")
def run_llm_diagnostic(request: LlmDiagnosticRequest) -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    llm = config.news_summary
    if llm.provider != "ollama":
        raise HTTPException(status_code=400, detail=f"LLM diagnostics currently support Ollama only, not {llm.provider}")
    kind = request.kind.strip().lower()
    if kind == "simple":
        prompt = (
            "You are running a connectivity test for market-signal-scanner.\n"
            "Reply with exactly this text and nothing else:\n"
            "LLM_OK"
        )
        payload = {
            "model": llm.model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 64, "num_ctx": 4096},
        }
        url = f"{llm.base_url}/api/generate"
    elif kind == "tool":
        payload = build_tool_diagnostic_payload(llm.model)
        url = f"{llm.base_url}/v1/chat/completions"
    else:
        raise HTTPException(status_code=400, detail="kind must be simple or tool")

    started_at = datetime.now().isoformat(timespec="seconds")
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=llm.timeout_seconds,
        )
        response.raise_for_status()
        api_response = response.json()
        output = diagnostic_model_text(kind, api_response)
        cleaned_output = clean_llm_response(output)
        return {
            "ok": True,
            "kind": kind,
            "created_at": started_at,
            "provider": llm.provider,
            "model": llm.model,
            "base_url": llm.base_url,
            "endpoint": url.replace(llm.base_url, ""),
            "raw_input": payload,
            "raw_output": diagnostic_raw_output(kind, api_response, output),
            "model_text": cleaned_output,
            "raw_model_text": output,
            "format_check": validate_llm_diagnostic(kind, cleaned_output, api_response),
        }
    except Exception as exc:
        return {
            "ok": False,
            "kind": kind,
            "created_at": started_at,
            "provider": llm.provider,
            "model": llm.model,
            "base_url": llm.base_url,
            "endpoint": url.replace(llm.base_url, ""),
            "raw_input": payload,
            "raw_output": None,
            "model_text": "",
            "error": str(exc),
            "format_check": {"ok": False, "message": "Diagnostic request failed before a usable model response was returned."},
        }


def stop_process() -> None:
    os.kill(os.getpid(), signal.SIGINT)


def build_tool_diagnostic_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Use the provided tool to get a quote for AAPL. Do not answer in prose.",
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_quote",
                    "description": "Get a latest market quote for a ticker symbol.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "ticker": {
                                "type": "string",
                                "description": "Ticker symbol, for example AAPL.",
                            }
                        },
                        "required": ["ticker"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_quote"}},
        "temperature": 0,
        "max_tokens": 256,
        "stream": False,
        "think": False,
        "options": {"num_ctx": 4096},
    }


def diagnostic_model_text(kind: str, api_response: dict[str, Any]) -> str:
    if kind == "simple":
        return str(api_response.get("response") or "")
    message = diagnostic_chat_message(api_response)
    content = message.get("content")
    if content:
        return str(content)
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        return json.dumps({"tool_calls": tool_calls}, ensure_ascii=False, indent=2)
    return ""


def diagnostic_raw_output(kind: str, api_response: dict[str, Any], output: str) -> Any:
    if kind == "simple":
        return output
    message = diagnostic_chat_message(api_response)
    return {
        "role": message.get("role", "assistant"),
        "content": message.get("content") or "",
        "tool_calls": message.get("tool_calls") or [],
    }


def diagnostic_chat_message(api_response: dict[str, Any]) -> dict[str, Any]:
    choices = api_response.get("choices") or []
    if not choices:
        return {}
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    return message if isinstance(message, dict) else {}


def validate_llm_diagnostic(kind: str, output: str, api_response: dict[str, Any] | None = None) -> dict[str, Any]:
    text = clean_llm_response(output)
    if kind == "simple":
        return {
            "ok": text == "LLM_OK",
            "message": "Expected final text LLM_OK." if text != "LLM_OK" else "Simple response matched after cleanup.",
        }
    if kind == "tool":
        tool_call = first_tool_call(api_response or {})
        if not tool_call:
            return {"ok": False, "message": "No tool call was returned by the chat/completions response."}
        function = tool_call.get("function") or {}
        name = function.get("name")
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
        except Exception as exc:
            return {"ok": False, "message": f"Tool arguments are not valid JSON: {exc}", "tool_call": tool_call}
        parsed = {"tool": name, "arguments": arguments}
        expected = {"tool": "get_quote", "arguments": {"ticker": "AAPL"}}
        return {
            "ok": parsed == expected,
            "message": "Tool call matched exactly." if parsed == expected else "Tool call returned, but did not match the expected function/arguments exactly.",
            "parsed": parsed,
            "tool_call": tool_call,
        }
    return {"ok": False, "message": "Unknown diagnostic kind."}


def first_tool_call(api_response: dict[str, Any]) -> dict[str, Any]:
    message = diagnostic_chat_message(api_response)
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return {}
    first = tool_calls[0]
    return first if isinstance(first, dict) else {}


def parse_chart_moving_averages(raw: str) -> tuple[int, ...]:
    values: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if 2 <= value <= 500:
            values.append(value)
    return tuple(values or [20, 50, 100, 200])


def normalize_chart_period_interval(period: str, interval: str) -> tuple[str, str]:
    clean_period = (period or "1y").strip()
    clean_interval = (interval or "1d").strip()
    valid_periods = {"5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}
    if clean_period not in valid_periods:
        clean_period = "1y"
    allowed = {
        "5d": {"5m", "15m", "30m", "1h", "1d"},
        "1mo": {"5m", "15m", "30m", "1h", "1d"},
        "3mo": {"1h", "1d", "1wk", "1mo"},
        "6mo": {"1h", "1d", "1wk", "1mo"},
        "1y": {"1h", "1d", "1wk", "1mo"},
        "2y": {"1h", "1d", "1wk", "1mo"},
        "5y": {"1d", "1wk", "1mo"},
        "max": {"1d", "1wk", "1mo"},
    }
    if clean_interval not in allowed[clean_period]:
        clean_interval = "15m" if clean_period in {"5d", "1mo"} else "1d"
    return clean_period, clean_interval


@app.get("/api/config", response_class=PlainTextResponse)
def get_config() -> str:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="config/config.yaml not found")
    return CONFIG_PATH.read_text(encoding="utf-8")


@app.get("/api/agent/suggested-questions")
def get_agent_suggested_questions() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    return {"questions": config.agent.suggested_questions}


@app.get("/api/chart/tickers")
def get_chart_tickers() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    configured = [ticker for ticker in config.tickers if ticker]
    latest_scan: list[str] = []
    latest_scan_run = None
    try:
        latest_scan_run, rows = latest_scan_signal_rows()
        latest_scan = [str(row.get("ticker") or "").strip().upper() for row in rows if row.get("ticker")]
    except HTTPException:
        latest_scan = []
    tickers = dedupe_strings([*configured, *latest_scan])
    return {
        "tickers": tickers,
        "configured": configured,
        "latest_scan": latest_scan,
        "latest_scan_run": latest_scan_run,
    }


@app.get("/api/config/tickers")
def get_config_tickers() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    details = latest_scan_details_by_ticker()
    tickers = []
    for ticker in config.tickers:
        row = details.get(ticker, {})
        tickers.append({
            "ticker": ticker,
            "name": row.get("name") or "",
            "score": row.get("score"),
            "recommendation": row.get("recommendation") or "",
            "last_price": row.get("last_price"),
            "summary": configured_ticker_summary(row),
        })
    return {"tickers": tickers, "count": len(tickers)}


@app.post("/api/ticker-discovery")
def discover_tickers(request: TickerDiscoveryRequest) -> dict[str, Any]:
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Enter a theme or search phrase.")
    max_results = max(1, min(40, request.max_results))
    config = load_config(CONFIG_PATH)
    existing = set(config.tickers)
    candidates = ticker_theme_candidates(query) + yahoo_ticker_search(query, max_results=max_results)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        ticker = normalize_config_ticker(candidate.get("ticker", ""))
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        deduped.append({
            "ticker": ticker,
            "name": candidate.get("name") or ticker,
            "asset_type": candidate.get("asset_type") or candidate.get("type") or "Unknown",
            "exchange": candidate.get("exchange") or "",
            "source": candidate.get("source") or "search",
            "reason": candidate.get("reason") or f"Matched the search theme '{query}'. Research before adding.",
            "already_configured": ticker in existing,
        })
        if len(deduped) >= max_results:
            break
    return {
        "query": query,
        "count": len(deduped),
        "candidates": deduped,
        "note": "Search results are watchlist ideas only. Run Scanner, News Summary, or Agent before investing.",
    }


@app.post("/api/ticker-discovery/deep")
def deep_discover_tickers(request: TickerDiscoveryRequest) -> dict[str, Any]:
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Enter a theme or search phrase.")
    max_results = max(1, min(24, request.max_results))
    config = load_config(CONFIG_PATH)
    evidence = collect_ticker_discovery_evidence(query, config)
    if not evidence:
        return {
            "query": query,
            "count": 0,
            "candidates": [],
            "note": "Deep search did not fetch usable source evidence. Try a shorter phrase or check internet access.",
        }
    prompt = build_deep_ticker_discovery_prompt(query, evidence, max_results)
    state: dict[str, Any] = {"llm_calls": []}
    try:
        response = call_logged_ollama(config.agent, prompt, state, "deep_ticker_discovery")
        parsed = json.loads(extract_json_object(response))
    except Exception as exc:
        LOGGER.warning("Deep ticker discovery LLM extraction failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Deep research search fetched sources but could not extract tickers with the configured LLM: {exc}") from exc
    config_tickers = set(config.tickers)
    candidates = normalize_deep_ticker_candidates(parsed.get("candidates", []), config_tickers, evidence, max_results)
    return {
        "query": query,
        "count": len(candidates),
        "candidates": candidates,
        "sources_reviewed": len(evidence),
        "note": "Deep results are source-grounded watchlist ideas. Verify the business, ticker, and liquidity before adding.",
    }


@app.post("/api/config/tickers/add")
def add_config_tickers(request: TickerConfigUpdateRequest) -> dict[str, Any]:
    requested = [normalize_config_ticker(ticker) for ticker in request.tickers]
    requested = dedupe_strings([ticker for ticker in requested if ticker])
    if not requested:
        raise HTTPException(status_code=400, detail="No tickers provided.")
    text = CONFIG_PATH.read_text(encoding="utf-8")
    config = load_config(CONFIG_PATH)
    existing = set(config.tickers)
    additions = [ticker for ticker in requested if ticker not in existing]
    if additions:
        text = add_tickers_to_config_text(text, additions)
        CONFIG_PATH.write_text(text, encoding="utf-8")
    return {
        "ok": True,
        "added": additions,
        "skipped_existing": [ticker for ticker in requested if ticker in existing],
        "message": f"Added {len(additions)} ticker(s)." if additions else "All selected tickers were already in config.",
    }


@app.post("/api/config/tickers/remove")
def remove_config_tickers(request: TickerConfigUpdateRequest) -> dict[str, Any]:
    requested = dedupe_strings([normalize_config_ticker(ticker) for ticker in request.tickers if ticker])
    if not requested:
        raise HTTPException(status_code=400, detail="No tickers provided.")
    text = CONFIG_PATH.read_text(encoding="utf-8")
    updated_text, removed = remove_tickers_from_config_text(text, set(requested))
    if removed:
        CONFIG_PATH.write_text(updated_text, encoding="utf-8")
    return {
        "ok": True,
        "removed": removed,
        "not_found": [ticker for ticker in requested if ticker not in set(removed)],
        "message": f"Removed {len(removed)} ticker(s)." if removed else "No selected tickers were found in config.",
    }


@app.post("/api/config")
def save_config(request: SaveConfigRequest) -> dict[str, Any]:
    CONFIG_PATH.write_text(request.text, encoding="utf-8")
    return {"ok": True, "path": str(CONFIG_PATH)}


@app.get("/api/chart/interactive")
def interactive_chart_data(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
    chart_type: str = "candle",
    lookback: int = 260,
    moving_averages: str = "20,50,100,200",
    support_resistance: bool = True,
    bollinger: bool = True,
    volume: bool = True,
    rsi: bool = True,
    macd: bool = True,
) -> dict[str, Any]:
    ticker = ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")
    period, interval = normalize_chart_period_interval(period, interval)
    config = load_config(CONFIG_PATH)
    cache = Cache(config.runtime.cache_dir)
    prices = fetch_price_history(
        [ticker],
        cache,
        config.runtime.refresh_prices_hours,
        period=period,
        interval=interval,
    )
    if ticker not in prices:
        fallback = load_chart_cache_fallback(cache, ticker, interval, period)
        if fallback is not None:
            prices[ticker] = fallback
    if ticker not in prices:
        raise HTTPException(status_code=404, detail=f"No usable price history was fetched for {ticker}")
    options = ChartOptions(
        ticker=ticker,
        chart_type="line" if chart_type == "line" else "candle",
        lookback=max(30, min(5000, int(lookback))),
        moving_averages=parse_chart_moving_averages(moving_averages),
        show_support_resistance=support_resistance,
        show_bollinger=bollinger,
        show_volume=volume,
        show_rsi=rsi,
        show_macd=macd,
    )
    return build_interactive_chart_payload(prices[ticker], options)


def load_chart_cache_fallback(cache: Cache, ticker: str, interval: str, period: str) -> Any:
    periods = [period]
    if period == "max":
        periods.extend(["5y", "2y", "1y"])
    elif period == "5y":
        periods.extend(["max", "2y", "1y"])
    elif period == "2y":
        periods.extend(["5y", "max", "1y"])
    else:
        periods.extend(["2y", "5y", "max"])
    for candidate in dict.fromkeys(periods):
        path = cache.price_path(ticker, interval, candidate)
        if not path.exists():
            continue
        try:
            frame = cache.read_pickle(path)
        except Exception:
            continue
        if validate_price_frame(frame):
            LOGGER.info("Using stale chart cache fallback for %s interval=%s period=%s", ticker, interval, candidate)
            return frame
    return None


@app.get("/api/opportunity-map")
def opportunity_map_data() -> dict[str, Any]:
    run_id, rows = latest_scan_signal_rows()
    return {
        "run_id": run_id,
        "row_count": len(rows),
        "summary": opportunity_summary(rows),
        "rows": rows,
    }


@app.get("/api/investor-guardrails")
def investor_guardrails_data() -> dict[str, Any]:
    run_id, rows = latest_scan_signal_rows()
    research = sorted(
        [row for row in rows if is_research_candidate(row)],
        key=lambda item: numeric_value(item.get("opportunity")),
        reverse=True,
    )[:20]
    fomo = sorted(
        [guardrail_item(row, "fomo") for row in rows if fomo_score(row) >= 35],
        key=lambda item: numeric_value(item.get("alert_score")),
        reverse=True,
    )[:20]
    sell_review = sorted(
        [guardrail_item(row, "sell_review") for row in rows if sell_review_score(row) >= 35],
        key=lambda item: numeric_value(item.get("alert_score")),
        reverse=True,
    )[:20]
    sleep_list = sorted(
        [guardrail_item(row, "sleep_on_it") for row in rows if sleep_on_it_score(row) >= 35],
        key=lambda item: numeric_value(item.get("alert_score")),
        reverse=True,
    )[:20]
    return {
        "run_id": run_id,
        "row_count": len(rows),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "research_count": len(research),
            "fomo_count": len(fomo),
            "sell_review_count": len(sell_review),
            "sleep_on_it_count": len(sleep_list),
        },
        "research": [guardrail_item(row, "research") for row in research],
        "fomo": fomo,
        "sell_review": sell_review,
        "sleep_on_it": sleep_list,
        "principles": [
            "Use the map to decide what deserves research, not to force a trade.",
            "Avoid buying immediately after a large move unless the thesis still works at the new price.",
            "For small long-term accounts, starter positions and dollar-cost averaging are usually safer than all-in entries.",
            "A sell review is not an automatic sell; it is a prompt to check whether the original thesis is broken.",
        ],
    }


def latest_scan_signal_rows() -> tuple[str, list[dict[str, Any]]]:
    run_dir = newest_run("scans")
    if run_dir is None:
        raise HTTPException(status_code=404, detail="No scan runs found. Run a current scan first.")
    csv_path = run_dir / "ranked_signals.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="Latest scan does not include ranked_signals.csv")
    rows: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            rows.append(normalize_opportunity_row(raw))
    rows = [row for row in rows if row.get("ticker")]
    rows.sort(key=lambda item: numeric_value(item.get("score")), reverse=True)
    return run_dir.name, rows


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value or "").strip().upper()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def latest_scan_details_by_ticker() -> dict[str, dict[str, Any]]:
    try:
        _, rows = latest_scan_signal_rows()
    except HTTPException:
        return {}
    return {str(row.get("ticker") or "").upper(): row for row in rows}


def configured_ticker_summary(row: dict[str, Any]) -> str:
    if not row:
        return "Configured manually; run Scanner to see score, recommendation, and current signals."
    score = row.get("score")
    recommendation = row.get("recommendation") or "n/a"
    risk = row.get("risk")
    parts = [f"Latest scan: {recommendation}"]
    if score is not None:
        parts.append(f"score {score:.1f}" if isinstance(score, (int, float)) else f"score {score}")
    if risk is not None:
        parts.append(f"risk {risk:.1f}" if isinstance(risk, (int, float)) else f"risk {risk}")
    return ", ".join(parts) + "."


def ticker_theme_candidates(query: str) -> list[dict[str, str]]:
    query_lower = query.lower()
    matches: list[dict[str, str]] = []
    aliases = {
        "top dividend": "dividend",
        "dividend companies": "dividend",
        "dividend stocks": "dividend",
        "dividend growth": "dividend",
        "high dividend": "dividend",
        "yield": "income",
        "monthly dividend": "income",
        "income etf": "income",
        "income stocks": "income",
        "real estate": "reit",
        "reits": "reit",
    }
    matched_themes = set()
    for theme, candidates in THEME_TICKER_CANDIDATES.items():
        if theme in query_lower or query_lower in theme:
            matched_themes.add(theme)
    for phrase, theme in aliases.items():
        if phrase in query_lower:
            matched_themes.add(theme)
    for theme in matched_themes:
        candidates = THEME_TICKER_CANDIDATES.get(theme, [])
        for candidate in candidates:
            matches.append(candidate | {"source": f"built-in {theme} theme"})
    return matches


def yahoo_ticker_search(query: str, max_results: int = 18) -> list[dict[str, str]]:
    try:
        response = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": query, "quotesCount": max_results, "newsCount": 0, "listsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        LOGGER.warning("Ticker discovery search failed for %s: %s", query, exc)
        return []
    candidates: list[dict[str, str]] = []
    allowed_types = {"EQUITY", "ETF"}
    for quote in payload.get("quotes", []):
        quote_type = str(quote.get("quoteType") or "").upper()
        raw_symbol = str(quote.get("symbol") or "").strip()
        exchange = quote.get("exchange") or quote.get("exchDisp") or ""
        symbol = normalize_config_ticker(raw_symbol)
        if not symbol or quote_type not in allowed_types or not is_us_market_ticker(raw_symbol, exchange):
            continue
        name = quote.get("shortname") or quote.get("longname") or symbol
        candidates.append({
            "ticker": symbol,
            "name": str(name),
            "asset_type": quote_type.title(),
            "exchange": str(exchange),
            "source": "Yahoo Finance search",
            "reason": f"Yahoo Finance matched this ticker to the search phrase '{query}'. Use it as a watchlist idea, then verify with Scanner/Agent.",
        })
    return candidates


def collect_ticker_discovery_evidence(query: str, config: Any) -> list[AgentEvidence]:
    searches = [
        f"{query} public companies stock ticker",
        f"{query} listed companies suppliers stock market",
        f"{query} market leaders public company ticker",
        f"{query} ETF holdings stocks",
    ]
    evidence: list[AgentEvidence] = []
    for search_query in searches[: max(2, min(4, config.agent.max_search_queries))]:
        results, _note = search_web(
            search_query,
            limit=min(4, config.agent.search_results_per_query),
            region=config.agent.search_region,
            ticker="",
        )
        evidence.extend(results)
    evidence = dedupe_evidence(evidence)[:8]
    for item in evidence:
        fetched = fetch_page_text(item.url, max_chars=min(5000, config.agent.max_page_chars))
        item.content = fetched.text
        item.fetched_at = current_datetime_text()
        if fetched.published_at and not item.published_at:
            item.published_at = fetched.published_at
        if not item.content and item.snippet:
            item.content = item.snippet
        if item.content:
            item.summary = extractive_source_summary(item.content, max_chars=900)
    return [item for item in evidence if item.summary or item.content or item.snippet]


def build_deep_ticker_discovery_prompt(query: str, evidence: list[AgentEvidence], max_results: int) -> str:
    sources = []
    for index, item in enumerate(evidence, start=1):
        text = item.summary or item.snippet or extractive_source_summary(item.content, max_chars=700)
        sources.append(
            f"[{index}] {item.title}\n"
            f"URL: {item.url}\n"
            f"Published: {item.published_at or 'unknown'}\n"
            f"Search query: {item.query}\n"
            f"Text: {text[:1200]}"
        )
    return f"""
You are helping build a stock/ETF watchlist from web evidence.

Current date/time: {current_datetime_text()}
User search phrase: {query}

Task:
Extract up to {max_results} publicly traded companies or ETFs that are relevant to the phrase.

Rules:
- Return ONLY valid JSON.
- Do not recommend buying. These are watchlist candidates only.
- Prefer U.S.-tradable ticker symbols when clear.
- Only include tickers traded on U.S. markets such as NYSE, Nasdaq, NYSE Arca, NYSE American, BATS, or U.S. OTC.
- Exclude tickers from non-U.S. exchanges such as .TO, .AX, .L, .HK, .T, .NS, .PA, .DE, and similar suffixes.
- Do not include private companies unless there is a public parent company ticker.
- Do not invent tickers. If the source does not support a ticker, omit it.
- Each candidate must have a source-grounded one-sentence reason.
- Confidence should be High, Medium, or Low based on how directly sources connect the company to the theme.
- Include source indexes used, matching the source numbers below.

JSON schema:
{{
  "candidates": [
    {{
      "ticker": "TICKER",
      "name": "Company or ETF name",
      "asset_type": "Equity or ETF",
      "reason": "One concise reason this belongs on the watchlist for the user's phrase.",
      "confidence": "High",
      "source_indexes": [1, 2]
    }}
  ]
}}

Sources:
{chr(10).join(sources)}
""".strip()


def normalize_deep_ticker_candidates(candidates: Any, configured: set[str], evidence: list[AgentEvidence], max_results: int) -> list[dict[str, Any]]:
    if not isinstance(candidates, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        ticker = normalize_config_ticker(item.get("ticker") or "")
        if not ticker or ticker in seen or len(ticker) > 15 or not is_us_market_ticker(ticker, ""):
            continue
        seen.add(ticker)
        source_indexes = item.get("source_indexes") if isinstance(item.get("source_indexes"), list) else []
        sources = []
        for index in source_indexes[:4]:
            try:
                source = evidence[int(index) - 1]
            except Exception:
                continue
            sources.append({"title": source.title, "url": source.url})
        normalized.append({
            "ticker": ticker,
            "name": str(item.get("name") or ticker),
            "asset_type": str(item.get("asset_type") or "Unknown"),
            "exchange": "",
            "source": "Deep research",
            "reason": str(item.get("reason") or "Source-grounded match for the search phrase."),
            "confidence": str(item.get("confidence") or "Medium"),
            "sources": sources,
            "already_configured": ticker in configured,
        })
        if len(normalized) >= max_results:
            break
    return normalized


def normalize_config_ticker(ticker: Any) -> str:
    return str(ticker or "").strip().upper().replace(".", "-")


def is_us_market_ticker(ticker: Any, exchange: Any = "") -> bool:
    raw = str(ticker or "").strip().upper()
    normalized = normalize_config_ticker(raw)
    if not normalized:
        return False
    non_us_suffixes = (
        ".TO", ".V", ".CN", ".NE", ".AX", ".L", ".PA", ".DE", ".F", ".MI", ".AS", ".BR",
        ".SW", ".ST", ".OL", ".HE", ".CO", ".HK", ".SS", ".SZ", ".T", ".KS", ".KQ", ".SI",
        ".NS", ".BO", ".SA", ".MX", ".JO", ".NZ", ".TW", ".TWO", ".IR", ".IS",
        "-TO", "-V", "-CN", "-NE", "-AX", "-L", "-PA", "-DE", "-F", "-MI", "-AS", "-BR",
        "-SW", "-ST", "-OL", "-HE", "-CO", "-HK", "-SS", "-SZ", "-T", "-KS", "-KQ", "-SI",
        "-NS", "-BO", "-SA", "-MX", "-JO", "-NZ", "-TW", "-TWO", "-IR", "-IS",
    )
    if raw.endswith(non_us_suffixes) or normalized.endswith(non_us_suffixes):
        return False
    allowed_exchanges = {
        "",
        "ASE", "NMS", "NYQ", "NGM", "NCM", "PCX", "BTS", "PNK", "NYS", "NAS", "NASDAQ",
        "NYSE", "NYSEARCA", "NYSE AMERICAN", "BATS", "OTC", "OTC MARKETS",
    }
    exchange_text = str(exchange or "").strip().upper()
    if exchange_text and exchange_text not in allowed_exchanges:
        return False
    return True


def ticker_block_bounds(lines: list[str]) -> tuple[int, int]:
    start = next((index for index, line in enumerate(lines) if line.strip() == "tickers:"), None)
    if start is None:
        raise HTTPException(status_code=400, detail="Could not find top-level tickers: section in config.yaml")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith((" ", "\t", "#")) and line.strip().endswith(":"):
            end = index
            break
    return start, end


def add_tickers_to_config_text(text: str, tickers: list[str]) -> str:
    lines = text.splitlines()
    start, end = ticker_block_bounds(lines)
    insert_at = start + 1
    has_discovery_section = False
    for index in range(start + 1, end):
        if lines[index].strip() == "# Added from GUI ticker discovery":
            has_discovery_section = True
        if lines[index].strip().startswith("- "):
            insert_at = index + 1

    insert_lines = []
    if not has_discovery_section:
        if insert_at > 0 and lines[insert_at - 1].strip():
            insert_lines.append("")
        insert_lines.append("  # Added from GUI ticker discovery")
    insert_lines.extend([f"  - {ticker}" for ticker in tickers])
    lines[insert_at:insert_at] = insert_lines
    return "\n".join(lines).rstrip() + "\n"


def remove_tickers_from_config_text(text: str, tickers: set[str]) -> tuple[str, list[str]]:
    lines = text.splitlines()
    start, end = ticker_block_bounds(lines)
    removed: list[str] = []
    kept = list(lines)
    for index in range(end - 1, start, -1):
        line = kept[index]
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        ticker = normalize_config_ticker(stripped[2:].split("#", 1)[0].strip())
        if ticker in tickers:
            removed.append(ticker)
            del kept[index]
    for index in range(len(kept) - 1, start, -1):
        if kept[index].strip() != "# Added from GUI ticker discovery":
            continue
        next_content = next((kept[next_index].strip() for next_index in range(index + 1, len(kept)) if kept[next_index].strip()), "")
        if not next_content.startswith("- "):
            del kept[index]
    removed.reverse()
    return "\n".join(kept).rstrip() + "\n", removed


def normalize_opportunity_row(raw: dict[str, str]) -> dict[str, Any]:
    ticker = str(raw.get("ticker") or "").strip().upper()
    numeric_fields = [
        "last_price",
        "return_1d",
        "return_5d",
        "return_1m",
        "return_3m",
        "return_6m",
        "return_1y",
        "volatility_annual",
        "downside_volatility",
        "max_drawdown",
        "sharpe_like",
        "rsi_14",
        "volume_spike",
        "market_cap",
        "avg_volume_20d",
        "trend_score",
        "momentum_score",
        "risk_penalty",
        "valuation_score",
        "quality_score",
        "score",
        "price_vs_sma_50",
        "price_vs_sma_200",
        "trailing_pe",
        "forward_pe",
        "peg_ratio",
        "price_to_book",
        "revenue_growth",
        "earnings_growth",
        "profit_margin",
        "dividend_yield",
    ]
    row: dict[str, Any] = {
        "ticker": ticker,
        "name": raw.get("entity_name") or ticker,
        "asset_type": opportunity_asset_type(ticker, raw),
        "recommendation": raw.get("recommendation") or "Hold",
        "yahoo_finance_url": raw.get("yahoo_finance_url") or "",
        "tradingview_url": raw.get("tradingview_url") or "",
    }
    for field in numeric_fields:
        row[field] = parse_float(raw.get(field))
    for field in ("golden_cross", "death_cross", "macd_bullish"):
        row[field] = parse_bool(raw.get(field))
    row["risk"] = opportunity_risk(row)
    row["opportunity"] = opportunity_quality(row)
    row["quadrant"] = opportunity_quadrant(row)
    return row


def parse_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def numeric_value(value: Any, default: float = -9999.0) -> float:
    return value if isinstance(value, (int, float)) and value == value else default


def opportunity_asset_type(ticker: str, raw: dict[str, str]) -> str:
    name = f"{raw.get('entity_name') or ''} {raw.get('asset_type') or ''}".lower()
    if ticker.endswith("-USD") or "crypto" in name:
        return "Crypto"
    etf_words = (" etf", " fund", " trust", "ishares", "vanguard", "spdr", "invesco", "proshares", "wisdomtree", "msci", "select sector")
    if any(word in name for word in etf_words):
        return "ETF"
    return "Stock"


def opportunity_risk(row: dict[str, Any]) -> Optional[float]:
    volatility = row.get("volatility_annual")
    drawdown = row.get("max_drawdown")
    if volatility is None and drawdown is None:
        return None
    vol_part = min(100.0, max(0.0, numeric_value(volatility, 0.0) * 100.0))
    drawdown_part = min(100.0, max(0.0, abs(numeric_value(drawdown, 0.0)) * 100.0))
    return round((vol_part * 0.58) + (drawdown_part * 0.42), 2)


def opportunity_quality(row: dict[str, Any]) -> Optional[float]:
    score = row.get("score")
    risk = row.get("risk")
    if score is None:
        return None
    risk_penalty = 0 if risk is None else max(0.0, risk - 25.0) * 0.35
    rsi = row.get("rsi_14")
    overbought_penalty = max(0.0, numeric_value(rsi, 50.0) - 72.0) * 0.6
    return round(numeric_value(score, 0.0) - risk_penalty - overbought_penalty, 2)


def opportunity_quadrant(row: dict[str, Any]) -> str:
    score = numeric_value(row.get("score"), 0.0)
    risk = numeric_value(row.get("risk"), 50.0)
    if score >= 45 and risk <= 45:
        return "Attractive"
    if score >= 45 and risk > 45:
        return "Speculative"
    if score >= 10 and risk <= 45:
        return "Watch"
    return "Avoid"


def opportunity_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rec_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    quadrant_counts: dict[str, int] = {}
    for row in rows:
        rec_counts[str(row.get("recommendation") or "Hold")] = rec_counts.get(str(row.get("recommendation") or "Hold"), 0) + 1
        type_counts[str(row.get("asset_type") or "Stock")] = type_counts.get(str(row.get("asset_type") or "Stock"), 0) + 1
        quadrant_counts[str(row.get("quadrant") or "Watch")] = quadrant_counts.get(str(row.get("quadrant") or "Watch"), 0) + 1
    return {
        "recommendations": rec_counts,
        "asset_types": type_counts,
        "quadrants": quadrant_counts,
        "top_opportunities": rows[:10],
    }


def is_research_candidate(row: dict[str, Any]) -> bool:
    return (
        str(row.get("recommendation")) in {"Strong Buy", "Buy"}
        and numeric_value(row.get("score"), 0.0) >= 40
        and numeric_value(row.get("opportunity"), 0.0) >= 28
        and numeric_value(row.get("risk"), 100.0) <= 62
        and numeric_value(row.get("rsi_14"), 50.0) <= 74
    )


def fomo_score(row: dict[str, Any]) -> float:
    score = 0.0
    score += max(0.0, numeric_value(row.get("return_5d"), 0.0) - 0.05) * 420
    score += max(0.0, numeric_value(row.get("return_1m"), 0.0) - 0.12) * 180
    score += max(0.0, numeric_value(row.get("rsi_14"), 50.0) - 68) * 2.4
    score += max(0.0, numeric_value(row.get("volume_spike"), 1.0) - 1.5) * 14
    score += max(0.0, numeric_value(row.get("risk"), 0.0) - 40) * 0.55
    score -= max(0.0, numeric_value(row.get("score"), 0.0) - 50) * 0.18
    return round(max(0.0, min(100.0, score)), 2)


def sell_review_score(row: dict[str, Any]) -> float:
    score = 0.0
    recommendation = str(row.get("recommendation") or "")
    if recommendation == "Strong Sell":
        score += 45
    elif recommendation == "Sell":
        score += 32
    score += max(0.0, -numeric_value(row.get("score"), 0.0)) * 0.75
    score += max(0.0, -numeric_value(row.get("return_3m"), 0.0) - 0.10) * 140
    score += max(0.0, -numeric_value(row.get("price_vs_sma_200"), 0.0) - 0.05) * 160
    score += max(0.0, abs(numeric_value(row.get("max_drawdown"), 0.0)) - 0.30) * 80
    if row.get("death_cross"):
        score += 18
    return round(max(0.0, min(100.0, score)), 2)


def sleep_on_it_score(row: dict[str, Any]) -> float:
    score = fomo_score(row) * 0.65
    score += max(0.0, numeric_value(row.get("risk"), 0.0) - 55) * 0.7
    score += max(0.0, numeric_value(row.get("rsi_14"), 50.0) - 75) * 1.8
    score += max(0.0, numeric_value(row.get("score"), 0.0) - 60) * 0.25
    return round(max(0.0, min(100.0, score)), 2)


def guardrail_item(row: dict[str, Any], mode: str) -> dict[str, Any]:
    reasons = guardrail_reasons(row, mode)
    alert_score = {
        "fomo": fomo_score,
        "sell_review": sell_review_score,
        "sleep_on_it": sleep_on_it_score,
    }.get(mode, lambda item: numeric_value(item.get("opportunity"), 0.0))(row)
    return {
        "ticker": row.get("ticker"),
        "name": row.get("name"),
        "asset_type": row.get("asset_type"),
        "recommendation": row.get("recommendation"),
        "score": row.get("score"),
        "opportunity": row.get("opportunity"),
        "risk": row.get("risk"),
        "rsi_14": row.get("rsi_14"),
        "return_5d": row.get("return_5d"),
        "return_1m": row.get("return_1m"),
        "return_3m": row.get("return_3m"),
        "return_1y": row.get("return_1y"),
        "max_drawdown": row.get("max_drawdown"),
        "volume_spike": row.get("volume_spike"),
        "alert_score": alert_score,
        "posture": guardrail_posture(row, mode),
        "reasons": reasons,
        "checklist": due_diligence_checklist(row, mode),
        "links": {
            "yahoo": row.get("yahoo_finance_url"),
            "tradingview": row.get("tradingview_url"),
        },
    }


def guardrail_reasons(row: dict[str, Any], mode: str) -> list[str]:
    reasons: list[str] = []
    if mode in {"fomo", "sleep_on_it"}:
        if numeric_value(row.get("return_5d"), 0.0) >= 0.08:
            reasons.append(f"5D move is {numeric_value(row.get('return_5d'), 0.0) * 100:.1f}%, so chasing risk is elevated.")
        if numeric_value(row.get("return_1m"), 0.0) >= 0.18:
            reasons.append(f"1M move is {numeric_value(row.get('return_1m'), 0.0) * 100:.1f}%, which may already price in good news.")
        if numeric_value(row.get("rsi_14"), 50.0) >= 70:
            reasons.append(f"RSI is {numeric_value(row.get('rsi_14'), 0.0):.1f}, so the asset may be overbought.")
        if numeric_value(row.get("volume_spike"), 1.0) >= 1.8:
            reasons.append(f"Volume is {numeric_value(row.get('volume_spike'), 1.0):.1f}x normal, often a sign of crowded attention.")
    if mode == "sell_review":
        if str(row.get("recommendation")) in {"Sell", "Strong Sell"}:
            reasons.append(f"Scanner recommendation is {row.get('recommendation')}.")
        if numeric_value(row.get("score"), 0.0) < 0:
            reasons.append(f"Score is {numeric_value(row.get('score'), 0.0):.1f}, below the neutral zone.")
        if row.get("death_cross"):
            reasons.append("Death cross is active.")
        if numeric_value(row.get("price_vs_sma_200"), 0.0) < -0.08:
            reasons.append("Price is materially below the 200-day moving average.")
        if numeric_value(row.get("return_3m"), 0.0) < -0.12:
            reasons.append(f"3M return is {numeric_value(row.get('return_3m'), 0.0) * 100:.1f}%.")
    if mode == "research":
        reasons.append(f"Score is {numeric_value(row.get('score'), 0.0):.1f} with {row.get('recommendation')} rating.")
        reasons.append(f"Risk composite is {numeric_value(row.get('risk'), 0.0):.1f}, which is acceptable relative to the score.")
        if row.get("golden_cross"):
            reasons.append("Golden cross is active.")
        if numeric_value(row.get("quality_score"), 0.0) > 0:
            reasons.append("Quality score contributes positively.")
    return reasons[:5] or ["No single red flag dominates; review the full thesis before acting."]


def guardrail_posture(row: dict[str, Any], mode: str) -> str:
    if mode == "research":
        return "Research calmly; consider a starter only after thesis and valuation checks."
    if mode == "fomo":
        return "Do not chase; wait for pullback, consolidation, or a fresh researched thesis."
    if mode == "sell_review":
        return "Review existing exposure; decide whether the original thesis is still valid."
    return "Sleep on it; use a limit, small size, or no trade until the setup cools."


def due_diligence_checklist(row: dict[str, Any], mode: str) -> list[str]:
    base = [
        "Write the one-sentence thesis before buying or selling.",
        "Check whether the catalyst is durable or already priced in.",
        "Compare valuation against growth, margins, and balance-sheet risk.",
        "Decide the invalidation point before entering.",
        "Use starter sizing if conviction is not yet high.",
    ]
    if mode == "sell_review":
        return [
            "Compare the original buy thesis with the current facts.",
            "Separate price pain from business deterioration.",
            "Check whether risk is concentrated too heavily in one theme.",
            "Define what evidence would make you hold, trim, or exit.",
            "Avoid revenge trading after selling.",
        ]
    if mode in {"fomo", "sleep_on_it"}:
        return [
            "Wait 24 hours before buying unless you already researched it.",
            "Check if the move came from real fundamentals or attention only.",
            "Look for a lower-risk entry near support or after consolidation.",
            "Limit starter size and avoid averaging up blindly.",
            "Write what would make this trade a mistake.",
        ]
    return base


@app.get("/api/runs")
def list_runs() -> dict[str, Any]:
    return {
        "scans": runs_for("scans"),
        "backtests": runs_for("backtests"),
        "charts": runs_for("charts"),
        "news": runs_for("news"),
        "agents": runs_for("agents"),
        "trend-catcher": runs_for("trend-catcher"),
    }


@app.post("/api/trend-catcher/sessions")
def create_trend_catcher_session() -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat(timespec="seconds")
    session = {
        "id": session_id,
        "status": "queued",
        "events": [{"kind": "thought", "message": "Queued Trend Catcher market disruption scan.", "created_at": created_at}],
        "output_dir": None,
        "report": "",
        "sources": [],
        "market_pulse": [],
        "error": None,
        "cancel_requested": False,
        "created_at": created_at,
        "finished_at": None,
    }
    with trend_catcher_lock:
        trend_catcher_sessions[session_id] = session
    thread = threading.Thread(target=run_trend_catcher_session, args=(session_id,), daemon=True)
    thread.start()
    return serialize_trend_catcher_session(session)


@app.get("/api/trend-catcher/sessions/{session_id}")
def get_trend_catcher_session(session_id: str) -> dict[str, Any]:
    with trend_catcher_lock:
        session = trend_catcher_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Trend Catcher session not found")
    return serialize_trend_catcher_session(session)


@app.post("/api/trend-catcher/sessions/{session_id}/cancel")
def cancel_trend_catcher_session(session_id: str) -> dict[str, Any]:
    with trend_catcher_lock:
        session = trend_catcher_sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Trend Catcher session not found")
        if session["status"] in {"completed", "failed", "cancelled"}:
            return serialize_trend_catcher_session(session)
        session["cancel_requested"] = True
        session["status"] = "cancelling"
        session["events"].append({"kind": "observation", "message": "Cancel requested by user.", "created_at": datetime.now().isoformat(timespec="seconds")})
        return serialize_trend_catcher_session(session)


@app.post("/api/agent/sessions")
def create_agent_session(request: AgentStartRequest) -> dict[str, Any]:
    if not request.query.strip() and not (request.ticker or "").strip():
        raise HTTPException(status_code=400, detail="Enter a ticker or a research question")
    session_id = str(uuid.uuid4())
    session = {
        "id": session_id,
        "status": "queued",
        "ticker": (request.ticker or "").strip().upper(),
        "query": request.query.strip(),
        "events": [],
        "messages": [{"role": "user", "content": request.query.strip() or (request.ticker or "").strip().upper(), "created_at": datetime.now().isoformat(timespec="seconds")}],
        "output_dir": None,
        "report": "",
        "sources": [],
        "error": None,
        "cancel_requested": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "context_path": None,
    }
    with agent_lock:
        agent_sessions[session_id] = session
    thread = threading.Thread(target=run_agent_session, args=(session_id,), daemon=True)
    thread.start()
    return serialize_agent_session(session)


@app.get("/api/agent/sessions/{session_id}")
def get_agent_session(session_id: str) -> dict[str, Any]:
    with agent_lock:
        session = agent_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Agent session not found")
    return serialize_agent_session(session)


@app.post("/api/agent/sessions/{session_id}/cancel")
def cancel_agent_session(session_id: str) -> dict[str, Any]:
    with agent_lock:
        session = agent_sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Agent session not found")
        if session["status"] in {"completed", "failed", "cancelled"}:
            return serialize_agent_session(session)
        session["cancel_requested"] = True
        session["status"] = "cancelling"
        session["events"].append({"kind": "observation", "message": "Cancel requested by user.", "created_at": datetime.now().isoformat(timespec="seconds")})
        return serialize_agent_session(session)


@app.post("/api/agent/sessions/{session_id}/messages")
def ask_agent_question(session_id: str, request: AgentQuestionRequest) -> dict[str, Any]:
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required")
    with agent_lock:
        session = agent_sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Agent session not found")
        if session["status"] != "completed":
            raise HTTPException(status_code=400, detail="Wait for the agent run to finish before asking follow-up questions")
        session["messages"].append({"role": "user", "content": question, "created_at": datetime.now().isoformat(timespec="seconds")})
        context_path = session.get("context_path")
    if not context_path:
        raise HTTPException(status_code=400, detail="Agent context is unavailable")
    context = json_load(Path(context_path))
    context["chat"] = session["messages"]
    config = load_config(CONFIG_PATH)
    answer = answer_followup(question, context, config)
    with agent_lock:
        session = agent_sessions[session_id]
        session["messages"].append({"role": "assistant", "content": answer, "created_at": datetime.now().isoformat(timespec="seconds")})
        if context_path:
            context["chat"] = session["messages"]
            Path(context_path).write_text(json_dump(context), encoding="utf-8")
            sync_agent_log_llm_calls(Path(context_path).parent, context.get("llm_calls", []))
    return serialize_agent_session(session)


@app.get("/api/runs/{kind}/{run_id}")
def run_detail(kind: str, run_id: str) -> dict[str, Any]:
    run_dir = safe_run_dir(kind, run_id)
    files = []
    for path in sorted(run_dir.iterdir()):
        if path.is_file():
            files.append({"name": path.name, "size": path.stat().st_size, "url": f"/api/files/{kind}/{run_id}/{path.name}"})
    return {"kind": kind, "run_id": run_id, "path": str(run_dir), "files": files}


@app.delete("/api/runs/{kind}/{run_id}")
def delete_run(kind: str, run_id: str) -> dict[str, Any]:
    run_dir = safe_run_dir(kind, run_id)
    shutil.rmtree(run_dir)
    return {"ok": True, "kind": kind, "run_id": run_id, "message": "Run deleted."}


@app.delete("/api/runs/{kind}")
def delete_runs_for_kind(kind: str) -> dict[str, Any]:
    if kind not in {"scans", "backtests", "charts", "news", "agents", "trend-catcher"}:
        raise HTTPException(status_code=400, detail="Invalid run kind")
    root = (OUTPUT_ROOT / kind).resolve()
    if not root.exists():
        return {"ok": True, "kind": kind, "deleted": 0}
    deleted = 0
    for path in root.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
            deleted += 1
    return {"ok": True, "kind": kind, "deleted": deleted}


@app.get("/api/files/{kind}/{run_id}/{filename:path}")
def get_file(kind: str, run_id: str, filename: str) -> FileResponse:
    run_dir = safe_run_dir(kind, run_id)
    target = (run_dir / filename).resolve()
    if run_dir not in target.parents and target != run_dir:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target)


@app.get("/api/preview/{kind}/{run_id}/{filename:path}")
def preview_file(kind: str, run_id: str, filename: str) -> dict[str, Any]:
    run_dir = safe_run_dir(kind, run_id)
    target = (run_dir / filename).resolve()
    if run_dir not in target.parents and target != run_dir:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if target.suffix.lower() == ".csv":
        return {"type": "csv", "rows": csv_preview(target)}
    if target.suffix.lower() in {".md", ".txt", ".log"}:
        return {"type": "text", "text": target.read_text(encoding="utf-8", errors="replace")}
    return {"type": "binary", "url": f"/api/files/{kind}/{run_id}/{filename}"}


@app.post("/api/jobs")
def create_job(request: JobRequest) -> dict[str, Any]:
    if request.command not in {"scan", "backtest", "chart", "news"}:
        raise HTTPException(status_code=400, detail="command must be scan, backtest, chart, or news")
    if request.command in {"chart", "news"} and not request.ticker:
        raise HTTPException(status_code=400, detail=f"ticker is required for {request.command} jobs")

    job = Job(id=str(uuid.uuid4()), command=request.command)
    with jobs_lock:
        jobs[job.id] = job
    thread = threading.Thread(target=run_job, args=(job.id, request), daemon=True)
    thread.start()
    return serialize_job(job)


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    with jobs_lock:
        return [serialize_job(job) for job in sorted(jobs.values(), key=lambda item: item.created_at, reverse=True)]


@app.get("/api/activity")
def list_activity() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with jobs_lock:
        items.extend(serialize_job(job) for job in jobs.values())
    with agent_lock:
        items.extend(serialize_agent_activity(session) for session in agent_sessions.values())
    with trend_catcher_lock:
        items.extend(serialize_trend_catcher_activity(session) for session in trend_catcher_sessions.values())
    return sorted(items, key=lambda item: item.get("created_at") or "", reverse=True)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return serialize_job(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status in {"completed", "failed", "cancelled"}:
            return serialize_job(job)
        job.status = "cancelling"
        job.error = "Cancel requested by user."
        process = job.process
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    with jobs_lock:
        job = jobs[job_id]
        if job.status not in {"completed", "failed", "cancelled"}:
            job.status = "cancelled"
            job.returncode = job.returncode if job.returncode is not None else -signal.SIGTERM
            job.finished_at = datetime.now().isoformat(timespec="seconds")
    return serialize_job(job)


def run_job(job_id: str, request: JobRequest) -> None:
    with jobs_lock:
        job = jobs[job_id]
        if job.status in {"cancelling", "cancelled"}:
            job.status = "cancelled"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            return
        job.status = "running"
        job.started_at = datetime.now().isoformat(timespec="seconds")

    before = set(run_names(kind_for_command(request.command)))
    args = build_cli_args(request)
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            args,
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        with jobs_lock:
            jobs[job_id].process = process
        logs: list[str] = []
        if process.stdout is not None:
            for line in process.stdout:
                logs.append(line.rstrip())
                with jobs_lock:
                    job = jobs[job_id]
                    job.logs = "\n".join(logs[-800:])
        returncode = process.wait()
        after = set(run_names(kind_for_command(request.command)))
        new_runs = sorted(after - before)
        output_dir = newest_run(kind_for_command(request.command), preferred=new_runs)
        with jobs_lock:
            job = jobs[job_id]
            job.returncode = returncode
            job.logs = "\n".join(logs).strip()
            job.output_dir = output_dir.name if output_dir else None
            if job.status == "cancelling":
                job.status = "cancelled"
                job.error = "Cancelled by user."
            else:
                job.status = "completed" if returncode == 0 else "failed"
                job.error = None if returncode == 0 else f"Command exited with {returncode}"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            job.process = None
    except Exception as exc:
        with jobs_lock:
            job = jobs[job_id]
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            job.process = None


def run_agent_session(session_id: str) -> None:
    with agent_lock:
        session = agent_sessions[session_id]
        if session.get("cancel_requested"):
            session["status"] = "cancelled"
            session["finished_at"] = datetime.now().isoformat(timespec="seconds")
            return
        session["status"] = "running"
        session["events"].append({"kind": "thought", "message": "Starting ReAct financial research agent.", "created_at": datetime.now().isoformat(timespec="seconds")})

    def progress(kind: str, message: str) -> None:
        with agent_lock:
            current = agent_sessions[session_id]
            if current.get("cancel_requested"):
                raise SessionCancelled("Agent cancelled by user.")
            current["events"].append({"kind": kind, "message": message, "created_at": datetime.now().isoformat(timespec="seconds")})

    try:
        with agent_lock:
            current = dict(agent_sessions[session_id])
        config = load_config(CONFIG_PATH)
        result = run_agent_research(
            query=current.get("query", ""),
            ticker=current.get("ticker", ""),
            config=config,
            output_base=OUTPUT_ROOT,
            progress=progress,
        )
        context = json_load(result.context_path)
        report = result.report_path.read_text(encoding="utf-8", errors="replace")
        with agent_lock:
            session = agent_sessions[session_id]
            session["status"] = "completed"
            session["output_dir"] = result.output_dir.name
            session["report"] = report
            session["sources"] = context.get("evidence", [])
            session["context_path"] = str(result.context_path)
            session["finished_at"] = datetime.now().isoformat(timespec="seconds")
            saved_event = {"kind": "observation", "message": f"Saved agent outputs to agents/{result.output_dir.name}.", "created_at": session["finished_at"]}
            session["events"].append(saved_event)
        append_agent_log_event(result.output_dir, saved_event)
    except SessionCancelled as exc:
        with agent_lock:
            session = agent_sessions[session_id]
            session["status"] = "cancelled"
            session["error"] = str(exc)
            session["finished_at"] = datetime.now().isoformat(timespec="seconds")
            session["events"].append({"kind": "observation", "message": str(exc), "created_at": session["finished_at"]})
    except Exception as exc:
        with agent_lock:
            session = agent_sessions[session_id]
            session["status"] = "cancelled" if session.get("cancel_requested") else "failed"
            session["error"] = str(exc)
            session["finished_at"] = datetime.now().isoformat(timespec="seconds")
            label = "Agent cancelled" if session.get("cancel_requested") else "Agent failed"
            session["events"].append({"kind": "observation", "message": f"{label}: {exc}", "created_at": session["finished_at"]})


def run_trend_catcher_session(session_id: str) -> None:
    with trend_catcher_lock:
        session = trend_catcher_sessions[session_id]
        if session.get("cancel_requested"):
            session["status"] = "cancelled"
            session["finished_at"] = datetime.now().isoformat(timespec="seconds")
            return
        session["status"] = "running"
        session["events"].append({"kind": "thought", "message": "Starting Trend Catcher market disruption scan.", "created_at": datetime.now().isoformat(timespec="seconds")})

    def progress(kind: str, message: str) -> None:
        with trend_catcher_lock:
            current = trend_catcher_sessions[session_id]
            if current.get("cancel_requested"):
                raise SessionCancelled("Trend Catcher cancelled by user.")
            current["events"].append({"kind": kind, "message": message, "created_at": datetime.now().isoformat(timespec="seconds")})

    try:
        config = load_config(CONFIG_PATH)
        result = run_trend_catcher(config=config, output_base=OUTPUT_ROOT, progress=progress)
        with trend_catcher_lock:
            session = trend_catcher_sessions[session_id]
            session["status"] = "completed"
            session["output_dir"] = result.output_dir.name
            session["report"] = result.report
            session["sources"] = [item.__dict__ for item in result.sources]
            session["market_pulse"] = result.market_pulse
            session["finished_at"] = datetime.now().isoformat(timespec="seconds")
            saved_event = {"kind": "observation", "message": f"Saved Trend Catcher outputs to trend-catcher/{result.output_dir.name}.", "created_at": session["finished_at"]}
            session["events"].append(saved_event)
        append_trend_catcher_log_event(result.output_dir, saved_event)
    except SessionCancelled as exc:
        with trend_catcher_lock:
            session = trend_catcher_sessions[session_id]
            session["status"] = "cancelled"
            session["error"] = str(exc)
            session["finished_at"] = datetime.now().isoformat(timespec="seconds")
            session["events"].append({"kind": "observation", "message": str(exc), "created_at": session["finished_at"]})
    except Exception as exc:
        with trend_catcher_lock:
            session = trend_catcher_sessions[session_id]
            session["status"] = "cancelled" if session.get("cancel_requested") else "failed"
            session["error"] = str(exc)
            session["finished_at"] = datetime.now().isoformat(timespec="seconds")
            label = "Trend Catcher cancelled" if session.get("cancel_requested") else "Trend Catcher failed"
            session["events"].append({"kind": "observation", "message": f"{label}: {exc}", "created_at": session["finished_at"]})


def build_cli_args(request: JobRequest) -> list[str]:
    args = [sys.executable, str(CLI_SCRIPT), request.command, "--config", str(CONFIG_PATH), "--output", str(OUTPUT_ROOT)]
    if request.command == "scan" and request.skip_fundamentals:
        args.append("--skip-fundamentals")
    if request.command == "chart":
        args.extend(["--ticker", request.ticker or ""])
        if request.period:
            args.extend(["--period", request.period])
        if request.interval:
            args.extend(["--interval", request.interval])
        args.extend(["--chart-type", request.chart_type])
        args.extend(["--lookback", str(request.lookback)])
        args.extend(["--ma", request.moving_averages])
        if request.no_support_resistance:
            args.append("--no-support-resistance")
        if request.no_bollinger:
            args.append("--no-bollinger")
        if request.no_volume:
            args.append("--no-volume")
        if request.no_rsi:
            args.append("--no-rsi")
        if request.no_macd:
            args.append("--no-macd")
    if request.command == "news":
        args.extend(["--ticker", request.ticker or ""])
    return args


def kind_for_command(command: str) -> str:
    return {"scan": "scans", "backtest": "backtests", "chart": "charts", "news": "news"}[command]


def runs_for(kind: str) -> list[dict[str, Any]]:
    root = OUTPUT_ROOT / kind
    if not root.exists():
        return []
    runs = []
    for path in sorted([p for p in root.iterdir() if p.is_dir()], reverse=True):
        files = [p.name for p in sorted(path.iterdir()) if p.is_file()]
        runs.append({"id": path.name, "kind": kind, "path": str(path), "files": files})
    return runs


def run_names(kind: str) -> list[str]:
    return [run["id"] for run in runs_for(kind)]


def newest_run(kind: str, preferred: Optional[list[str]] = None) -> Optional[Path]:
    root = OUTPUT_ROOT / kind
    names = preferred or run_names(kind)
    if not names:
        return None
    return root / sorted(names)[-1]


def safe_run_dir(kind: str, run_id: str) -> Path:
    if kind not in {"scans", "backtests", "charts", "news", "agents", "trend-catcher"}:
        raise HTTPException(status_code=400, detail="Invalid run kind")
    root = (OUTPUT_ROOT / kind).resolve()
    run_dir = (root / run_id).resolve()
    if root not in run_dir.parents and run_dir != root:
        raise HTTPException(status_code=400, detail="Invalid run id")
    if not run_dir.exists() or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="Run not found")
    return run_dir


def csv_preview(path: Path, limit: int = 100) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for index, row in enumerate(reader):
            if index >= limit:
                break
            rows.append(dict(row))
    return rows


def serialize_job(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "activity_type": "job",
        "command": job.command,
        "title": job.command,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "returncode": job.returncode,
        "output_dir": job.output_dir,
        "logs": job.logs,
        "error": job.error,
        "run_kind": kind_for_command(job.command),
        "cancellable": job.status in {"queued", "running", "cancelling"},
        "cancel_url": f"/api/jobs/{job.id}/cancel",
    }


def serialize_agent_activity(session: dict[str, Any]) -> dict[str, Any]:
    ticker = session.get("ticker") or ""
    query = session.get("query") or ""
    return {
        "id": session["id"],
        "activity_type": "agent",
        "command": "agent",
        "title": f"agent {ticker}".strip() if ticker else "agent research",
        "subtitle": query,
        "status": session["status"],
        "created_at": session.get("created_at"),
        "started_at": session.get("created_at"),
        "finished_at": session.get("finished_at"),
        "output_dir": session.get("output_dir"),
        "run_kind": "agents",
        "logs": format_session_events(session.get("events", [])),
        "error": session.get("error"),
        "cancellable": session["status"] in {"queued", "running", "cancelling"},
        "cancel_url": f"/api/agent/sessions/{session['id']}/cancel",
    }


def serialize_trend_catcher_activity(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session["id"],
        "activity_type": "trend-catcher",
        "command": "trend-catcher",
        "title": "trend-catcher",
        "subtitle": "early trend discovery",
        "status": session["status"],
        "created_at": session.get("created_at"),
        "started_at": session.get("created_at"),
        "finished_at": session.get("finished_at"),
        "output_dir": session.get("output_dir"),
        "run_kind": "trend-catcher",
        "logs": format_session_events(session.get("events", [])),
        "error": session.get("error"),
        "cancellable": session["status"] in {"queued", "running", "cancelling"},
        "cancel_url": f"/api/trend-catcher/sessions/{session['id']}/cancel",
    }


def format_session_events(events: list[dict[str, Any]]) -> str:
    return "\n".join(f"{event.get('created_at', '')} [{event.get('kind', 'event')}] {event.get('message', '')}" for event in events[-200:])


def serialize_agent_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session["id"],
        "status": session["status"],
        "ticker": session.get("ticker", ""),
        "query": session.get("query", ""),
        "events": session.get("events", []),
        "messages": session.get("messages", []),
        "output_dir": session.get("output_dir"),
        "run_kind": "agents",
        "report": session.get("report", ""),
        "sources": session.get("sources", []),
        "market_pulse": session.get("market_pulse", []),
        "error": session.get("error"),
        "cancel_requested": session.get("cancel_requested", False),
        "created_at": session.get("created_at"),
        "finished_at": session.get("finished_at"),
    }


def serialize_trend_catcher_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session["id"],
        "status": session["status"],
        "events": session.get("events", []),
        "output_dir": session.get("output_dir"),
        "run_kind": "trend-catcher",
        "report": session.get("report", ""),
        "sources": session.get("sources", []),
        "market_pulse": session.get("market_pulse", []),
        "error": session.get("error"),
        "cancel_requested": session.get("cancel_requested", False),
        "created_at": session.get("created_at"),
        "finished_at": session.get("finished_at"),
    }


def json_load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, default=str)


def get_llm_status() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    news_summary = config.news_summary
    model_names: list[str] = []
    server_running = False
    model_available = False
    error = None
    if news_summary.provider == "ollama":
        try:
            response = requests.get(f"{news_summary.base_url}/api/tags", timeout=2)
            response.raise_for_status()
            server_running = True
            data = response.json()
            model_names = sorted(
                str(item.get("name", ""))
                for item in data.get("models", [])
                if isinstance(item, dict) and item.get("name")
            )
            model_available = news_summary.model in model_names
        except Exception as exc:
            error = str(exc)
    else:
        error = f"Status checks are not implemented for provider '{news_summary.provider}'"

    with llm_lock:
        managed_by_app = bool(managed_ollama_process and managed_ollama_process.poll() is None)

    return {
        "provider": news_summary.provider,
        "model": news_summary.model,
        "base_url": news_summary.base_url,
        "server_running": server_running,
        "model_available": model_available,
        "installed_models": model_names,
        "managed_by_app": managed_by_app,
        "can_start": news_summary.provider == "ollama",
        "can_stop": news_summary.provider == "ollama" and managed_by_app,
        "error": None if server_running else error,
    }


def main() -> None:
    import uvicorn

    host = os.environ.get("MARKET_SIGNAL_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("MARKET_SIGNAL_PORT", "8000"))
    except ValueError:
        port = 8000
    uvicorn.run("market_signal_scanner.api.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()

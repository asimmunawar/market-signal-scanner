
from __future__ import annotations

import csv
import json
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

from market_signal_scanner.agent_researcher import append_agent_log_event, answer_followup, run_agent_research, sync_agent_log_llm_calls
from market_signal_scanner.config_loader import load_config
from market_signal_scanner.trend_catcher import append_trend_catcher_log_event, run_trend_catcher


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
OUTPUT_ROOT = PROJECT_ROOT / "output"
WEB_ROOT = PROJECT_ROOT / "market_signal_scanner" / "web"
CLI_SCRIPT = PROJECT_ROOT / "market-signal-scanner.py"


class SaveConfigRequest(BaseModel):
    text: str


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


def stop_process() -> None:
    os.kill(os.getpid(), signal.SIGINT)


@app.get("/api/config", response_class=PlainTextResponse)
def get_config() -> str:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="config/config.yaml not found")
    return CONFIG_PATH.read_text(encoding="utf-8")


@app.get("/api/agent/suggested-questions")
def get_agent_suggested_questions() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    return {"questions": config.agent.suggested_questions}


@app.post("/api/config")
def save_config(request: SaveConfigRequest) -> dict[str, Any]:
    CONFIG_PATH.write_text(request.text, encoding="utf-8")
    return {"ok": True, "path": str(CONFIG_PATH)}


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

    uvicorn.run("market_signal_scanner.api.server:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()

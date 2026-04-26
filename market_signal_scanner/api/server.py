
from __future__ import annotations

import csv
import os
import signal
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
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from market_signal_scanner.config_loader import load_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
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


app = FastAPI(title="market-signal-scanner GUI")
jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
managed_ollama_process: Optional[subprocess.Popen[str]] = None
llm_lock = threading.Lock()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")


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
    if config.agent.provider != "ollama":
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
        raise HTTPException(status_code=404, detail="config.yaml not found")
    return CONFIG_PATH.read_text(encoding="utf-8")


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
        "agents": runs_for("agents"),
    }


@app.get("/api/runs/{kind}/{run_id}")
def run_detail(kind: str, run_id: str) -> dict[str, Any]:
    run_dir = safe_run_dir(kind, run_id)
    files = []
    for path in sorted(run_dir.iterdir()):
        if path.is_file():
            files.append({"name": path.name, "size": path.stat().st_size, "url": f"/api/files/{kind}/{run_id}/{path.name}"})
    return {"kind": kind, "run_id": run_id, "path": str(run_dir), "files": files}


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
    if request.command not in {"scan", "backtest", "chart", "agent"}:
        raise HTTPException(status_code=400, detail="command must be scan, backtest, chart, or agent")
    if request.command in {"chart", "agent"} and not request.ticker:
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


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return serialize_job(job)


def run_job(job_id: str, request: JobRequest) -> None:
    with jobs_lock:
        job = jobs[job_id]
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
            job.status = "completed" if returncode == 0 else "failed"
            job.error = None if returncode == 0 else f"Command exited with {returncode}"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:
        with jobs_lock:
            job = jobs[job_id]
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = datetime.now().isoformat(timespec="seconds")


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
    if request.command == "agent":
        args.extend(["--ticker", request.ticker or ""])
    return args


def kind_for_command(command: str) -> str:
    return {"scan": "scans", "backtest": "backtests", "chart": "charts", "agent": "agents"}[command]


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
    if kind not in {"scans", "backtests", "charts", "agents"}:
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
        "command": job.command,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "returncode": job.returncode,
        "output_dir": job.output_dir,
        "logs": job.logs,
        "error": job.error,
        "run_kind": kind_for_command(job.command),
    }


def get_llm_status() -> dict[str, Any]:
    config = load_config(CONFIG_PATH)
    agent = config.agent
    model_names: list[str] = []
    server_running = False
    model_available = False
    error = None
    if agent.provider == "ollama":
        try:
            response = requests.get(f"{agent.base_url}/api/tags", timeout=2)
            response.raise_for_status()
            server_running = True
            data = response.json()
            model_names = sorted(
                str(item.get("name", ""))
                for item in data.get("models", [])
                if isinstance(item, dict) and item.get("name")
            )
            model_available = agent.model in model_names
        except Exception as exc:
            error = str(exc)
    else:
        error = f"Status checks are not implemented for provider '{agent.provider}'"

    with llm_lock:
        managed_by_app = bool(managed_ollama_process and managed_ollama_process.poll() is None)

    return {
        "provider": agent.provider,
        "model": agent.model,
        "base_url": agent.base_url,
        "server_running": server_running,
        "model_available": model_available,
        "installed_models": model_names,
        "managed_by_app": managed_by_app,
        "can_start": agent.provider == "ollama",
        "can_stop": agent.provider == "ollama" and managed_by_app,
        "error": None if server_running else error,
    }


def main() -> None:
    import uvicorn

    uvicorn.run("market_signal_scanner.api.server:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()

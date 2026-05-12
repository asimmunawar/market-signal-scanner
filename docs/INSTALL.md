# Installation And Setup

This guide is for running `market-signal-scanner` locally on your own machine.

The app is local-first. It uses free market data sources, writes reports to your local `output/` folder, and can use a local Ollama model for research features.

## Requirements

- Python 3.9 or newer
- Git
- macOS, Linux, or Windows with a normal Python environment
- Optional: Ollama for Agent, News Summary, Trend Catcher, and LLM checks

## Fastest macOS Setup

Clone the repo, then double-click:

```text
Market Signal Scanner.command
```

Or run:

```bash
./run_app.sh
```

The launcher will:

- create `.venv`
- install Python dependencies
- create `config/config.yaml` from the example if needed
- start the local web server
- open `http://127.0.0.1:8000`

If macOS blocks the launcher:

```bash
chmod +x run_app.sh "Market Signal Scanner.command"
```

## Manual Setup

```bash
git clone git@github.com:asimmunawar/market-signal-scanner.git
cd market-signal-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
python -m market_signal_scanner.api.server
```

Open:

```text
http://127.0.0.1:8000
```

## Port Or Host Override

The default server is:

```text
http://127.0.0.1:8000
```

To use another port:

```bash
MARKET_SIGNAL_PORT=8010 ./run_app.sh
```

To listen on another host:

```bash
MARKET_SIGNAL_HOST=0.0.0.0 MARKET_SIGNAL_PORT=8010 ./run_app.sh
```

Only expose the app to a network you trust.

## Optional Ollama Setup

Install Ollama, then pull a model:

```bash
ollama pull qwen3:14b
```

In `config/config.yaml`, set the same model under `agent`, `news_summary`, and `oracle` if you want all research features to use it.

The GUI has an **LLM** page with two checks:

- a simple direct model call
- a tool-call style chat/completions check

Those checks show the raw input and detokenized raw output so you can confirm your model is behaving.

## First Run Checklist

1. Open **Config** and review `tickers`.
2. Use **Ticker Discovery** if you want to search a theme such as `top dividend companies`, `water infrastructure`, or `companies providing water cooling technologies to data centers`.
3. Run **Scanner**.
4. Open **Opportunity Map** to see score versus risk.
5. Open **Decision Guardrails** before buying anything.
6. For a ticker you are considering, open **Interactive Chart**, **News Summary**, or **Agent**.

Ticker Discovery is intentionally limited to U.S.-listed stocks and ETFs. It may suggest watchlist candidates, but it does not make buy recommendations.

## Common Problems

If dependencies fail to install, upgrade Python and pip:

```bash
python3 --version
python3 -m pip install --upgrade pip
```

If the app says the port is already in use, either open the existing app or run with a different port:

```bash
MARKET_SIGNAL_PORT=8010 ./run_app.sh
```

If Agent, News, or Trend Catcher cannot use the LLM, open the **LLM** page and run both checks. The scanner, charts, backtests, and Opportunity Map can still work without an LLM.

## Updating

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

Keep your local `config/config.yaml`; it is your editable watchlist and settings file.

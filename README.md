# market-signal-scanner

[![Python](https://img.shields.io/badge/Python-3.9+-blue?logo=python)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Local First](https://img.shields.io/badge/Local--First-Ollama%20Ready-111827)](#llm-and-local-ai)
[![FastAPI](https://img.shields.io/badge/FastAPI-GUI-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![yfinance](https://img.shields.io/badge/Data-yfinance-6b7280)](https://github.com/ranaroussi/yfinance)

**A local-first market intelligence cockpit for scans, charts, backtests, news research, and early trend discovery.**

`market-signal-scanner` helps you turn a configurable ticker universe into ranked market signals, reports, technical charts, backtests, and LLM-assisted research. It runs locally, writes transparent CSV/Markdown outputs, and includes **Trend Catcher**, a broad-market mode designed to look for fresh market trends before they become obvious.

> Educational and analytical software only. Not financial advice, investment advice, or a trading recommendation.

The product philosophy is simple: help small long-term investors slow down, research first, avoid FOMO, size positions modestly, and review weak holdings before mistakes compound.

## Why It Exists

Markets move fast. A useful scanner should answer more than “what is the RSI?”

This project is built to help you ask:

- What looks strong or weak right now?
- What would my signals have done historically?
- What is the chart saying?
- What recent news explains a move?
- What market trend may be starting that I would otherwise miss?

It combines deterministic quant-style signals with local AI research so the output stays inspectable instead of magical.

## Highlights

- **Config-driven universe** from `config/config.yaml`
- **Local web GUI** for scans, charts, backtests, research, outputs, and LLM status
- **Transparent scoring** from `-100` to `+100`
- **Technical indicators**: returns, volatility, drawdown, SMA/EMA, RSI, MACD, stochastic, volume spikes
- **Optional fundamentals**: market cap, P/E, PEG, price/book, growth, margin, debt/equity, FCF, dividend yield
- **Backtesting** with contributions, rebalancing, transaction costs, slippage, and benchmark comparison
- **Opportunity Map** to visualize score versus risk, momentum rotation, and best/worst candidates
- **Decision Guardrails** to flag FOMO risk, sell-review names, research candidates, sleep-on-it setups, starter sizing, and local decision notes
- **Ticker Discovery** for finding U.S.-listed stock/ETF watchlist ideas from plain-English themes
- **Interactive Charting** with candles, moving averages, Bollinger bands, support/resistance, trendlines, RSI, MACD, volume, range controls, box zoom, and a ticker picker
- **News Summary** for ticker-level source-grounded summaries
- **Agent Research** with LangGraph/ReAct-style web research and follow-up Q&A
- **Trend Catcher** for early market trend discovery across news, viral narratives, catalysts, price action, and attention shifts
- **Local LLM support** through Ollama by default
- **Full audit logs** for Agent and Trend Catcher prompts, responses, tool steps, sources, and outputs

## The Core Workflows

| Workflow | What It Does | Output Folder |
|---|---|---|
| Scan | Ranks configured tickers by technical, risk, valuation, and quality signals | `output/scans/` |
| Opportunity Map | Visualizes latest scan output as risk/reward, leaderboard, and heatmap views | latest `output/scans/` |
| Decision Guardrails | Converts latest scan output into FOMO, research, sell-review, and sizing prompts | latest `output/scans/` |
| Ticker Discovery | Searches plain-English themes and helps add/remove U.S. stocks and ETFs from config | `config/config.yaml` |
| Backtest | Simulates how the scoring system would have behaved historically | `output/backtests/` |
| Chart | Generates marked-up technical charts and chart reports | `output/charts/` |
| News | Summarizes recent ticker news with signal/fundamental context | `output/news/` |
| Agent | Performs deeper ticker or question-based research with follow-up chat | `output/agents/` |
| Trend Catcher | Searches for newly forming market trends without starting from a ticker | `output/trend-catcher/` |

## How Sources Are Chosen

Not every workflow uses sources the same way:

- **Scanner** uses the fixed ticker universe from `config/config.yaml`. It does not search narrative pages; it fetches market and fundamental data for the configured tickers/groups.
- **News Summary** uses configured source channels, such as `yfinance_news`, `yahoo_rss`, and `google_news`. The channels are fixed by config, but the articles returned inside them are dynamic.
- **Agent** performs dynamic web research based on the user's question. Its exact pages can change between runs because search results, source freshness, and rankings change.
- **Trend Catcher** performs dynamic recent market/news discovery. When ticker pulse is disabled, it does not start from a fixed ticker list; it looks for current broad-market evidence first.

The relevant search depth and source limits live in `news_summary`, `agent`, and `oracle` sections of `config/config.yaml`.

## Decision Guardrails

Decision Guardrails is built for the investor who buys in small amounts and wants help avoiding the classic traps:

- buying only because a ticker is running
- skipping research because a score looks high
- holding a broken thesis too long
- sizing a position too aggressively
- forgetting why a ticker was bought in the first place

It reads the latest `ranked_signals.csv` and creates four buckets:

- **Research Calmly**: strong enough to investigate, not an automatic buy
- **FOMO Danger**: overbought, crowded, high-volume, or fast-moving setups where waiting may be wiser
- **Sell Review**: weak or deteriorating names that deserve a thesis check
- **Sleep On It**: exciting but risky setups where impulsive action is dangerous

It also includes a starter-position planner and a browser-local decision journal. See [Small Long-Term Investor Playbook](docs/INVESTOR_PLAYBOOK.md).

## Ticker Discovery

Ticker Discovery helps you expand or clean up your watchlist without manually hunting symbols.

You can search broad phrases such as:

- `top dividend companies`
- `water infrastructure`
- `companies providing water cooling technologies to data centers`
- `cybersecurity ETFs`
- `uranium miners`

The app returns candidate U.S.-listed stocks and ETFs with a short reason each may be interesting. It also shows whether each ticker is already in `config/config.yaml`, so **Add** changes to **Remove** after a successful update.

Ticker Discovery is for watchlist construction, not recommendations. After adding candidates, run Scanner, then use Opportunity Map, Decision Guardrails, charts, News Summary, and Agent before making a decision.

## Right Way To Use The App

The app is designed as a research workflow, not a buy button.

1. **Run Scanner**
   - This creates the latest technical and fundamental signal table.
   - The score tells you what deserves attention, not what you must buy.

2. **Use Ticker Discovery When Your Watchlist Feels Too Narrow**
   - Search by theme, problem, sector, or investing style.
   - Add only U.S.-listed stocks or ETFs you actually want to track.
   - Remove stale tickers when they are no longer part of your process.

3. **Open Opportunity Map**
   - Start with the score-versus-risk view.
   - Upper-left names are usually better research candidates than high-risk crowded names.
   - Click a ticker to open details.

4. **Understand The Evidence Type**
   - **Technical analysis** comes from price, trend, momentum, volume, volatility, drawdown, RSI, MACD, and moving averages.
   - **Fundamental analysis** comes from business and valuation data such as earnings, growth, margins, P/E, PEG, price/book, dividends, and balance-sheet measures.
   - **News analysis** comes from recent external sources and should always be checked for timestamps.

5. **Use Decision Guardrails**
   - Research Calmly means “worth studying,” not “buy now.”
   - FOMO Danger means “slow down and avoid chasing.”
   - Sell Review means “check whether the original thesis is still valid,” not “auto-sell.”
   - Sleep On It means the setup may be exciting but emotionally risky.
   - Adjust the risk and time-horizon sliders so the warnings match your actual temperament.

6. **Click Through Before A Final Decision**
   - From ticker detail panels, use:
     - **Interactive Chart** for technical context
     - **Run News Summary** for recent source-grounded news
     - **Ask Agent Before Deciding** for a decision-grade research brief

7. **Write The Thesis**
   - Use the Decision Journal to write why you are buying, holding, trimming, or avoiding.
   - If the thesis is only “it is going up,” wait.

This workflow is intentionally slower than impulse buying. That is the point.

## Trend Catcher

Trend Catcher is the “do not let me miss the move” mode.

It does not start with a ticker or a favorite sector. It searches broadly for early market trends that may be forming now, including:

- sudden buy or sell pressure
- unusual volume or attention
- viral investor narratives
- crypto or commodity breakouts
- policy, legal, macro, or geopolitical catalysts
- company-specific events
- early sector rotation
- pre-market or after-hours movers
- crowded moves that may already be late

Then it extracts explicit tickers from the discovered evidence and uses intraday market movement as verification. If nothing crosses the threshold, Trend Catcher should say **Nothing Urgent** instead of forcing a trade idea.

Trend Catcher writes:

- `trend_catcher_report.md`
- `trend_catcher_sources.csv`
- `trend_catcher_market_pulse.csv`
- `trend_catcher_context.json`
- `trend_catcher_log.md`
- `trend_catcher_log.json`

The logs include the full prompts sent to the LLM and the full responses received.

## Quick Start On macOS

Double-click:

```text
Market Signal Scanner.command
```

Or run:

```bash
./run_app.sh
```

The launcher will:

- create `.venv` if needed
- install dependencies
- create `config/config.yaml` from `config/config.example.yaml` if needed
- start the GUI server
- open `http://127.0.0.1:8000`

If macOS blocks the launcher:

```bash
chmod +x run_app.sh "Market Signal Scanner.command"
```

For full setup, port overrides, Ollama checks, and troubleshooting, see [Installation And Setup](docs/INSTALL.md).

## Manual Install

```bash
git clone git@github.com:asimmunawar/market-signal-scanner.git
cd market-signal-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
```

Run the GUI:

```bash
python -m market_signal_scanner.api.server
```

Open:

```text
http://127.0.0.1:8000
```

To stop it, use **Shutdown Server** in the sidebar or press `Ctrl+C` in the terminal.

## CLI

Current scan:

```bash
python market-signal-scanner.py scan --config config/config.yaml --output ./output
```

Backtest:

```bash
python market-signal-scanner.py backtest --config config/config.yaml --output ./output
```

Chart:

```bash
python market-signal-scanner.py chart --ticker AAPL --config config/config.yaml --output ./output
```

News summary:

```bash
python market-signal-scanner.py news --ticker AAPL --config config/config.yaml --output ./output
```

Agent research:

```bash
python market-signal-scanner.py agent --ticker AAPL --query "What is the latest buy/sell case?" --config config/config.yaml --output ./output
```

Trend Catcher:

```bash
python market-signal-scanner.py trend-catcher --config config/config.yaml --output ./output
```

## Output Layout

Every run gets a sortable timestamped folder:

```text
output/
  scans/<timestamp>/
  backtests/<timestamp>/
  charts/<timestamp>_<TICKER>/
  news/<timestamp>_<TICKER>/
  agents/<timestamp>_<TICKER>/
  trend-catcher/<timestamp>/
```

Generated outputs and caches are ignored by git:

```text
output/
cache/
.venv/
__pycache__/
```

## Configuration

The app is intentionally config-first. Edit `config/config.yaml`; do not hardcode tickers in source code.

Important sections:

- `tickers`: manual ticker universe
- `groups`: optional universe expansion for S&P 500, Nasdaq-100, Dow, and major crypto
- `limits`: max tickers and market-cap filters
- `runtime`: caching, workers, price period/interval, fundamentals behavior
- `backtest`: dates, contributions, rebalance frequency, costs, slippage, benchmark
- `news_summary`: source settings and local LLM settings
- `agent`: ReAct/search depth and LLM settings
- `oracle`: Trend Catcher settings kept under this config key for backward compatibility
- `ui`: GUI theme

Example:

```yaml
tickers:
  - AAPL
  - MSFT
  - NVDA
  - TSLA
  - SPY
  - QQQ
  - BTC-USD
  - ETH-USD

runtime:
  price_interval: "1d"
  price_period: "2y"
  cache_dir: "./cache"

oracle:
  alert_threshold: 70
  pulse_enabled: true
  pulse_use_baseline_tickers: false
  pulse_include_config_tickers: false

ui:
  theme: "green"
```

Price interval changes signal meaning. `SMA 50` with `1d` means 50 daily bars; with `1h` it means 50 hourly bars.

## LLM And Local AI

The default LLM provider is Ollama.

```yaml
agent:
  provider: "ollama"
  model: "gpt-oss:120b"
  base_url: "http://127.0.0.1:11434"

oracle:
  provider: "ollama"
  model: "gpt-oss:120b"
  base_url: "http://127.0.0.1:11434"
```

The GUI includes an **LLM** page that can:

- show the configured provider/model
- check whether Ollama is running
- list installed local models
- start Ollama when available
- stop Ollama only if this app started it

If the LLM is unavailable, the app still tries to write conservative fallback reports from the evidence it gathered.

## Agent And Trend Catcher Logs

Agent and Trend Catcher runs include audit-friendly logs:

- full timeline of actions and observations
- search queries
- fetched sources
- parsed/summarized source content
- full prompts sent to the LLM
- full LLM responses
- errors and fallbacks

This makes the research inspectable and easier to debug.

## CSV Link Columns

Signal CSV outputs include:

- `entity_name`
- `yahoo_finance_url`
- `google_finance_url`
- `tradingview_url`

Spreadsheet apps usually make URL columns clickable. The web GUI also renders URL cells as clickable links.

## Project Structure

```text
market-signal-scanner.py        # CLI launcher
main.py                         # alternate launcher
run_app.sh                      # local launcher
Market Signal Scanner.command   # double-click macOS launcher
config/
  config.example.yaml           # public example config
  config.yaml                   # local editable config
  prompts/                      # editable LLM prompts
docs/
  INSTALL.md                    # setup, troubleshooting, and update guide
  INVESTOR_PLAYBOOK.md          # small-investor workflow and guardrails
market_signal_scanner/
  api/server.py                 # FastAPI GUI backend
  web/                          # browser UI and app assets
  cli.py                        # command routing
  trend_catcher.py              # early trend discovery
  agent_researcher.py           # LangGraph/ReAct-style research
  news_summary.py               # ticker news summary
  config_loader.py              # YAML config parsing and groups
  data_fetcher.py               # yfinance downloads and cache
  indicators.py                 # signal computation
  scorer.py                     # -100 to +100 scoring
  reporter.py                   # scan reports
  backtester.py                 # historical simulations
  charting.py                   # technical charts
```

## Data And Limitations

- `yfinance` is free and can be delayed, incomplete, adjusted, unavailable, or rate-limited.
- Web search and source parsing can miss articles or be blocked by paywalls, login walls, JavaScript, or anti-bot systems.
- Fundamentals are skipped for crypto tickers.
- Missing data is left missing; the app should not fabricate numbers.
- Backtests are simulations, not predictions.
- Agent, News, and Trend Catcher reports are evidence summaries, not guarantees.
- Chart annotations such as support/resistance and trendlines are heuristics.
- Transaction cost and slippage assumptions are simplified.

## Roadmap Ideas

- Scheduled Trend Catcher runs every 10-15 minutes
- Email, Telegram, Discord, Slack, or macOS alerts
- Alert memory and duplicate suppression
- Portfolio-aware risk alerts
- SEC filing monitor
- Earnings calendar
- Options/IV signals
- Sector rotation dashboard
- Prediction tracking for Trend Catcher/Agent calls
- Docker packaging
- macOS `.app` packaging

## Contributing

Issues and pull requests are welcome. Good contributions include:

- new indicators
- better source parsers
- improved prompts
- cleaner backtest assumptions
- GUI polish
- tests and reliability improvements

See [Contributing](CONTRIBUTING.md) for setup, checks, and investor-safety guidelines.

Before committing, keep generated/local folders out of git:

```bash
git status
```

## License

MIT. See [LICENSE](LICENSE).

## Disclaimer

This software is provided for educational and analytical purposes only. It does not provide financial advice, investment advice, trading advice, or guarantees of future performance. You are responsible for your own research and decisions.

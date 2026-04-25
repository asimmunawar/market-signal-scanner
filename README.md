# market-signal-scanner

A configurable Python market scanner with a local web GUI, CLI workflows, technical charting, historical backtesting, and CSV/Markdown reports.

The app analyzes a user-defined ticker universe, computes market signals, ranks assets from `-100` to `+100`, generates buy/sell style analytical reports, and can simulate how the scoring rules would have behaved historically.

> This project is for educational and analytical purposes only. It is not financial advice, investment advice, or a trading recommendation.

## Features

- Config-driven ticker universe from `config.yaml`
- Optional group expansion for S&P 500, Nasdaq-100, Dow, and major crypto tickers
- Local FastAPI web GUI for running scans, backtests, charts, and viewing outputs
- CLI support for scan, backtest, and chart modes
- Cached `yfinance` price/fundamental data
- Technical signals: returns, volatility, drawdown, SMA/EMA, RSI, MACD, stochastic, volume spikes
- Optional fundamentals: market cap, P/E, PEG, price/book, growth, margins, debt/equity, free cash flow, dividend yield, analyst recommendation
- Transparent scoring model with component scores
- Backtesting with contributions, rebalance frequency, max positions, transaction costs, slippage, and benchmark comparison
- Chart generation with candles, moving averages, Bollinger bands, horizontal support/resistance, diagonal trendlines, RSI, MACD, and volume
- CSV outputs include entity names plus Yahoo Finance, Google Finance, and TradingView links

## Screens And Outputs

The local GUI can:

- edit and save `config.yaml`
- run current scans
- run historical backtests
- generate ticker charts
- browse output history
- preview Markdown reports
- preview CSV files with clickable finance links
- display generated chart images
- show background job status and logs

Outputs are written under timestamped folders:

```text
output/
  scans/<timestamp>/
  backtests/<timestamp>/
  charts/<timestamp>_<TICKER>/
```

`output/` and `cache/` are ignored by git.

## Project Structure

```text
market-signal-scanner.py        # CLI launcher
main.py                         # alternate launcher
run_app.sh                      # one-command macOS/local launcher
Market Signal Scanner.command   # double-click macOS launcher
config.example.yaml             # public example config
config.yaml                     # local editable config
requirements.txt
market_signal_scanner/
  api/server.py                 # FastAPI GUI backend
  web/                          # local browser UI assets
  cli.py                        # scan/backtest/chart command routing
  config_loader.py              # YAML config parsing and group expansion
  data_fetcher.py               # yfinance downloads and local cache
  indicators.py                 # signals and metadata columns
  scorer.py                     # transparent -100 to +100 scoring model
  reporter.py                   # scan CSV/Markdown reports
  backtester.py                 # historical simulation engine
  charting.py                   # technical chart and chart report generation
```

## macOS Quick Start

For the easiest local setup on a Mac, use one of the launchers included in the repo.

Double-click:

```text
Market Signal Scanner.command
```

Or run from Terminal:

```bash
./run_app.sh
```

The launcher will:

- create `.venv` if it does not exist
- install or update dependencies from `requirements.txt`
- create `config.yaml` from `config.example.yaml` if needed
- start the local GUI server
- open `http://127.0.0.1:8000` in your browser

If macOS says the command file is not allowed to run, enable execution once:

```bash
chmod +x run_app.sh "Market Signal Scanner.command"
```

## Manual Installation

Requires Python 3.9+.

```bash
git clone https://github.com/YOUR_USERNAME/market-signal-scanner.git
cd market-signal-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your local config:

```bash
cp config.example.yaml config.yaml
```

Then edit `config.yaml` with your tickers and settings.

## Run The GUI Manually

```bash
python -m market_signal_scanner.api.server
```

Open:

```text
http://127.0.0.1:8000
```

The GUI is the easiest way to run scans/backtests/charts and inspect generated reports.

To stop the GUI server, either click **Shutdown Server** in the sidebar or press `Ctrl+C` in the terminal where the server is running.

## CLI Usage

### Current Scan

```bash
python market-signal-scanner.py scan --config config.yaml --output ./output
```

`scan` is the default, so this also works:

```bash
python market-signal-scanner.py --config config.yaml --output ./output
```

Skip fundamentals for a faster technical-only scan:

```bash
python market-signal-scanner.py scan --config config.yaml --output ./output --skip-fundamentals
```

Scan outputs:

- `ranked_signals.csv`
- `top_buy_candidates.csv`
- `top_sell_candidates.csv`
- `portfolio_report.md`

### Backtest

```bash
python market-signal-scanner.py backtest --config config.yaml --output ./output
```

Backtest outputs:

- `backtest_summary.csv`
- `backtest_equity_curve.csv`
- `backtest_trades.csv`
- `backtest_holdings.csv`
- `backtest_rebalance_scores.csv`
- `backtest_report.md`

Backtests intentionally use technical signals only by default. Current fundamentals from free sources are not point-in-time historical fundamentals, so using them historically would create look-ahead bias.

### Ticker Chart

```bash
python market-signal-scanner.py chart --ticker AAPL --config config.yaml --output ./output
```

Useful chart options:

```bash
python market-signal-scanner.py chart --ticker BTC-USD --period 1y --interval 1d --lookback 180 --ma 20,50,200
python market-signal-scanner.py chart --ticker MSFT --chart-type line --no-macd --no-rsi
python market-signal-scanner.py chart --ticker NVDA --period 60d --interval 1h --lookback 240
```

Chart outputs:

- `<TICKER>_technical_chart.png`
- `<TICKER>_signals.csv`
- `chart_report.md`

## Configuration

The app is intentionally config-driven. Tickers should be edited in `config.yaml`, not hardcoded in source code.

Important sections:

- `tickers`: manual ticker universe
- `groups`: optional universe expansion
- `limits`: max tickers and minimum market cap filter
- `runtime`: caching, workers, price interval/period, fundamentals behavior
- `backtest`: start/end dates, contributions, rebalance frequency, costs, slippage, benchmark

Price interval examples:

```yaml
runtime:
  price_interval: "1d"
  price_period: "2y"
```

Intraday example:

```yaml
runtime:
  price_interval: "1h"
  price_period: "60d"
```

Changing interval changes signal meaning. For example, SMA 50 means 50 daily bars with `1d`, but 50 hourly bars with `1h`.

## CSV Link Columns

Signal CSV outputs include:

- `entity_name`
- `yahoo_finance_url`
- `google_finance_url`
- `tradingview_url`

Spreadsheet apps usually make URL columns clickable. The web GUI also renders URL cells as clickable links in CSV previews.

## Data And Limitations

- `yfinance` is a free data source and can have missing, delayed, adjusted, or rate-limited data.
- Broken tickers are skipped and logged.
- Fundamentals are skipped for crypto tickers.
- Missing data is left missing; the app should not fabricate fundamentals.
- Backtests are simulations, not predictions.
- Support/resistance and trendline detection are heuristic chart annotations, not guarantees.
- Transaction cost and slippage assumptions are configurable but simplified.

## GitHub Publishing Checklist

Before pushing:

```bash
git status
```

Expected tracked files should include source, docs, requirements, `.gitignore`, `LICENSE`, and config examples. Generated/local folders should stay ignored:

```text
.venv/
cache/
output/
__pycache__/
```

Suggested first commit:

```bash
git add .gitignore LICENSE README.md requirements.txt config.example.yaml config.yaml main.py market-signal-scanner.py market_signal_scanner
git commit -m "Initial market signal scanner app"
```

Then create a GitHub repo and push:

```bash
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/market-signal-scanner.git
git push -u origin main
```

## License

MIT. See [LICENSE](LICENSE).

## Disclaimer

This software is provided for educational and analytical purposes only. It does not provide financial advice, investment advice, trading advice, or guarantees of future performance. You are responsible for your own research and decisions.

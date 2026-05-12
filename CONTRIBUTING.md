# Contributing

Thanks for helping improve `market-signal-scanner`.

The goal is to make a practical, local-first research cockpit for small long-term investors. Good contributions should make the app clearer, safer, more explainable, or more reliable.

## Good First Contributions

- clearer explanations for novice investors
- better chart or Opportunity Map interactions
- additional technical indicators with plain-English descriptions
- better source extraction for Agent, News Summary, or Trend Catcher
- improved tests around config editing and report generation
- UI polish that makes risk, valuation, and evidence type easier to understand

## Development Setup

```bash
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

## Before Opening A Pull Request

Run the lightweight checks:

```bash
bash -n run_app.sh
PYTHONPYCACHEPREFIX=/tmp/market_signal_scanner_pycache python -m py_compile market_signal_scanner/api/server.py market_signal_scanner/charting.py market_signal_scanner/cli.py market_signal_scanner/agent_researcher.py market_signal_scanner/trend_catcher.py
node --check market_signal_scanner/web/app.js
git diff --check
```

If you change scanner logic, also run a small scan from the GUI or CLI.

## Contribution Rules

- Do not commit `output/`, `cache/`, `.venv/`, logs, or local secrets.
- Do not hardcode a personal ticker universe in source code.
- Keep ticker lists user-editable through `config/config.yaml`.
- Never fabricate market data, fundamentals, source timestamps, or LLM evidence.
- Preserve source links and generated timestamps in reports.
- Favor concise final reports with links to detailed logs.
- Add explanations or tooltips when a feature uses advanced investing terms.

## Investor Safety Standard

This app should slow users down before risky decisions.

Features should help users distinguish:

- technical analysis from price/volume data
- fundamental analysis from business and valuation data
- news analysis from fresh external sources
- watchlist candidates from buy recommendations

When in doubt, make the app more transparent and less hype-driven.

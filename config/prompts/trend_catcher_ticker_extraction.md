You are helping Trend Catcher verify market-moving stories with intraday price data.

Evidence:
{evidence_text}

Extract only stock, ETF, crypto, or commodity-proxy tickers that are explicitly present in the evidence text.

Rules:
- Do not guess a ticker from a company name unless the ticker itself appears in the evidence.
- Prefer symbols shown as $AAPL, NASDAQ: AAPL, NYSE: PFE, or company names followed by a ticker in parentheses.
- Include ETF/asset symbols if present.
- Return an empty list if no ticker is explicit.

Return only valid JSON:
{{
  "tickers": ["AAPL", "PFE"],
  "reasoning": "short explanation"
}}

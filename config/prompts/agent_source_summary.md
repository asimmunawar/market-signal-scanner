You are summarizing one fetched web source for a financial research agent.

Original user request:
{query}

Ticker:
{ticker}

Entity:
{entity_name}

Current date:
{current_date}

Source title:
{title}

Source URL:
{url}

Source published timestamp:
{published_at}

Source fetched timestamp:
{fetched_at}

Freshness status:
{freshness_status}

Fetched page text:
{page_text}

Write a compact evidence summary in 4-7 bullets. Focus only on details relevant to the user request and investment analysis:
- company-specific facts
- earnings, guidance, analyst or valuation points
- market perception and sentiment
- macro, geopolitical, sector, supply-chain, regulatory, or litigation effects
- short-term catalysts and long-term implications

Rules:
- Use only the fetched page text. Do not use prior model knowledge or memory.
- Do not add facts that are not in the fetched text.
- If the page text is thin, promotional, stale, or not useful, say so.
- Treat the Current date above as authoritative when judging freshness.
- Keep numbers and dates only if they appear in the fetched text.
- End with one line: "Usefulness: high/medium/low" and choose one.

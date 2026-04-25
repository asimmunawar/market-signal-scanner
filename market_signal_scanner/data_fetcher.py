from __future__ import annotations

import logging
import pickle
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf
from tqdm import tqdm


LOGGER = logging.getLogger(__name__)


class Cache:
    def __init__(self, cache_dir: str | Path) -> None:
        self.root = Path(cache_dir)
        self.prices_dir = self.root / "prices"
        self.fundamentals_dir = self.root / "fundamentals"
        self.prices_dir.mkdir(parents=True, exist_ok=True)
        self.fundamentals_dir.mkdir(parents=True, exist_ok=True)

    def is_fresh(self, path: Path, max_age_seconds: int) -> bool:
        if not path.exists():
            return False
        age = time.time() - path.stat().st_mtime
        return age <= max_age_seconds

    def price_path(self, ticker: str, interval: str = "1d", period: str = "2y") -> Path:
        return self.prices_dir / f"{safe_name(ticker)}_{safe_name(interval)}_{safe_name(period)}.pkl"

    def fundamentals_path(self, ticker: str) -> Path:
        return self.fundamentals_dir / f"{safe_name(ticker)}.pkl"

    def read_pickle(self, path: Path) -> Any:
        with path.open("rb") as handle:
            return pickle.load(handle)

    def write_pickle(self, path: Path, value: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as handle:
            pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)


def safe_name(ticker: str) -> str:
    return ticker.replace("/", "_").replace(":", "_")


def fetch_price_history(
    tickers: list[str],
    cache: Cache,
    refresh_hours: int,
    period: str = "2y",
    interval: str = "1d",
    retries: int = 2,
) -> dict[str, pd.DataFrame]:
    max_age = refresh_hours * 3600
    fresh: dict[str, pd.DataFrame] = {}
    stale: list[str] = []

    for ticker in tickers:
        path = cache.price_path(ticker, interval, period)
        if cache.is_fresh(path, max_age):
            try:
                frame = cache.read_pickle(path)
                if validate_price_frame(frame):
                    fresh[ticker] = frame
                    continue
            except Exception as exc:
                LOGGER.warning("Ignoring unreadable price cache for %s: %s", ticker, exc)
        stale.append(ticker)

    if stale:
        LOGGER.info("Downloading historical prices for %d tickers", len(stale))
        downloaded = _download_prices_batched(stale, period=period, interval=interval, retries=retries)
        for ticker, frame in downloaded.items():
            if validate_price_frame(frame):
                cache.write_pickle(cache.price_path(ticker, interval, period), frame)
                fresh[ticker] = frame

    return fresh


def _download_prices_batched(tickers: list[str], period: str, interval: str, retries: int) -> dict[str, pd.DataFrame]:
    for attempt in range(retries + 1):
        try:
            raw = yf.download(
                tickers=tickers,
                period=period,
                interval=interval,
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
            parsed = split_downloaded_prices(raw, tickers)
            if parsed:
                return parsed
        except Exception as exc:
            LOGGER.warning("Batch download attempt %d failed: %s", attempt + 1, exc)
        time.sleep(min(2**attempt, 8))

    LOGGER.warning("Falling back to one-by-one price downloads")
    result: dict[str, pd.DataFrame] = {}
    for ticker in tqdm(tickers, desc="prices"):
        frame = _download_one_price(ticker, period=period, interval=interval, retries=retries)
        if validate_price_frame(frame):
            result[ticker] = frame
    return result


def _download_one_price(ticker: str, period: str, interval: str, retries: int) -> pd.DataFrame:
    for attempt in range(retries + 1):
        try:
            return yf.download(
                tickers=ticker,
                period=period,
                interval=interval,
                auto_adjust=False,
                threads=False,
                progress=False,
            )
        except Exception as exc:
            LOGGER.warning("Price download failed for %s attempt %d: %s", ticker, attempt + 1, exc)
            time.sleep(min(2**attempt, 8))
    return pd.DataFrame()


def split_downloaded_prices(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    if raw.empty:
        return {}

    result: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        first_level = set(raw.columns.get_level_values(0))
        second_level = set(raw.columns.get_level_values(1))
        ticker_first = any(t in first_level for t in tickers)
        for ticker in tickers:
            try:
                frame = raw[ticker] if ticker_first else raw.xs(ticker, axis=1, level=1)
                frame = standardize_price_frame(frame)
                if validate_price_frame(frame):
                    result[ticker] = frame
            except Exception:
                continue
    else:
        ticker = tickers[0] if len(tickers) == 1 else ""
        frame = standardize_price_frame(raw)
        if ticker and validate_price_frame(frame):
            result[ticker] = frame
    return result


def standardize_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    clean = frame.copy()
    clean.columns = [str(col).title().replace(" ", "") for col in clean.columns]
    if "Adjclose" in clean.columns:
        clean = clean.rename(columns={"Adjclose": "Adj Close"})
    clean = clean.dropna(how="all")
    return clean


def validate_price_frame(frame: Any) -> bool:
    return isinstance(frame, pd.DataFrame) and not frame.empty and "Close" in frame.columns and len(frame.dropna(subset=["Close"])) > 60


def fetch_fundamentals(
    tickers: list[str],
    cache: Cache,
    refresh_days: int,
    workers: int,
    retries: int = 2,
) -> dict[str, dict[str, Any]]:
    max_age = refresh_days * 86400
    result: dict[str, dict[str, Any]] = {}
    stale: list[str] = []

    for ticker in tickers:
        path = cache.fundamentals_path(ticker)
        if cache.is_fresh(path, max_age):
            try:
                result[ticker] = cache.read_pickle(path)
                continue
            except Exception as exc:
                LOGGER.warning("Ignoring unreadable fundamentals cache for %s: %s", ticker, exc)
        stale.append(ticker)

    if not stale:
        return result

    LOGGER.info("Downloading fundamentals for %d tickers", len(stale))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_fetch_one_fundamental, ticker, retries): ticker for ticker in stale}
        for future in tqdm(as_completed(futures), total=len(futures), desc="fundamentals"):
            ticker = futures[future]
            try:
                info = future.result()
                cache.write_pickle(cache.fundamentals_path(ticker), info)
                result[ticker] = info
            except Exception as exc:
                LOGGER.warning("Skipping fundamentals for %s: %s", ticker, exc)
                result[ticker] = {}
    return result


def _fetch_one_fundamental(ticker: str, retries: int) -> dict[str, Any]:
    if ticker.endswith("-USD"):
        return {}
    for attempt in range(retries + 1):
        try:
            info = yf.Ticker(ticker).get_info()
            return info if isinstance(info, dict) else {}
        except Exception as exc:
            LOGGER.debug("Fundamental fetch failed for %s attempt %d: %s", ticker, attempt + 1, exc)
            time.sleep(min(2**attempt, 8))
    return {}

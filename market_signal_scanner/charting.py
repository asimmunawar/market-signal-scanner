from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from market_signal_scanner.indicators import compute_signals, macd, rsi
from market_signal_scanner.scorer import score_universe


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChartOptions:
    ticker: str
    chart_type: str = "candle"
    lookback: int = 180
    moving_averages: tuple[int, ...] = (20, 50, 100, 200)
    show_support_resistance: bool = True
    show_bollinger: bool = True
    show_volume: bool = True
    show_rsi: bool = True
    show_macd: bool = True


@dataclass
class ChartResult:
    output_dir: Path
    chart_path: Path
    report_path: Path
    signals: dict[str, Any]


def build_interactive_chart_payload(
    prices: pd.DataFrame,
    options: ChartOptions,
) -> dict[str, Any]:
    full_frame = prepare_price_frame(prices)
    if full_frame.empty or len(full_frame) < 30:
        raise ValueError(f"Not enough price history to chart {options.ticker}")

    frame = full_frame.tail(max(30, options.lookback)).copy()
    overlays = build_chart_overlays(full_frame, frame, options)
    scored = score_universe(pd.DataFrame([compute_signals(options.ticker, full_frame, {})]))
    signals = scored.iloc[0].to_dict() if not scored.empty else compute_signals(options.ticker, full_frame, {})
    latest = frame.iloc[-1]
    previous_close = frame["Close"].iloc[-2] if len(frame) > 1 else np.nan
    change = float(latest["Close"] - previous_close) if pd.notna(previous_close) else 0.0
    change_pct = float(change / previous_close * 100) if pd.notna(previous_close) and previous_close else 0.0
    return {
        "ticker": options.ticker,
        "chart_type": options.chart_type,
        "rows": [
            {
                "date": index.isoformat(),
                "open": none_if_nan(row["Open"]),
                "high": none_if_nan(row["High"]),
                "low": none_if_nan(row["Low"]),
                "close": none_if_nan(row["Close"]),
                "volume": none_if_nan(row["Volume"]),
                **{name: none_if_nan(series.loc[index]) for name, series in overlays["series"].items()},
            }
            for index, row in frame.iterrows()
        ],
        "levels": overlays["levels"],
        "trendlines": overlays["trendlines"],
        "signals": json_safe_dict(signals),
        "summary": {
            "last_close": none_if_nan(latest["Close"]),
            "change": none_if_nan(change),
            "change_pct": none_if_nan(change_pct),
            "last_volume": none_if_nan(latest.get("Volume", np.nan)),
            "start": frame.index[0].isoformat(),
            "end": frame.index[-1].isoformat(),
            "bars": len(frame),
        },
    }


def build_chart_overlays(full_frame: pd.DataFrame, frame: pd.DataFrame, options: ChartOptions) -> dict[str, Any]:
    series: dict[str, pd.Series] = {}
    for window in options.moving_averages:
        if window > 1 and len(full_frame) >= window:
            series[f"sma_{window}"] = full_frame["Close"].rolling(window).mean().reindex(frame.index)
    if len(full_frame) >= 20:
        series["ema_20"] = full_frame["Close"].ewm(span=20, adjust=False).mean().reindex(frame.index)
    if len(full_frame) >= 50:
        series["ema_50"] = full_frame["Close"].ewm(span=50, adjust=False).mean().reindex(frame.index)
    if options.show_bollinger and len(full_frame) >= 20:
        mid = full_frame["Close"].rolling(20).mean().reindex(frame.index)
        std = full_frame["Close"].rolling(20).std().reindex(frame.index)
        series["bb_mid"] = mid
        series["bb_upper"] = mid + 2 * std
        series["bb_lower"] = mid - 2 * std
    if options.show_rsi:
        series["rsi_14"] = rsi(full_frame["Close"], 14).reindex(frame.index)
    if options.show_macd:
        macd_line, signal_line, hist = macd(full_frame["Close"])
        series["macd"] = macd_line.reindex(frame.index)
        series["macd_signal"] = signal_line.reindex(frame.index)
        series["macd_hist"] = hist.reindex(frame.index)

    levels = support_resistance_levels(frame) if options.show_support_resistance else []
    trendlines = []
    if options.show_support_resistance:
        for line in diagonal_trendlines(frame):
            trendlines.append({
                "type": line["type"],
                "label": line["label"],
                "short_label": line["short_label"],
                "points": [
                    {"date": date.isoformat(), "value": none_if_nan(value)}
                    for date, value in zip(line["dates"], line["values"])
                ],
            })
    return {"series": series, "levels": json_safe_list(levels), "trendlines": trendlines}


def generate_chart_report(prices: pd.DataFrame, options: ChartOptions, output_base: str | Path) -> ChartResult:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    full_frame = prepare_price_frame(prices)
    frame = full_frame.tail(options.lookback).copy()
    if frame.empty or len(frame) < 30:
        raise ValueError(f"Not enough price history to chart {options.ticker}")

    scored = score_universe(pd.DataFrame([compute_signals(options.ticker, full_frame, {})]))
    signals = scored.iloc[0].to_dict() if not scored.empty else compute_signals(options.ticker, prepare_price_frame(prices), {})

    output_dir = Path(output_base) / "charts" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{safe_name(options.ticker)}"
    output_dir.mkdir(parents=True, exist_ok=True)

    panel_count = 1 + int(options.show_volume) + int(options.show_rsi) + int(options.show_macd)
    height_ratios = [4] + ([1] if options.show_volume else []) + ([1] if options.show_rsi else []) + ([1.2] if options.show_macd else [])
    fig, axes = plt.subplots(panel_count, 1, figsize=(16, 9), sharex=True, gridspec_kw={"height_ratios": height_ratios})
    if panel_count == 1:
        axes = [axes]
    ax_price = axes[0]
    x = mdates.date2num(frame.index.to_pydatetime())

    if options.chart_type == "line":
        ax_price.plot(frame.index, frame["Close"], color="#1f77b4", linewidth=1.8, label="Close")
    else:
        draw_candles(ax_price, x, frame, Rectangle)

    for window in options.moving_averages:
        if window > 1 and len(full_frame) >= window:
            ma = full_frame["Close"].rolling(window).mean().reindex(frame.index)
            ax_price.plot(frame.index, ma, linewidth=1.1, label=f"SMA {window}")

    if options.show_bollinger and len(full_frame) >= 20:
        mid = full_frame["Close"].rolling(20).mean().reindex(frame.index)
        std = full_frame["Close"].rolling(20).std().reindex(frame.index)
        upper = mid + 2 * std
        lower = mid - 2 * std
        ax_price.plot(frame.index, upper, color="#7f7f7f", linewidth=0.8, linestyle="--", label="Bollinger 20,2")
        ax_price.plot(frame.index, lower, color="#7f7f7f", linewidth=0.8, linestyle="--")
        ax_price.fill_between(frame.index, lower.to_numpy(), upper.to_numpy(), color="#7f7f7f", alpha=0.08)

    levels = []
    trendlines = []
    if options.show_support_resistance:
        levels = support_resistance_levels(frame)
        trendlines = diagonal_trendlines(frame)
        for line in trendlines:
            color = "#1b9e77" if line["type"] == "trend_support" else "#d95f02"
            ax_price.plot(line["dates"], line["values"], color=color, linestyle="--", linewidth=1.35, alpha=0.9, label=line["label"])
            ax_price.text(frame.index[-1], line["values"][-1], f" {line['short_label']} {line['values'][-1]:.2f}", color=color, va="center", fontsize=8)
        for level in levels:
            color = "#2ca02c" if level["type"] == "support" else "#d62728"
            ax_price.axhline(level["price"], color=color, linestyle=":", linewidth=1.1, alpha=0.85)
            ax_price.text(frame.index[-1], level["price"], f" {level['type']} {level['price']:.2f}", color=color, va="center", fontsize=8)

    if len(x) > 1:
        pad = max(1.0, np.median(np.diff(x)))
        ax_price.set_xlim(x[0] - pad, x[-1] + pad * 10)
    annotate_latest(ax_price, frame, signals)
    ax_price.set_title(f"{options.ticker} Technical Chart", loc="left", fontsize=15, fontweight="bold")
    ax_price.set_ylabel("Price")
    ax_price.grid(True, alpha=0.2)
    ax_price.legend(loc="upper left", fontsize=8, ncols=3)

    panel_index = 1
    if options.show_volume:
        ax_volume = axes[panel_index]
        colors = np.where(frame["Close"] >= frame["Open"], "#2ca02c", "#d62728")
        ax_volume.bar(frame.index, frame["Volume"], color=colors, alpha=0.45, width=0.8)
        vol_avg = full_frame["Volume"].rolling(20).mean().reindex(frame.index)
        ax_volume.plot(frame.index, vol_avg, color="#444444", linewidth=1, label="20-bar avg volume")
        ax_volume.set_ylabel("Volume")
        ax_volume.grid(True, alpha=0.2)
        ax_volume.legend(loc="upper left", fontsize=8)
        panel_index += 1

    if options.show_rsi:
        ax_rsi = axes[panel_index]
        rsi_line = rsi(full_frame["Close"], 14).reindex(frame.index)
        ax_rsi.plot(frame.index, rsi_line, color="#9467bd", linewidth=1.2, label="RSI 14")
        ax_rsi.axhline(70, color="#d62728", linestyle="--", linewidth=0.9)
        ax_rsi.axhline(30, color="#2ca02c", linestyle="--", linewidth=0.9)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel("RSI")
        ax_rsi.grid(True, alpha=0.2)
        ax_rsi.legend(loc="upper left", fontsize=8)
        panel_index += 1

    if options.show_macd:
        ax_macd = axes[panel_index]
        macd_line, signal_line, hist = macd(full_frame["Close"])
        macd_line = macd_line.reindex(frame.index)
        signal_line = signal_line.reindex(frame.index)
        hist = hist.reindex(frame.index)
        hist_colors = np.where(hist >= 0, "#2ca02c", "#d62728")
        ax_macd.bar(frame.index, hist, color=hist_colors, alpha=0.35, width=0.8, label="MACD hist")
        ax_macd.plot(frame.index, macd_line, color="#1f77b4", linewidth=1.1, label="MACD")
        ax_macd.plot(frame.index, signal_line, color="#ff7f0e", linewidth=1.1, label="Signal")
        ax_macd.axhline(0, color="#444444", linewidth=0.8)
        ax_macd.set_ylabel("MACD")
        ax_macd.grid(True, alpha=0.2)
        ax_macd.legend(loc="upper left", fontsize=8)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()

    chart_path = output_dir / f"{safe_name(options.ticker)}_technical_chart.png"
    fig.savefig(chart_path, dpi=160, bbox_inches="tight")
    plt.close(fig)

    signals_path = output_dir / f"{safe_name(options.ticker)}_signals.csv"
    pd.DataFrame([signals]).to_csv(signals_path, index=False)
    report_path = output_dir / "chart_report.md"
    report_path.write_text(build_chart_report(options, signals, levels, trendlines), encoding="utf-8")
    return ChartResult(output_dir, chart_path, report_path, signals)


def none_if_nan(value: Any) -> float | int | str | None:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        if not np.isfinite(value):
            return None
        return float(value)
    return value


def json_safe_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {str(key): none_if_nan(value) for key, value in values.items()}


def json_safe_list(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json_safe_dict(value) for value in values]


def prepare_price_frame(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.copy().sort_index()
    frame.index = pd.to_datetime(frame.index).tz_localize(None)
    for column in ["Open", "High", "Low", "Close", "Volume"]:
        if column not in frame.columns:
            if column == "Volume":
                frame[column] = 0
            else:
                frame[column] = frame["Close"]
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["Open", "High", "Low", "Close"])


def draw_candles(ax, x: np.ndarray, frame: pd.DataFrame, rectangle_cls) -> None:
    if len(x) > 1:
        width = max(0.2, min(0.8, np.median(np.diff(x)) * 0.7))
    else:
        width = 0.6
    for date_num, (_, row) in zip(x, frame.iterrows()):
        open_price = row["Open"]
        high = row["High"]
        low = row["Low"]
        close = row["Close"]
        color = "#2ca02c" if close >= open_price else "#d62728"
        ax.vlines(date_num, low, high, color=color, linewidth=0.8)
        lower = min(open_price, close)
        height = abs(close - open_price)
        if height == 0:
            height = max(close * 0.0005, 0.01)
        ax.add_patch(rectangle_cls((date_num - width / 2, lower), width, height, facecolor=color, edgecolor=color, alpha=0.75))


def support_resistance_levels(frame: pd.DataFrame, window: int = 5, max_levels: int = 6) -> list[dict[str, Any]]:
    highs = frame["High"]
    lows = frame["Low"]
    pivots: list[dict[str, Any]] = []
    for i in range(window, len(frame) - window):
        high_slice = highs.iloc[i - window : i + window + 1]
        low_slice = lows.iloc[i - window : i + window + 1]
        if highs.iloc[i] == high_slice.max():
            pivots.append({"type": "resistance", "price": float(highs.iloc[i]), "date": frame.index[i]})
        if lows.iloc[i] == low_slice.min():
            pivots.append({"type": "support", "price": float(lows.iloc[i]), "date": frame.index[i]})

    latest = float(frame["Close"].iloc[-1])
    tolerance = latest * 0.0125
    clustered: list[dict[str, Any]] = []
    for pivot in sorted(pivots, key=lambda p: abs(p["price"] - latest)):
        if any(abs(level["price"] - pivot["price"]) <= tolerance for level in clustered):
            continue
        pivot = dict(pivot)
        pivot["distance_pct"] = pivot["price"] / latest - 1
        clustered.append(pivot)
        if len(clustered) >= max_levels:
            break
    return sorted(clustered, key=lambda p: p["price"])


def diagonal_trendlines(frame: pd.DataFrame, window: int = 5, min_points: int = 3) -> list[dict[str, Any]]:
    pivots = pivot_points(frame, window)
    lines: list[dict[str, Any]] = []
    for pivot_type, line_type, label, short_label in (
        ("low", "trend_support", "Diagonal support", "trend support"),
        ("high", "trend_resistance", "Diagonal resistance", "trend resistance"),
    ):
        points = [point for point in pivots if point["pivot_type"] == pivot_type]
        recent = points[-8:]
        if len(recent) < min_points:
            continue
        candidate = fit_trendline(frame, recent, line_type, label, short_label)
        if candidate is not None:
            lines.append(candidate)
    return lines


def pivot_points(frame: pd.DataFrame, window: int = 5) -> list[dict[str, Any]]:
    highs = frame["High"]
    lows = frame["Low"]
    points: list[dict[str, Any]] = []
    for i in range(window, len(frame) - window):
        high_slice = highs.iloc[i - window : i + window + 1]
        low_slice = lows.iloc[i - window : i + window + 1]
        if highs.iloc[i] == high_slice.max():
            points.append({"pivot_type": "high", "price": float(highs.iloc[i]), "date": frame.index[i], "index": i})
        if lows.iloc[i] == low_slice.min():
            points.append({"pivot_type": "low", "price": float(lows.iloc[i]), "date": frame.index[i], "index": i})
    return points


def fit_trendline(
    frame: pd.DataFrame,
    points: list[dict[str, Any]],
    line_type: str,
    label: str,
    short_label: str,
) -> dict[str, Any] | None:
    x_points = np.array([point["index"] for point in points], dtype=float)
    y_points = np.array([point["price"] for point in points], dtype=float)
    if len(np.unique(x_points)) < 2:
        return None
    slope, intercept = np.polyfit(x_points, y_points, 1)
    fitted = slope * x_points + intercept
    residuals = y_points - fitted
    latest_price = float(frame["Close"].iloc[-1])
    tolerance = max(latest_price * 0.035, np.nanstd(frame["Close"].pct_change().dropna()) * latest_price * 2)
    if np.nanmean(np.abs(residuals)) > tolerance:
        return None

    x_all = np.arange(len(frame), dtype=float)
    values = slope * x_all + intercept
    if line_type == "trend_support":
        anchor_adjustment = np.percentile(frame["Low"].to_numpy() - values, 12)
    else:
        anchor_adjustment = np.percentile(frame["High"].to_numpy() - values, 88)
    values = values + anchor_adjustment
    if not np.isfinite(values).all():
        return None

    slope_text = "rising" if slope > 0 else "falling" if slope < 0 else "flat"
    return {
        "type": line_type,
        "label": f"{label} ({slope_text})",
        "short_label": short_label,
        "dates": list(frame.index),
        "values": values.tolist(),
        "slope_per_bar": float(slope),
        "points_used": len(points),
    }


def annotate_latest(ax, frame: pd.DataFrame, signals: dict[str, Any]) -> None:
    latest_price = frame["Close"].iloc[-1]
    latest_date = frame.index[-1]
    score = signals.get("score")
    recommendation = signals.get("recommendation", "")
    label = f"Close {latest_price:.2f}"
    if score is not None:
        label += f" | Score {float(score):.1f} {recommendation}"
    ax.scatter([latest_date], [latest_price], color="#111111", s=24, zorder=5)
    ax.annotate(label, xy=(latest_date, latest_price), xytext=(10, 10), textcoords="offset points", fontsize=9, bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "#999999", "alpha": 0.9})


def build_chart_report(options: ChartOptions, signals: dict[str, Any], levels: list[dict[str, Any]], trendlines: list[dict[str, Any]]) -> str:
    positives, negatives = explain_signals(signals)
    last_price = as_float(signals.get("last_price"))
    chart_read = plain_english_chart_read(signals)
    lines = [
        f"# {options.ticker} Chart Report",
        "",
        f"Generated: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z%z')}",
        "",
        "This is a **technical analysis** report. It looks at price, trend, momentum, volatility, drawdown, volume, and chart levels. It does **not** include fresh news analysis or deep fundamental analysis. It is not financial advice.",
        "",
        "## How To Use This Report",
        "",
        "- Use it to understand whether the ticker is technically extended, weak, or in a constructive trend.",
        "- Do **not** buy only because the chart looks strong. Check fundamentals and recent news before making a decision.",
        "- If RSI is high or price is far above nearby levels, consider waiting for a calmer entry instead of chasing.",
        "- If you already own it, use the caution signals to decide whether the original thesis still deserves confidence.",
        "",
        "## Plain-English Read",
        "",
        chart_read,
        "",
        "## Snapshot",
        "",
        f"- Last price: {fmt_number(signals.get('last_price'))}",
        f"- Score: {fmt_number(signals.get('score'))} — scanner score from -100 to +100; higher means stronger combined signals after risk penalties.",
        f"- Recommendation: {signals.get('recommendation', 'N/A')} — score bucket, not a command to buy or sell.",
        f"- RSI 14: {fmt_number(signals.get('rsi_14'))} — short-term momentum gauge; above 70 can mean overbought.",
        f"- Annualized volatility: {fmt_pct(signals.get('volatility_annual'))} — how bumpy the asset has been; higher means harder to hold.",
        f"- Max drawdown: {fmt_pct(signals.get('max_drawdown'))} — largest historical fall from a prior high in the measured data.",
        "",
        "## Nearby Price Reference Levels",
        "",
        "These are historical pivot areas. They are **not guaranteed floors or ceilings**. A resistance level below the current price often means price already broke above an old ceiling; that old area may become a retest zone.",
        "",
    ]
    if levels:
        lines.extend(level_lines(levels[:6], last_price))
    else:
        lines.append("- No clear nearby pivot levels found in the selected lookback window.")

    lines.extend(["", "## Diagonal Trendlines", ""])
    if trendlines:
        for line in trendlines[:3]:
            slope = "rising" if line["slope_per_bar"] > 0 else "falling"
            meaning = "possible rising support" if line["type"] == "trend_support" else "possible resistance path"
            lines.append(f"- {line['label']} ({slope}): currently near {line['values'][-1]:.2f}. This is a {meaning}, fitted from {line['points_used']} pivot points. Slope: {line['slope_per_bar']:+.4f} price units per bar.")
    else:
        lines.append("- No reliable diagonal trendlines found from recent pivots.")

    lines.extend([
        "",
        "## Positive Signals",
        "",
        *(f"- {item}" for item in positives[:4]),
        "",
        "## Negative / Caution Signals",
        "",
        *(f"- {item}" for item in negatives[:4]),
        "",
        "## What This Does Not Tell You",
        "",
        "- It does not tell you whether the company is fundamentally cheap or expensive.",
        "- It does not tell you whether today’s move is caused by earnings, product news, macro news, or hype.",
        "- It does not know your cost basis, taxes, time horizon, or portfolio concentration.",
        "",
        "## Suggested Next Step",
        "",
        f"- If you are considering a decision on {options.ticker}, run **News Summary** or **Ask Agent Before Deciding** from the GUI to add source-grounded news and fundamental context.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def plain_english_chart_read(signals: dict[str, Any]) -> str:
    score = as_float(signals.get("score"), 0) or 0
    rsi_value = as_float(signals.get("rsi_14"))
    price_vs_200 = as_float(signals.get("price_vs_sma_200"), 0) or 0
    volatility = as_float(signals.get("volatility_annual"), 0) or 0
    parts: list[str] = []
    if score >= 60:
        parts.append("The technical setup is strong, but still needs valuation and news confirmation.")
    elif score >= 30:
        parts.append("The technical setup is constructive enough to research, but not strong enough to skip due diligence.")
    elif score <= -30:
        parts.append("The technical setup is weak and deserves caution or sell-review if you already hold it.")
    else:
        parts.append("The technical setup is mixed; avoid forcing a decision from the chart alone.")
    if price_vs_200 > 0.05:
        parts.append("Price is above the long-term moving average, which usually supports the trend.")
    elif price_vs_200 < -0.05:
        parts.append("Price is below the long-term moving average, which can signal trend weakness.")
    if rsi_value is not None and rsi_value >= 70:
        parts.append("RSI is elevated, so chasing immediately may carry FOMO risk.")
    elif rsi_value is not None and rsi_value <= 35:
        parts.append("RSI is low, which can mean weakness or a possible oversold bounce; news and fundamentals matter.")
    if volatility >= 0.40:
        parts.append("Volatility is high, so position size should usually be smaller.")
    return " ".join(parts)


def level_lines(levels: list[dict[str, Any]], last_price: float | None) -> list[str]:
    lines: list[str] = []
    for level in levels:
        price = float(level["price"])
        distance = as_float(level.get("distance_pct"), 0.0) or 0.0
        if last_price is None:
            location = "nearby"
            implication = "watch how price reacts if it returns to this area"
        elif price < last_price:
            location = "below current price"
            implication = "possible retest/support area"
        elif price > last_price:
            location = "above current price"
            implication = "possible overhead resistance"
        else:
            location = "at current price"
            implication = "active decision area"
        pivot_type = str(level["type"]).title()
        lines.append(f"- {pivot_type} pivot at {price:.2f} ({distance:+.2%}, {location}) — {implication}.")
    return lines


def explain_signals(signals: dict[str, Any]) -> tuple[list[str], list[str]]:
    positives: list[str] = []
    negatives: list[str] = []
    if as_float(signals.get("price_vs_sma_50"), 0) > 0:
        positives.append("Price is above the 50-period moving average.")
    else:
        negatives.append("Price is below the 50-period moving average.")
    if as_float(signals.get("price_vs_sma_200"), 0) > 0:
        positives.append("Price is above the 200-period moving average.")
    else:
        negatives.append("Price is below the 200-period moving average.")
    if signals.get("golden_cross") is True:
        positives.append("SMA 50 is above SMA 200, a constructive long-term trend signal.")
    if signals.get("death_cross") is True:
        negatives.append("SMA 50 is below SMA 200, a bearish long-term trend signal.")
    rsi_value = as_float(signals.get("rsi_14"))
    if rsi_value is not None:
        if 45 <= rsi_value <= 65:
            positives.append("RSI is in a balanced momentum zone.")
        elif rsi_value > 72:
            negatives.append("RSI is elevated, so the asset may be overbought in the short term.")
        elif rsi_value < 30:
            negatives.append("RSI is depressed, showing weak momentum or oversold conditions.")
    if signals.get("macd_bullish") is True:
        positives.append("MACD is above its signal line.")
    else:
        negatives.append("MACD is below its signal line.")
    if as_float(signals.get("volatility_annual"), 0) > 0.45:
        negatives.append("Annualized volatility is high, increasing position risk.")
    if as_float(signals.get("max_drawdown"), 0) < -0.35:
        negatives.append("Historical drawdown in the selected history is severe.")
    return positives or ["No prominent positive signals."], negatives or ["No prominent caution signals."]


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(number):
        return default
    return number


def fmt_number(value: Any) -> str:
    number = as_float(value)
    return "N/A" if number is None else f"{number:,.2f}"


def fmt_pct(value: Any) -> str:
    number = as_float(value)
    return "N/A" if number is None else f"{number:.2%}"


def safe_name(value: str) -> str:
    return value.replace("/", "_").replace(":", "_").replace(" ", "_")

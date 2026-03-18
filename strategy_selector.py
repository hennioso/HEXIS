"""
Strategy Selector Agent – Auto-selects the best strategy per symbol.

Scores each strategy (0-10) based on current market conditions and
returns the highest-scoring one with a human-readable reasoning log.

Scoring criteria:
  SNIPER  – Price at/near Fib 0.882 with clear swing structure
  LSOB    – Active liquidity sweep + valid orderblock present
  SCALP   – RSI extreme + Bollinger Band edge + volume spike
  TREND   – Strong multi-EMA alignment (EMA9 > EMA21 > EMA50)
"""

import logging

from indicators import (
    klines_to_df,
    add_fib_indicators,
    add_scalp_indicators,
    add_indicators,
    ema,
)
from strategy_lsob import check_lsob_signal, OB_LOOKBACK, OB_SCAN_DEPTH
from strategy_fvg import check_fvg_signal

log = logging.getLogger(__name__)

# Minimum score advantage needed to switch away from current strategy
# (prevents rapid flapping between strategies with similar scores)
MIN_SWITCH_DELTA = 2


def _score_sniper(df5m, df15m=None) -> tuple[int, list[str]]:
    """
    High score when price is at the Fib 0.882 retracement level
    with a clear and wide enough swing structure.
    """
    score = 0
    reasons: list[str] = []

    try:
        df = add_fib_indicators(df5m.copy(), lookback=50)
        if len(df) < 52:
            return 0, ["insufficient data"]

        prev = df.iloc[-2]
        price = float(df.iloc[-1]["close"])

        for col in ("swing_high", "swing_low"):
            if prev[col] != prev[col]:
                return 0, ["no swing structure (NaN)"]

        swing_high = float(prev["swing_high"])
        swing_low  = float(prev["swing_low"])
        rng        = swing_high - swing_low

        if rng / price < 0.015:
            return 0, [f"range too narrow ({rng/price*100:.2f}%)"]

        # Clear swing structure exists
        score += 2
        reasons.append(f"swing range {rng/price*100:.1f}%")

        # Proximity to Fib 0.882
        long_entry  = swing_high - 0.882 * rng
        short_entry = swing_low  + 0.882 * rng
        long_dist   = abs(price - long_entry)  / long_entry
        short_dist  = abs(price - short_entry) / short_entry
        best_dist   = min(long_dist, short_dist)
        direction   = "long" if long_dist < short_dist else "short"

        if best_dist <= 0.005:
            score += 6
            reasons.append(f"price AT Fib 0.882 ({best_dist*100:.2f}% {direction})")
        elif best_dist <= 0.008:
            score += 3
            reasons.append(f"price NEAR Fib 0.882 ({best_dist*100:.2f}% {direction})")
        elif best_dist <= 0.015:
            score += 1
            reasons.append(f"approaching Fib 0.882 ({best_dist*100:.2f}%)")

        # EMA50 trend filter — must align with direction (hard block, matches entry check)
        if df15m is not None and len(df15m) >= 55:
            try:
                ema50 = float(ema(df15m["close"], 50).iloc[-1])
                aligned = (direction == "long"  and price > ema50) or \
                          (direction == "short" and price < ema50)
                if aligned:
                    score += 2
                    reasons.append("EMA50 aligned with direction")
                else:
                    # Entry check enforces this strictly — don't surface counter-trend signals
                    return 0, ["EMA50 counter-trend (blocked)"]
            except Exception:
                pass

    except Exception as e:
        return 0, [f"error: {e}"]

    return score, reasons


def _score_lsob(klines) -> tuple[int, list[str]]:
    """
    Highest score when a complete LSOB setup is active (price in OB zone).
    Medium score when a recent sweep occurred but price not yet in OB.
    """
    score = 0
    reasons: list[str] = []

    try:
        signal = check_lsob_signal(klines, lookback=OB_LOOKBACK, scan_depth=OB_SCAN_DEPTH)
        if signal:
            score = 9
            reasons.append(
                f"ACTIVE OB {signal.direction.upper()} | "
                f"zone [{signal.ob_bottom:.4f}–{signal.ob_top:.4f}] | "
                f"TP {signal.tp_price:.4f}"
            )
            return score, reasons

        # No active signal — scan for recent sweep (setup forming)
        df = klines_to_df(klines)
        if len(df) < OB_LOOKBACK + 10:
            return 0, ["insufficient data"]

        # Check last 10 candles for a sweep event (wick past prior swing, close back inside)
        for i in range(2, min(10, len(df) - OB_LOOKBACK)):
            idx   = -(i + 1)
            c     = df.iloc[idx]
            prior = df.iloc[:idx]
            if len(prior) < OB_LOOKBACK:
                break

            prior_high = float(prior["high"].iloc[-OB_LOOKBACK:].max())
            prior_low  = float(prior["low"].iloc[-OB_LOOKBACK:].min())

            bearish_sweep = (float(c["high"]) > prior_high * 1.001 and
                             float(c["close"]) < prior_high)
            bullish_sweep = (float(c["low"])  < prior_low  * 0.999 and
                             float(c["close"]) > prior_low)

            if bearish_sweep or bullish_sweep:
                sweep_type = "bearish" if bearish_sweep else "bullish"
                # Recent sweeps score higher — score decays with age
                score = max(2, 5 - i)
                reasons.append(f"{sweep_type} sweep {i} candles ago (OB forming)")
                break

    except Exception as e:
        return 0, [f"error: {e}"]

    return score, reasons


def _score_scalp(df5m) -> tuple[int, list[str]]:
    """
    High score when RSI is extreme AND price is at a Bollinger Band edge,
    signalling potential mean-reversion.
    """
    score = 0
    reasons: list[str] = []

    try:
        df = add_scalp_indicators(df5m.copy())
        if len(df) < 25:
            return 0, ["insufficient data"]

        last = df.iloc[-1]

        rsi_val  = float(last["rsi_scalp"])
        bb_pct   = float(last["bb_pct"])
        vol_r    = float(last["vol_ratio"])
        bb_width = float(last["bb_width"])

        # RSI extreme
        if rsi_val < 25 or rsi_val > 75:
            score += 4
            reasons.append(f"RSI extreme ({rsi_val:.1f})")
        elif rsi_val < 35 or rsi_val > 65:
            score += 2
            reasons.append(f"RSI elevated ({rsi_val:.1f})")

        # Bollinger Band edge
        if bb_pct < 0.10 or bb_pct > 0.90:
            score += 3
            reasons.append(f"BB edge ({bb_pct:.2f})")
        elif bb_pct < 0.20 or bb_pct > 0.80:
            score += 1
            reasons.append(f"BB near edge ({bb_pct:.2f})")

        # Volume confirmation
        if vol_r > 2.0:
            score += 2
            reasons.append(f"high volume ({vol_r:.1f}x avg)")
        elif vol_r > 1.5:
            score += 1
            reasons.append(f"elevated volume ({vol_r:.1f}x avg)")

        # Penalize if BB is very narrow (no volatility = no mean reversion)
        close_price = float(df.iloc[-1]["close"])
        if close_price > 0 and bb_width / close_price < 0.005:
            score -= 2
            reasons.append("BB too narrow (low vol penalty)")

    except Exception as e:
        return 0, [f"error: {e}"]

    return score, reasons


def _score_fvg(klines, klines_15m=None) -> tuple[int, list[str]]:
    """
    High score when price is currently inside an unfilled Fair Value Gap —
    a structural imbalance left by a strong impulse move.
    Max score: 9 (4 base + 3 gap size + 2 freshness).
    """
    score = 0
    reasons: list[str] = []

    try:
        signal = check_fvg_signal(klines, klines_15m=klines_15m)
        if not signal:
            return 0, ["no active FVG"]

        # Base: price is inside a valid, unfilled gap
        score += 4
        reasons.append(
            f"price IN {signal.direction.upper()} FVG "
            f"[{signal.fvg_bottom:.4f}–{signal.fvg_top:.4f}]"
        )

        # Gap size quality
        if signal.gap_pct >= 0.010:
            score += 3
            reasons.append(f"large gap ({signal.gap_pct*100:.1f}%)")
        elif signal.gap_pct >= 0.005:
            score += 2
            reasons.append(f"medium gap ({signal.gap_pct*100:.1f}%)")
        else:
            score += 1
            reasons.append(f"small gap ({signal.gap_pct*100:.1f}%)")

        # Freshness — newer gaps are more reliable
        if signal.candle_age <= 5:
            score += 2
            reasons.append(f"fresh gap ({signal.candle_age} candles ago)")
        elif signal.candle_age <= 12:
            score += 1
            reasons.append(f"recent gap ({signal.candle_age} candles ago)")

    except Exception as e:
        return 0, [f"error: {e}"]

    return score, reasons


def _score_trend(df5m, df15m=None) -> tuple[int, list[str]]:
    """
    High score when multiple EMAs are aligned (9 > 21 > 50),
    indicating a strong directional trend.
    """
    score = 0
    reasons: list[str] = []

    try:
        df = add_indicators(df5m.copy(), fast_ema=9, slow_ema=21)
        if len(df) < 55:
            return 0, ["insufficient data"]

        price    = float(df.iloc[-1]["close"])
        ema9     = float(df.iloc[-1]["ema_fast"])
        ema21    = float(df.iloc[-1]["ema_slow"])
        ema50_5m = float(ema(df["close"], 50).iloc[-1])

        bull = ema9 > ema21 > ema50_5m
        bear = ema9 < ema21 < ema50_5m

        if bull or bear:
            score += 4
            tag = "BULL" if bull else "BEAR"
            reasons.append(f"EMA9/21/50 aligned ({tag})")
        elif (ema9 > ema21) or (ema9 < ema21):
            score += 2
            reasons.append("EMA9/21 aligned (partial)")

        # 15m confirmation
        if df15m is not None and len(df15m) >= 25:
            try:
                df15 = add_indicators(df15m.copy(), fast_ema=9, slow_ema=21)
                ema9_15  = float(df15.iloc[-1]["ema_fast"])
                ema21_15 = float(df15.iloc[-1]["ema_slow"])
                if (bull and ema9_15 > ema21_15) or (bear and ema9_15 < ema21_15):
                    score += 2
                    reasons.append("15m EMA confirms direction")
            except Exception:
                pass

        # RSI between 45-65 (trending, not exhausted)
        rsi_val = float(df.iloc[-1]["rsi"])
        if 45 <= rsi_val <= 65:
            score += 1
            reasons.append(f"RSI in trend zone ({rsi_val:.1f})")

    except Exception as e:
        return 0, [f"error: {e}"]

    return score, reasons


def select_strategy(
    symbol: str,
    klines_5m: list[dict],
    klines_15m: list[dict] | None = None,
    current_strategy: str = "sniper",
) -> tuple[str, dict]:
    """
    Evaluates all four strategies and returns the best one.

    Returns:
      (chosen_strategy, scores_dict)
      scores_dict = { 'sniper': (score, reasons), 'lsob': ..., ... }
    """
    df5m  = klines_to_df(klines_5m)
    df15m = klines_to_df(klines_15m) if klines_15m else None

    scores = {
        "sniper": _score_sniper(df5m, df15m),
        "lsob":   _score_lsob(klines_5m),
        "scalp":  _score_scalp(df5m),
        "trend":  _score_trend(df5m, df15m),
        "fvg":    _score_fvg(klines_5m, klines_15m),
    }

    best_strategy = max(scores, key=lambda s: scores[s][0])
    best_score    = scores[best_strategy][0]
    current_score = scores.get(current_strategy, (0, []))[0]

    # Hysteresis: only switch if new strategy is significantly better
    if best_strategy != current_strategy:
        if best_score - current_score < MIN_SWITCH_DELTA:
            best_strategy = current_strategy
            log.debug(
                f"{symbol} AUTO: keeping {current_strategy.upper()} "
                f"(delta {best_score - current_score} < {MIN_SWITCH_DELTA})"
            )
        else:
            log.info(
                f"{symbol} AUTO: switching {current_strategy.upper()} → "
                f"{best_strategy.upper()} "
                f"(score {current_score} → {best_score} | "
                f"{', '.join(scores[best_strategy][1][:2])})"
            )
    else:
        log.debug(
            f"{symbol} AUTO: {best_strategy.upper()} score={best_score} "
            f"| {', '.join(scores[best_strategy][1][:2])}"
        )

    return best_strategy, {k: {"score": v[0], "reasons": v[1]} for k, v in scores.items()}

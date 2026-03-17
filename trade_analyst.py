"""
Trade Analyst – AI-powered trade performance analyzer.

Uses Claude (claude-opus-4-6 with adaptive thinking) to evaluate strategy
performance and recommend conservative parameter adjustments:

  - MIN_OPEN_SCORE threshold (Agent Scanner gate, range 5–9)
  - Per-symbol strategy preferences (auto / sniper / lsob / scalp / trend)

Safety rules:
  - Never adjusts while any position is open
  - MIN_OPEN_SCORE is hard-capped to [5, 9]
  - All decisions are logged to analyst.log with full reasoning
  - Minimum MIN_CLOSED_TRADES needed before analysis runs
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Optional

import anthropic
from pydantic import BaseModel

import config
import database as db
import strategy_scanner
import strategy_state


def _compute_streak(recent_trades: list[dict]) -> dict:
    """Current win/loss streak from the most recent closed trades."""
    closed = [t for t in recent_trades if t["status"] != "open" and t.get("pnl_usdt") is not None]
    if not closed:
        return {"type": "none", "length": 0}
    streak_type = "win" if closed[0]["pnl_usdt"] > 0 else "loss"
    length = 0
    for t in closed:
        if (t["pnl_usdt"] > 0) == (streak_type == "win"):
            length += 1
        else:
            break
    return {"type": streak_type, "length": length}

log = logging.getLogger("TradeAnalyst")

# ---- Configuration -----------------------------------------------------------
ANALYSIS_INTERVAL_MINUTES = 240  # How often to run (minutes)
STARTUP_DELAY_MINUTES     = 30   # Wait before first analysis after bot start
MIN_CLOSED_TRADES         = 5    # Minimum closed trades required to analyze
SCORE_BOUNDS              = (5, 9)


# ---- Pydantic output schema --------------------------------------------------

class ScoreAdjustment(BaseModel):
    new_value: int    # Target MIN_OPEN_SCORE
    reason: str


class SymbolAdjustment(BaseModel):
    symbol:   str    # e.g. "BTCUSDT"
    strategy: str    # "auto" | "sniper" | "lsob" | "scalp" | "trend"
    reason:   str


class AnalysisResult(BaseModel):
    summary:            str
    should_adjust:      bool
    wait_reason:        Optional[str] = None
    score_adjustment:   Optional[ScoreAdjustment] = None
    symbol_adjustments: list[SymbolAdjustment] = []


# ---- Prompt builder ----------------------------------------------------------

def _build_context(
    stats: dict,
    recent_trades: list[dict],
    current_score: int,
    current_strategies: dict,
) -> str:
    """Assemble all bot performance data into a prompt string."""

    # Per-strategy PnL summary (closed trades only)
    strat_stats: dict[str, dict] = {}
    for t in recent_trades:
        if t["status"] == "open":
            continue
        strat = t.get("strategy") or t.get("trend_15m") or "unknown"
        if strat not in strat_stats:
            strat_stats[strat] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        pnl = t.get("pnl_usdt") or 0.0
        if pnl > 0:
            strat_stats[strat]["wins"] += 1
        else:
            strat_stats[strat]["losses"] += 1
        strat_stats[strat]["total_pnl"] = round(strat_stats[strat]["total_pnl"] + pnl, 4)

    # Per-symbol PnL summary (closed trades only)
    sym_stats: dict[str, dict] = {}
    for t in recent_trades:
        if t["status"] == "open":
            continue
        sym = t["symbol"]
        if sym not in sym_stats:
            sym_stats[sym] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        pnl = t.get("pnl_usdt") or 0.0
        if pnl > 0:
            sym_stats[sym]["wins"] += 1
        else:
            sym_stats[sym]["losses"] += 1
        sym_stats[sym]["total_pnl"] = round(sym_stats[sym]["total_pnl"] + pnl, 4)

    # Last 20 closed trades for pattern inspection
    last_20 = [
        {
            "symbol":    t["symbol"],
            "direction": t["direction"],
            "strategy":  t.get("strategy") or t.get("trend_15m", "?"),
            "pnl":       t.get("pnl_usdt"),
            "status":    t["status"],
            "entry":     t["entry_time"],
        }
        for t in recent_trades[:30]
        if t["status"] != "open"
    ][:20]

    # Drawdown metrics
    analytics   = db.get_analytics()
    drawdown    = analytics.get("drawdown", {})
    streak      = _compute_streak(recent_trades)

    return f"""You are a quantitative trading analyst evaluating HEXIS, an autonomous crypto futures agent on Bitunix.

## Agent Setup
- Symbols: {config.SYMBOLS}
- Leverage: {config.LEVERAGE}x | Risk per trade: {config.RISK_PER_TRADE*100:.0f}% of balance
- Agent Scanner: scores all (symbol × strategy) combos every 15s, opens order only if best score ≥ MIN_OPEN_SCORE

## Scoring System (max scores)
- SNIPER (Fibonacci 0.882 retracement): max 10
- LSOB (Liquidity Sweep Orderblock):    max  9
- SCALP (BB + RSI extremes + volume):   max  9
- FVG  (Fair Value Gap retest):         max  9
- TREND (EMA 9/21/50 alignment):        max  7
  → Note: TREND can never reach a threshold of 8+, so raising score to 8 excludes TREND entries.
  → FVG: price retesting an unfilled 3-candle imbalance zone in the trend direction.

## Current Configuration
- MIN_OPEN_SCORE: {current_score}/10
- Per-symbol strategies: {json.dumps(current_strategies, indent=2)}

## Overall Performance ({stats['total_trades']} closed trades)
- Win rate:        {stats['win_rate']}%
- Total PnL:       {stats['total_pnl']} USDT
- Avg win:         {stats['avg_win']} USDT
- Avg loss:        {stats['avg_loss']} USDT
- Best trade:      {stats['best_trade']} USDT | Worst: {stats['worst_trade']} USDT
- Long/Short ratio: {stats['long_trades']}/{stats['short_trades']}
- Max drawdown:    {drawdown.get('max_drawdown_usdt', 'N/A')} USDT
- Current streak:  {streak['length']} consecutive {streak['type']}s

## Per-Strategy Breakdown
{json.dumps(strat_stats, indent=2)}

## Per-Symbol Breakdown
{json.dumps(sym_stats, indent=2)}

## Last 20 Closed Trades
{json.dumps(last_20, indent=2)}

## Your Task
Analyze the performance data and produce recommendations for exactly two things:

1. **MIN_OPEN_SCORE** (currently {current_score}):
   - Raise by 1 if: win rate < 40%, or SL-hit rate is high, or too many marginal entries
   - Lower by 1 if: win rate > 60% with 15+ trades AND you suspect the threshold is filtering good setups
   - Keep as-is if: insufficient data (<10 closed trades per strategy) or performance is acceptable
   - Hard bounds: minimum 5, maximum 9

2. **Per-symbol strategy** (currently {json.dumps(current_strategies)}):
   - Pin a symbol to a specific strategy only if it consistently outperforms others (≥5 trades evidence)
   - Set back to "auto" if a pinned strategy is underperforming
   - No change if data is insufficient or mixed
   - Valid values: auto | sniper | lsob | scalp | trend | fvg

Be conservative. If in doubt, recommend no change. Stability is valuable.
Explain your reasoning clearly with reference to the numbers."""


# ---- Core analysis -----------------------------------------------------------

def _run_single_analysis(client: anthropic.Anthropic) -> None:
    """Run one analysis cycle and apply safe adjustments."""

    # Guard: skip if any position is open
    open_trades = [t for t in db.get_all_trades(limit=50) if t["status"] == "open"]
    if open_trades:
        log.info(f"Analysis skipped — {len(open_trades)} open position(s) active.")
        return

    stats = db.get_stats()
    if stats["total_trades"] < MIN_CLOSED_TRADES:
        log.info(
            f"Analysis skipped — only {stats['total_trades']} closed trades "
            f"(minimum: {MIN_CLOSED_TRADES})."
        )
        return

    recent_trades    = db.get_all_trades(limit=100)
    current_score    = strategy_scanner.MIN_OPEN_SCORE
    current_strats   = strategy_state.load()

    log.info(
        f"Analyzing {stats['total_trades']} closed trades | "
        f"Win rate: {stats['win_rate']}% | "
        f"PnL: {stats['total_pnl']} USDT"
    )

    prompt = _build_context(stats, recent_trades, current_score, current_strats)

    response = client.messages.parse(
        model="claude-opus-4-6",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=(
            "You are an expert quantitative trading analyst. "
            "Analyze crypto futures bot performance and recommend conservative, "
            "data-driven parameter adjustments. Be precise and concise."
        ),
        messages=[{"role": "user", "content": prompt}],
        output_format=AnalysisResult,
    )

    result: AnalysisResult | None = response.parsed_output
    if result is None:
        log.error("Claude returned no structured output — skipping.")
        return

    _log_to_file(result, current_score, current_strats)

    if not result.should_adjust:
        log.info(f"Analyst: no changes. {result.wait_reason or result.summary[:120]}")
        return

    log.info(f"Analyst summary: {result.summary[:200]}")

    # ---- Apply score adjustment ----
    if result.score_adjustment is not None:
        new_score = max(SCORE_BOUNDS[0], min(SCORE_BOUNDS[1], result.score_adjustment.new_value))
        if new_score != current_score:
            strategy_scanner.MIN_OPEN_SCORE = new_score
            log.info(
                f"Analyst: MIN_OPEN_SCORE {current_score} → {new_score} | "
                f"{result.score_adjustment.reason}"
            )
        else:
            log.info(f"Analyst: score stays at {current_score} (clamped or unchanged).")

    # ---- Apply symbol strategy adjustments ----
    valid_strategies = {"auto", "sniper", "lsob", "scalp", "trend", "fvg"}
    for adj in result.symbol_adjustments:
        if adj.symbol not in config.SYMBOLS:
            log.warning(f"Analyst: unknown symbol '{adj.symbol}' — skipping.")
            continue
        if adj.strategy not in valid_strategies:
            log.warning(f"Analyst: unknown strategy '{adj.strategy}' — skipping.")
            continue
        prev = strategy_state.get_strategy(adj.symbol)
        if prev != adj.strategy:
            strategy_state.set_strategy(adj.symbol, adj.strategy)
            log.info(
                f"Analyst: {adj.symbol} {prev.upper()} → {adj.strategy.upper()} | "
                f"{adj.reason}"
            )


def _log_to_file(result: AnalysisResult, prev_score: int, prev_strats: dict) -> None:
    """Append the analysis result to analyst.log."""
    try:
        with open("analyst.log", "a", encoding="utf-8") as f:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            f.write(f"\n{'='*60}\n  ANALYSIS  {ts}\n{'='*60}\n")
            f.write(f"Summary: {result.summary}\n\n")
            f.write(f"Adjustments applied: {result.should_adjust}\n")
            if result.wait_reason:
                f.write(f"Reason for no change: {result.wait_reason}\n")
            if result.score_adjustment:
                f.write(
                    f"Score: {prev_score} → {result.score_adjustment.new_value} "
                    f"| {result.score_adjustment.reason}\n"
                )
            for adj in result.symbol_adjustments:
                prev = prev_strats.get(adj.symbol, "?")
                f.write(f"  {adj.symbol}: {prev} → {adj.strategy} | {adj.reason}\n")
            f.write("\n")
    except Exception as e:
        log.warning(f"Could not write analyst.log: {e}")


# ---- Thread entry point ------------------------------------------------------

def run_analysis_loop(stop_event: threading.Event) -> None:
    """
    Background thread: waits STARTUP_DELAY_MINUTES, then analyzes every
    ANALYSIS_INTERVAL_MINUTES. Skips gracefully on any error.
    """
    log.info(
        f"Trade Analyst started — first run in {STARTUP_DELAY_MINUTES} min, "
        f"then every {ANALYSIS_INTERVAL_MINUTES} min."
    )

    # Initial startup delay
    stop_event.wait(STARTUP_DELAY_MINUTES * 60)
    if stop_event.is_set():
        return

    try:
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    except Exception as e:
        log.error(f"Could not create Anthropic client: {e}. Trade Analyst disabled.")
        return

    while not stop_event.is_set():
        try:
            _run_single_analysis(client)
        except anthropic.AuthenticationError:
            log.error("Invalid ANTHROPIC_API_KEY — Trade Analyst disabled.")
            return
        except Exception as e:
            log.error(f"Analysis error: {e}", exc_info=True)

        stop_event.wait(ANALYSIS_INTERVAL_MINUTES * 60)

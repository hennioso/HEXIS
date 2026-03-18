"""
Multi-AI Trade Analyst – parallel analysis by Claude, GPT-4o, and Gemini.

Each AI independently analyzes the same trade data with a different angle:
  - Claude  (Opus 4.6)    — conservative quant analyst, stability focus
  - GPT-4o  (OpenAI)      — risk management specialist, drawdown focus
  - Gemini  (2.0 Flash)   — pattern analyst, time/symbol consistency focus

Results are combined via a consensus mechanism:
  - MIN_OPEN_SCORE: rounded average of all adjustment recommendations
  - Symbol strategies: applied only when ≥2 AIs agree on the same change

Safety rules:
  - Never adjusts while any position is open
  - MIN_OPEN_SCORE is hard-capped to [5, 9]
  - All individual + consensus decisions logged to analyst.log
  - Minimum MIN_CLOSED_TRADES needed before analysis runs
  - GPT-4o and Gemini are optional — system works with Claude alone
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import anthropic
from pydantic import BaseModel

import config
import database as db
import strategy_scanner
import strategy_state

# Optional AI providers — gracefully absent if not installed or not configured
try:
    import openai as _openai_module
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

try:
    from google import genai as _genai_module
    from google.genai import types as _genai_types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False


log = logging.getLogger("TradeAnalyst")

# ---- Configuration -----------------------------------------------------------
ANALYSIS_INTERVAL_MINUTES = 240
STARTUP_DELAY_MINUTES     = 30
MIN_CLOSED_TRADES         = 5
SCORE_BOUNDS              = (5, 9)


# ---- Pydantic output schema --------------------------------------------------

class ScoreAdjustment(BaseModel):
    new_value: int
    reason:    str


class SymbolAdjustment(BaseModel):
    symbol:   str
    strategy: str
    reason:   str


class AnalysisResult(BaseModel):
    summary:            str
    should_adjust:      bool
    wait_reason:        Optional[str] = None
    score_adjustment:   Optional[ScoreAdjustment] = None
    symbol_adjustments: list[SymbolAdjustment] = []


# ---- System prompts (each AI gets a different angle) -------------------------

_SYSTEM_CLAUDE = (
    "You are an expert quantitative trading analyst. "
    "Analyze crypto futures agent performance and recommend conservative, "
    "data-driven parameter adjustments. Prioritize stability — if in doubt, "
    "recommend no change."
)

_SYSTEM_OPENAI = (
    "You are a risk management specialist for algorithmic crypto futures trading. "
    "Your primary focus is capital preservation: drawdown reduction, avoiding "
    "over-trading, and protecting gains. Be conservative — only recommend changes "
    "when the risk/reward evidence is clear."
)

_SYSTEM_GEMINI = (
    "You are a quantitative pattern analyst for crypto futures trading. "
    "Your focus is identifying consistent edges: which strategies and symbols "
    "show repeatable win rates, which time windows underperform, and whether "
    "the current parameter configuration matches observed patterns. "
    "Only recommend changes backed by statistical evidence."
)


# ---- Helper functions --------------------------------------------------------

def _compute_streak(recent_trades: list[dict]) -> dict:
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


def _build_context(
    stats: dict,
    recent_trades: list[dict],
    current_score: int,
    current_strategies: dict,
) -> str:
    """Assemble all agent performance data into a shared prompt string."""

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

    analytics    = db.get_analytics()
    drawdown     = analytics.get("drawdown", {})
    streak       = _compute_streak(recent_trades)
    hourly_stats = db.get_hourly_stats()

    return f"""You are analyzing HEXIS, an autonomous crypto futures agent on Bitunix.

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
  → TREND can never reach threshold 8+; raising to 8 excludes TREND entries.

## Current Configuration
- MIN_OPEN_SCORE: {current_score}/10
- Per-symbol strategies: {json.dumps(current_strategies, indent=2)}

## Overall Performance ({stats['total_trades']} closed trades)
- Win rate:         {stats['win_rate']}%
- Total PnL:        {stats['total_pnl']} USDT
- Avg win:          {stats['avg_win']} USDT
- Avg loss:         {stats['avg_loss']} USDT
- Best / Worst:     {stats['best_trade']} / {stats['worst_trade']} USDT
- Long/Short ratio: {stats['long_trades']}/{stats['short_trades']}
- Max drawdown:     {drawdown.get('max_drawdown_usdt', 'N/A')} USDT
- Current streak:   {streak['length']} consecutive {streak['type']}s

## Per-Strategy Breakdown
{json.dumps(strat_stats, indent=2)}

## Per-Symbol Breakdown
{json.dumps(sym_stats, indent=2)}

## Last 20 Closed Trades
{json.dumps(last_20, indent=2)}

## Hourly Performance (UTC)
{json.dumps(hourly_stats, indent=2) if hourly_stats else "Insufficient data."}

## Your Task
Recommend adjustments for exactly two parameters:

1. **MIN_OPEN_SCORE** (currently {current_score}):
   - Raise by 1 if: win rate < 40%, high SL-hit rate, or too many marginal entries
   - Lower by 1 if: win rate > 60% with 15+ trades AND threshold may be too restrictive
   - Keep if: data is insufficient or performance is acceptable
   - Bounds: min 5, max 9

2. **Per-symbol strategy** ({json.dumps(current_strategies)}):
   - Pin to a strategy only with ≥5 trades of consistent evidence
   - Revert to "auto" if pinned strategy underperforms
   - Valid: auto | sniper | lsob | scalp | trend | fvg

3. **Time-of-day** (advisory only — not auto-applied):
   - Flag UTC hours with consistently low win rate (≥5 trades) in your summary

Be conservative. Stability is valuable. Explain reasoning with reference to numbers."""


# ---- Per-AI analysis functions -----------------------------------------------

def _analyse_with_claude(client: anthropic.Anthropic, prompt: str) -> AnalysisResult:
    response = client.messages.parse(
        model="claude-opus-4-6",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=_SYSTEM_CLAUDE,
        messages=[{"role": "user", "content": prompt}],
        output_format=AnalysisResult,
    )
    result = response.parsed_output
    if result is None:
        raise ValueError("Claude returned no structured output")
    return result


def _analyse_with_openai(client, prompt: str) -> AnalysisResult:
    response = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _SYSTEM_OPENAI},
            {"role": "user",   "content": prompt},
        ],
        response_format=AnalysisResult,
    )
    result = response.choices[0].message.parsed
    if result is None:
        raise ValueError("GPT-4o returned no structured output")
    return result


def _analyse_with_gemini(client, prompt: str) -> AnalysisResult:
    full_prompt = f"{_SYSTEM_GEMINI}\n\n{prompt}"
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=full_prompt,
        config=_genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AnalysisResult,
        ),
    )
    return AnalysisResult.model_validate_json(response.text)


# ---- Consensus ---------------------------------------------------------------

def _build_consensus(
    results: list[tuple[str, AnalysisResult]],
    current_score: int,
) -> AnalysisResult:
    """
    Combines multiple AI analyses into a single consensus recommendation.
      - MIN_OPEN_SCORE: rounded average of all non-None adjustments
      - Symbol adjustments: require majority agreement (≥2 AIs, same strategy)
    """
    if len(results) == 1:
        name, r = results[0]
        r.summary = f"[{name}] {r.summary}"
        return r

    # --- Score consensus ---
    score_votes = [
        (name, r.score_adjustment.new_value)
        for name, r in results
        if r.should_adjust and r.score_adjustment is not None
    ]
    consensus_score: Optional[ScoreAdjustment] = None
    if score_votes:
        avg = round(sum(v for _, v in score_votes) / len(score_votes))
        names = ", ".join(n for n, _ in score_votes)
        consensus_score = ScoreAdjustment(
            new_value=avg,
            reason=f"Consensus ({len(score_votes)}/{len(results)} analysts agree, avg={avg}): {names}",
        )

    # --- Symbol consensus ---
    # Collect votes: {symbol: Counter({strategy: count})}
    sym_votes: dict[str, Counter] = {}
    sym_reasons: dict[str, dict[str, list[str]]] = {}
    for name, r in results:
        if not r.should_adjust:
            continue
        for adj in r.symbol_adjustments:
            sym_votes.setdefault(adj.symbol, Counter())[adj.strategy] += 1
            sym_reasons.setdefault(adj.symbol, {}).setdefault(adj.strategy, []).append(
                f"{name}: {adj.reason[:80]}"
            )

    majority = max(2, len(results) // 2 + 1)
    consensus_syms: list[SymbolAdjustment] = []
    for symbol, counts in sym_votes.items():
        best_strat, votes = counts.most_common(1)[0]
        if votes >= majority:
            reasons_str = " | ".join(sym_reasons[symbol].get(best_strat, [])[:2])
            consensus_syms.append(SymbolAdjustment(
                symbol=symbol,
                strategy=best_strat,
                reason=f"Majority ({votes}/{len(results)}): {reasons_str}",
            ))

    should_adjust = bool(consensus_score or consensus_syms)

    # Build combined summary
    individual = "\n".join(
        f"  [{n}]: {r.summary[:150]}" for n, r in results
    )
    summary = f"Multi-AI consensus ({len(results)} analysts):\n{individual}"

    return AnalysisResult(
        summary=summary,
        should_adjust=should_adjust,
        score_adjustment=consensus_score,
        symbol_adjustments=consensus_syms,
    )


# ---- Orchestrator ------------------------------------------------------------

def _run_analysis_cycle(
    claude_client: anthropic.Anthropic,
    openai_client,
    gemini_model,
) -> None:
    """Run one full analysis cycle: all AIs in parallel → consensus → apply."""

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

    recent_trades  = db.get_all_trades(limit=100)
    current_score  = strategy_scanner.MIN_OPEN_SCORE
    current_strats = strategy_state.load()

    analysts_available = ["Claude"]
    if openai_client:  analysts_available.append("GPT-4o")
    if gemini_model:   analysts_available.append("Gemini")

    log.info(
        f"Analyzing {stats['total_trades']} closed trades | "
        f"Win rate: {stats['win_rate']}% | PnL: {stats['total_pnl']} USDT | "
        f"Analysts: {', '.join(analysts_available)}"
    )

    prompt = _build_context(stats, recent_trades, current_score, current_strats)

    # ---- Run all AIs in parallel ----
    tasks: dict[str, callable] = {"Claude": lambda: _analyse_with_claude(claude_client, prompt)}
    if openai_client:
        tasks["GPT-4o"] = lambda: _analyse_with_openai(openai_client, prompt)
    if gemini_model:
        _gm = gemini_model  # capture for lambda
        tasks["Gemini"] = lambda: _analyse_with_gemini(_gm, prompt)

    results: list[tuple[str, AnalysisResult]] = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                results.append((name, result))
                log.info(
                    f"[{name}] adjust={result.should_adjust} | "
                    f"score→{result.score_adjustment.new_value if result.score_adjustment else 'no change'} | "
                    f"{result.summary[:100]}"
                )
            except Exception as e:
                log.warning(f"[{name}] analysis failed: {e}")

    if not results:
        log.error("All analysts failed — skipping this cycle.")
        return

    # ---- Build consensus ----
    consensus = _build_consensus(results, current_score)
    _log_to_file(results, consensus, current_score, current_strats)

    if not consensus.should_adjust:
        log.info(f"Consensus: no changes recommended.")
        return

    log.info(f"Consensus summary: {consensus.summary[:300]}")

    # ---- Apply score adjustment ----
    if consensus.score_adjustment is not None:
        new_score = max(SCORE_BOUNDS[0], min(SCORE_BOUNDS[1], consensus.score_adjustment.new_value))
        if new_score != current_score:
            strategy_scanner.MIN_OPEN_SCORE = new_score
            log.info(
                f"Consensus: MIN_OPEN_SCORE {current_score} → {new_score} | "
                f"{consensus.score_adjustment.reason}"
            )
        else:
            log.info(f"Consensus: score stays at {current_score} (clamped or unchanged).")

    # ---- Apply symbol strategy adjustments ----
    valid_strategies = {"auto", "sniper", "lsob", "scalp", "trend", "fvg"}
    for adj in consensus.symbol_adjustments:
        if adj.symbol not in config.SYMBOLS:
            log.warning(f"Consensus: unknown symbol '{adj.symbol}' — skipping.")
            continue
        if adj.strategy not in valid_strategies:
            log.warning(f"Consensus: unknown strategy '{adj.strategy}' — skipping.")
            continue
        prev = strategy_state.get_strategy(adj.symbol)
        if prev != adj.strategy:
            strategy_state.set_strategy(adj.symbol, adj.strategy)
            log.info(
                f"Consensus: {adj.symbol} {prev.upper()} → {adj.strategy.upper()} | "
                f"{adj.reason}"
            )


def _log_to_file(
    results: list[tuple[str, AnalysisResult]],
    consensus: AnalysisResult,
    prev_score: int,
    prev_strats: dict,
) -> None:
    """Append all individual analyses + consensus to analyst.log."""
    try:
        with open("analyst.log", "a", encoding="utf-8") as f:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            f.write(f"\n{'='*60}\n  MULTI-AI ANALYSIS  {ts}\n{'='*60}\n")

            # Individual AI results
            for name, r in results:
                f.write(f"\n--- {name} ---\n")
                f.write(f"Summary: {r.summary}\n")
                f.write(f"Adjust: {r.should_adjust}")
                if r.wait_reason:
                    f.write(f" | Wait: {r.wait_reason}")
                f.write("\n")
                if r.score_adjustment:
                    f.write(f"  Score: {prev_score} → {r.score_adjustment.new_value} | {r.score_adjustment.reason}\n")
                for adj in r.symbol_adjustments:
                    prev = prev_strats.get(adj.symbol, "?")
                    f.write(f"  {adj.symbol}: {prev} → {adj.strategy} | {adj.reason}\n")

            # Consensus
            f.write(f"\n--- CONSENSUS ---\n")
            f.write(f"Adjust: {consensus.should_adjust}\n")
            if consensus.score_adjustment:
                f.write(
                    f"  Score: {prev_score} → {consensus.score_adjustment.new_value} "
                    f"| {consensus.score_adjustment.reason}\n"
                )
            for adj in consensus.symbol_adjustments:
                prev = prev_strats.get(adj.symbol, "?")
                f.write(f"  {adj.symbol}: {prev} → {adj.strategy} | {adj.reason}\n")
            f.write("\n")
    except Exception as e:
        log.warning(f"Could not write analyst.log: {e}")


# ---- Thread entry point ------------------------------------------------------

def run_analysis_loop(stop_event: threading.Event) -> None:
    """
    Background thread: waits STARTUP_DELAY_MINUTES, then analyzes every
    ANALYSIS_INTERVAL_MINUTES. Initialises all available AI clients on startup.
    """
    log.info(
        f"Trade Analyst started — first run in {STARTUP_DELAY_MINUTES} min, "
        f"then every {ANALYSIS_INTERVAL_MINUTES} min."
    )

    stop_event.wait(STARTUP_DELAY_MINUTES * 60)
    if stop_event.is_set():
        return

    # ---- Initialise Claude (required) ----
    try:
        claude_client = anthropic.Anthropic()
    except Exception as e:
        log.error(f"Could not create Anthropic client: {e}. Trade Analyst disabled.")
        return

    # ---- Initialise GPT-4o (optional) ----
    openai_client = None
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if _OPENAI_AVAILABLE and openai_key:
        try:
            openai_client = _openai_module.OpenAI(api_key=openai_key)
            log.info("GPT-4o analyst: enabled.")
        except Exception as e:
            log.warning(f"GPT-4o init failed: {e}")
    elif not openai_key:
        log.info("GPT-4o analyst: disabled (OPENAI_API_KEY not set).")
    else:
        log.info("GPT-4o analyst: disabled (openai package not installed).")

    # ---- Initialise Gemini (optional) ----
    gemini_model = None
    gemini_key = os.getenv("GOOGLE_API_KEY", "")
    if _GEMINI_AVAILABLE and gemini_key:
        try:
            gemini_model = _genai_module.Client(api_key=gemini_key)
            log.info("Gemini analyst: enabled.")
        except Exception as e:
            log.warning(f"Gemini init failed: {e}")
    elif not gemini_key:
        log.info("Gemini analyst: disabled (GOOGLE_API_KEY not set).")
    else:
        log.info("Gemini analyst: disabled (google-genai package not installed).")

    while not stop_event.is_set():
        try:
            _run_analysis_cycle(claude_client, openai_client, gemini_model)
        except anthropic.AuthenticationError:
            log.error("Invalid ANTHROPIC_API_KEY — Trade Analyst disabled.")
            return
        except Exception as e:
            log.error(f"Analysis error: {e}", exc_info=True)

        stop_event.wait(ANALYSIS_INTERVAL_MINUTES * 60)

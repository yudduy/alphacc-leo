# Pareto oracle for LEO CCA evolution.
# Provides dense, continuous reward signal via weighted shortfall-to-target.
#
# Design:
#   - One-sided: only penalizes being WORSE than target, not better
#   - Weighted: util 50%, robustness 30%, RTT 20% (matches domain priority)
#   - Per-scenario gap analysis gives LLM targeted feedback
#
# Why not L2 distance: symmetric L2 penalizes exceeding the target, which
# would tell the LLM to reduce util from 99% to 97%. One-sided shortfall
# correctly says "you're already past the target on util, focus on robustness."

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class ParetoTarget:
    """A target point on the Pareto frontier to evolve toward."""
    name: str
    description: str
    target_util: float   # percentage (0-100), higher is better
    target_rtt: float    # milliseconds, lower is better
    target_robust: float  # percentage (0-100), higher is better


# Default targets calibrated to simulator capability (seeds=[42,7,99], 30-60s sims).
# Best seed CCA achieves ~94.9% util, ~97ms RTT, ~93.7% robustness.
# Targets are ~0.5-1pp stretch beyond best seed to give meaningful gradient
# without being unreachable. Real Starlink targets (97%+ util) require
# Mahimahi validation — our toy sim has a lower ceiling.
DEFAULT_TARGETS = [
    ParetoTarget(
        name="beat_all_seeds",
        description="Beat the best seed CCA on util AND rtt AND robustness simultaneously",
        target_util=95.5,
        target_rtt=93.0,
        target_robust=94.5,
    ),
    ParetoTarget(
        name="high_util_low_rtt",
        description="Max utilization with lowest RTT — the ideal operating point",
        target_util=95.0,
        target_rtt=80.0,
        target_robust=94.0,
    ),
    ParetoTarget(
        name="robust_high_util",
        description="High utilization with max robustness across all scenarios",
        target_util=94.0,
        target_rtt=110.0,
        target_robust=95.0,
    ),
]

# Dimension weights — matches domain priority:
#   util matters most (the whole point of CC), robustness second (LEO variability),
#   RTT third (latency is secondary to throughput for bulk transfer).
# These match the old fitness function's spirit (50/25/15 → 50/30/20).
WEIGHT_UTIL = 0.50
WEIGHT_ROBUST = 0.30
WEIGHT_RTT = 0.20

# Normalization denominators: how many raw units = "1 unit of shortfall"
# Chosen so that a CCA 10pp below target on util ≈ 10pp below on robust ≈ 30ms above on RTT
# (i.e., the range of "bad to good" in each dimension)
NORM_UTIL = 30.0    # 30pp range (70-100%)
NORM_RTT = 110.0    # 110ms range (40-150ms)
NORM_ROBUST = 40.0  # 40pp range (60-100%)

# Per-scenario targets — best achieved in simulator (seeds=[42,7,99], 30-60s).
# These are the best util and best rtt observed across ALL seed CCAs per scenario.
# NOT from real Starlink (our toy sim has a ~95% util ceiling vs real LeoCC's 97%).
# Using achievable targets ensures the LLM gets honest gradient signal.
SCENARIO_TARGETS = {
    "leo_steady": {"util": 95.0, "rtt": 97.0},
    "leo_handoff": {"util": 95.0, "rtt": 97.0},
    "leo_capacity_osc": {"util": 93.8, "rtt": 96.0},
    "leo_aggressive": {"util": 95.4, "rtt": 145.0},
    "leo_high_bw": {"util": 95.3, "rtt": 58.0},
    "leo_deep_buffer": {"util": 95.1, "rtt": 117.0},
}


def calibrate_targets_from_population(all_results: dict) -> None:
    """Recalibrate SCENARIO_TARGETS from actual population performance.

    Called after seeding to set targets based on best-achieved-per-scenario
    across all CCAs. This ensures targets are ambitious but reachable.

    Args:
        all_results: dict of cca_name -> {scenario_name -> {"result": SimResult}}
    """
    global SCENARIO_TARGETS
    for scenario in list(SCENARIO_TARGETS.keys()):
        best_util = 0.0
        best_rtt = float('inf')
        for cca_name, results in all_results.items():
            if scenario not in results:
                continue
            data = results[scenario]
            if data.get("error"):
                continue
            r = data["result"]
            u = r.utilization * 100
            rt = r.avg_latency * 1000 if r.avg_latency else float('inf')
            if u > best_util:
                best_util = u
            if rt < best_rtt:
                best_rtt = rt
        if best_util > 0:
            # Add stretch beyond best-achieved to ensure meaningful gradient
            SCENARIO_TARGETS[scenario] = {
                "util": round(best_util + 0.5, 1),   # +0.5pp stretch
                "rtt": round(best_rtt * 0.95, 0),    # -5% RTT stretch
            }


def compute_shortfall(
    med_util: float,
    med_rtt: float,
    robustness: float,
    target: ParetoTarget,
) -> float:
    """Compute weighted one-sided shortfall from CCA metrics to target.

    Only penalizes being WORSE than target:
    - util below target → shortfall
    - rtt above target → shortfall
    - robustness below target → shortfall
    Being better than target on any dimension contributes 0 shortfall.

    Returns value in [0, ~1.0] (0 = dominates target).
    """
    # Shortfall per dimension: max(0, gap) / normalization
    # util: higher is better, so shortfall = max(0, target - actual)
    s_util = max(0.0, target.target_util - med_util) / NORM_UTIL
    # rtt: lower is better, so shortfall = max(0, actual - target)
    s_rtt = max(0.0, med_rtt - target.target_rtt) / NORM_RTT
    # robust: higher is better
    s_robust = max(0.0, target.target_robust - robustness) / NORM_ROBUST

    # Weighted sum (not L2 — more interpretable, no cross-dimension interaction)
    shortfall = (
        WEIGHT_UTIL * s_util +
        WEIGHT_ROBUST * s_robust +
        WEIGHT_RTT * s_rtt
    )
    return shortfall


def shortfall_to_fitness(shortfall: float) -> float:
    """Convert shortfall to a [0, 1] fitness score.

    fitness = 1 / (1 + 5 * shortfall)
    Shortfall 0.00 → fitness 1.000 (dominates target)
    Shortfall 0.02 → fitness 0.909
    Shortfall 0.05 → fitness 0.800
    Shortfall 0.10 → fitness 0.667
    Shortfall 0.20 → fitness 0.500

    The 5x multiplier spreads the [0, 0.2] shortfall range across [0.5, 1.0]
    fitness, giving the LLM useful gradient in the competitive region.
    """
    return 1.0 / (1.0 + 5.0 * shortfall)


def compute_pareto_fitness(
    results: dict,
    target: Optional[ParetoTarget] = None,
) -> Tuple[float, Dict]:
    """Compute Pareto-oracle fitness from LEO benchmark results.

    Args:
        results: dict of scenario_name → {"result": SimResult, "error": ...}
        target: ParetoTarget to compute shortfall toward. Uses beat_leocc_all if None.

    Returns:
        (fitness, metrics_dict) where metrics_dict has med_util, med_rtt, robustness,
        shortfall, per_scenario gaps.
    """
    if target is None:
        target = DEFAULT_TARGETS[0]  # beat_leocc_all

    utils = []
    rtts = []
    error_count = 0
    per_scenario = {}
    for name, data in results.items():
        r = data["result"]
        if data.get("error"):
            error_count += 1
            per_scenario[name] = {"util": 0.0, "rtt": 1000.0, "gap_util": 0.0, "gap_rtt": 0.0, "error": True}
            continue
        util_pct = r.utilization * 100.0
        rtt_ms = (r.avg_latency * 1000) if r.avg_latency else 0
        utils.append(util_pct)
        rtts.append(rtt_ms)

        # Per-scenario gap analysis
        s_target = SCENARIO_TARGETS.get(name, {"util": 95.0, "rtt": 80.0})
        gap_util = s_target["util"] - util_pct  # positive = below target
        gap_rtt = rtt_ms - s_target["rtt"]       # positive = above target
        per_scenario[name] = {
            "util": util_pct,
            "rtt": rtt_ms,
            "gap_util": gap_util,
            "gap_rtt": gap_rtt,
        }

    if not utils:
        return 0.0, {}

    med_util = statistics.median(utils)
    med_rtt = statistics.median(rtts)
    # Robustness: min util across *successful* scenarios, with proportional error penalty.
    # Each error penalizes robustness by 15pp (proportional, not cliff).
    min_util = min(utils) - error_count * 15.0
    min_util = max(0.0, min_util)

    shortfall = compute_shortfall(med_util, med_rtt, min_util, target)
    fitness = shortfall_to_fitness(shortfall)

    # Find worst scenarios for LLM focus
    worst_by_gap = sorted(per_scenario.items(), key=lambda x: x[1]["gap_util"], reverse=True)

    metrics = {
        "med_util": med_util,
        "med_rtt": med_rtt,
        "robustness": min_util,
        "shortfall": shortfall,
        "fitness": fitness,
        "target": target.name,
        "per_scenario": per_scenario,
        "worst_scenarios": [s for s, _ in worst_by_gap[:3]],
    }
    return fitness, metrics


def format_pareto_context(metrics: dict, target: ParetoTarget) -> str:
    """Format Pareto oracle context for LLM prompts.

    Gives the LLM:
    1. Current position vs target (with shortfall breakdown)
    2. Per-scenario gap analysis (which scenarios to focus on)
    3. Specific numeric targets per scenario
    """
    if not metrics:
        return ""

    # Per-dimension shortfall breakdown for LLM
    s_util = max(0.0, target.target_util - metrics["med_util"])
    s_rtt = max(0.0, metrics["med_rtt"] - target.target_rtt)
    s_robust = max(0.0, target.target_robust - metrics["robustness"])

    lines = [
        "## Pareto Oracle — Shortfall to Target",
        f"Target: {target.name} — {target.description}",
        f"  Target:  util={target.target_util:.1f}%, rtt={target.target_rtt:.0f}ms, "
        f"robust={target.target_robust:.1f}%",
        f"  Current: util={metrics['med_util']:.1f}%, rtt={metrics['med_rtt']:.0f}ms, "
        f"robust={metrics['robustness']:.1f}%",
        f"  Shortfall: {metrics['shortfall']:.4f} (0 = dominates target)",
        f"  Fitness:   {metrics['fitness']:.4f}",
        "",
        "  Shortfall breakdown:",
    ]

    if s_util > 0:
        lines.append(f"    util:   {s_util:+.1f}pp below target (weight 50%)")
    else:
        lines.append(f"    util:   OK (at or above target)")
    if s_rtt > 0:
        lines.append(f"    rtt:    {s_rtt:+.0f}ms above target (weight 20%)")
    else:
        lines.append(f"    rtt:    OK (at or below target)")
    if s_robust > 0:
        lines.append(f"    robust: {s_robust:+.1f}pp below target (weight 30%)")
    else:
        lines.append(f"    robust: OK (at or above target)")

    lines.append("")
    lines.append("## Per-Scenario Gap Analysis (positive gap = below target)")

    for s_name in sorted(metrics.get("per_scenario", {}).keys()):
        s = metrics["per_scenario"][s_name]
        s_target = SCENARIO_TARGETS.get(s_name, {"util": 95.0, "rtt": 80.0})

        # Status indicator
        util_ok = s["gap_util"] <= 0
        rtt_ok = s["gap_rtt"] <= 0
        status = "OK" if (util_ok and rtt_ok) else "FOCUS"

        lines.append(
            f"  {s_name:20s}: util={s['util']:5.1f}% (target {s_target['util']:.0f}%, "
            f"gap {s['gap_util']:+.1f}pp) | "
            f"rtt={s['rtt']:5.0f}ms (target {s_target['rtt']:.0f}ms, "
            f"gap {s['gap_rtt']:+.0f}ms) [{status}]"
        )

    worst = metrics.get("worst_scenarios", [])
    if worst:
        lines.append(f"\n**Priority scenarios to improve**: {', '.join(worst)}")
        lines.append("Focus mutations on closing the util gap in these scenarios.")

    return "\n".join(lines)


def load_oracle_config(path: str = "output_frontier_benchmark/oracle_config.json") -> dict:
    """Load pre-computed oracle config from frontier benchmark."""
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}

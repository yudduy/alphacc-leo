"""CCA performance diagnosis for DGM evolution.

Replaces DGM's prompts/self_improvement_prompt.py for the CCA domain.
Generates problem statements that tell the LLM coding agent what to fix
in the CCA C source code.

The diagnosis model (o1 / o4-mini) analyzes per-trace results and produces
a structured improvement proposal, just like DGM's diagnose_problem().
"""

import json
import os
import random
from typing import Dict, List, Optional, Tuple

from alphacc.dgm_cca.cca_harness import DEFAULT_RESOLVE_THRESHOLD
from utils.common_utils import load_json_file

# ── CCA Agent Summary (replaces DGM's coding_agent_summary) ─────────

CCA_AGENT_SUMMARY = """# CCA Kernel Module Workspace

- **Main File**: `tcp_evolved.c` — Linux kernel congestion control module (~800 lines)
- **Build**: `make` (requires linux-headers)
- **Load**: `sudo insmod tcp_evolved.ko`
- **Test**: `iperf3 -c 100.64.0.1 -C evolved -t 10`
- **Unload**: `sudo rmmod tcp_evolved`
- **Parameters**: Runtime-tunable via /sys/module/tcp_evolved/parameters/

## CRITICAL: How to Edit Files

The file is ~800 lines of C. **ALWAYS use the `str_replace` command** to make targeted edits:

```
editor(command="str_replace", path="/path/to/tcp_evolved.c",
       old_str="exact text to find",
       new_str="replacement text")
```

**NEVER use `edit` command** on tcp_evolved.c — it requires the ENTIRE file content which will be truncated and break the build. Use `str_replace` for EVERY edit, no matter how large.

## Architecture

The CCA follows the tcp_congestion_ops interface:
- `.init` — per-connection initialization
- `.cong_control` — called per ACK (main control loop)
- `.ssthresh` — called on loss (return new ssthresh)

Key kernel types:
- `struct sock *sk` — socket
- `struct tcp_sock *tp` — TCP socket (tp->snd_cwnd, tp->srtt_us, etc.)
- `struct rate_sample *rs` — delivery rate sample (rs->rtt_us, rs->delivered, etc.)
- `inet_csk_ca(sk)` — per-connection private state

Fixed-point arithmetic: P_SCALE=8, P_UNIT=256.

## CRITICAL: Struct Size Constraint (BUILD_BUG_ON)

`struct leocc` currently uses ~100 of 112 bytes (ICSK_CA_PRIV_SIZE). You have
only **12 bytes free** (~3 u32 fields). Adding `struct minmax` (24 bytes) or
more than 3 u32 fields WILL cause BUILD_BUG_ON and a failed build.

**NEVER add struct minmax, arrays, or large sub-structs to struct leocc.**
To add state: reuse/repurpose existing bitfields, pack into existing u32
bitfield words, or replace unused fields. Prefer algorithmic changes that
reuse existing state over adding new state variables.
"""

# ── Diagnosis System Message ─────────────────────────────────────────

DIAGNOSE_SYSTEM_MESSAGE = """You are analyzing a Linux kernel congestion control algorithm (CCA) that is being evolved to maximize throughput on LEO satellite links (Starlink).

{cca_summary}

# Current CCA Implementation
----- CCA Source Code -----
{c_code}
----- CCA Source Code End -----

# Evolution Playbook (learned from prior generations)
{playbook}

# Reference Library (top performers from archive)
{reference_library}

Your task is to identify ONE detailed plan that would improve the CCA's performance on LEO satellite traces. The improvement should target the specific bottleneck identified in the performance results. Use the playbook lessons and reference implementations to guide your proposal.
"""

# ── Diagnosis Prompts ────────────────────────────────────────────────

DIAGNOSE_PROMPT_PERF = """# Performance Results
The CCA was evaluated on Starlink satellite traces using LeoReplayer + iperf3.

{per_trace_table}

# Pareto Gap Analysis
**BOTTLENECK DIMENSION: {bottleneck_dim}** (weighted gap from ideal: {bottleneck_gap:.3f})
Metrics: util={obj_util:.3f}, delay_quality={obj_dq:.3f}, loss_efficiency={obj_le:.3f}, robustness={obj_rob:.3f}

Focus your improvement proposal on the bottleneck dimension. The other dimensions are closer to ideal.

# Worst-Performing Traces
{worst_traces}

# Analysis Task
Analyze why the CCA performs poorly on the bottleneck dimension and worst traces. Consider:
- Is throughput limited by conservative cwnd growth? (affects util)
- Is queuing delay too high relative to propagation delay? (affects delay_quality)
- Are retransmits excessive? (affects loss_efficiency)
- Is worst-case utilization much lower than median? (affects robustness)
- Does the CCA handle LEO-specific patterns (handoffs, delay variations)?

Respond precisely in the following format including the JSON start and end markers:

```json
<JSON>
```

In <JSON>, provide a JSON response with the following fields:
- "performance_analysis": Analyze the per-trace results and identify patterns in failures.
- "bottleneck_identification": What specific mechanism in the CCA causes poor performance?
- "improvement_proposal": ONE concrete code change to the CCA that would improve performance. Be specific about which function to modify and how.
- "implementation_suggestion": Describe the exact C code changes needed, referencing specific functions and line numbers.
- "problem_description": Phrase the improvement as a task description for a developer editing tcp_evolved.c.
- "anti_repeat_check": Cite similar failures from the fleet log and explain why this proposal is materially different.
- "expected_metric_movement": Predicted directional impact on util/delay_quality/loss_efficiency/robustness.
- "rollback_trigger": One measurable signal indicating this proposal is likely wrong.

Your response will be automatically parsed, so ensure the string response is precisely in the correct format. Do NOT include the `<JSON>` tag in your output."""

DIAGNOSE_PROMPT_BUILD_FAIL = """The CCA failed to compile. The LLM coding agent needs to fix compilation errors.

Since the CCA is a Linux kernel module, common issues include:
- Missing includes
- Wrong kernel API usage (kernel version mismatch)
- Struct too large for ICSK_CA_PRIV_SIZE
- Invalid C syntax

Respond precisely in the following format including the JSON start and end markers:

```json
<JSON>
```

In <JSON>, provide a JSON response with the following fields:
- "improvement_proposal": Fix the compilation error so the module builds cleanly.
- "implementation_suggestion": Describe what the coding agent should check and fix.
- "problem_description": Phrase as a task for a developer.

Your response will be automatically parsed. Do NOT include the `<JSON>` tag in your output."""

DIAGNOSE_PROMPT_ROBUSTNESS = """# Performance Results
{per_trace_table}

The CCA shows high variance across traces. Some traces perform well but others fail badly.

Analyze why performance is inconsistent and propose a change that improves the worst-case performance without degrading the best-case.

Respond precisely in the following format including the JSON start and end markers:

```json
<JSON>
```

In <JSON>, provide a JSON response with the following fields:
- "performance_analysis": Why does performance vary so much across traces?
- "bottleneck_identification": What causes the worst traces to fail?
- "improvement_proposal": ONE change to improve worst-case performance.
- "implementation_suggestion": Exact C code changes needed.
- "problem_description": Task description for a developer.
- "anti_repeat_check": Cite similar failures from the fleet log and explain why this proposal is materially different.
- "expected_metric_movement": Predicted directional impact on util/delay_quality/loss_efficiency/robustness.
- "rollback_trigger": One measurable signal indicating this proposal is likely wrong.

Your response will be automatically parsed. Do NOT include the `<JSON>` tag in your output."""


# ── Entry Types ──────────────────────────────────────────────────────

ENTRY_TYPES = [
    "improve_throughput",
    "reduce_delay",
    "reduce_loss",
    "improve_robustness",
    "fix_compilation",
]

# Map bottleneck dimension → entry type
_BOTTLENECK_TO_ENTRY = {
    "util": "improve_throughput",
    "delay_quality": "reduce_delay",
    "loss_efficiency": "reduce_loss",
    "robustness": "improve_robustness",
}


def _one_sided_shortfalls(obj: Dict, target: Dict) -> Dict[str, float]:
    dims = ("util", "delay_quality", "loss_efficiency", "robustness")
    out = {}
    for d in dims:
        out[d] = max(0.0, target.get(d, 1.0) - obj.get(d, 0.0))
    return out


def choose_entry(performance: Dict, frontier_targets: Optional[List[Dict]] = None) -> str:
    """Choose improvement target based on Pareto gap analysis.

    Priority:
    1) Target-shortfall routing vs nearest frontier target (one-sided gaps)
    2) Stored bottleneck_dim from evaluation
    3) Robust fallback
    """
    if not performance or performance.get("total_submitted_instances") is None:
        return "fix_compilation"

    accuracy = performance.get("accuracy_score", 0)
    per_trace = performance.get("per_trace", {})

    # If nothing resolved, likely build/load failure
    if accuracy == 0 and not per_trace:
        return "fix_compilation"

    # 1) Frontier-target shortfall routing
    obj = performance.get("objective_vector") or {}
    if obj and frontier_targets:
        nearest = None
        nearest_l2 = float("inf")
        nearest_shortfalls = None
        for t in frontier_targets:
            s = _one_sided_shortfalls(obj, t)
            l2 = (s["util"] ** 2 + s["delay_quality"] ** 2 + s["loss_efficiency"] ** 2 + s["robustness"] ** 2) ** 0.5
            if l2 < nearest_l2:
                nearest_l2 = l2
                nearest = t
                nearest_shortfalls = s
        if nearest_shortfalls:
            dominant_dim = max(nearest_shortfalls, key=nearest_shortfalls.get)
            if nearest_shortfalls[dominant_dim] > 1e-6:
                return _BOTTLENECK_TO_ENTRY.get(dominant_dim, "improve_throughput")

    # 2) Bottleneck-driven routing from evaluate_cca
    bottleneck = performance.get("bottleneck_dim")
    if bottleneck and bottleneck in _BOTTLENECK_TO_ENTRY:
        return _BOTTLENECK_TO_ENTRY[bottleneck]

    # 3) Fallback for old-format metadata (no objective_vector)
    return "improve_throughput"


def _format_trace_table(per_trace: Dict) -> str:
    """Format per-trace results as a readable table with 4D objectives."""
    lines = [f"{'Trace':<20} {'Tput(Mbps)':>10} {'RTT(ms)':>8} {'Util':>6} {'DelayQ':>7} {'LossEff':>8} {'Scalar':>7} {'Status':>8}"]
    lines.append("-" * 90)
    for trace_id, tr in sorted(per_trace.items()):
        scalar = tr.get("fitness", 0)
        status = "OK" if scalar >= DEFAULT_RESOLVE_THRESHOLD else "FAIL"
        if tr.get("error"):
            status = "ERR"
        obj = tr.get("objectives", {})
        lines.append(
            f"{trace_id:<20} {tr.get('throughput_mbps', 0):>10.1f} "
            f"{tr.get('rtt_ms', 0):>8.1f} "
            f"{obj.get('util', 0):>6.2f} {obj.get('delay_quality', 0):>7.3f} "
            f"{obj.get('loss_efficiency', 0):>8.3f} {scalar:>7.3f} {status:>8}"
        )
    return "\n".join(lines)


def _format_worst_traces(per_trace: Dict, n: int = 3) -> str:
    """Format details about the N worst-performing traces with bottleneck guidance."""
    sorted_traces = sorted(per_trace.items(), key=lambda x: x[1].get("fitness", 0))
    lines = []
    for trace_id, tr in sorted_traces[:n]:
        obj = tr.get("objectives", {})
        min_rtt_ms = tr.get("min_rtt_us", 0) / 1000.0
        queuing_ms = max(tr.get("rtt_ms", 0) - min_rtt_ms, 0) if min_rtt_ms > 0 else 0
        lines.append(f"## {trace_id}")
        lines.append(f"  Throughput: {tr.get('throughput_mbps', 0):.1f} Mbps")
        lines.append(f"  Mean RTT: {tr.get('rtt_ms', 0):.1f} ms")
        lines.append(f"  Min RTT: {min_rtt_ms:.1f} ms (propagation baseline)")
        lines.append(f"  Queuing delay: {queuing_ms:.1f} ms")
        lines.append(f"  Retransmits: {tr.get('retransmits', 0)}")
        lines.append(f"  Max cwnd: {tr.get('max_snd_cwnd', 'N/A')}")
        lines.append(f"  Objectives: util={obj.get('util',0):.3f} "
                     f"delay_q={obj.get('delay_quality',0):.3f} "
                     f"loss_eff={obj.get('loss_efficiency',0):.3f}")
        lines.append(f"  Scalar fitness: {tr.get('fitness', 0):.3f}")
        if tr.get("error"):
            lines.append(f"  Error: {tr['error']}")
        # Bottleneck-specific guidance
        util_v = obj.get("util", 0)
        dq_v = obj.get("delay_quality", 0)
        le_v = obj.get("loss_efficiency", 0)
        worst_dim = min({"util": util_v, "delay_quality": dq_v, "loss_efficiency": le_v},
                        key=lambda k: {"util": util_v, "delay_quality": dq_v, "loss_efficiency": le_v}[k])
        if worst_dim == "util" and util_v < 0.8:
            lines.append(f"  -> BOTTLENECK: Low utilization ({util_v:.0%}). "
                         "Consider faster cwnd growth, BDP floor, or more aggressive probing.")
        elif worst_dim == "delay_quality" and dq_v < 0.7:
            lines.append(f"  -> BOTTLENECK: Excessive queuing (delay_quality={dq_v:.3f}). "
                         "Consider queue-aware backoff or inflight-based congestion detection.")
        elif worst_dim == "loss_efficiency" and le_v < 0.95:
            lines.append(f"  -> BOTTLENECK: High retransmits (loss_eff={le_v:.3f}). "
                         "Consider loss discrimination: handoff-induced vs congestion loss.")
        lines.append(f"  BW trace: {tr.get('bw_trace', 'N/A')}")
        lines.append("")
    return "\n".join(lines)


BANDIT_PROMPT_SECTION = """
# PRIORITY MUTATION DIRECTION
Based on evolutionary history (UCB1 bandit), focus your improvement on **{category}** mechanisms.
Past mutations in other categories have shown lower reward. You may still propose changes
in other areas if the analysis strongly indicates a different bottleneck, but prefer
{category} when the choice is ambiguous.
"""

STAGNATION_PROMPT_SECTION = """
# STAGNATION DETECTED ({consecutive} generations without improvement, EMA={ema:.4f})

The evolution has plateaued. Review the FLEET FAILURE LOG above carefully.

Guidelines:
1. Do NOT repeat approaches that already failed (see failure log)
2. Prefer SMALL, TARGETED changes over big architectural rewrites
3. Big rewrites (new state machine modes, STARTUP re-entry, recovery probes) have
   consistently regressed by 20-40% — the LeoCC architecture is fragile
4. The most promising direction: modify HOW existing signals are used, not the structure
5. You MUST stay within 112-byte ICSK_CA_PRIV_SIZE (~12 bytes free)

Recent mutation categories tried: {recent_categories}
Prefer a different category than recent attempts.
"""

FAILURE_LOG_PROMPT_SECTION = """
# FLEET FAILURE LOG (recent regressions)
{failure_log}
"""

PREMORTEM_PROMPT_SECTION = """
# PRE-MORTEM (MANDATORY BEFORE PROPOSING A CHANGE)
Before finalizing your proposal:
1. Identify the 1-2 most similar failed mutations from the failure log.
2. Explain why your proposal avoids the same failure mode.
3. State one concrete rollback trigger (metric movement that would falsify your hypothesis).
"""

SCAFFOLD_PROMPT_SECTION = """
# SCAFFOLD MODE (Exploit-First)
- Parent lineage is a known high-performing controller. Preserve architecture.
- Propose ONE local mechanism adjustment only.
- Do NOT introduce new modes, state-machine branches, or STARTUP/DRAIN redesign.
- Keep patch small and targeted (roughly <= {max_patch_lines} changed lines).
- Focus on parameter/threshold/guard tuning around the selected bottleneck.
"""


def _truncate_reason(text: str, max_chars: int = 120) -> str:
    if not text:
        return "no_reason_recorded"
    one_line = " ".join(text.strip().split())
    return one_line[:max_chars] + ("..." if len(one_line) > max_chars else "")


def _build_failure_log(output_dir: str, limit: int = 10) -> str:
    """Build a compact failure log from shared/local JSONL and metadata files."""
    entries: List[Dict] = []
    seen = set()

    # Prefer explicit failure logs if available.
    for candidate in ("shared_failure_log.jsonl", "failure_log.jsonl"):
        path = os.path.join(output_dir, candidate)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    run_id = rec.get("run_id") or rec.get("child")
                    if not run_id or run_id in seen:
                        continue
                    seen.add(run_id)
                    entries.append(rec)
        except OSError:
            pass

    # Fallback/augmentation: mine recent metadata.json files.
    try:
        if os.path.isdir(output_dir):
            for entry in sorted(os.listdir(output_dir), reverse=True):
                meta_path = os.path.join(output_dir, entry, "metadata.json")
                if not os.path.isfile(meta_path):
                    continue
                if entry in seen:
                    continue
                try:
                    meta = load_json_file(meta_path)
                except Exception:
                    continue
                perf = meta.get("overall_performance", {})
                child_score = float(perf.get("accuracy_score", 0.0) or 0.0)
                early = bool(perf.get("early_stopped", False))
                parent_commit = meta.get("parent_commit", "")
                parent_score = None
                if parent_commit and parent_commit != "initial":
                    pmeta = os.path.join(output_dir, parent_commit, "metadata.json")
                    if os.path.isfile(pmeta):
                        try:
                            parent_score = float(
                                load_json_file(pmeta).get("overall_performance", {}).get("accuracy_score", 0.0) or 0.0
                            )
                        except Exception:
                            parent_score = None
                delta = None if parent_score is None else child_score - parent_score
                if early or (delta is not None and delta < -0.02) or child_score < 0.70:
                    seen.add(entry)
                    entries.append({
                        "run_id": entry,
                        "parent_commit": parent_commit,
                        "entry": meta.get("entry"),
                        "mutation_category": meta.get("mutation_category"),
                        "child_score": child_score,
                        "parent_score": parent_score,
                        "delta": delta,
                        "early_stopped": early,
                        "bottleneck_dim": perf.get("bottleneck_dim"),
                        "reason": _truncate_reason(meta.get("problem_statement", "")),
                    })
                if len(entries) >= (limit * 4):
                    break
    except OSError:
        pass

    if not entries:
        return "- none recorded yet"

    # Sort by severity: early_stop first, then worst delta/score.
    def _sev(rec: Dict) -> Tuple[int, float, float]:
        early = 0 if rec.get("early_stopped") else 1
        delta = rec.get("delta")
        if delta is None:
            delta = rec.get("child_score", 0.0) - rec.get("parent_score", 0.0)
        return (
            early,
            float(delta),
            float(rec.get("child_score", 0.0)),
        )

    entries = sorted(entries, key=_sev)[:limit]
    lines = []
    for rec in entries:
        run_id = rec.get("run_id", "?")
        cat = rec.get("mutation_category", "unknown")
        ent = rec.get("entry", "unknown")
        bottleneck = rec.get("bottleneck_dim", "unknown")
        child = float(rec.get("child_score", 0.0) or 0.0)
        parent = rec.get("parent_score")
        if parent is None:
            delta_str = "delta=n/a"
        else:
            delta = child - float(parent or 0.0)
            delta_str = f"delta={delta:+.3f}"
        early = " early_stop" if rec.get("early_stopped") else ""
        reason = _truncate_reason(rec.get("reason", ""))
        lines.append(
            f"- {run_id}: score={child:.3f} {delta_str}{early}; "
            f"entry={ent}; cat={cat}; bottleneck={bottleneck}; why={reason}"
        )
    return "\n".join(lines)


def get_diagnose_prompt(
    entry_type: str,
    performance: Dict,
    c_code: str,
    playbook: str = "",
    reference_library: str = "",
    bandit_suggestion: Optional[str] = None,
    stagnation_meta: Optional[Dict] = None,
    failure_log: str = "",
    scaffold_mode: bool = False,
    max_patch_lines: int = 0,
) -> Tuple[str, str]:
    """Get system message and user prompt for CCA diagnosis.

    Returns (system_message, user_prompt) analogous to DGM's
    get_diagnose_prompt_swe().
    """
    per_trace = performance.get("per_trace", {})
    trace_table = _format_trace_table(per_trace) if per_trace else "No trace results available."
    worst_traces = _format_worst_traces(per_trace) if per_trace else "No trace data."

    system_msg = DIAGNOSE_SYSTEM_MESSAGE.format(
        cca_summary=CCA_AGENT_SUMMARY,
        c_code=c_code[:50000],  # Truncate if very long
        playbook=playbook or "(No playbook yet — first generation)",
        reference_library=reference_library or "No reference implementations available yet.",
    )

    # Extract objective vector for bottleneck info in prompts
    obj = performance.get("objective_vector", {})
    bottleneck_dim = performance.get("bottleneck_dim", "util")
    bottleneck_gap = performance.get("bottleneck_gap", 1.0)

    if entry_type == "fix_compilation":
        user_prompt = CCA_AGENT_SUMMARY + DIAGNOSE_PROMPT_BUILD_FAIL
    elif entry_type == "improve_robustness":
        user_prompt = DIAGNOSE_PROMPT_ROBUSTNESS.format(per_trace_table=trace_table)
    else:
        user_prompt = DIAGNOSE_PROMPT_PERF.format(
            per_trace_table=trace_table,
            worst_traces=worst_traces,
            bottleneck_dim=bottleneck_dim,
            bottleneck_gap=bottleneck_gap,
            obj_util=obj.get("util", 0),
            obj_dq=obj.get("delay_quality", 0),
            obj_le=obj.get("loss_efficiency", 0),
            obj_rob=obj.get("robustness", 0),
        )

    # Inject fleet failure log before guidance blocks.
    if failure_log and entry_type != "fix_compilation":
        user_prompt += FAILURE_LOG_PROMPT_SECTION.format(failure_log=failure_log)
        user_prompt += PREMORTEM_PROMPT_SECTION

    # Inject bandit suggestion (soft guidance)
    if bandit_suggestion and entry_type != "fix_compilation":
        user_prompt += BANDIT_PROMPT_SECTION.format(category=bandit_suggestion)

    # Inject stagnation meta-guidance (strong directive)
    if stagnation_meta and stagnation_meta.get("triggered") and entry_type != "fix_compilation":
        user_prompt += STAGNATION_PROMPT_SECTION.format(
            consecutive=stagnation_meta.get("consecutive", 5),
            ema=stagnation_meta.get("ema", 0.0),
            recent_categories=", ".join(stagnation_meta.get("recent_categories", [])),
        )

    # Exploit-first scaffold constraints.
    if scaffold_mode and entry_type != "fix_compilation":
        user_prompt += SCAFFOLD_PROMPT_SECTION.format(
            max_patch_lines=max(1, int(max_patch_lines or 40)),
        )

    return system_msg, user_prompt


def get_problem_description(response_json: Dict) -> str:
    """Convert diagnosis JSON to problem description for coding agent.

    Analogous to DGM's get_problem_description_prompt().
    """
    impl_suggestion = response_json.get("implementation_suggestion", "")
    problem_desc = response_json.get("problem_description", "")
    return CCA_AGENT_SUMMARY + f"# To Implement\n\n{impl_suggestion}\n\n{problem_desc}"


def _get_static_sota_references() -> str:
    """Load published kernel CCA sources as static references for early generations.

    Gives the LLM LeoCC and FRCC implementations so it can recombine
    mechanisms immediately instead of rediscovering known patterns.
    """
    sota_dir = os.path.join(os.path.dirname(__file__), '..', '..')
    refs = []

    # LeoCC — strong reported baseline for LEO satellite settings (SIGCOMM 2025)
    leocc_path = os.path.join(sota_dir, 'leocc_ref', 'leocc', 'live_network', 'leocc.c')
    if os.path.exists(leocc_path):
        with open(leocc_path) as f:
            code = f.read()
        refs.append(
            "### LeoCC (SIGCOMM 2025) — reported strong LEO baseline in published evaluation\n"
            "Key mechanisms: BBR-derived state machine (STARTUP→DRAIN→DYNAMIC_CRUISE→PROBE_RTT),\n"
            "Kalman filter BW/RTT estimation (R=Q=4), dual estimator switch (max-filter vs Kalman),\n"
            "reconfiguration detection (handoff-aware), 8-phase pacing cycle [1.25,0.75,1,1,1,1,1,1].\n"
            "Use as a mechanism source. Goal: improve the empirical 4D frontier under this harness.\n"
            f"```c\n{code[:6000]}\n```\n"
        )

    # FRCC — formally analyzed fair rate control reference (NSDI 2026)
    frcc_path = os.path.join(sota_dir, 'frcc_ref', 'frcc_kernel', 'tcp_frcc.c')
    if os.path.exists(frcc_path):
        with open(frcc_path) as f:
            code = f.read()
        refs.append(
            "### FRCC (NSDI 2026) — formal fairness/starvation analysis reference\n"
            "Key mechanisms: slot-based probing, contract-based fair share encoding,\n"
            "configurable parameters via /sys/module, formal safety guarantees.\n"
            "Use as a reference mechanism; do not assume it is optimal for LEO transients.\n"
            f"```c\n{code[:6000]}\n```\n"
        )

    return "\n".join(refs) if refs else ""


def _get_reference_library(output_dir: str, archive: list, top_k: int = 3) -> str:
    """Read C source from top-K archive members by fitness.

    Provides the LLM with diverse reference implementations to trigger
    creative recombination (AlphaEvolve's key lesson).

    On early generations (small archive), includes static published references
    (LeoCC, FRCC) so the LLM doesn't waste generations rediscovering
    known mechanisms.
    """
    refs = []
    for commit in archive:
        try:
            meta = load_json_file(os.path.join(output_dir, commit, "metadata.json"))
            score = meta["overall_performance"]["accuracy_score"]
            # Find C source in workspace
            ws = os.path.join(output_dir, commit, "workspace")
            c_files = [f for f in os.listdir(ws) if f.endswith('.c')] if os.path.isdir(ws) else []
            if c_files:
                with open(os.path.join(ws, c_files[0])) as f:
                    code = f.read()
                refs.append((score, commit, code[:8000]))  # truncate
        except Exception as e:
            print(f"[ref_library] Skipping {commit}: {e}")
            continue
    refs.sort(reverse=True)
    lines = []
    for score, commit, code in refs[:top_k]:
        lines.append(f"### {commit} (fitness={score:.3f})\n```c\n{code}\n```\n")

    # Include static published references when archive is small (< 5 evolved members)
    evolved_count = len([c for c in archive if c != 'initial'])
    if evolved_count < 5:
        sota = _get_static_sota_references()
        if sota:
            lines.insert(0, "## Published Reference Implementations (external priors, verify empirically)\n")
            lines.insert(1, sota)
            if evolved_count > 0:
                lines.insert(2, "## Evolved Archive Members\n")

    return "\n".join(lines) if lines else "No reference implementations available yet."


def get_failure_log(output_dir: str, limit: int = 10) -> str:
    """Public helper for diagnosis prompt construction."""
    return _build_failure_log(output_dir=output_dir, limit=limit)


def get_test_description() -> str:
    """Get test description for the CCA coding agent.

    Analogous to DGM's get_test_description().
    """
    return """To test this CCA kernel module:
1. Build: cd to the workspace directory and run `make`
2. Load: `sudo insmod tcp_evolved.ko`
3. Verify: `sysctl net.ipv4.tcp_available_congestion_control` should include 'evolved'
4. Quick test: `iperf3 -s -D && iperf3 -c 127.0.0.1 -C evolved -t 3 && pkill iperf3`
5. Unload: `sudo rmmod tcp_evolved`

The build must succeed without errors. The module must load and unload cleanly.
iperf3 must complete without segfaults or kernel panics.

IMPORTANT: Do NOT use kzalloc or kmalloc in the init function. All per-connection
state must fit in the struct (ICSK_CA_PRIV_SIZE = 112 bytes on stock kernels).
If struct is too large, reduce fields or use smaller types (u16 instead of u32)."""

"""CCA evaluation harness using LeoCC's LeoReplayer pipeline.

Replaces DGM's swe_bench/harness.py. Runs kernel CCAs through
LeoReplayer (Mahimahi fork) + iperf3 on real Starlink traces.

Returns DGM-compatible performance dicts so the outer loop works unchanged.

Usage:
    from alphacc.dgm_cca.cca_harness import evaluate_cca, find_trace_pairs

    traces = find_trace_pairs("data/starlink_traces", max_traces=5)
    result = evaluate_cca("evolved", traces, duration=30)
    print(result["accuracy_score"], result["total_resolved_ids"])

Requires: Linux, LeoReplayer (mm-delay, mm-link, mm-loss), iperf3
"""

import json
import os
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Fitness threshold: trace is "resolved" if scalar fitness >= this value
# Kept for backward-compat resolved/unresolved classification only
DEFAULT_RESOLVE_THRESHOLD = 0.5

# LeoCC-aligned scoring (SIGCOMM 2025).
# LeoCC evaluates via 2D Pareto: average throughput vs average one-way delay.
# No composite scalar, no dispersion penalties, no repeat-eval in the paper.
# We use a simple 2D weighted score for evolution selection:
#   score = UTIL_WEIGHT * mean_util + DELAY_WEIGHT * mean_delay_quality
# This mirrors LeoCC's "upper-left on throughput-delay scatter" criterion.
UTIL_WEIGHT = 0.60   # throughput-primary (LeoCC achieves 95.2% util)
DELAY_WEIGHT = 0.40  # delay-secondary (LeoCC achieves 44-56% lower delay than BBRv1/VIVACE)

# Legacy constants kept for backward compat (per-trace shortfall score, bottleneck calc)
TCHEBYCHEFF_RHO = 0.05
IDEAL_POINT = (1.0, 1.0, 1.0, 1.0)
DEFAULT_WEIGHTS = (0.35, 0.25, 0.20, 0.20)
WEIGHT_ROTATION = [
    (0.40, 0.25, 0.15, 0.20),
    (0.20, 0.40, 0.20, 0.20),
    (0.25, 0.15, 0.40, 0.20),
    (0.20, 0.20, 0.20, 0.40),
    (0.25, 0.25, 0.25, 0.25),
]

# Selection weights — simplified to 2D (util + delay). Loss and robustness
# are reported but not part of the primary selection score, matching LeoCC
# which reports raw throughput and delay without composite weighting.
SELECTION_WEIGHTS = {
    "util": 0.60,
    "delay_quality": 0.40,
    "loss_efficiency": 0.00,
    "robustness": 0.00,
}

# LeoCC defaults (from SIGCOMM 2025 paper + leoreplayer/README.md "Parameters Suggestion")
# See: https://github.com/SpaceNetLab/LeoCC
# NOTE: The run.sh example uses simplified params (500/500 queue, 0.002 loss only).
# The README specifies the actual paper evaluation params below.
DEFAULT_UPLINK_QUEUE_PKTS = 500       # uplink bottleneck queue (data path)
DEFAULT_DOWNLINK_QUEUE_PKTS = 50000   # ACK-path buffer (README: 50000 for downlink)
DEFAULT_UPLINK_LOSS_RATE = 0.005      # high-throughput CCAs (BBR, LeoCC): 0.005 per README
DEFAULT_DOWNLINK_LOSS_RATE = 0.0001   # minimal ACK-path loss (README: 0.0001 for all CCAs)
DEFAULT_DELAY_INTERVAL = 10           # ms granularity for delay trace
DEFAULT_DURATION = 120                # full trace duration (8 reconfiguration events at 15s period)
# Per-CCA loss rate recommendation from README:
#   Low-throughput (Copa, Cubic, Reno, Vegas): uplink_loss = 0.002
#   High-throughput (BBRv1, BBRv3, LeoCC):    uplink_loss = 0.005


# --- LeoCC per-trace reconfiguration offset extraction ---
# Ported from leocc_ref/traces/extract_reconfiguration.py (SIGCOMM 2025)
LEOCC_PERIOD_TICKS = 1500       # 15s reconfiguration period at 10ms/tick
LEOCC_JITTER_TICKS = 10         # ±10 tick tolerance for matching repeats
LEOCC_DEFAULT_OFFSET_MS = 12000 # fallback if extraction fails
LEOCC_SYSFS = "/sys/module/tcp_evolved/parameters/"


def extract_offset(delay_trace_path: str) -> int:
    """Extract reconfiguration offset from a LeoCC delay trace.

    Finds the first periodic reconfiguration event by detecting delay spikes
    that repeat every 15 seconds (1500 ticks at 10ms granularity).

    Returns offset in milliseconds for the kernel module parameter.
    """
    try:
        with open(delay_trace_path) as f:
            raw_lines = f.readlines()
    except OSError:
        return LEOCC_DEFAULT_OFFSET_MS

    # Parse delay values with tick positions
    delay_values = []
    for i, line in enumerate(raw_lines):
        line = line.strip()
        if line:
            try:
                delay_values.append((int(line), i))
            except ValueError:
                continue

    if len(delay_values) < LEOCC_PERIOD_TICKS:
        return LEOCC_DEFAULT_OFFSET_MS

    # Top 100 largest delays in first 15s period
    delay_values_15s = sorted(delay_values[:LEOCC_PERIOD_TICKS],
                              key=lambda x: x[0], reverse=True)[:100]

    # Top 100 largest delays across entire trace — store positions as set
    sorted_all = sorted(delay_values, key=lambda x: x[0], reverse=True)[:100]
    large_value_index = {pos for _, pos in sorted_all}

    # For each candidate in first period, count periodic repeats
    possibility = [0] * len(delay_values_15s)
    trace_len = len(delay_values)
    for k, (value, position) in enumerate(delay_values_15s):
        i = position
        while i < trace_len:
            i += LEOCC_PERIOD_TICKS
            for j in range(-LEOCC_JITTER_TICKS, LEOCC_JITTER_TICKS + 1):
                if (i + j) in large_value_index:
                    possibility[k] += 1
                    break

    if not possibility or max(possibility) == 0:
        return LEOCC_DEFAULT_OFFSET_MS

    # Pick best: max repeats, then largest delay, then average index
    max_poss = max(possibility)
    max_indices = [i for i, p in enumerate(possibility) if p == max_poss]
    if len(max_indices) == 1:
        best = max_indices[0]
    else:
        max_delay = max(delay_values_15s[i][0] for i in max_indices)
        delay_indices = [i for i in max_indices
                         if delay_values_15s[i][0] == max_delay]
        best = delay_indices[0] if len(delay_indices) == 1 else round(
            sum(delay_indices) / len(delay_indices))

    # Convert tick position to milliseconds (each tick = 10ms)
    return delay_values_15s[best][1] * 10


def _set_module_param(name: str, value: int):
    """Set a kernel module parameter at runtime via sysfs."""
    param_path = os.path.join(LEOCC_SYSFS, name)
    subprocess.run(
        ["sudo", "tee", param_path],
        input=str(value).encode(),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _module_loaded(module_name: str) -> bool:
    """Return True iff module appears in lsmod output as an exact module name."""
    result = subprocess.run(["lsmod"], capture_output=True, text=True)
    for line in result.stdout.splitlines()[1:]:
        cols = line.split()
        if cols and cols[0] == module_name:
            return True
    return False


def _force_unload_module(module_name: str, retries: int = 4, sleep_s: float = 0.4) -> Optional[str]:
    """Best-effort unload with retries. Returns last error, or None if unloaded."""
    last_err = None
    for _ in range(retries):
        if not _module_loaded(module_name):
            return None
        result = subprocess.run(
            ["sudo", "rmmod", module_name],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            time.sleep(sleep_s)
            if not _module_loaded(module_name):
                return None
        else:
            last_err = result.stderr.strip() or result.stdout.strip() or "unknown rmmod error"
            time.sleep(sleep_s)
    if not _module_loaded(module_name):
        return None
    return last_err or "module still loaded after retries"


def find_trace_pairs(trace_dir: str, max_traces: int = 10) -> List[Tuple[str, str]]:
    """Find Starlink trace pairs (bw_*.txt, delay_*.txt).

    Matches LeoCC's trace format: directories A-H, each with subdirs
    containing bw_*.txt and delay_*.txt files.
    """
    trace_dir = Path(trace_dir).resolve()
    if not trace_dir.is_dir():
        return []
    pairs = []
    for subdir in sorted(trace_dir.iterdir()):
        if not subdir.is_dir():
            continue
        for trace_subdir in sorted(subdir.iterdir()):
            if not trace_subdir.is_dir():
                continue
            bw_files = sorted(trace_subdir.glob("bw_*.txt"))
            delay_files = sorted(trace_subdir.glob("delay_*.txt"))
            if bw_files and delay_files:
                pairs.append((str(bw_files[0]), str(delay_files[0])))
    # Deterministic subset (local RNG to avoid polluting global random state)
    if len(pairs) > max_traces:
        import random
        rng = random.Random(42)
        pairs = rng.sample(pairs, max_traces)
    return sorted(pairs)


def _run_single_trace(
    cca_name: str,
    bw_trace: str,
    delay_trace: str,
    duration: int,
    uplink_queue_pkts: int,
    downlink_queue_pkts: int,
    uplink_loss_rate: float,
    downlink_loss_rate: float,
    work_dir: str,
) -> Dict:
    """Run one iperf3 session through LeoReplayer.

    Follows LeoCC's exact run.sh pattern (SIGCOMM 2025):
        mm-delay INTERVAL DELAY_TRACE
            mm-loss uplink UPLINK_LOSS
            mm-loss downlink DOWNLINK_LOSS
            mm-link BW BW
                --uplink-queue droptail --uplink-queue-args packets=UPLINK_Q
                --downlink-queue droptail --downlink-queue-args packets=DOWNLINK_Q
            bash inner.sh DURATION ALG

    Returns dict with throughput_mbps, rtt_ms, or error.
    """
    os.makedirs(work_dir, exist_ok=True)

    # Write inner.sh (matches leocc_ref/leoreplayer/replayer/example/Cubic/inner.sh)
    inner_script = os.path.join(work_dir, "inner.sh")
    iperf_json = os.path.join(work_dir, "iperf.json")
    with open(inner_script, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(f"iperf3 -c 100.64.0.1 -C $2 -t $1 --json > {iperf_json} 2>&1\n")
    os.chmod(inner_script, 0o755)

    result = {
        "bw_trace": bw_trace,
        "delay_trace": delay_trace,
        "throughput_mbps": 0.0,
        "rtt_ms": 0.0,
        "min_rtt_us": 0,
        "max_rtt_us": 0,
        "max_snd_cwnd": 0,
        "bytes_sent": 0,
        "retransmits": 0,
        "error": None,
    }

    # Kill any leftover iperf3 from previous runs, wait for port release
    subprocess.run(["pkill", "-f", "iperf3"], capture_output=True)
    time.sleep(0.5)

    # Start iperf3 server in one-off mode (-1 exits after first client)
    server_proc = subprocess.Popen(
        ["iperf3", "-s", "-1", "-p", "5201"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)
    if server_proc.poll() is not None:
        result["error"] = f"iperf3 server failed to start (exit={server_proc.returncode})"
        return result

    # Build LeoReplayer command matching LeoCC paper's exact nesting order
    import shlex
    # Build mm-loss chain — skip downlink loss if rate is 0 (matches LeoCC run.sh)
    loss_chain = f"mm-loss uplink {uplink_loss_rate} "
    if downlink_loss_rate > 0:
        loss_chain += f"mm-loss downlink {downlink_loss_rate} "
    mm_cmd = (
        f"mm-delay {DEFAULT_DELAY_INTERVAL} {shlex.quote(delay_trace)} "
        f"{loss_chain}"
        f"mm-link {shlex.quote(bw_trace)} {shlex.quote(bw_trace)} "
        f"--uplink-queue droptail --uplink-queue-args packets={uplink_queue_pkts} "
        f"--downlink-queue droptail --downlink-queue-args packets={downlink_queue_pkts} "
        f"bash {shlex.quote(inner_script)} {duration} {shlex.quote(cca_name)}"
    )

    try:
        proc = subprocess.run(
            mm_cmd, shell=True, timeout=duration + 30,
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            result["error"] = f"mm exit={proc.returncode}: {proc.stderr[:200]}"
    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
    finally:
        # Ensure iperf3 server is cleaned up (it should auto-exit with -1)
        try:
            server_proc.kill()
            server_proc.wait(timeout=5)
        except Exception:
            pass
        subprocess.run(["pkill", "-f", "iperf3 -s"], capture_output=True)

    # Parse iperf3 JSON output
    if os.path.exists(iperf_json):
        try:
            with open(iperf_json) as f:
                d = json.load(f)
            end = d.get("end", {})
            sent = end.get("sum_sent", {})
            result["throughput_mbps"] = sent.get("bits_per_second", 0) / 1e6
            result["retransmits"] = sent.get("retransmits", 0)
            result["bytes_sent"] = sent.get("bytes", 0)
            # RTT from iperf3 streams (may not be available in all versions)
            streams = end.get("streams", [{}])
            if streams:
                sender = streams[0].get("sender", {})
                rtt_us = sender.get("mean_rtt", 0)
                result["rtt_ms"] = rtt_us / 1000.0
                result["min_rtt_us"] = sender.get("min_rtt", 0)
                result["max_rtt_us"] = sender.get("max_rtt", 0)
                result["max_snd_cwnd"] = sender.get("max_snd_cwnd", 0)
        except (json.JSONDecodeError, ValueError, KeyError):
            if not result["error"]:
                result["error"] = "corrupt iperf json"

    return result


def _parse_trace_capacity_mbps(bw_trace_path: str) -> float:
    """Parse Mahimahi bandwidth trace to get mean capacity in Mbps.

    Mahimahi trace format: one entry per 'tick' (12ms). Each line = one
    packet delivery opportunity. The rate is (count_per_second * 1500 * 8) bits/s.
    For simplicity, count total lines and divide by trace duration in seconds.
    """
    try:
        with open(bw_trace_path) as f:
            timestamps = [int(line.strip()) for line in f if line.strip()]
        if len(timestamps) < 2:
            return 100.0  # fallback
        duration_ms = timestamps[-1] - timestamps[0]
        if duration_ms <= 0:
            return 100.0
        # Each entry = one 1500-byte packet opportunity
        pkts_per_sec = len(timestamps) * 1000.0 / duration_ms
        capacity_mbps = pkts_per_sec * 1500 * 8 / 1e6
        return max(capacity_mbps, 1.0)  # floor at 1 Mbps
    except Exception as e:
        print(f"[cca_harness] WARNING: Failed to parse {bw_trace_path}: {e}. Using 100Mbps fallback.")
        return 100.0  # fallback to avoid division issues


def _weighted_trace_score(util: float, delay_quality: float, loss_efficiency: float) -> float:
    """One-sided shortfall score in [0, 1] for per-trace classification.

    This avoids negative Tchebycheff values that break resolved-threshold logic.
    """
    w_util, w_dq, w_le = DEFAULT_WEIGHTS[0], DEFAULT_WEIGHTS[1], DEFAULT_WEIGHTS[2]
    norm = w_util + w_dq + w_le
    shortfall = (
        w_util * max(0.0, 1.0 - util)
        + w_dq * max(0.0, 1.0 - delay_quality)
        + w_le * max(0.0, 1.0 - loss_efficiency)
    ) / max(norm, 1e-9)
    return max(0.0, min(1.0, 1.0 - shortfall))


def _tchebycheff_scalar(f, z_star, w, rho=TCHEBYCHEFF_RHO):
    """Augmented Tchebycheff scalarization. Higher = better (less negative)."""
    deviations = [w[i] * max(0.0, z_star[i] - f[i]) for i in range(len(f))]
    return -(max(deviations) + rho * sum(deviations))


def _iqr(values: List[float]) -> float:
    if len(values) < 4:
        return 0.0
    sorted_vals = sorted(values)
    q1_idx = int(0.25 * (len(sorted_vals) - 1))
    q3_idx = int(0.75 * (len(sorted_vals) - 1))
    return max(0.0, sorted_vals[q3_idx] - sorted_vals[q1_idx])


def _compute_trace_fitness(trace_result: Dict) -> Dict:
    """Compute per-trace objective vector with self-normalizing metrics.

    All metrics are ratios (no hardcoded ms or % denominators):
    - util: throughput / trace_capacity
    - delay_quality: min_rtt / mean_rtt (1.0 = no queuing, PCC Vivace insight)
    - loss_efficiency: 1 - retransmits / packets_est (1.0 = no loss)

    Returns dict with objectives + one-sided shortfall score.
    The "fitness" key is a float for backward compatibility with all downstream
    readers that do tr.get("fitness", 0).
    """
    zero = {"util": 0.0, "delay_quality": 0.0, "loss_efficiency": 0.0, "scalar": 0.0, "fitness": 0.0}
    if trace_result.get("error") or trace_result.get("throughput_mbps", 0) <= 0:
        return zero

    # Dim 1: Utilization — self-normalizing (throughput / capacity)
    capacity = _parse_trace_capacity_mbps(trace_result["bw_trace"])
    util = min(trace_result["throughput_mbps"] / capacity, 1.0)

    # Dim 2: Delay quality — self-normalizing (min_rtt / mean_rtt)
    # Ratio: 1.0 when zero queuing, approaches 0 with heavy queuing.
    # No arbitrary denominator (was: queuing_ms / 50.0).
    min_rtt_ms = trace_result.get("min_rtt_us", 0) / 1000.0
    mean_rtt_ms = trace_result.get("rtt_ms", 0)
    if min_rtt_ms > 0 and mean_rtt_ms > 0:
        delay_quality = min(min_rtt_ms / mean_rtt_ms, 1.0)
    else:
        delay_quality = 0.5  # no RTT data — neutral, not optimistic

    # Dim 3: Loss efficiency — self-normalizing (1 - retransmits / packets)
    # No arbitrary denominator (was: retransmit_rate / 0.10).
    bytes_sent = trace_result.get("bytes_sent", 0)
    retransmits = trace_result.get("retransmits", 0)
    if bytes_sent > 0:
        packets_est = bytes_sent / 1500.0
        loss_efficiency = max(0.0, 1.0 - retransmits / max(packets_est, 1.0))
    else:
        loss_efficiency = 0.0

    scalar = _weighted_trace_score(util, delay_quality, loss_efficiency)
    # Conservative proxy for per-trace robustness to avoid util double-counting.
    robustness_proxy = min(util, delay_quality, loss_efficiency)
    tcheby = _tchebycheff_scalar(
        (util, delay_quality, loss_efficiency, robustness_proxy),
        IDEAL_POINT,
        DEFAULT_WEIGHTS,
    )

    return {
        "util": util,
        "delay_quality": delay_quality,
        "loss_efficiency": loss_efficiency,
        "robustness_proxy": robustness_proxy,
        "tchebycheff": tcheby,
        "scalar": scalar,
        "fitness": scalar,  # backward compat: downstream does tr.get("fitness", 0)
    }


def evaluate_cca(
    cca_name: str,
    trace_pairs: List[Tuple[str, str]],
    duration: int = DEFAULT_DURATION,
    uplink_queue_pkts: int = DEFAULT_UPLINK_QUEUE_PKTS,
    downlink_queue_pkts: int = DEFAULT_DOWNLINK_QUEUE_PKTS,
    uplink_loss_rate: float = DEFAULT_UPLINK_LOSS_RATE,
    downlink_loss_rate: float = DEFAULT_DOWNLINK_LOSS_RATE,
    resolve_threshold: float = DEFAULT_RESOLVE_THRESHOLD,
    work_dir: Optional[str] = None,
    # Sequential early elimination with fixed margin (not F-Race/Birattari)
    parent_score: Optional[float] = None,
    racing_traces: int = 3,
    racing_margin: float = 0.03,
    enable_racing: bool = False,
) -> Dict:
    """Evaluate a kernel CCA through LeoReplayer on Starlink traces.

    Returns a DGM-compatible performance dict:
    {
        "accuracy_score": float (0-1),
        "total_resolved_instances": int,
        "total_submitted_instances": int,
        "total_resolved_ids": [...],
        "total_unresolved_ids": [...],
        "total_emptypatch_ids": [],
        "per_trace": {...},
    }
    """
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="cca_eval_")

    per_trace = {}
    resolved_ids = []
    unresolved_ids = []

    print(f"[cca_harness] Evaluating '{cca_name}' on {len(trace_pairs)} traces ({duration}s each)")

    for i, (bw, delay) in enumerate(trace_pairs):
        trace_id = f"{Path(bw).parent.parent.name}/{Path(bw).parent.name}"
        trace_work = os.path.join(work_dir, f"trace_{i}")

        # Set per-trace reconfiguration offset (LeoCC SIGCOMM'25 requirement)
        trace_offset = extract_offset(delay)
        try:
            _set_module_param("offset", trace_offset)
        except Exception:
            pass  # non-fatal: module may not be loaded yet or param may not exist

        print(f"  [{i+1}/{len(trace_pairs)}] {trace_id} (offset={trace_offset}ms)...", end=" ", flush=True)

        tr = _run_single_trace(
            cca_name, bw, delay, duration,
            uplink_queue_pkts, downlink_queue_pkts,
            uplink_loss_rate, downlink_loss_rate,
            trace_work,
        )
        objectives = _compute_trace_fitness(tr)
        tr["objectives"] = objectives
        tr["fitness"] = objectives["fitness"]  # float for backward compat
        per_trace[trace_id] = tr

        scalar = objectives["scalar"]
        if tr.get("error"):
            print(f"err={tr['error'][:40]}")
            unresolved_ids.append(trace_id)
        elif scalar >= resolve_threshold:
            print(f"{tr.get('throughput_mbps',0):.1f}Mbps rtt={tr.get('rtt_ms',0):.1f}ms "
                  f"u={objectives['util']:.2f} dq={objectives['delay_quality']:.2f} "
                  f"le={objectives['loss_efficiency']:.2f} s={scalar:.3f} OK")
            resolved_ids.append(trace_id)
        else:
            print(f"{tr.get('throughput_mbps',0):.1f}Mbps rtt={tr.get('rtt_ms',0):.1f}ms "
                  f"u={objectives['util']:.2f} dq={objectives['delay_quality']:.2f} "
                  f"le={objectives['loss_efficiency']:.2f} s={scalar:.3f} BELOW")
            unresolved_ids.append(trace_id)

        # Sequential early elimination with fixed margin.
        # Starting at racing_traces, check after EVERY trace whether the running
        # mean is below parent by more than racing_margin. This gives later traces
        # a chance to recover a slow start, while still aborting clearly hopeless
        # candidates early. Skipped traces are imputed as zero fitness to avoid
        # inflating early-stopped candidates relative to full evaluations.
        if (enable_racing and parent_score is not None
                and i >= racing_traces - 1 and i < len(trace_pairs) - 1):
            racing_scores = [per_trace[tid]["fitness"] for tid in per_trace]
            racing_mean = sum(racing_scores) / len(racing_scores)
            if racing_mean < parent_score - racing_margin:
                print(f"[cca_harness] EARLY_STOP after {i+1}/{len(trace_pairs)} traces: "
                      f"mean={racing_mean:.3f} < parent={parent_score:.3f} - {racing_margin}")
                break

    total_evaluated = len(per_trace)
    total = len(trace_pairs)

    # Aggregate: mean over ALL requested traces. Skipped traces (from early-stop)
    # are imputed as 0.0 fitness so early-stopped candidates are always penalized
    # relative to fully-evaluated ones. This prevents denominator bias.
    accuracy = (
        sum(t["fitness"] for t in per_trace.values()) / total
        if total > 0 else 0.0
    )

    # 4D objective vector (median per dim, min for robustness)
    utils = [t["objectives"]["util"] for t in per_trace.values() if "objectives" in t]
    delay_qs = [t["objectives"]["delay_quality"] for t in per_trace.values() if "objectives" in t]
    loss_effs = [t["objectives"]["loss_efficiency"] for t in per_trace.values() if "objectives" in t]

    if utils:
        obj_vector = {
            "util": statistics.median(utils),
            "delay_quality": statistics.median(delay_qs),
            "loss_efficiency": statistics.median(loss_effs),
            "robustness": min(utils),  # worst-case utilization across traces
        }
        # Bottleneck: which dimension has worst deviation from ideal
        f = (obj_vector["util"], obj_vector["delay_quality"],
             obj_vector["loss_efficiency"], obj_vector["robustness"])
        deviations = {
            "util": DEFAULT_WEIGHTS[0] * (IDEAL_POINT[0] - f[0]),
            "delay_quality": DEFAULT_WEIGHTS[1] * (IDEAL_POINT[1] - f[1]),
            "loss_efficiency": DEFAULT_WEIGHTS[2] * (IDEAL_POINT[2] - f[2]),
            "robustness": DEFAULT_WEIGHTS[3] * (IDEAL_POINT[3] - f[3]),
        }
        bottleneck_dim = max(deviations, key=deviations.get)
        bottleneck_gap = max(0.0, deviations[bottleneck_dim])
    else:
        obj_vector = {"util": 0.0, "delay_quality": 0.0, "loss_efficiency": 0.0, "robustness": 0.0}
        bottleneck_dim = "util"
        bottleneck_gap = 1.0

    # LeoCC-aligned selection score: 2D weighted (util × delay_quality).
    # No dispersion penalty, no repeat-eval penalty.
    # Mirrors LeoCC SIGCOMM 2025: raw average throughput vs average delay.
    #
    # Util floor: CCAs below 50% utilization get a steep penalty to prevent
    # the "send nothing, perfect delay" attractor (reviewer SIGCOMM critique).
    # Without this, a CCA at 50% util / 100% delay scores 0.70, too close
    # to the seed at 0.80. The floor makes low-util CCAs score near zero,
    # creating a clear gradient toward the seed's operating region.
    UTIL_FLOOR = 0.50
    util_for_score = obj_vector["util"]
    if util_for_score < UTIL_FLOOR:
        # Quadratic penalty: score drops rapidly below floor
        util_for_score = util_for_score * (util_for_score / UTIL_FLOOR)

    base_selection_score = (
        SELECTION_WEIGHTS["util"] * util_for_score
        + SELECTION_WEIGHTS["delay_quality"] * obj_vector["delay_quality"]
        + SELECTION_WEIGHTS["loss_efficiency"] * obj_vector["loss_efficiency"]
        + SELECTION_WEIGHTS["robustness"] * obj_vector["robustness"]
    )

    confidence_penalty = 0.0
    selection_score = max(0.0, min(1.0, base_selection_score))

    # Core DGM-compatible scalar remains mean per-trace score.
    tchebycheff_score = _tchebycheff_scalar(
        (obj_vector["util"], obj_vector["delay_quality"], obj_vector["loss_efficiency"], obj_vector["robustness"]),
        IDEAL_POINT,
        DEFAULT_WEIGHTS,
    )

    early_stopped = len(per_trace) < total

    result = {
        # DGM-compatible fields (backward compat)
        "accuracy_score": accuracy,
        "total_resolved_instances": len(resolved_ids),
        "total_submitted_instances": total,
        "total_resolved_ids": resolved_ids,
        "total_unresolved_ids": unresolved_ids,
        "total_emptypatch_ids": [],
        "per_trace": per_trace,
        # New: 4D Pareto-aware fields
        "objective_vector": obj_vector,
        "bottleneck_dim": bottleneck_dim,
        "bottleneck_gap": bottleneck_gap,
        "tchebycheff_score": tchebycheff_score,
        "selection_score": selection_score,
        "raw_score": base_selection_score,
        "confidence_penalty": confidence_penalty,
        "mean_trace_score": accuracy,
        # Racing / early-stop metadata
        "early_stopped": early_stopped,
        "racing_traces_evaluated": len(per_trace),
    }

    print(f"[cca_harness] Result: score={selection_score:.3f} "
          f"u={obj_vector['util']:.2f} dq={obj_vector['delay_quality']:.2f} "
          f"le={obj_vector['loss_efficiency']:.2f} rob={obj_vector['robustness']:.2f} "
          f"bottleneck={bottleneck_dim}({bottleneck_gap:.3f})")
    return result


def build_and_load_cca(
    source_dir: str,
    module_name: str = "tcp_evolved",
) -> Optional[str]:
    """Build kernel module from source and load it.

    Returns error string or None on success.
    """
    # Clean stale build artifacts (may be owned by root from prior sudo runs)
    abs_dir = os.path.abspath(source_dir)
    subprocess.run(
        ["sudo", "make", "-C", f"/lib/modules/{os.uname().release}/build",
         f"M={abs_dir}", "clean"],
        capture_output=True, text=True, timeout=30,
    )
    # Also remove hidden dep files that make-clean misses
    subprocess.run(
        ["sudo", "bash", "-c", f"rm -f {abs_dir}/.*.o.d {abs_dir}/.*.o.cmd {abs_dir}/.*.ko.cmd"],
        capture_output=True, text=True, timeout=10,
    )

    # Build with sudo (workspace may be root-owned) and -Wno-error
    # so unused-variable warnings from LLM edits don't kill the build
    result = subprocess.run(
        ["sudo", "make", "-C", f"/lib/modules/{os.uname().release}/build",
         f"M={abs_dir}", "modules",
         "EXTRA_CFLAGS=-Wno-error"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return f"build failed: {result.stderr[-500:]}"

    ko_path = os.path.join(source_dir, f"{module_name}.o".replace(".o", ".ko"))
    if not os.path.exists(ko_path):
        # Try finding it
        import glob
        kos = glob.glob(os.path.join(source_dir, "*.ko"))
        if not kos:
            return "no .ko file after build"
        ko_path = kos[0]

    # Unload any prior instance before insmod.
    unload_err = _force_unload_module(module_name)
    if unload_err is not None:
        return f"pre-insmod unload failed: {unload_err}"

    # Load
    result = subprocess.run(
        ["sudo", "insmod", ko_path],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        err = result.stderr.strip()
        if "File exists" in err:
            retry_unload_err = _force_unload_module(module_name, retries=6, sleep_s=0.5)
            if retry_unload_err is None:
                retry = subprocess.run(
                    ["sudo", "insmod", ko_path],
                    capture_output=True, text=True, timeout=10,
                )
                if retry.returncode == 0:
                    return None
                err = retry.stderr.strip()
            else:
                err = f"{err}; retry_unload_failed: {retry_unload_err}"
        return f"insmod failed: {err}"

    return None


def unload_cca(module_name: str = "tcp_evolved") -> Optional[str]:
    """Unload kernel module. Returns error or None."""
    if not _module_loaded(module_name):
        return None
    err = _force_unload_module(module_name)
    if err is not None:
        return f"rmmod failed: {err}"
    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="CCA evaluation harness")
    parser.add_argument("--cca", required=True, help="CCA name (as in iperf3 -C)")
    parser.add_argument("--traces", required=True, help="Trace directory")
    parser.add_argument("--num", type=int, default=5, help="Number of traces")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION, help="Seconds per trace")
    args = parser.parse_args()

    traces = find_trace_pairs(args.traces, max_traces=args.num)
    if not traces:
        print(f"No traces found in {args.traces}")
        exit(1)

    result = evaluate_cca(args.cca, traces, duration=args.duration)
    print(json.dumps(result, indent=2, default=str))

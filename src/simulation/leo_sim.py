# LEO satellite simulation extensions for packet-level simulator.
# Adds time-varying network conditions: capacity oscillation, handoff events,
# non-congestion loss — calibrated from published Starlink measurements.
#
# References:
#   - LeoCC (SIGCOMM 2025): 4800 real Starlink traces, 10-70 Mbps capacity
#   - IMC 2022: 15-second reconfiguration intervals
#   - APNIC 2024: +30-50ms handoff RTT spikes, ~7ms jitter
#   - LeoTCP 2025: 20-200ms hard handover disconnects

import math
import random
import signal
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .network_sim import (
    BYTES_PER_PACKET,
    EVENT_ACK,
    EVENT_SEND,
    Link,
    Network,
    Sender,
    SimResult,
)


class _WarmupCCA:
    """Wraps a CCA to track acks after a warmup period.

    The simulator sets ``_sim_time`` on the CCA before each callback.
    This wrapper forwards every call and counts post-warmup acks so
    that utilization is measured in steady state, not during slow-start.
    """

    def __init__(self, inner, warmup_end: float):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_warmup_end", warmup_end)
        object.__setattr__(self, "acked_after_warmup", 0)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        if name == "_sim_time":
            # Keep both copies in sync
            object.__setattr__(self, name, value)
            self._inner._sim_time = value
        elif name in ("_inner", "_warmup_end", "acked_after_warmup"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    def on_ack(self, rtt):
        if getattr(self, "_sim_time", 0.0) >= self._warmup_end:
            object.__setattr__(self, "acked_after_warmup", self.acked_after_warmup + 1)
        self._inner.on_ack(rtt)

    def on_loss(self):
        self._inner.on_loss()

    def on_send(self):
        self._inner.on_send()

    def get_cwnd(self):
        return self._inner.get_cwnd()

    def get_rate(self):
        return self._inner.get_rate()

    def reset(self):
        if hasattr(self._inner, "reset"):
            self._inner.reset()


class LEOLink(Link):
    """Network link with time-varying conditions modeling LEO satellite dynamics.

    Extends Link with:
    - Capacity schedule: bandwidth changes at specified times
    - Handoff events: periodic RTT spikes + loss bursts every ~15s
    - Baseline jitter: random delay variation within intervals
    """

    def __init__(
        self,
        bandwidth: float,
        delay: float,
        queue_size: int,
        loss_rate: float,
        capacity_schedule: Optional[List[Tuple[float, float]]] = None,
        handoff_events: Optional[List[Tuple[float, float, float, float]]] = None,
        jitter_std: float = 0.0,
    ):
        """
        Args:
            bandwidth: base packets per second
            delay: base one-way propagation delay (seconds)
            queue_size: max queue size in packets
            loss_rate: baseline random loss probability
            capacity_schedule: [(time, new_bw_pps), ...] — bandwidth changes
            handoff_events: [(time, duration_s, extra_delay_s, extra_loss), ...]
            jitter_std: std dev of random delay jitter (seconds)
        """
        super().__init__(bandwidth, delay, queue_size, loss_rate)
        self.base_bw = bandwidth
        self.base_delay = delay
        self.queue_size_pkts = queue_size
        self.capacity_schedule = sorted(capacity_schedule or [], key=lambda x: x[0])
        self.handoff_events = sorted(handoff_events or [], key=lambda x: x[0])
        self.jitter_std = jitter_std

    def _get_current_bw(self, event_time: float) -> float:
        """Look up current bandwidth from capacity schedule."""
        bw = self.base_bw
        for t, new_bw in self.capacity_schedule:
            if event_time >= t:
                bw = new_bw
            else:
                break
        return bw

    def _get_handoff_state(self, event_time: float) -> Tuple[bool, float, float]:
        """Check if currently in a handoff event.

        Returns: (in_handoff, extra_delay, extra_loss)
        """
        for t, dur, extra_delay, extra_loss in self.handoff_events:
            if t <= event_time < t + dur:
                return True, extra_delay, extra_loss
        return False, 0.0, 0.0

    def get_cur_latency(self, event_time: float) -> float:
        """Propagation + queue delay + handoff spike + jitter."""
        base_lat = self.dl + self.get_cur_queue_delay(event_time)
        # Handoff extra delay
        in_handoff, extra_delay, _ = self._get_handoff_state(event_time)
        if in_handoff:
            base_lat += extra_delay
        # Random jitter
        if self.jitter_std > 0:
            base_lat += abs(random.gauss(0, self.jitter_std))
        return max(0.001, base_lat)  # never zero

    def packet_enters_link(self, event_time: float) -> bool:
        """Check if packet is accepted, with time-varying loss and bandwidth."""
        # Time-varying loss
        in_handoff, _, extra_loss = self._get_handoff_state(event_time)
        effective_loss = self.lr + (extra_loss if in_handoff else 0.0)
        if random.random() < effective_loss:
            return False

        # Time-varying bandwidth for queue model
        cur_bw = self._get_current_bw(event_time)
        if cur_bw <= 0:
            return False

        self.queue_delay = self.get_cur_queue_delay(event_time)
        self.queue_delay_update_time = event_time
        extra_delay = 1.0 / cur_bw
        # Max queue delay based on current bandwidth
        cur_max_queue = self.queue_size_pkts / cur_bw if cur_bw > 0 else 0
        if extra_delay + self.queue_delay > cur_max_queue:
            return False
        self.queue_delay += extra_delay
        return True

    def reset(self):
        super().reset()


def _build_capacity_schedule(
    duration: float,
    interval: float,
    bw_range: Tuple[float, float],
    seed: int = 42,
) -> List[Tuple[float, float]]:
    """Generate a capacity schedule with step changes at regular intervals.

    Uses deterministic sawtooth oscillation between lo and hi to ensure
    the time-weighted mean equals (lo+hi)/2 over full cycles.
    Starting phase varies by seed for per-seed variation.
    Seeded RNG adds ±10% noise to each level.
    """
    rng = random.Random(seed)
    schedule = []
    t = 0.0
    lo, hi = bw_range
    # Vary starting phase by seed — prevents always starting at minimum
    start_phase = ((seed * 2654435761) >> 16) % 4  # hash-based for better phase distribution
    i = 0
    while t < duration:
        # Sawtooth: cycle through 4 levels with seed-dependent start
        frac = ((i + start_phase) % 4) / 3.0  # 0, 0.33, 0.67, 1.0
        base_bw = lo + frac * (hi - lo)
        # Add ±10% noise per seed for variation
        noise = rng.uniform(-0.1, 0.1) * base_bw
        bw = max(lo * 0.5, base_bw + noise)
        schedule.append((t, bw))
        t += interval
        i += 1
    return schedule


def _build_handoff_events(
    duration: float,
    interval: float,
    handoff_duration: float,
    extra_delay: float,
    extra_loss: float,
    jitter: float = 2.0,
    rng: Optional[random.Random] = None,
) -> List[Tuple[float, float, float, float]]:
    """Generate periodic handoff events with per-event timing jitter.

    Args:
        jitter: max seconds of uniform jitter added to each handoff time (default +-2s).
        rng: seeded Random instance for reproducibility. If None, no jitter applied.
    """
    events = []
    # First handoff at t=interval (not t=0)
    t = interval
    while t < duration:
        if rng is not None and jitter > 0:
            offset = rng.uniform(-jitter, jitter)
            actual_t = max(0.0, t + offset)
        else:
            actual_t = t
        # Only add if the handoff fits within the simulation duration
        if actual_t < duration:
            events.append((actual_t, handoff_duration, extra_delay, extra_loss))
        t += interval
    return events


@dataclass
class LEOScenario:
    """Configuration for a LEO simulation scenario."""
    name: str
    base_bw: float  # pps
    base_delay: float  # one-way seconds
    queue_size: int  # packets
    base_loss: float  # baseline loss rate
    duration: float  # seconds
    handoff_interval: Optional[float] = None  # seconds between handoffs
    handoff_duration: float = 0.1  # handoff event duration (seconds)
    handoff_delay_spike: float = 0.03  # extra delay during handoff (seconds)
    handoff_loss: float = 0.03  # extra loss during handoff
    capacity_range: Optional[Tuple[float, float]] = None  # (min, max) pps
    jitter_std: float = 0.003  # ~3ms jitter std dev


# ── Starlink-calibrated scenarios ──────────────────────────────────

LEO_SCENARIOS = [
    # Scenario 1: Steady state — no handoffs, no capacity variation
    # Tests baseline CCA performance under LEO delay + non-congestion loss
    LEOScenario(
        name="leo_steady",
        base_bw=2500,          # ~30 Mbps (real Starlink mid-range)
        base_delay=0.025,      # 25ms one-way (50ms RTT)
        queue_size=300,
        base_loss=0.005,       # 0.5% non-congestion loss
        duration=30.0,
        handoff_interval=None,
        jitter_std=0.003,
    ),

    # Scenario 2: Regular handoffs — 15s interval, moderate spikes
    # Tests CCA recovery from periodic disruptions
    LEOScenario(
        name="leo_handoff",
        base_bw=2500,          # ~30 Mbps
        base_delay=0.025,
        queue_size=300,
        base_loss=0.005,
        duration=60.0,
        handoff_interval=15.0,
        handoff_duration=0.1,
        handoff_delay_spike=0.030,  # +30ms RTT spike
        handoff_loss=0.03,          # 3% loss during handoff
        jitter_std=0.003,
    ),

    # Scenario 3: Capacity oscillation — bandwidth varies at 15s intervals
    # Tests CCA adaptation to changing link capacity
    LEOScenario(
        name="leo_capacity_osc",
        base_bw=2500,          # ~30 Mbps
        base_delay=0.025,
        queue_size=300,
        base_loss=0.005,
        duration=60.0,
        handoff_interval=15.0,
        handoff_duration=0.1,
        handoff_delay_spike=0.030,
        handoff_loss=0.03,
        capacity_range=(833, 4167),  # oscillates between ~10-50 Mbps
        jitter_std=0.005,
    ),

    # Scenario 4: Aggressive LEO — hard handoffs, high loss, variable delay
    # Worst-case Starlink conditions
    LEOScenario(
        name="leo_aggressive",
        base_bw=1667,          # ~20 Mbps (worst-case Starlink)
        base_delay=0.040,      # 40ms one-way (80ms RTT)
        queue_size=167,
        base_loss=0.01,        # 1% baseline loss
        duration=60.0,
        handoff_interval=15.0,
        handoff_duration=0.2,  # 200ms hard handoff
        handoff_delay_spike=0.050,  # +50ms spike
        handoff_loss=0.06,     # 6% loss during handoff
        capacity_range=(667, 2917),  # ~8-35 Mbps
        jitter_std=0.007,
    ),

    # Scenario 5: High-throughput LEO — best-case, fast link
    # Tests whether CCAs can fully utilize a fast LEO link
    LEOScenario(
        name="leo_high_bw",
        base_bw=4583,          # ~55 Mbps (best-case Starlink)
        base_delay=0.015,      # 15ms one-way (30ms RTT)
        queue_size=458,
        base_loss=0.002,
        duration=60.0,
        handoff_interval=15.0,
        handoff_duration=0.08,
        handoff_delay_spike=0.020,
        handoff_loss=0.01,
        capacity_range=(2500, 5833),  # ~30-70 Mbps
        jitter_std=0.003,
    ),

    # Scenario 6: Deep buffer LEO — bufferbloat risk with satellite delay
    # Tests delay-aware CCAs vs buffer-filling CCAs
    LEOScenario(
        name="leo_deep_buffer",
        base_bw=2500,          # ~30 Mbps
        base_delay=0.030,
        queue_size=1000,       # large buffer (~10x BDP)
        base_loss=0.005,
        duration=60.0,
        handoff_interval=15.0,
        handoff_duration=0.1,
        handoff_delay_spike=0.030,
        handoff_loss=0.02,
        jitter_std=0.003,
    ),
]


class _SimTimeout(Exception):
    pass


def run_leo_simulation(
    cca,
    scenario: LEOScenario,
    seed: int = 42,
    timeout: int = 120,
) -> SimResult:
    """Run a single-flow simulation under LEO satellite conditions.

    Args:
        cca: CCA object with on_ack/on_loss/on_send/get_cwnd/get_rate
        scenario: LEOScenario configuration
        seed: random seed
        timeout: max wall-clock seconds before aborting (catches degenerate CCAs)

    Returns:
        SimResult with throughput, latency, loss metrics.

    Raises:
        _SimTimeout: if simulation exceeds wall-clock timeout
    """
    def _alarm(signum, frame):
        raise _SimTimeout(f"Simulation timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout)

    try:
        return _run_leo_simulation_inner(cca, scenario, seed)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _run_leo_simulation_inner(cca, scenario, seed):
    random.seed(seed)
    handoff_rng = random.Random(seed)

    # Build capacity schedule
    cap_schedule = None
    if scenario.capacity_range is not None:
        cap_schedule = _build_capacity_schedule(
            scenario.duration,
            scenario.handoff_interval or 15.0,
            scenario.capacity_range,
            seed=seed,
        )

    # Build handoff events (with +-2s timing jitter per event)
    handoff_events = None
    if scenario.handoff_interval is not None:
        handoff_events = _build_handoff_events(
            scenario.duration,
            scenario.handoff_interval,
            scenario.handoff_duration,
            scenario.handoff_delay_spike,
            scenario.handoff_loss,
            jitter=2.0,
            rng=handoff_rng,
        )

    # Store queue size for max_queue_delay recalculation
    fwd = LEOLink(
        scenario.base_bw,
        scenario.base_delay,
        scenario.queue_size,
        scenario.base_loss,
        capacity_schedule=cap_schedule,
        handoff_events=handoff_events,
        jitter_std=scenario.jitter_std,
    )
    # Reverse path: simple, no LEO dynamics
    rev = Link(scenario.base_bw, scenario.base_delay, scenario.queue_size, 0.0)
    path = [fwd, rev]

    # Wrap CCA to track post-warmup acks for steady-state utilization
    warmup = min(10.0, scenario.duration * 0.3)
    wrapped_cca = _WarmupCCA(cca, warmup)

    initial_rate = 0.5 * scenario.base_bw
    sender = Sender(wrapped_cca, initial_rate, path, dest=0)

    net = Network([sender], [fwd, rev])
    net.run_for_dur(scenario.duration)

    # Compute average bandwidth over the post-warmup window only.
    # Throughput is measured post-warmup, so avg_bw must match that window
    # to prevent >100% utilization from capacity schedule misalignment.
    if cap_schedule:
        total_time = 0.0
        total_bw_time = 0.0
        for i, (t, bw) in enumerate(cap_schedule):
            next_t = cap_schedule[i + 1][0] if i + 1 < len(cap_schedule) else scenario.duration
            # Clip interval to [warmup, duration]
            t0 = max(t, warmup)
            t1 = max(next_t, warmup)
            dt = t1 - t0
            if dt > 0:
                total_bw_time += bw * dt
                total_time += dt
        avg_bw = total_bw_time / total_time if total_time > 0 else scenario.base_bw
    else:
        avg_bw = scenario.base_bw

    result = SimResult(
        packets_sent=sender.sent,
        packets_acked=sender.acked,
        packets_lost=sender.lost,
        duration=scenario.duration,
        link_bw=avg_bw,
        link_delay=scenario.base_delay,
        link_queue=scenario.queue_size,
        link_loss=scenario.base_loss,
    )

    if sender.acked > 0 and scenario.duration > 0:
        # Use post-warmup acks for throughput to exclude slow-start transient
        steady_acked = wrapped_cca.acked_after_warmup
        steady_dur = scenario.duration - warmup
        if steady_acked > 0 and steady_dur > 0:
            result.throughput_bps = 8.0 * steady_acked * BYTES_PER_PACKET / steady_dur
        else:
            # Fallback: use all acks (short sim or no post-warmup acks)
            result.throughput_bps = 8.0 * sender.acked * BYTES_PER_PACKET / scenario.duration
        # RTT samples include warmup (they don't bias as much)
        result.avg_latency = sum(sender.rtt_samples) / len(sender.rtt_samples)
        sorted_rtts = sorted(sender.rtt_samples)
        p95_idx = int(0.95 * len(sorted_rtts))
        result.p95_latency = sorted_rtts[min(p95_idx, len(sorted_rtts) - 1)]

    if sender.sent > 0:
        result.loss_rate = sender.lost / sender.sent

    return result


def _average_sim_results(results_list: List[SimResult]) -> SimResult:
    """Average multiple SimResult instances across seeds.

    Averages: utilization (via throughput_bps), avg_latency, p95_latency,
    loss_rate, packets_acked, packets_sent, packets_lost.
    Preserves link_bw/link_delay/link_queue/link_loss/duration from first result.
    """
    n = len(results_list)
    if n == 0:
        return SimResult()
    if n == 1:
        return results_list[0]

    ref = results_list[0]
    avg = SimResult(
        throughput_bps=sum(r.throughput_bps for r in results_list) / n,
        avg_latency=sum(r.avg_latency for r in results_list) / n,
        p95_latency=sum(r.p95_latency for r in results_list) / n,
        loss_rate=sum(r.loss_rate for r in results_list) / n,
        packets_sent=int(sum(r.packets_sent for r in results_list) / n),
        packets_acked=int(sum(r.packets_acked for r in results_list) / n),
        packets_lost=int(sum(r.packets_lost for r in results_list) / n),
        duration=ref.duration,
        link_bw=sum(r.link_bw for r in results_list) / n,
        link_delay=ref.link_delay,
        link_queue=ref.link_queue,
        link_loss=ref.link_loss,
    )
    return avg


def evaluate_leo_benchmark(
    cca,
    scenarios: Optional[List[LEOScenario]] = None,
    seed: int = 42,
    seeds: Optional[List[int]] = None,
) -> dict:
    """Run CCA through all LEO scenarios and return results.

    Args:
        cca: CCA object with on_ack/on_loss/on_send/get_cwnd/get_rate
        scenarios: list of LEOScenario (default: LEO_SCENARIOS)
        seed: single seed (used if seeds is None, for backward compat)
        seeds: list of seeds to average over (default: [42, 7, 99]).
            Each scenario is run once per seed and results are averaged.

    Returns dict with per-scenario SimResult (averaged across seeds) and aggregate metrics.
    """
    if scenarios is None:
        scenarios = LEO_SCENARIOS
    if seeds is None:
        seeds = [42, 7, 99, 2024, 314]  # 5 seeds, covers all 4 capacity phases

    results = {}
    for scenario in scenarios:
        seed_results = []
        errors = []
        for s in seeds:
            if hasattr(cca, 'reset'):
                cca.reset()
            try:
                result = run_leo_simulation(cca, scenario, seed=s)
                seed_results.append(result)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")

        if seed_results:
            results[scenario.name] = {
                "result": _average_sim_results(seed_results),
                "error": None,
            }
        else:
            results[scenario.name] = {
                "result": SimResult(),
                "error": "; ".join(errors) if errors else "all seeds failed",
            }

    return results

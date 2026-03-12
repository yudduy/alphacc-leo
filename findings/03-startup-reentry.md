# Finding 3: Over-Aggressive STARTUP Re-Entry During LEO Handoffs

## Summary

LeoCC uses a timer-based mechanism to detect LEO satellite reconfigurations (handoffs) and re-enters STARTUP mode with 2.885x pacing gain. On low-capacity uplink paths, this aggressive probing fills the queue for 15-25ms per handoff event, degrading latency without proportional throughput benefit.

## The Mechanism

LeoCC detects reconfigurations using a periodic timer aligned to the satellite handoff schedule:

```c
static const int PERIOD = 15000;  // 15s handoff period
static u32 offset = 12000;        // ms offset within period

// In evolved_main():
u32 delta_since_start = (tcp_jiffies32 - init_stamp) * 1000 / HZ;
u32 relative_time = delta_since_start % PERIOD;
u32 magic_offet = 100;  // 100ms trigger window

if (!leocc->reconfiguration_trigger &&
    relative_time >= offset - magic_offet &&
    relative_time <= offset) {
    leocc->reconfiguration_trigger = 1;
}
```

When `reconfiguration_trigger` fires, `leocc_reset_mode()` re-enters STARTUP:

```c
static void leocc_reset_mode(struct sock *sk)
{
    struct leocc *leocc = inet_csk_ca(sk);
    if (!leocc_full_bw_reached(sk) || leocc->reconfiguration_trigger)
        leocc_reset_startup_mode(sk);  // 2.885x pacing gain
    else
        leocc_reset_probe_bw_mode(sk);
}
```

STARTUP uses `leocc_high_gain = 2.885x` pacing gain — nearly 3x the estimated bandwidth. On LEO uplink paths with ~18-49 Mbps capacity and 500-packet queues, this overshoot fills the queue rapidly.

## The Problem

1. **Timer assumes known handoff schedule.** The `offset` parameter must be calibrated per satellite constellation. Different Starlink ground stations have different handoff timing. A miscalibrated offset triggers STARTUP during normal operation.

2. **2.885x is too aggressive for uplink.** Downlink has ~420 Mbps capacity — 2.885x overshoot is absorbed by the large BDP. Uplink has 18-49 Mbps — the same overshoot fills the 500-packet queue in ~2 RTTs.

3. **Every handoff triggers a full STARTUP.** Even when the capacity change is minor (e.g., 30 Mbps → 35 Mbps), the CCA re-probes with 2.885x as if starting from zero.

## Impact

- **-15ms mean RTT** when the aggressive re-entry is replaced with passive detection
- -2pp utilization (the cost of less aggressive probing)
- Net: **+5-8% composite score** on uplink traces

The RTT improvement is consistent across all 5 uplink traces (5.7ms to 22.5ms reduction). The utilization cost is small and concentrated on traces with large capacity changes.

## Fix: Event-Based Reconfiguration Detection

Replace the timer-based trigger with passive detection via RTT observations during PROBE_RTT:

```c
// In leocc_update_bw():
if (leocc->mode == LEOCC_PROBE_RTT && rs->rtt_us > 0 &&
    leocc->rtt_hat_post > rs->rtt_us + delta_thresh) {
    // RTT dropped significantly → likely reconfiguration
    leocc->reconfiguration_max_bw = leocc->latest_bw;
}
```

**Key change:** Instead of predicting when handoffs occur (timer), detect their effects (RTT changes). This is:
- **More robust:** Works regardless of handoff schedule or offset calibration
- **Less aggressive:** Only detects reconfigurations that actually change the RTT, not every 15s tick
- **Lower latency cost:** Doesn't trigger 2.885x overshoot for minor capacity changes

## How We Found It

This was the first DGM mutation (gen3-m1) that improved over the LeoCC seed. The LLM removed the timer-based STARTUP re-entry and the system scored 0.799 vs 0.787 on 15 real Starlink traces.

## Verification

- **15 Starlink traces**, 120s each, via LeoReplayer/Mahimahi
- **Uplink:** Evolved wins on all 5 traces (32.9-40.8ms vs 38.6-63.3ms RTT)
- **Downlink:** Comparable performance (within noise)
- See [results/leo_benchmark.md](../results/leo_benchmark.md) for full trace breakdown

## Design Trade-off

This finding highlights a fundamental tension in LEO CCA design:

**Aggressive probing** (LeoCC's approach): Fast capacity tracking after handoffs, at the cost of queuing delay. Optimal for throughput-sensitive applications on high-capacity downlink.

**Passive detection** (evolved approach): Lower latency, at the cost of slower capacity tracking. Optimal for latency-sensitive applications on capacity-constrained uplink.

The "right" answer depends on the deployment context. For interactive applications (video calls, gaming) over Starlink uplink, the evolved approach is strictly better. For bulk transfers on downlink, the original LeoCC approach may be preferable.

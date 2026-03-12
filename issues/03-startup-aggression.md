# Issue: Timer-based STARTUP re-entry causes queue-filling on low-capacity uplink

## Description

LeoCC detects satellite reconfigurations using a periodic timer (`PERIOD = 15000ms`, `offset = 12000ms`) and re-enters STARTUP mode with 2.885x pacing gain. On low-capacity uplink paths (18-49 Mbps), this aggressive probing fills the 500-packet queue, spiking RTT by 15-25ms per handoff event.

## Location

`leocc/simulation/leocc.c`, `evolved_main()`:
```c
u32 relative_time = delta_since_start % PERIOD;
u32 magic_offet = 100;
if (!leocc->reconfiguration_trigger &&
    relative_time >= offset - magic_offet && relative_time <= offset) {
    leocc->reconfiguration_trigger = 1;
}
```

And `leocc_reset_mode()`:
```c
if (!leocc_full_bw_reached(sk) || leocc->reconfiguration_trigger)
    leocc_reset_startup_mode(sk);  // 2.885x pacing gain
```

## Observed Impact

On 5 uplink Starlink traces (120s each), replacing timer-based STARTUP re-entry with event-based detection reduced mean RTT by **14.5ms** (from 50.9ms to 36.4ms) at a cost of -2pp utilization. Net composite improvement: +1.5%.

| Trace | LeoCC RTT | Evolved RTT | Reduction |
|-------|-----------|-------------|-----------|
| uplink/1 | 38.6ms | 32.9ms | -5.7ms |
| uplink/10 | 63.3ms | 40.8ms | -22.5ms |
| uplink/11 | 55.4ms | 40.3ms | -15.1ms |
| uplink/12 | 45.8ms | 37.2ms | -8.6ms |
| uplink/13 | 51.5ms | 40.7ms | -10.8ms |

Downlink traces showed comparable performance (the larger BDP absorbs the overshoot).

## Root Cause

1. **2.885x is calibrated for downlink** (~420 Mbps). On uplink (18-49 Mbps, 10-20x lower), the same multiplicative overshoot fills the 500-packet queue in ~2 RTTs.
2. **Timer assumes known handoff schedule.** The `offset` parameter must be calibrated per constellation. Miscalibration triggers STARTUP during normal operation.
3. **Every handoff triggers full STARTUP.** Even minor capacity changes (30→35 Mbps) get the same 2.885x treatment.

## Alternative: Event-Based Detection

Our evolved variant detects reconfigurations via RTT drops in PROBE_RTT:
```c
if (leocc->mode == LEOCC_PROBE_RTT && rs->rtt_us > 0 &&
    leocc->rtt_hat_post > rs->rtt_us + delta_thresh) {
    leocc->reconfiguration_max_bw = leocc->latest_bw;
}
```

This is less aggressive (may miss some reconfigurations) but avoids queue-filling. The trade-off favors latency-sensitive applications on uplink.

## Suggestion

Consider:
1. **Asymmetric pacing gains**: Use 2.885x for downlink, lower (e.g., 1.5-2.0x) for uplink
2. **BDP-proportional probing**: Scale pacing gain inversely with BDP (smaller pipes need less overshoot)
3. **Event-based detection**: Supplement timer with passive RTT monitoring for deployments where handoff timing is unknown

## Context

Found via automated CCA evolution (DGM-CCA) as the first mutation (gen3-m1) that improved over the LeoCC seed. Full analysis and trace-by-trace data: https://github.com/yudduy/alphacc-leo/blob/main/findings/03-startup-reentry.md

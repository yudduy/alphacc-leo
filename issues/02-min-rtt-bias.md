# Issue: Spurious min_rtt samples from ACK aggregation bias BDP downward

## Description

`leocc_update_min_rtt()` accepts any RTT sample lower than the current minimum within the 20-second window, including samples during DYNAMIC_CRUISE when the queue is non-empty. On LEO paths with ACK aggregation and jitter, this allows spuriously low RTT samples to pin `min_rtt` below the true propagation delay, causing systematic BDP underestimation.

## Location

`leocc/simulation/leocc.c`, `leocc_update_min_rtt()`:
```c
if (rs->rtt_us >= 0 &&
    (rs->rtt_us < leocc->min_rtt_us ||
     (filter_expired && !rs->is_ack_delayed))) {
    leocc->min_rtt_us = rs->rtt_us;
```

## Root Cause

Three LEO-specific phenomena produce artificially low RTT samples:
1. **ACK aggregation**: Batched ACKs at receiver/intermediate node. First ACK in burst shows low RTT.
2. **Timestamp granularity**: TCP timestamps have ms granularity; on ~25ms base RTT, 1ms error = 4% bias.
3. **Jitter asymmetry**: Forward/return path jitter is asymmetric on satellite links. Samples catching low jitter in both directions are unrepresentative.

Since `min_rtt` is a running minimum, even one spurious sample pins it low for the entire 20s window.

## Observed Impact

+2pp utilization when `min_rtt` updates are restricted to PROBE_RTT, STARTUP, or low-inflight states (where queue is drained and measurements are most reliable).

## Suggested Fix

```diff
+    bool allow_min_decrease = (leocc->mode == LEOCC_PROBE_RTT) ||
+                              (leocc->mode == LEOCC_STARTUP) ||
+                              (leocc->min_rtt_us == 0) ||
+                              (tcp_packets_in_flight(tp) <= leocc_probe_rtt_cwnd(sk));
     if (rs->rtt_us >= 0 &&
-        (rs->rtt_us < leocc->min_rtt_us ||
+        ((allow_min_decrease && rs->rtt_us < leocc->min_rtt_us) ||
          (filter_expired && !rs->is_ack_delayed))) {
```

**Note:** This guard may be too aggressive on some traces where RTT decreases genuinely reflect reduced queuing. A more nuanced version could require sustained low RTT for N rounds before accepting. We observed one outlier trace (-0.136 regression) where the guard prevented a legitimate min_rtt update.

## Applicability

BBR's `bbr_update_min_rtt()` has the identical vulnerability — it also accepts any sample as a min_rtt candidate. This issue affects any path with significant ACK aggregation (satellite, cellular, Wi-Fi with TX aggregation).

## Context

Found via automated CCA evolution (DGM-CCA). Full analysis: https://github.com/yudduy/alphacc-leo/blob/main/findings/02-min-rtt-guard.md

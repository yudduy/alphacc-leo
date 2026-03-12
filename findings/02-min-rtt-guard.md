# Finding 2: Spurious min_rtt Samples Bias BDP Estimation

## Summary

LeoCC (and BBR) accept any RTT sample as a candidate for `min_rtt`, the estimated propagation delay. On LEO satellite paths with ACK aggregation and jitter, spurious low RTT samples systematically pin `min_rtt` below the true propagation delay, causing BDP underestimation and chronic underutilization.

## The Problem

LeoCC's `min_rtt` update logic (from BBR):

```c
if (rs->rtt_us >= 0 &&
    (rs->rtt_us < leocc->min_rtt_us ||
     (filter_expired && !rs->is_ack_delayed))) {
    leocc->min_rtt_us = rs->rtt_us;
    leocc->min_rtt_stamp = tcp_jiffies32;
}
```

This accepts **any** RTT sample that is lower than the current minimum within a 20-second window. On LEO paths, several phenomena produce artificially low RTT samples:

1. **ACK aggregation**: Multiple ACKs batched at the receiver or intermediate node arrive simultaneously. The first ACK in a burst may appear to have very low RTT because the batching delays were absorbed by the receiver.

2. **Timestamp granularity**: TCP timestamps have millisecond granularity. On paths with ~25ms base RTT, a 1ms error is a 4% bias.

3. **Jitter asymmetry**: LEO links have asymmetric jitter — forward path jitter (satellite to ground) differs from return path. A sample that catches low forward jitter AND low return jitter is unrepresentatively fast.

**The consequence:** `min_rtt` drifts below the true propagation delay. Since BDP = bandwidth × min_rtt, the inflight target is too low. The CCA under-fills the pipe.

## Impact

- **+2pp utilization** when guarded
- Most pronounced on uplink traces where ACK aggregation is stronger (ground station batches ACKs)
- The bias is persistent — min_rtt is a running minimum, so even one spurious sample pins it low for the entire 20s window

## Fix

Restrict `min_rtt` decreases to states where the measurement is most reliable:

```diff
+    bool allow_min_decrease = (leocc->mode == LEOCC_PROBE_RTT) ||
+                              (leocc->mode == LEOCC_STARTUP) ||
+                              (leocc->min_rtt_us == 0) ||
+                              (tcp_packets_in_flight(tp) <= leocc_probe_rtt_cwnd(sk));
+    if ((filter_expired && !rs->is_ack_delayed) ||
+        (allow_min_decrease && rs->rtt_us < leocc->min_rtt_us)) {
         leocc->min_rtt_us = rs->rtt_us;
```

**Rationale:** During PROBE_RTT and STARTUP, the CCA is deliberately draining the queue or starting fresh — RTT samples are closest to the true propagation delay. During DYNAMIC_CRUISE, the queue is non-empty and samples are more susceptible to jitter artifacts.

## How We Found It

DGM-CCA mutation. The LLM identified that the diagnosis showed consistent BDP underestimation across uplink traces and proposed guarding `min_rtt` updates by mode.

## Verification

- **Candidate ID:** `20260305_125913_260885`
- **Score:** 0.766 (vs seed 0.738)
- **Evaluation:** 10 Starlink traces, 120s each

## Interaction with Finding 1

When combined with the Kalman precision fix (Finding 1), the composition scores 0.745 (+1.0% over seed). However, the min_rtt guard can be too aggressive on some traces — it prevents legitimate `min_rtt` updates when capacity increases genuinely reduce queuing delay. A more nuanced version (requiring sustained low RTT for N rounds before accepting) would avoid this edge case.

## Applicability Beyond LeoCC

**BBR has the identical vulnerability.** BBR's `bbr_update_min_rtt()` uses the same global-minimum-over-window approach. Any path with ACK aggregation (satellite, cellular, Wi-Fi) is susceptible. Google's internal BBRv3 may have mitigations not present in the public kernel version.

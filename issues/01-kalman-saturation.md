# Issue: Kalman filter posterior variance saturates at 5-bit max (31)

## Description

The Kalman filter posterior variance fields `p_post_bw` and `p_post_rtt` in `struct leocc` are declared as 5-bit bitfields (max value 31). After a few rounds of the Kalman update, these fields saturate at 31, causing the Kalman gain K = P/(P+R) to freeze at 0.886 (with R=4). The filter can no longer adapt its confidence — it becomes a fixed-weight exponential moving average rather than a proper Kalman filter.

## Location

`leocc/simulation/leocc.c`, `struct leocc`:
```c
u32 ...
    p_post_bw:5,     // saturates at 31
    p_post_rtt:5,    // saturates at 31
```

## Expected Behavior

The posterior variance P should be able to grow when the estimate is uncertain (e.g., after a handoff or capacity change), producing higher Kalman gain K (trust measurements more). With 5 bits, K can never exceed P/(P+R) = 31/35 = 0.886.

## Observed Impact

On Starlink uplink traces (18-49 Mbps, frequent reconfigurations), the frozen Kalman gain causes the bandwidth estimate to lag behind capacity changes. We measured +2pp utilization improvement when widening to 10 bits and clamping at 1023.

## Reproduction

1. Load LeoCC kernel module
2. Run on any Starlink trace with capacity variations
3. Monitor `p_post_bw` via `/proc/net/tcpstat` or tracepoints
4. Observe P saturates at 31 within ~8 rounds and never varies after

## Suggested Fix

```diff
-    p_post_bw:5,
-    p_post_rtt:5,
+    p_post_bw:10,
+    p_post_rtt:10,
```

And clamp the accumulation:
```diff
-    leocc->p_post_bw = leocc->p_post_bw + var_Q;
+    leocc->p_post_bw = min_t(u32, leocc->p_post_bw + var_Q, 1023U);
```

Note: Widening these fields requires adjusting adjacent bitfields to stay within `ICSK_CA_PRIV_SIZE`. We verified the struct fits after this change by reducing `unused` field width.

## Context

Found via automated CCA evolution (DGM-CCA). The LLM-guided mutation system proposed this widening as a fix for underutilization on high-RTT paths. Full analysis: https://github.com/yudduy/alphacc-leo/blob/main/findings/01-kalman-precision.md

# Finding 1: Kalman Filter Precision Saturation

## Summary

LeoCC's Kalman filter posterior variance fields (`p_post_bw`, `p_post_rtt`) use 5-bit unsigned integers, saturating at 31. This causes the Kalman gain to freeze at a fixed value, preventing the bandwidth estimate from tracking capacity changes on LEO satellite links.

## The Bug

In the `struct leocc` definition:

```c
u32 ...
    p_post_bw:5,    // max value: 31
    p_post_rtt:5,   // max value: 31
    ...
```

The Kalman update runs every round:
```c
leocc->p_post_bw = leocc->p_post_bw + var_Q;  // var_Q = 4
leocc->kalman_gain_bw = leocc->p_post_bw * LEOCC_UNIT / (leocc->p_post_bw + var_R);
// ...
leocc->p_post_bw = (LEOCC_UNIT - leocc->kalman_gain_bw) * leocc->p_post_bw / LEOCC_UNIT;
```

**What happens:** After a few rounds, `p_post_bw` saturates at 31 (the 5-bit maximum). With `var_R = 4`:

```
K = P / (P + R) = 31 / (31 + 4) = 0.886
```

The Kalman gain K locks at 0.886 permanently. The filter's posterior variance can never grow beyond 31, so K never adapts. The bandwidth estimate becomes a fixed-weight exponential moving average (weight 0.886) instead of a proper Kalman filter that adjusts its confidence based on estimation uncertainty.

**The consequence:** When LEO link capacity changes (handoffs, orbital dynamics), the filter tracks changes at a fixed rate regardless of how uncertain the estimate is. After a large capacity shift, the filter should temporarily increase K (trust measurements more) — but it can't because P is capped.

## Impact

- **+2pp utilization** on high-RTT Starlink traces when fixed
- Chronic underutilization on paths where capacity changes frequently (uplink, multi-hop)
- The bandwidth estimate lags behind reality after handoff events

## Fix

Widen the bitfields from 5 to 10 bits and clamp accumulation:

```diff
-    p_post_bw:5,
-    p_post_rtt:5,
+    p_post_bw:10,
+    p_post_rtt:10,
```

```diff
-    leocc->p_post_bw = leocc->p_post_bw + var_Q;
+    leocc->p_post_bw = min_t(u32, leocc->p_post_bw + var_Q, 1023U);
```

With 10 bits, P can grow to 1023, giving K up to 0.996 — the filter can genuinely track capacity changes by increasing its gain when uncertain.

## How We Found It

The DGM-CCA evolutionary system proposed this mutation autonomously. The LLM (gpt-5.3-codex) was given the LeoCC source code, per-trace performance metrics, and a diagnosis prompt identifying underutilization as the bottleneck. The mutation scored 0.770 (vs seed 0.738), the highest among 163 evaluated mutations.

## Verification

- **Candidate ID:** `20260305_135541_439629`
- **Score:** 0.770 (2D: 0.60 × util + 0.40 × delay_quality)
- **Evaluation:** 10 Starlink traces, 120s each, via LeoReplayer/Mahimahi
- **Struct size:** Verified `sizeof(struct leocc) <= ICSK_CA_PRIV_SIZE` after widening

## Applicability Beyond LeoCC

This is a general pitfield in kernel CCA implementations using bitfield-packed structs. Any state variable stored in a narrow bitfield that is expected to grow unboundedly will silently saturate. BBR's `struct bbr` also uses bitfields extensively — worth auditing for similar issues.

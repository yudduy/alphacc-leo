# Finding 4: The Formal Verification Gap in LEO Congestion Control

## Summary

LeoCC achieves state-of-the-art empirical performance on Starlink traces (97.3% utilization in simulation, 0.787 composite on real traces) but proves **zero formal safety properties** under CCAC's adversarial model. Simpler AIMD-based CCAs prove 5-8 out of 9 properties but achieve only 85-90% utilization. No known CCA closes this gap.

## Formal Verification with CCAC

[CCAC](https://github.com/venkatarun95/ccac) (SIGCOMM 2021, Venkat Arun et al.) encodes CCAs and a non-deterministic network model as Z3 SMT constraints. To prove a property P, it checks if the negation is satisfiable:
- **UNSAT** = property proven for all possible network behaviors
- **SAT** = counterexample found (adversary that defeats the CCA)

The network model includes: single bottleneck link with capacity C, propagation delay R, finite queue, and a **jitter box** that can delay any packet by up to D seconds. This single adversarial element captures: ACK aggregation, OS scheduling, wireless retransmissions, and variable-rate links.

## Property Battery (9 properties)

| Property | Description | What it proves |
|----------|-------------|----------------|
| cwnd_lower | cwnd >= alpha after loss | Minimum throughput floor |
| cwnd_upper | cwnd <= 2 * C * R | No queue explosion |
| loss_recovery | cwnd recovers after loss | Liveness |
| timeout_recovery | cwnd recovers after timeout | Robustness |
| queue_drains | Queue empties periodically | Bounded delay |
| util_25pct | >= 25% utilization at steady state | Minimum efficiency |
| util_50pct | >= 50% utilization at steady state | Moderate efficiency |
| util_75pct | >= 75% utilization at steady state | High efficiency |
| starvation_ratio | Bounded flow imbalance | Fairness |

## Results

### CCA Formal Properties Comparison

| CCA | Properties | Fails | Utilization (sim) | Notes |
|-----|-----------|-------|-------------------|-------|
| **AIMD-SlowStart** (LLM-evolved) | **8/9** | util_75pct | 82.1% | Best formal |
| AIMD-DelayCap | 5/9 | util_* | 86.3% | Delay-bounding baseline |
| AIMD-LossDiscrim-SS | 5/9 | util_* | 85.4% | +free mechanisms |
| LeoCC (simplified) | 2/8 | most | 97.3% | SOTA empirical |
| BBR (simplified) | 1/8 | most | 96.2% | Near-zero formal |
| CUBIC | 0/8 | all | 92.4% | Loss-based, no formal |

### The Pareto Frontier

```
Formal Properties ↑
    9 |                    ??? (unreachable?)
    8 |  AIMD-SS ●
    7 |
    6 |
    5 |         AIMD-DC ●    AIMD-LDS ●
    4 |
    3 |
    2 |                              LeoCC ●
    1 |                           BBR ●
    0 |                        CUBIC ●
      +----+----+----+----+----+----+----→ Utilization
         80%  82%  84%  86%  88%  90%  95%+
```

The gap between 8/9 formal (82% util) and 2/8 formal (97% util) is **15 percentage points of utilization**. This is the fundamental trade-off we've mapped.

## Why LeoCC Fails Formally

LeoCC's architecture is inherently difficult to verify:

1. **Rate-based control** (pacing gain, BDP estimation) instead of window-based. CCAC's model reasons about cwnd — rate-based behavior must be approximated, losing precision.

2. **Kalman filter state** is continuous and nonlinear. Z3 reasons over integers and linear arithmetic. The Kalman update equations create non-linear dependencies that the SMT solver cannot handle efficiently.

3. **Multi-mode state machine** (STARTUP → DRAIN → DYNAMIC_CRUISE → PROBE_RTT) with complex transition conditions. Each mode has different properties, and the adversary can manipulate mode transitions.

4. **Timer-based reconfiguration detection** depends on global clock state, which CCAC doesn't model (it reasons about packet-level events, not wall clock time).

## The 8/9 Ceiling

The LLM-evolved AIMD-SlowStart CCA achieves 8/9 properties — the highest we've found. The missing property (util_75pct) appears to be in fundamental tension with queue_drains:

- **Profile A** (AIMD-SlowStart): 8/9, fails util_75pct. Has inflight tracking → bounds queue.
- **Profile B** (high-init AIMD): 8/9, fails queue_drains. No inflight tracking → higher utilization.

Achieving 9/9 may require a fundamentally new CCA architecture or refined property definitions.

## Implications

1. **For deployment:** LeoCC's lack of formal guarantees means there exist adversarial network conditions that cause unbounded queue growth, starvation, or cwnd collapse. These may not arise on typical Starlink paths but could on degraded or contested paths.

2. **For research:** The formal-empirical gap suggests that current formal models (CCAC, T<=20 timesteps, single bottleneck) may be too conservative — rejecting CCAs that work well in practice because they fail against adversaries that never occur.

3. **For CCA design:** The "formally free mechanisms" concept (Finding 5) offers a middle path: start with a formally verified base and layer on practical improvements that don't weaken formal guarantees.

## Limitations

- CCAC is bounded to T<=20 timesteps — does not prove long-horizon convergence
- Single-flow only — multi-flow fairness untested
- LeoCC "simplified" for Z3 encoding may not capture all behaviors of the full implementation
- The 9-property battery is not exhaustive — there may be important properties not tested

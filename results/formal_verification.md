# Formal Verification Results (CCAC/Z3)

## Overview

We evaluated multiple CCAs under CCAC's adversarial network model using Z3 SMT solving. The model includes a single bottleneck link (capacity C, propagation delay R, queue size beta) and a non-deterministic jitter box (delay up to D seconds).

Properties are proved at T=20 timesteps. SAT = counterexample found (property violated). UNSAT = property proven.

## Property Battery

| # | Property | Description | Constraint Details |
|---|----------|-------------|-------------------|
| 1 | cwnd_lower | cwnd >= alpha after loss | Floor on minimum sending rate |
| 2 | cwnd_upper | cwnd <= 2 × C × R | No unbounded queue growth |
| 3 | loss_recovery | cwnd recovers after loss event | Liveness after congestion |
| 4 | timeout_recovery | cwnd recovers after timeout | Robustness under severe loss |
| 5 | queue_drains | Queue empties periodically | Bounded queuing delay |
| 6 | util_25pct | >= 25% utilization steady state | Minimum efficiency (alpha >= BDP/8, no timeout) |
| 7 | util_50pct | >= 50% utilization steady state | Moderate efficiency |
| 8 | util_75pct | >= 75% utilization steady state | High efficiency |
| 9 | starvation | Bounded 2-flow imbalance | Fairness |

**Enhanced utilization constraints** (properties 6-8): We add `alpha >= BDP/8` (prevents adversarial slow-ramp) and no-timeout (timeouts tested separately by property 4). Without these, the adversary exploits tiny alpha + repeated timeouts to defeat any CCA.

## Results Matrix

| CCA | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | Total |
|-----|---|---|---|---|---|---|---|---|---|-------|
| **AIMD-SlowStart** | P | P | P | P | P | P | P | F | P | **8/9** |
| AIMD-DelayCap | P | P | P | P | P | F | F | F | F | **5/9** |
| AIMD-LossDiscrim-SS | P | P | P | P | P | F | F | F | F | **5/9** |
| AIMD-Floor | P | P | P | F | P | F | F | F | F | **4/9** |
| LeoCC (simplified) | P | F | P | F | F | F | F | F | F | **2/9** |
| BBR (simplified) | P | F | F | F | F | F | F | F | F | **1/9** |
| CUBIC | F | F | F | F | F | F | F | F | F | **0/9** |

P = Proven (UNSAT), F = Failed (counterexample found)

## Key Observations

### 1. The 8/9 Ceiling

AIMD-SlowStart is the highest-scoring CCA we've found. It combines:
- Additive increase (cwnd += alpha) above estimated BDP
- Exponential increase (cwnd += 1.0/ACK) below BDP (slow-start)
- Multiplicative decrease on loss (cwnd *= 0.5)
- Inflight tracking (cwnd halved if inflight > 2×BDP)

The single failing property (util_75pct) requires 75% utilization at steady state. The CCA achieves ~73.9% — the inflight check that enables queue_drains constrains the cwnd just enough to miss the 75% threshold.

### 2. util_75pct vs queue_drains Trade-off

No CCA we tested proves both:
- **Profile A** (AIMD-SlowStart): Proves queue_drains, fails util_75pct. Inflight check bounds queue but limits cwnd growth.
- **Profile B** (high-init AIMD): Proves util_75pct, fails queue_drains. No inflight check allows higher utilization but unbounded queue.

This suggests a fundamental trade-off in the CCAC model at T=20.

### 3. Rate-Based CCAs Have Near-Zero Formal Guarantees

LeoCC (2/9) and BBR (1/9) both use rate-based control with bandwidth estimation. The CCAC adversary defeats rate estimation by manipulating delivery patterns — the jitter box can make any bandwidth sample appear high or low. Window-based CCAs (AIMD variants) are more amenable to formal reasoning because cwnd is a direct, observable state variable.

### 4. LLM-Discovered Mechanisms

The CEGIS evolution (LLM + Z3 verifier in a loop) independently rediscovered several known CCA mechanisms:
- **Inflight-based queue detection** (similar to BBR's inflight check)
- **Delivery-rate monitoring** (similar to BBR's btlbw estimation)
- **Non-congestion floor** (cwnd >= 4α when no loss)
- **Anti-explosion guard** (cwnd <= 2×prev_cwnd)

Starting from a blank `constant_window` seed, the LLM reinvented AIMD + inflight tracking in 10 generations (1163s), reaching the same 8/9 ceiling as hand-crafted baselines.

### 5. LLM Cheat Detection

During CEGIS, the LLM discovered two classes of exploits:
1. **S_f cheat**: CCA constrains network variables directly (makes properties trivially true)
2. **BDP-floor cheat**: CCA assumes `cwnd >= C×R` when no loss, creating contradictions

Both were caught by strengthened consistency checks in the verifier. The LLM reliably discovers novel exploit classes — formal verification must anticipate adversarial proposals from the generator, not just adversarial network conditions.

## LEO Jitter Sensitivity

| CCA | D=0 | D=1 | D=2 | D=3 |
|-----|-----|-----|-----|-----|
| AIMD-SlowStart | 8/9 | 8/9 | 7/9 | 6/9 |
| AIMD-DelayCap | 5/9 | 5/9 | 5/9 | 4/9 |
| LeoCC (simplified) | 2/9 | 2/9 | 1/9 | 1/9 |

D = jitter box parameter in units of base RTT. LEO links have D ≈ 1-2 (handoff-induced jitter ~25-50ms on ~25ms base RTT). AIMD-SlowStart degrades gracefully (loses util_50pct at D=2). LeoCC degrades sharply.

## Reproduction

```bash
# Requires Z3 Python bindings
pip install z3-solver

# Run single property check
python3 -m ccac.verify --cca aimd_slowstart --property util_75pct --timesteps 20

# Run full battery
python3 -m ccac.verify --cca aimd_slowstart --all --timesteps 20
```

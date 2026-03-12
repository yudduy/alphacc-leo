# Finding 5: Formally Free Mechanisms

## Summary

We identify a class of CCA improvements we call **"formally free mechanisms"** — modifications that improve real-world performance but are invisible to formal verification under CCAC's adversarial model. These mechanisms exploit structural properties of real networks (decorrelated loss, non-adversarial jitter) that the formal model's adversary can negate by construction.

## The Concept

CCAC's formal model includes an adversary (the jitter box) that can:
- Delay any packet by up to D seconds
- Choose when and where loss events occur
- Manipulate the timing of ACK arrivals

This adversary is maximally powerful — it can always trigger the worst-case behavior of any mechanism. A mechanism that distinguishes "congestion loss" from "non-congestion loss" based on inflight/BDP ratio works well in practice (real networks have structural decorrelation between loss events and inflight levels) but is useless against the Z3 adversary (which can arrange loss to occur at any inflight level).

**Formally free = the mechanism doesn't weaken any proven property AND doesn't strengthen any provable property, but improves real performance.**

## Two Formally Free Mechanisms

### Mechanism 1: Loss Discrimination

**What:** Classify loss events by inflight/BDP ratio:
- Inflight >= BDP → likely congestion → standard halve (cwnd × 0.5)
- Inflight < BDP → likely non-congestion (random loss, handoff) → mild reduction (cwnd × 0.8)

**Real-world gain:** +2.5pp median utilization, +8.1pp worst-case on LEO simulation

**Why formally invisible:** The Z3 adversary controls loss timing. It can trigger loss when inflight >= BDP (making the CCA always halve) or when inflight < BDP (making the CCA always use mild reduction). The adversary chooses whichever path is worse for the property being tested.

**Why it works in practice:** On real Starlink paths, non-congestion loss (0.2-0.5% IID) is structurally decorrelated from inflight levels. Most random losses occur when inflight < BDP because the CCA spends most of its time ramping up, not at peak.

### Mechanism 2: Slow-Start Below BDP

**What:** When cwnd < estimated BDP, increase cwnd by 1.0 per ACK (exponential growth) instead of additive increase. Fast recovery to BDP after loss or timeout.

**Real-world gain:** +2.4pp median utilization, +6.1pp worst-case on LEO simulation

**Why formally invisible:** CCAC's adversary can set conditions where cwnd always stays below BDP (by manipulating capacity) or always above (by withholding loss). The exponential growth rate doesn't help against an adversary that controls when growth stops.

**Why it works in practice:** After a loss event or handoff, cwnd drops well below BDP. Fast recovery to BDP reduces the time spent in the underutilization zone. The speedup is proportional to log(BDP/cwnd) — significant on high-BDP LEO paths.

## Stacking Property

These mechanisms are **independently invisible** to formal verification but **stack additively** in practice:

| CCA | Formal | Sim Utilization | Delta |
|-----|--------|-----------------|-------|
| AIMD-DelayCap (base) | 5/9 | 80.5% | — |
| + Loss Discrimination | 5/9 | 83.0% | +2.5pp |
| + Slow Start | 5/9 | 85.4% | +2.4pp |
| **Combined** | **5/9** | **85.4%** | **+4.9pp** |

The formal property count remains identical (5/9) across all variants. Each mechanism closes ~40% of the remaining gap to LeoCC's empirical performance.

## The Paradox

The CEGIS-evolved CCA that achieves 8/9 formal properties (79.4% simulation utilization) is **worse** in practice than the 5/9 CCA with both formally free mechanisms (85.4% utilization).

```
CEGIS 8/9:    79.4% util, 8 formal properties
5/9 + free:   85.4% util, 5 formal properties
```

The LLM drops formally free mechanisms during CEGIS evolution because they provide no signal to the Z3 verifier. The verifier only rewards mechanism changes that help prove properties — and formally free mechanisms, by definition, do not.

**This creates a misalignment:** the evolution objective (maximize formal properties) is anti-correlated with the deployment objective (maximize real-world performance) in the region where formally free mechanisms dominate.

## Implications

### For CCA Design

The optimal deployment strategy may be:
1. **Start with a formally verified base** (e.g., AIMD-DelayCap, 5/9 properties)
2. **Layer formally free mechanisms** that exploit real-world structure
3. **Verify that the formal properties are preserved** (they will be, by construction)

This gives both formal safety guarantees AND practical performance — at the cost of not maximizing formal properties.

### For Formal Verification Research

Formally free mechanisms exist because the adversarial model is **more powerful than any real network.** The gap between adversarial worst-case and typical-case is where practical performance lives. A weaker, more realistic adversary model would:
- Make some formally free mechanisms formally visible (testable)
- Reduce the formal-empirical gap
- But also weaken the guarantees (proofs hold for fewer real-world scenarios)

This is a fundamental modeling trade-off, not a bug.

### For Automated CCA Synthesis

Any synthesis system that optimizes only formal properties (CEGIS, CCmatic) will systematically miss formally free mechanisms. A hybrid objective — maximize formal properties subject to maintaining real-world performance — would avoid this pitfall.

## Limitations

- "Formally free" is model-dependent. A different formal model (e.g., stochastic instead of adversarial) might make these mechanisms formally visible.
- The stacking property may not hold for all mechanism combinations. Interaction effects can cause regression (as seen with the three-mechanism composition in Finding 1+2+3).
- The term "formally free" is descriptive, not a formal definition. We have not proven that these mechanisms cannot affect any possible formal property — only that they don't affect the 9 properties in our battery.

# Evolution Log: DGM-CCA Runs on LeoCC

## Overview

8 evolution runs across 4 GCP VMs (island model), evaluating 163+ mutations on real Starlink traces. Each mutation: LLM proposes C code patch → compile → insmod → evaluate on 10-15 traces × 120s.

## Run Timeline

| Run | Dates | Gens | Mutations | Best Score | Key Innovation |
|-----|-------|------|-----------|------------|----------------|
| 1-4 | Mar 3-4 | ~20 | ~40 | 0.794 | Initial seed eval, transport debugging |
| 5 | Mar 5 | 44 | 95 | **0.8146** | gpt-5.4, 10 traces, first improvements |
| 6 | Mar 5-6 | 66+ | 400+ | 0.794 | 4 VMs (islands), mode collapse |
| 7 | Mar 7 | ~20 | ~60 | 0.794 | Scoring overhaul (4D→2D), fresh start |
| 8 | Mar 8+ | 100+ | ~100 | 0.799 | Racing fix, continued from archive |

## Run 5: First Improvements (Best: 0.8146)

**Configuration:** Single VM, gpt-5.4 (codex), 10 traces, 120s duration.

**Result:** 16/95 mutations scored >= 0.800 (17%). All-time best: 0.8146 on dimensions (util=0.811, robust=0.568, delay_quality=0.803).

**Mechanism discoveries:**
- Kalman precision expansion (5→10 bits): 0.770
- min_rtt guard against spurious samples: 0.766
- RTT step-up detection: 0.765
- BDP-gated conservative switching: 0.765
- Pacing gain tuning (5/4→11/8): 0.765

**Plateau:** 48 consecutive mutations without improvement after gen 30. Root cause: mode collapse to `improve_robustness` entry type. Every mutation targeted the same bottleneck dimension.

## Run 6: Island Model (4 VMs, Deep Stagnation)

**Configuration:** 4 VMs with different seeds (42-45), temperatures (0.7-1.4), and selection ratios. Shared archive via periodic sync.

**Result:** Zero meaningful improvement across 400+ mutations on all 4 VMs.

| VM | Generations | Best Score | Consecutive Zero-Improvement |
|---|---|---|---|
| alphacc-eval | 66+ | 0.794 | 49+ |
| alphacc-island-1 | 128+ | ~0.794 | 60+ |
| alphacc-island-2 | 60+ | ~0.790 | 50+ |
| alphacc-island-3 | 50+ | ~0.605 | never improved |

**Root causes:**
1. Scoring too complex (4D Tchebycheff + dispersion penalty + repeat-eval)
2. LeoCC seed scored only ~0.738 under this regime → minimal headroom
3. Dispersion penalty punished trace-specific excellence
4. Weight rotation prevented consistent optimization direction
5. LeoCC architecture fragility (small mutations break Kalman/state machine)

## Run 7: Scoring Overhaul (Fresh Start)

**Key change:** Simplified scoring from 4D Tchebycheff to 2D additive:
```
score = 0.60 × utilization + 0.40 × delay_quality
```
Added util floor (quadratic penalty below 50%) to prevent "do nothing" attractor.

**Result:** Stagnated due to racing death spiral (see below).

## Run 8: Racing Fix

**Discovery:** Racing early-stop mechanism was systematically destroying viable candidates:

```
Racing margin (0.03) too tight
  → Candidates scoring 0.707/trace get killed after 3 traces
  → Zero-imputed to 0.212 in archive
  → MAP-Elites picks 0.212 candidates as parents
  → Children of bad parents also score poorly
  → Archive poisoned
```

**Fixes:**
1. Disabled racing by default (margin widened 0.03→0.15)
2. Early-stopped candidates skip MAP-Elites bins
3. Workspace git-reset before seed verification

## Mechanism Classes Discovered

All successful mutations target the same root failure: **LeoCC's conservative response to LEO dynamics causes chronic underutilization on high-RTT paths.**

| Class | Mechanism | Best Score | Key Insight |
|-------|-----------|------------|-------------|
| 1 | Kalman precision (5→10 bits) | 0.770 | P_post saturates, freezing gain |
| 2 | min_rtt guard | 0.766 | ACK compression biases BDP down |
| 3 | RTT step-up detection | 0.765 | Handoffs ≠ congestion |
| 3b | BDP-gated switch | 0.765 | Conservative switch needs inflight check |
| E1 | Pacing gain tuning | 0.765 | 5/4→11/8 probe, 3/4→5/7 drain |

## Composition Experiments

| Variant | Score | Util | Delay | Notes |
|---------|-------|------|-------|-------|
| Seed LeoCC | 0.738 | 0.76 | 0.70 | Baseline |
| **Fix 1+2** | **0.745** | **0.78** | 0.70 | Best composition (+1.0%) |
| Fix 1+2+3 | 0.732 | 0.79 | 0.65 | Over-probing, delay regressed |

Why no multi-mechanism composition emerged from evolution:
- 30-line patch limit prevents multi-mechanism changes
- `anchor_best_parent` forces mutations from single parent
- No cross-breeding between top candidates

## Selection Mechanism Lessons

| Mechanism | Intended Effect | Actual Effect |
|-----------|----------------|---------------|
| UCB1 mutation bandit | Prevent fixation on one mutation type | Modest benefit, correctly avoids depleted categories |
| Three-way selection | Maintain diversity | Helps early, less useful when archive converges |
| Racing/early-stop | Save compute on bad mutations | **Death spiral** — too tight margin poisons archive |
| Stagnation detection | Inject structural change | Triggers correctly but LLM proposals still regress |
| MAP-Elites | Population diversity | Effective when archive has diverse quality; fragile with poisoned entries |

## Key Engineering Lessons

1. **Scoring function design is critical.** 4D Tchebycheff with penalties → stagnation. 2D additive → headroom. The scoring function IS the fitness landscape the LLM navigates.

2. **Racing must be conservative.** A tight margin + zero imputation creates a positive feedback loop that destroys the archive. Better: wide margin, or no imputation for early-stopped candidates.

3. **Island model works when:** Diversity is genuine (different seeds, temperatures, selection ratios). Fails when: All islands converge to the same local optimum because the architecture is too constrained.

4. **48% build failure rate** is normal for kernel CCA evolution. The LLM frequently produces structurally valid C that doesn't compile due to: struct size overflow, missing includes, type mismatches, macro expansion issues.

5. **LLM is better at fixing broken CCAs than improving good ones.** Random-parent mutations improved ~50% of the time. Best-parent mutations: 0/12 genuine improvements. The LLM has strong priors about "what a CCA should look like" that interfere with fine-tuning already-good designs.

# AlphaCC-LEO: LLM-Guided Evolution of LEO Satellite Congestion Control

**Automated discovery of bugs and improvements in LeoCC (SIGCOMM 2025) using LLM-guided evolutionary synthesis with formal verification.**

## Key Findings

We applied LLM-guided evolution (DGM-CCA) to LeoCC — the state-of-the-art congestion control algorithm for LEO satellite networks — and discovered:

1. **Three implementation bugs** in LeoCC's Kalman filter and reconfiguration logic that cause chronic underutilization on high-RTT Starlink paths
2. **An evolved variant that beats reference LeoCC** by +1.5% composite score on real Starlink traces (15 traces, 120s each), trading -2pp utilization for -15ms RTT
3. **A formal verification gap**: LeoCC proves 0/8 safety properties under CCAC's adversarial model, while simpler AIMD-based CCAs prove 5-8/9
4. **"Formally free mechanisms"** — a novel concept: improvements that boost real-world performance but are invisible to formal verification because the adversary can trigger worst-case timing

## Results at a Glance

### Real Starlink Traces (Mahimahi/LeoReplayer, 120s, 15 traces)

| CCA | Composite | Utilization | Mean RTT | Loss Eff. | Robustness |
|-----|-----------|-------------|----------|-----------|------------|
| **Evolved (DGM gen3-m1)** | **0.799** | 0.78 | **-15ms** | 0.96 | 0.53 |
| LeoCC (reference) | 0.787 | 0.78 | baseline | 0.96 | 0.54 |
| BBR | 0.761 | — | — | — | — |
| CUBIC | 0.537 | 0.02 | — | — | — |

### Uplink Performance (where the gap is largest)

| Trace | Evolved RTT | LeoCC RTT | RTT Reduction | Evolved Util | LeoCC Util |
|-------|-------------|-----------|---------------|--------------|------------|
| uplink/1 | 32.9ms | 38.6ms | **-5.7ms** | 0.94 | 0.95 |
| uplink/10 | 40.8ms | 63.3ms | **-22.5ms** | 0.94 | 0.97 |
| uplink/11 | 40.3ms | 55.4ms | **-15.1ms** | 0.93 | 0.94 |
| uplink/12 | 37.2ms | 45.8ms | **-8.6ms** | 0.87 | 0.90 |
| uplink/13 | 40.7ms | 51.5ms | **-10.8ms** | 0.92 | 0.92 |

**Why:** LeoCC's 2.885x pacing gain during STARTUP re-entry fills the queue on low-capacity uplink paths. The evolved variant detects reconfigurations passively via RTT drops, avoiding aggressive probing.

### Formal Verification (CCAC/Z3)

| CCA | Properties Proven | Key Failure |
|-----|-------------------|-------------|
| AIMD-SlowStart (evolved) | **8/9** | util_75pct (73.9%) |
| AIMD-DelayCap | 5/9 | util properties |
| LeoCC (simplified) | 2/8 | most properties |
| BBR (simplified) | 1/8 | most properties |

## Bug Discoveries

### Bug 1: Kalman Filter Precision Saturation
LeoCC's 5-bit posterior variance fields (`p_post_bw`, `p_post_rtt`) saturate at 31, causing Kalman gain K = P/(P+R) to freeze at 0.886. The bandwidth estimate stops tracking capacity changes. **Fix: widen to 10 bits.** [Details](findings/01-kalman-precision.md)

### Bug 2: Spurious min_rtt Samples
ACK compression and timestamp noise pin `min_rtt` too low, causing BDP underestimation. BBR has the same vulnerability. **Fix: restrict min_rtt decreases to PROBE_RTT/STARTUP/low-inflight states.** [Details](findings/02-min-rtt-guard.md)

### Bug 3: Over-Aggressive STARTUP Re-Entry
Timer-based reconfiguration detection triggers STARTUP with 2.885x pacing gain during every LEO handoff, filling queues for 15-25ms on uplink. **Fix: event-based detection via RTT drops in PROBE_RTT.** [Details](findings/03-startup-reentry.md)

## Novel Concepts

### Formally Free Mechanisms
Improvements that boost real-world performance but are invisible to Z3 formal verification. The adversary in the formal model controls loss timing and jitter perfectly — so mechanisms that exploit real-world structural decorrelation (e.g., loss discrimination, slow-start) show no formal benefit but stack additively in practice. [Details](findings/05-formally-free.md)

### The Formal-Empirical Gap
LeoCC achieves 97.3% utilization in simulation but proves 0/8 formal safety properties. Simpler AIMD-based CCAs prove 5-8/9 properties but achieve only 85-90% utilization. No known CCA simultaneously achieves >95% utilization AND >6/9 formal properties. This gap is the core research question. [Details](findings/04-formal-gap.md)

## Method: DGM-CCA (Darwinian Godel Machine for CCAs)

LLM-guided evolutionary synthesis operating directly on Linux kernel CCA modules:

```
Parent CCA (kernel module .c)
    ↓ Diagnosis (o4-mini): identify performance bottleneck
    ↓ Mutation (gpt-5.3-codex): propose code patch
    ↓ Build: gcc → insmod tcp_evolved.ko
    ↓ Evaluate: LeoReplayer + real Starlink traces (120s × 10 traces)
    ↓ Selection: 3-way (exploit/explore/MAP-Elites diversity)
    ↓ Archive: Pareto-filtered population
    → Next generation
```

**Selection mechanisms:** UCB1 mutation-type bandit, three-way parent selection (20% explore / 50% exploit / 30% MAP-Elites diversity), stagnation detection with EMA.

**Evaluation:** Real Starlink traces (4,800 traces from Tsinghua, 8 terminal-server paths) replayed through LeoReplayer (Mahimahi fork with time-varying delay).

## Repository Structure

```
findings/           # Detailed write-ups of each finding
  01-kalman-precision.md
  02-min-rtt-guard.md
  03-startup-reentry.md
  04-formal-gap.md
  05-formally-free.md
results/            # Benchmark data and analysis
  leo_benchmark.md
  formal_verification.md
  evolution_log.md
src/                # Source code
  tcp_evolved.c     # Evolved kernel CCA (LeoCC base + DGM mutations)
  dgm_cca/          # Evolution system
  simulation/       # LEO packet-level simulator
scripts/            # Reproduction scripts
  gcp/              # GCP deployment
  plots/            # Visualization
issues/             # GitHub issue drafts for SpaceNetLab/LeoCC
```

## Reproduction

See [REPRODUCE.md](REPRODUCE.md) for step-by-step instructions.

**Quick start (simulation only, no GCP needed):**
```bash
pip install -r requirements.txt
python3 -m src.simulation.leo_sim --scenario leo_steady --cca aimd_delay_cap
```

**Full benchmark (requires GCP + Starlink traces):**
```bash
# Setup
bash scripts/gcp/setup_leoreplayer.sh
bash scripts/gcp/setup_leo_traces.sh

# Run
bash scripts/gcp/run_benchmark_full.sh 120  # 120s per trace
```

## Context

This work builds on:
- **LeoCC** (SIGCOMM 2025) — LEO-optimized CCA with dual Kalman estimator. [SpaceNetLab/LeoCC](https://github.com/SpaceNetLab/LeoCC)
- **CCAC** (SIGCOMM 2021) — Formal CCA verification via Z3 SMT solving. [Venkat Arun et al.](https://github.com/venkatarun95/ccac)
- **CCmatic** (NSDI 2024) — Automated CCA synthesis with CEGIS. [Venkat Arun et al.]
- **AlphaEvolve** (2025) — LLM-guided code evolution. [DeepMind]
- **DGM** — Darwinian Godel Machine self-improvement framework

## Acknowledgments

Stanford CS244C (Winter 2026). LeoCC reference implementation from SpaceNetLab/Tsinghua University. Starlink traces from the LeoCC dataset. CCAC framework from Venkat Arun (MIT).

## License

MIT

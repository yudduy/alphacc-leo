# alphacc-leo

LLM-guided evolutionary synthesis of congestion control for LEO satellite networks. Evolves Linux kernel CCA modules against real Starlink traces using [LeoCC](https://github.com/SpaceNetLab/LeoCC) (SIGCOMM 2025) as the seed.

## Setup

**Requirements:** GCP VM (e2-standard-4, Ubuntu 22.04), Linux kernel headers, Python 3.10+, OpenAI API key.

```bash
# 1. Build LeoReplayer (Mahimahi fork with time-varying delay)
bash scripts/gcp/setup_leoreplayer.sh

# 2. Download Starlink traces (~14 GB, 4800 traces)
bash scripts/gcp/setup_leo_traces.sh

# 3. Compile kernel module
cd src && make && sudo insmod tcp_evolved.ko
```

## Usage

**Run benchmark** (evolved vs baselines on real Starlink traces):
```bash
bash scripts/gcp/run_benchmark_full.sh 120   # 120s per trace
bash scripts/gcp/run_benchmark_quick.sh       # quick 30s, 5 traces
```

**Run evolution** (requires `OPENAI_API_KEY`):
```bash
bash scripts/gcp/launch_dgm.sh
```

**LEO simulation** (local, no GCP):
```bash
pip install -r requirements.txt
python3 -m src.simulation.leo_sim
```

## Structure

```
src/
  tcp_evolved.c              # kernel CCA module (LeoCC seed + mutations)
  Makefile                    # builds tcp_evolved.ko
  dgm_cca/
    outer.py                 # evolution loop: selection, stagnation, archive
    cca_step.py              # LLM mutation via gpt-5.3-codex
    cca_harness.py           # compile, insmod, evaluate on traces
    cca_diagnosis.py         # bottleneck routing, prompt generation
    mutation_bandit.py       # UCB1 mutation-type selection
    initial_playbook.txt     # seed engineering playbook
    initial/metadata.json    # seed evaluation baseline
  simulation/
    leo_sim.py               # packet-level LEO link simulator
    pareto_oracle.py         # multi-objective fitness (util/RTT/robustness)
scripts/
  gcp/                       # VM setup, benchmarking, evolution launch
  plots/                     # figure generation
results/
  leo_benchmark.md           # raw benchmark data (15 traces, 120s)
```

## Method

```
select parent from archive (exploit / explore / MAP-Elites diversity)
  → diagnose bottleneck dimension (o4-mini)
  → propose C code mutation (gpt-5.3-codex)
  → compile kernel module (gcc → insmod)
  → evaluate on Starlink traces via LeoReplayer (120s × 10 traces)
  → update Pareto archive
```

Selection: 3-way (20% explore / 50% exploit / 30% MAP-Elites). Stagnation detection triggers structural mutation prompts. UCB1 bandit steers mutation categories.

## Evaluation parameters

Matches LeoCC SIGCOMM 2025 methodology:

| Parameter | Value |
|-----------|-------|
| Duration | 120s (8 reconfigurations at 15s period) |
| Uplink queue | 500 pkts |
| Downlink queue | 50,000 pkts |
| Uplink loss | 0.005 IID |
| Downlink loss | 0.0001 |
| Scoring | 0.60 × util + 0.40 × delay_quality |

## References

- [LeoCC](https://github.com/SpaceNetLab/LeoCC) — LEO-optimized CCA (SIGCOMM 2025)
- [CCAC](https://github.com/venkatarun95/ccac) — formal CCA verification via Z3 (SIGCOMM 2021)
- [CCmatic](https://arxiv.org/abs/2310.12672) — automated CCA synthesis (NSDI 2024)

Stanford CS244C, Winter 2026.

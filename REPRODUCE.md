# Reproduction Guide

## Prerequisites

- GCP account (for full benchmark; simulation runs locally)
- Python 3.10+
- Z3 solver (`pip install z3-solver`)
- Linux kernel headers (for kernel module compilation)

## Option 1: Simulation Only (Local, No GCP)

### Setup
```bash
git clone https://github.com/yudduy/alphacc-leo.git
cd alphacc-leo
pip install -r requirements.txt  # z3-solver, numpy, matplotlib
```

### Run LEO Simulation
```bash
# Single scenario
python3 -m src.simulation.leo_sim --scenario leo_steady --cca aimd_delay_cap

# All 6 scenarios, 5 seeds
python3 -m src.simulation.leo_sim --all --seeds 5

# Compare CCAs
python3 -m src.simulation.leo_sim --compare aimd_delay_cap,aimd_loss_discrim,leocc_simple
```

### Run Formal Verification
```bash
# Single property
python3 -m ccac.verify --cca aimd_slowstart --property util_75pct --timesteps 20

# Full battery
python3 -m ccac.verify --cca aimd_slowstart --all --timesteps 20

# Compare all CCAs
python3 -m ccac.verify --compare-all --timesteps 20
```

## Option 2: Full Benchmark (GCP + Starlink Traces)

### 1. Create GCP VM
```bash
gcloud compute instances create alphacc-eval \
  --zone=us-west1-b \
  --machine-type=e2-standard-4 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB
```

### 2. Setup LeoReplayer (Mahimahi Fork)
```bash
gcloud compute ssh alphacc-eval -- 'bash -s' < scripts/gcp/setup_leoreplayer.sh
```

This compiles Mahimahi from source with time-varying delay support (LeoReplayer).

### 3. Download Starlink Traces
```bash
gcloud compute ssh alphacc-eval -- 'bash -s' < scripts/gcp/setup_leo_traces.sh
```

Downloads 4,800 Starlink traces (~14.4 GB) from Tsinghua University's dataset.

### 4. Compile Kernel Module
```bash
gcloud compute ssh alphacc-eval
cd ~/alphacc-leo/src
make  # produces tcp_evolved.ko
sudo insmod tcp_evolved.ko
```

### 5. Run Benchmark
```bash
# Quick benchmark (5 traces, 30s each)
bash scripts/gcp/run_benchmark_quick.sh

# Full benchmark (15 traces, 120s each, matches paper)
bash scripts/gcp/run_benchmark_full.sh 120
```

### 6. Generate Plots
```bash
python3 scripts/plots/plot_paper_v2.py --input output_dgm_cca/benchmark_full_120s.json
```

## Option 3: Run DGM-CCA Evolution

Requires GCP VM + OpenAI API key (for gpt-5.3-codex mutations).

### Setup
```bash
export OPENAI_API_KEY=<your-key>
```

### Launch Single-VM Evolution
```bash
bash scripts/gcp/launch_dgm.sh
```

### Launch Island Model (Multiple VMs)
See `scripts/gcp/launch_island_vms.sh` for multi-VM setup with different seeds and temperatures.

## Trace Format

Starlink traces use Mahimahi format:
- **Bandwidth traces** (`bw_*.txt`): One line per millisecond, packet delivery opportunity count
- **Delay traces** (`delay_*.txt`): One line per 10ms interval, one-way delay in milliseconds

## Fitness Function

```
composite = 0.60 × utilization + 0.40 × delay_quality

utilization = throughput / link_capacity  (capped at 1.0)
delay_quality = 1 - (mean_rtt - min_rtt) / 50ms  (queuing delay penalty)
```

This matches the LeoCC paper's emphasis on throughput with delay as secondary objective.

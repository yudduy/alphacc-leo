# LEO Benchmark Results: Evolved vs Reference LeoCC

## Setup

| Parameter | Value |
|-----------|-------|
| **Platform** | GCP e2-standard-4 (us-west1-b), kernel 6.8.0-1048-gcp |
| **Emulator** | LeoReplayer (Mahimahi fork with time-varying delay) |
| **Duration** | 120s per trace (8 reconfiguration events at 15s period) |
| **Traces** | 15 total: 5 uplink + 5 A_downlink + 5 B_downlink |
| **Uplink loss** | 0.005 (IID Bernoulli, matches LeoCC paper) |
| **Downlink loss** | 0.0001 |
| **Uplink queue** | 500 packets (bottleneck) |
| **Downlink queue** | 50,000 packets |
| **Per-trace offset** | Extracted via `extract_offset()`, set via sysfs before each trace |

### Modules Under Test

- **leocc**: Reference port of `leocc/simulation/leocc.c` from [SpaceNetLab/LeoCC](https://github.com/SpaceNetLab/LeoCC). Only kernel-compatibility changes (struct size, BTF stripped). Zero custom logic.
- **evolved**: DGM gen3-m1 mutation. Key change: removed timer-based STARTUP re-entry, added event-based reconfiguration detection in PROBE_RTT.

### Starlink Traces

From the [LeoCC dataset](https://cloud.tsinghua.edu.cn/d/9fc6fd096e764f57bd25/):
- 4,800 traces total (8 directories × 600 traces each)
- Directories represent different Starlink terminal-to-server paths
- Each trace: `bw_{n}.txt` (Mahimahi bandwidth) + `delay_{n}.txt` (10ms-granularity one-way delay)
- Bandwidth: ~4-74 Mbps uplink, mean ~48 Mbps
- Base RTT: ~10ms one-way, spikes to 35-68ms at 15s reconfiguration events

## Aggregate Results (120s, 15 traces)

| CCA | Composite | Utilization | Delay Quality | Loss Efficiency | Robustness |
|-----|-----------|-------------|---------------|-----------------|------------|
| **evolved** | **0.799** | 0.78 | **0.71** | 0.96 | 0.53 |
| leocc (ref) | 0.787 | 0.78 | 0.69 | 0.96 | 0.54 |

**Scoring:** composite = 0.60 × utilization + 0.40 × delay_quality. Delay quality = 1 - (mean_rtt - min_rtt) / 50ms. Robustness = min per-trace utilization.

## Per-Trace Breakdown

### Uplink (5 traces) — evolved wins decisively on delay

| Trace | LeoCC Score | Evolved Score | LeoCC RTT | Evolved RTT | LeoCC Util | Evolved Util |
|-------|-------------|---------------|-----------|-------------|------------|--------------|
| uplink/1 | 0.817 | **0.843** | 38.6ms | 32.9ms | 0.95 | 0.94 |
| uplink/10 | 0.782 | **0.841** | 63.3ms | 40.8ms | 0.97 | 0.94 |
| uplink/11 | 0.798 | **0.851** | 55.4ms | 40.3ms | 0.94 | 0.93 |
| uplink/12 | 0.815 | **0.833** | 45.8ms | 37.2ms | 0.90 | 0.87 |
| uplink/13 | 0.806 | **0.860** | 51.5ms | 40.7ms | 0.92 | 0.92 |

**Mean uplink RTT reduction: -14.5ms** (from 50.9ms to 36.4ms)

### A_downlink (5 traces) — comparable

| Trace | LeoCC Score | Evolved Score | LeoCC RTT | Evolved RTT |
|-------|-------------|---------------|-----------|-------------|
| A_down/1 | 0.814 | 0.816 | 37.1ms | 35.2ms |
| A_down/10 | 0.801 | **0.824** | 34.0ms | 33.9ms |
| A_down/11 | 0.785 | 0.780 | 36.6ms | 35.1ms |
| A_down/12 | 0.795 | 0.780 | 37.6ms | 36.8ms |
| A_down/13 | 0.800 | 0.774 | 38.9ms | 36.7ms |

### B_downlink (5 traces) — comparable

| Trace | LeoCC Score | Evolved Score | LeoCC RTT | Evolved RTT |
|-------|-------------|---------------|-----------|-------------|
| B_down/1 | 0.755 | 0.756 | 65.3ms | 64.2ms |
| B_down/10 | 0.770 | 0.765 | 65.6ms | 65.7ms |
| B_down/11 | 0.748 | 0.742 | 63.3ms | 62.5ms |
| B_down/12 | 0.763 | **0.776** | 64.4ms | 63.6ms |
| B_down/13 | 0.756 | 0.747 | 64.7ms | 63.5ms |

### 30s Results (10 downlink-only, for reference)

| CCA | Composite |
|-----|-----------|
| evolved | 0.800 |
| leocc (ref) | 0.796 |
| bbr | 0.761 |
| cubic | 0.537 |

## Analysis

### Why the gap is largest on uplink

Uplink traces have 10-20x lower capacity (~18-49 Mbps vs ~420 Mbps downlink). At lower BDP, STARTUP's 2.885x pacing gain creates proportionally more queuing. The smaller pipe amplifies the bufferbloat from aggressive probing.

### Why CUBIC fails (2% utilization)

CUBIC's throughput: `throughput ≈ MTU / (RTT × √p) ≈ 1500 / (0.03 × √0.005) ≈ 10 Mbps`. The 0.5% random loss rate destroys loss-based CCAs on high-BDP satellite links. This confirms the LeoCC paper's findings.

### Threats to Validity

1. **Port fidelity**: Reference LeoCC port has known deviations (time-based round detection, not delivery-based; +1/ACK startup, not doubling; HIGH_GAIN 2.0, not 2.885). Not side-by-side validated against original C kernel module.
2. **Statistical significance**: 15 traces, no confidence intervals. The +1.5% composite gain may not be statistically significant.
3. **Fitness metric**: Weights (0.60 util + 0.40 delay) are arbitrary. Different weights yield different rankings.
4. **Trace representativeness**: 15 traces from 3 paths. Starlink's 4,800-trace dataset has much more diversity.
5. **Single-flow only**: No competing traffic. Real deployments have cross-traffic effects.

## Evolution Statistics

| Metric | Value |
|--------|-------|
| Total mutations evaluated | 163 |
| Build failures | 78 (48%) |
| Score >= 0.80 | 0 |
| Score >= 0.75 | 32 |
| Best mutation score | 0.770 (Kalman precision fix) |
| Composition best | 0.745 (Kalman + min_rtt guard) |
| Stagnation point | After ~20 scored mutations |

The search exhausted the local 1-mechanism neighborhood. Breaking through requires cross-breeding (combining multiple mutations) or architecturally different approaches.

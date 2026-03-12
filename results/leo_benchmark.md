# Benchmark: Evolved vs Reference LeoCC

GCP e2-standard-4, kernel 6.8.0-1048-gcp, LeoReplayer, 120s per trace.
15 Starlink traces (5 uplink + 5 A_downlink + 5 B_downlink), loss 0.005/0.0001.

## Aggregate (15 traces, 120s)

| CCA | Composite | Util | Delay Q | Loss Eff | Robustness |
|-----|-----------|------|---------|----------|------------|
| evolved | 0.799 | 0.78 | 0.71 | 0.96 | 0.53 |
| leocc | 0.787 | 0.78 | 0.69 | 0.96 | 0.54 |

## Uplink

| Trace | leocc | evolved | leocc RTT | evolved RTT | leocc util | evolved util |
|-------|-------|---------|-----------|-------------|------------|--------------|
| uplink/1 | 0.817 | 0.843 | 38.6ms | 32.9ms | 0.95 | 0.94 |
| uplink/10 | 0.782 | 0.841 | 63.3ms | 40.8ms | 0.97 | 0.94 |
| uplink/11 | 0.798 | 0.851 | 55.4ms | 40.3ms | 0.94 | 0.93 |
| uplink/12 | 0.815 | 0.833 | 45.8ms | 37.2ms | 0.90 | 0.87 |
| uplink/13 | 0.806 | 0.860 | 51.5ms | 40.7ms | 0.92 | 0.92 |

## A_downlink

| Trace | leocc | evolved | leocc RTT | evolved RTT |
|-------|-------|---------|-----------|-------------|
| A_down/1 | 0.814 | 0.816 | 37.1ms | 35.2ms |
| A_down/10 | 0.801 | 0.824 | 34.0ms | 33.9ms |
| A_down/11 | 0.785 | 0.780 | 36.6ms | 35.1ms |
| A_down/12 | 0.795 | 0.780 | 37.6ms | 36.8ms |
| A_down/13 | 0.800 | 0.774 | 38.9ms | 36.7ms |

## B_downlink

| Trace | leocc | evolved | leocc RTT | evolved RTT |
|-------|-------|---------|-----------|-------------|
| B_down/1 | 0.755 | 0.756 | 65.3ms | 64.2ms |
| B_down/10 | 0.770 | 0.765 | 65.6ms | 65.7ms |
| B_down/11 | 0.748 | 0.742 | 63.3ms | 62.5ms |
| B_down/12 | 0.763 | 0.776 | 64.4ms | 63.6ms |
| B_down/13 | 0.756 | 0.747 | 64.7ms | 63.5ms |

## 30s (10 downlink, reference)

| CCA | Composite |
|-----|-----------|
| evolved | 0.800 |
| leocc | 0.796 |
| bbr | 0.761 |
| cubic | 0.537 |

## Evolution stats

163 mutations, 78 build failures (48%), 32 scored >= 0.75, 0 scored >= 0.80.

#!/bin/bash
# Full SOTA benchmark: evolved (LeoCC) vs all available CCAs
# Usage: sudo bash run_benchmark_full.sh [duration]
set -euo pipefail

DURATION=${1:-30}
CSDIR="${CS244C_DIR:-/home/duy/cs244c}"
cd "$CSDIR"

# Load kernel modules for CCAs that aren't built-in
for mod in tcp_vegas tcp_bbr tcp_hybla tcp_westwood tcp_veno tcp_htcp tcp_bic tcp_illinois tcp_highspeed tcp_cdg tcp_nv tcp_yeah; do
    sudo modprobe "$mod" 2>/dev/null || true
done

echo "=== Full SOTA Benchmark (${DURATION}s, 5 traces) ==="
echo "Available CCAs: $(cat /proc/sys/net/ipv4/tcp_available_congestion_control)"
echo "Params: DL_Q=50000, UL_LOSS=0.005, DL_LOSS=0.0001"
echo ""

source "$CSDIR/.venv/bin/activate"

# CCAs to benchmark — evolved (our LeoCC) + paper baselines + interesting extras
# Paper baselines: cubic, reno, vegas, bbr
# Extras available: hybla (satellite-optimized!), westwood, htcp, bic, illinois
CCAS="evolved cubic reno bbr vegas hybla westwood htcp"

python3 -c "
import json, sys, time
sys.path.insert(0, '.')
from alphacc.dgm_cca.cca_harness import evaluate_cca, find_trace_pairs

traces = find_trace_pairs('data/starlink_traces', max_traces=5)
print(f'Traces: {len(traces)}')
print()

ccas = '${CCAS}'.split()
results = {}

for cca in ccas:
    print(f'--- {cca} ({${DURATION}}s) ---')
    t0 = time.time()
    try:
        result = evaluate_cca(cca, traces, duration=${DURATION})
        results[cca] = result
        v = result['objective_vector']
        print(f'  score={result[\"accuracy_score\"]:.3f} util={v[\"util\"]:.3f} delay={v[\"delay_quality\"]:.3f} loss={v[\"loss_efficiency\"]:.3f} rob={v[\"robustness\"]:.3f} ({time.time()-t0:.0f}s)')
    except Exception as e:
        print(f'  FAILED: {e}')
    print()

# Summary table
print()
print('=== SUMMARY ===')
print(f'{\"CCA\":<12} {\"Score\":>7} {\"Util\":>7} {\"Delay\":>7} {\"Loss\":>7} {\"Robust\":>7}')
print('-' * 55)
for cca in ccas:
    if cca in results:
        r = results[cca]
        v = r['objective_vector']
        print(f'{cca:<12} {r[\"accuracy_score\"]:>7.3f} {v[\"util\"]:>7.3f} {v[\"delay_quality\"]:>7.3f} {v[\"loss_efficiency\"]:>7.3f} {v[\"robustness\"]:>7.3f}')

# Save full results
with open('output_dgm_cca/benchmark_full_${DURATION}s.json', 'w') as f:
    json.dump(results, f, indent=2)
print(f'\nSaved to output_dgm_cca/benchmark_full_${DURATION}s.json')
"

#!/bin/bash
# Quick benchmark: 30s eval, 5 traces, evolved + bbr
# Usage: bash run_benchmark_quick.sh [duration]
set -euo pipefail

DURATION=${1:-30}
CSDIR="${CS244C_DIR:-/home/duy/cs244c}"
cd "$CSDIR"

echo "=== Quick Benchmark (${DURATION}s, 5 traces) ==="
echo "Params: DL_Q=50000, UL_LOSS=0.005, DL_LOSS=0.0001"
echo "Kernel module offset: $(cat /sys/module/tcp_evolved/parameters/offset)"
echo "Kernel module min_rtt_fluctuation: $(cat /sys/module/tcp_evolved/parameters/min_rtt_fluctuation)"
echo ""

# Activate venv
source "$CSDIR/.venv/bin/activate"

python3 -c "
import json, sys
sys.path.insert(0, '.')
from alphacc.dgm_cca.cca_harness import evaluate_cca, find_trace_pairs

traces = find_trace_pairs('data/starlink_traces', max_traces=5)
print(f'Found {len(traces)} trace pairs')

for cca in ['evolved', 'bbr']:
    print(f'\n--- Evaluating {cca} ({${DURATION}}s) ---')
    result = evaluate_cca(cca, traces, duration=${DURATION})
    print(json.dumps(result, indent=2))
"

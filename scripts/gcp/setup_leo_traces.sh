#!/bin/bash
# Download a sample of LeoCC Starlink traces for E-009 validation.
# Traces are hosted at Tsinghua University cloud storage.
# Each trace has: bw_*.txt (mm-link format) + delay_*.txt (10ms intervals).
#
# Usage: bash scripts/gcp/setup_leo_traces.sh [NUM_TRACES_PER_DIR]
# Default: 10 traces from each of dirs A,B,C (30 total, ~90 MB)

set -euo pipefail

NUM_TRACES=${1:-10}
TRACE_DIR="data/starlink_traces"
DOWNLOAD_URL="https://cloud.tsinghua.edu.cn/d/9fc6fd096e764f57bd25"
# Selected directories representing different Starlink conditions
DIRS="A B C"

echo "=== E-009: Downloading Starlink traces ==="
echo "Traces per directory: $NUM_TRACES"
echo "Directories: $DIRS"
echo "Output: $TRACE_DIR"
echo ""

mkdir -p "$TRACE_DIR"

echo "NOTE: Automatic download from Tsinghua cloud requires manual steps."
echo "The traces are at: $DOWNLOAD_URL"
echo ""
echo "Manual download instructions:"
echo "  1. Visit $DOWNLOAD_URL in a browser"
echo "  2. Download directories A, B, C (or all A-H)"
echo "  3. Extract to $TRACE_DIR/"
echo "  4. Expected structure: $TRACE_DIR/A/1/bw_1.txt, $TRACE_DIR/A/1/delay_1.txt, ..."
echo ""

# Check if traces already exist
FOUND=0
for dir in $DIRS; do
    if [ -d "$TRACE_DIR/$dir" ]; then
        COUNT=$(find "$TRACE_DIR/$dir" -name "bw_*.txt" 2>/dev/null | wc -l)
        echo "  Found $COUNT traces in $TRACE_DIR/$dir/"
        FOUND=$((FOUND + COUNT))
    fi
done

if [ "$FOUND" -gt 0 ]; then
    echo ""
    echo "Total traces found: $FOUND"
    echo "To proceed with benchmark, run: bash scripts/gcp/run_leo_benchmark.sh"
else
    echo ""
    echo "No traces found. Please download manually from:"
    echo "  $DOWNLOAD_URL"
    echo ""
    echo "Quick method (if wget/curl works with Tsinghua cloud):"
    echo "  # This may not work due to auth redirects — use browser if so"
    echo "  mkdir -p $TRACE_DIR"
    echo "  cd $TRACE_DIR"
    echo "  # Download individual trace files..."
fi

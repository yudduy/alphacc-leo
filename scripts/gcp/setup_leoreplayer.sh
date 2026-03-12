#!/usr/bin/env bash
# E-009: Build LeoReplayer (LeoCC's modified Mahimahi) on GCP.
#
# LeoReplayer extends Mahimahi with time-varying delay support:
#   mm-delay INTERVAL_MS DELAY_TRACE_FILE [command...]
# Stock Mahimahi only supports fixed delay:
#   mm-delay FIXED_MS [command...]
#
# This is critical for LEO satellite evaluation — real Starlink delay
# varies 8-50ms within a single trace, and handoffs cause 30-50ms spikes.
#
# Prerequisites: Ubuntu 22.04 GCP instance (e2-standard-4 or larger)
# Usage: bash scripts/gcp/setup_leoreplayer.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LEOREPLAYER_SRC="$ROOT/leocc_ref/leoreplayer/replayer"

echo "=== E-009: LeoReplayer Setup ==="

# ── 1. Platform check ──────────────────────────────────────────────
echo "[1/6] Platform check"
if [[ "$(uname -s)" != "Linux" ]]; then
    echo "ERROR: Linux required (are you on the GCP VM?)"
    exit 1
fi

# ── 2. System deps (same as stock Mahimahi + autotools) ────────────
echo "[2/6] System dependencies"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    build-essential git autoconf automake autotools-dev libtool pkg-config \
    libssl-dev libxcb-composite0-dev libxcb-present-dev libcairo2-dev \
    libpango1.0-dev dnsmasq-base apache2-bin apache2-dev ssl-cert \
    protobuf-compiler libprotobuf-dev \
    iproute2 iptables \
    python3-venv python3-pip gnuplot

# ── 3. Build LeoReplayer from source ──────────────────────────────
echo "[3/6] Building LeoReplayer (modified Mahimahi with time-varying delay)"
if [[ ! -d "$LEOREPLAYER_SRC" ]]; then
    echo "ERROR: LeoReplayer source not found at $LEOREPLAYER_SRC"
    echo "Make sure leocc_ref/ is checked into the repo."
    exit 1
fi

cd "$LEOREPLAYER_SRC"

# Remove any stale stock Mahimahi to avoid conflicts
if command -v mm-delay >/dev/null 2>&1; then
    echo "  Removing existing Mahimahi installation..."
    sudo make uninstall 2>/dev/null || true
    # Also check /usr/local/bin directly
    sudo rm -f /usr/local/bin/mm-delay /usr/local/bin/mm-link /usr/local/bin/mm-loss 2>/dev/null || true
fi

echo "  Running autogen.sh..."
./autogen.sh

echo "  Configuring..."
./configure

echo "  Building ($(nproc) cores)..."
make -j"$(nproc)"

echo "  Installing..."
sudo make install
cd "$ROOT"

# ── 4. iptables-legacy + IP forwarding ────────────────────────────
echo "[4/6] iptables-legacy + IP forwarding"
sudo update-alternatives --set iptables /usr/sbin/iptables-legacy 2>/dev/null || true
sudo update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy 2>/dev/null || true
sudo sysctl -w net.ipv4.ip_forward=1

# ── 5. Python venv ─────────────────────────────────────────────────
echo "[5/6] Python venv"
if [[ ! -d "$ROOT/.venv" ]]; then
    python3 -m venv "$ROOT/.venv"
fi
source "$ROOT/.venv/bin/activate"
pip install --upgrade pip -q
pip install numpy -q

# ── 6. Verification ────────────────────────────────────────────────
echo "[6/6] Verification"
echo ""

# Check all binaries exist
for cmd in mm-delay mm-link mm-loss python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  OK: $cmd ($(which $cmd))"
    else
        echo "  MISSING: $cmd"
        exit 1
    fi
done

# Verify time-varying delay support
# LeoReplayer's mm-delay accepts: mm-delay INTERVAL_MS DELAY_FILE [command...]
# Stock Mahimahi's mm-delay accepts: mm-delay FIXED_MS [command...]
echo ""
echo "  Testing LeoReplayer mm-delay with example trace..."
EXAMPLE_DIR="$LEOREPLAYER_SRC/example/Cubic"
if [[ -f "$EXAMPLE_DIR/delay_example.txt" ]]; then
    # LeoReplayer needs BASE_TIMESTAMP env var
    export BASE_TIMESTAMP=$(date +%s%3N)
    if mm-delay 10 "$EXAMPLE_DIR/delay_example.txt" -- echo "leoreplayer works" 2>/dev/null; then
        echo "  OK: LeoReplayer functional (time-varying delay supported)"
    else
        echo "  WARNING: LeoReplayer test failed."
        echo "  Try: sudo sysctl -w kernel.unprivileged_userns_clone=1"
    fi
else
    echo "  SKIP: Example trace not found (build may still be OK)"
fi

echo ""
echo "=== LeoReplayer Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Download Starlink traces: bash scripts/gcp/setup_leo_traces.sh"
echo "  2. Run benchmark: bash scripts/gcp/run_e009_benchmark.sh"

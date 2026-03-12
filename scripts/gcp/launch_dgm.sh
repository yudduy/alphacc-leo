#!/bin/bash
# Launch DGM-CCA evolution on GCP.
# Sources .bashrc for API key, then runs the DGM outer loop.
# Usage: sudo -E bash launch_dgm.sh [extra args for outer.py]
set -euo pipefail

# Source API key from user's bashrc
export HOME=/home/duy
# Non-interactive SSH + sudo don't source .bashrc properly, so extract the key directly
eval "$(grep '^export OPENAI_API_KEY' /home/duy/.bashrc 2>/dev/null || true)"

CSDIR="/home/duy/cs244c"
cd "$CSDIR"
source "$CSDIR/.venv/bin/activate"

# Verify API key
if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "ERROR: OPENAI_API_KEY not set even after sourcing .bashrc"
    exit 1
fi
echo "[OK] OPENAI_API_KEY set"

# Fidelity preflight against upstream references.
python3 -m alphacc.dgm_cca.verify_fidelity --repo_root "$CSDIR"

# Run DGM with 30s evals for fast iteration (20 gens, 2 mutations/gen)
# selfimprove_workers=1 because kernel module is shared resource
exec python3 -m alphacc.dgm_cca.outer \
    --selfimprove_workers 1 \
    --trace_dir data/starlink_traces \
    --duration 30 \
    --choose_selfimproves_method best \
    --anchor_best_parent \
    --anchor_parent_frac 1.0 \
    --max_patch_lines 30 \
    --no_racing \
    --migration_leader \
    --max_generation 20 \
    --selfimprove_size 2 \
    "$@"

"""DGM outer loop adapted for CCA evolution.

Copied from dgm/DGM_outer.py via `cp`. Then edited:
- Lines 9-13: imports — removed docker/swe_bench/polyglot, added CCA equivalents
- Lines 15-35: initialize_run — removed polyglot param, fixed initial source path
- Lines 37-48: any_exceeding_context_length — REMOVED (SWE-bench-specific)
- Lines 50-150: choose_selfimproves — parent selection (78-109) VERBATIM,
                entry selection (111-148) replaced with CCA choose_entry
- Lines 152-165: filter_compiled — added try/except (no Docker guarantee)
- Lines 167-190: get_original_score, update_archive — VERBATIM
- Lines 192-219: get_full_eval_threshold — REMOVED (SWE-bench-specific)
- Lines 221-334: main — SWE-bench args/loading replaced with CCA trace loading

Usage:
    python3 -m alphacc.dgm_cca.outer --max_generation 20 --selfimprove_size 2
"""

import argparse
import datetime
import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed, TimeoutError
from typing import Dict, List, Optional, Tuple

# Ensure dgm is importable
import alphacc.dgm_cca  # noqa: F401

# DGM imports (via sys.path from __init__.py)
from utils.common_utils import load_json_file, read_file
from utils.evo_utils import load_dgm_metadata, is_compiled_self_improve
# CHANGED: self_improve_step.self_improve → cca_step.self_improve_cca
from alphacc.dgm_cca.cca_step import self_improve_cca, CCA_WORKSPACE
# CHANGED: prompts.self_improvement_prompt → cca_diagnosis.choose_entry
from alphacc.dgm_cca.cca_diagnosis import choose_entry, ENTRY_TYPES
# ADDED: CCA trace discovery + Tchebycheff utilities
from alphacc.dgm_cca.cca_harness import (
    build_and_load_cca,
    evaluate_cca,
    find_trace_pairs,
    unload_cca,
    SELECTION_WEIGHTS,
)
# ADDED: Mutation bandit
from alphacc.dgm_cca.mutation_bandit import MutationBandit, classify_mutation
# REMOVED: from utils.docker_utils import setup_logger — replaced with logging module
# REMOVED: from prompts.self_improvement_prompt import find_selfimprove_eval_logs


# DGM: DGM_outer.py:15-35, CHANGED: removed polyglot param, fixed initial source path
def initialize_run(output_dir, prevrun_dir=None):
    # Initialize archive
    start_gen_num = 0
    if not prevrun_dir:
        archive = ['initial']
    else:
        # Load previous run's archive
        metadata_path = os.path.join(prevrun_dir, "dgm_metadata.jsonl")
        metadata = load_dgm_metadata(metadata_path, last_only=True)
        archive = metadata['archive']
        start_gen_num = metadata['generation'] + 1

    # Copy cached initial version into experiment dir
    # CHANGED: polyglot removed, os.system("cp -r") → shutil.copytree, initial source path
    if not prevrun_dir and not os.path.exists(f"{output_dir}/initial"):
        initial_source = os.path.join(os.path.dirname(__file__), 'initial')
        if os.path.exists(initial_source):
            shutil.copytree(initial_source, f"{output_dir}/initial")
        else:
            raise RuntimeError("Error: Need to properly configure evaluation results for the initial version.")

    return archive, start_gen_num


# REMOVED: any_exceeding_context_length (DGM:37-48) — SWE-bench-specific


OBJ_DIMS = ("util", "delay_quality", "loss_efficiency", "robustness")


def _parse_metadata_jsonl(path: str) -> List[Dict]:
    """Parse dgm_metadata.jsonl which may contain multi-line JSON entries (indent=2)."""
    if not os.path.exists(path):
        return []
    text = open(path).read()
    entries = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    entries.append(json.loads(text[start:i + 1]))
                except json.JSONDecodeError:
                    pass
                start = None
    return entries

# MAP-Elites bin boundaries for three-way selection (3×3 grid)
UTIL_BINS = [0.0, 0.70, 0.85, 1.01]
ROBUST_BINS = [0.0, 0.50, 0.70, 1.01]


def _get_bin(obj: Dict) -> Tuple[int, int]:
    """Map objective vector to (util_bin, robustness_bin) indices."""
    u = float(obj.get("util", 0.0))
    r = float(obj.get("robustness", 0.0))
    ub = next((i for i, edge in enumerate(UTIL_BINS[1:]) if u < edge), 2)
    rb = next((i for i, edge in enumerate(ROBUST_BINS[1:]) if r < edge), 2)
    return (ub, rb)


class StagnationDetector:
    """EMA-based stagnation detection for evolutionary search.

    Triggers meta-guidance injection when EMA of fitness improvements
    drops below threshold for consecutive_threshold generations.

    Reference: Karafotias et al. (2015) "Parameter Control in EC."
    """

    def __init__(self, alpha: float = 0.3, threshold: float = 0.001,
                 consecutive_threshold: int = 5):
        self.alpha = alpha
        self.threshold = threshold
        self.consecutive_threshold = consecutive_threshold
        self.ema = 0.0
        self.consecutive_below = 0
        self.best_score = 0.0

    def update(self, generation: int, best_child_score: float,
               best_archive_score: float) -> bool:
        """Update EMA. Returns True if stagnation triggered."""
        improvement = max(0.0, best_child_score - self.best_score)
        self.best_score = max(self.best_score, best_child_score, best_archive_score)
        self.ema = self.alpha * improvement + (1 - self.alpha) * self.ema

        if self.ema < self.threshold:
            self.consecutive_below += 1
        else:
            self.consecutive_below = 0

        return self.consecutive_below >= self.consecutive_threshold

    def get_state(self) -> Dict:
        return {
            "ema": round(self.ema, 6),
            "consecutive_below": self.consecutive_below,
            "best_score": round(self.best_score, 5),
            "triggered": self.consecutive_below >= self.consecutive_threshold,
        }

    @classmethod
    def from_metadata(cls, output_dir: str, **kwargs) -> "StagnationDetector":
        """Reconstruct detector state from dgm_metadata.jsonl for --continue_from."""
        det = cls(**kwargs)
        meta_path = os.path.join(output_dir, "dgm_metadata.jsonl")
        entries = _parse_metadata_jsonl(meta_path)
        last_state = None
        for entry in entries:
            s = entry.get("stagnation_state")
            if s:
                last_state = s
        if last_state:
            det.ema = last_state.get("ema", 0.0)
            det.consecutive_below = last_state.get("consecutive_below", 0)
            det.best_score = last_state.get("best_score", 0.0)
        return det


def _dominates(a: Dict, b: Dict) -> bool:
    return all(a[d] >= b[d] for d in OBJ_DIMS) and any(a[d] > b[d] for d in OBJ_DIMS)


def _parse_feature_bins(spec: str, dims: List[str], default_bins: int = 8) -> Dict[str, int]:
    """
    Parse feature-bin config from CLI.
    Accepted:
    - "8" (same bins for all dims)
    - "util:10,robustness:6,delay_quality:8,loss_efficiency:6"
    """
    out = {d: default_bins for d in dims}
    text = (spec or "").strip()
    if not text:
        return out
    if text.isdigit():
        b = max(2, int(text))
        return {d: b for d in dims}
    for part in text.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip()
        if k in out:
            try:
                out[k] = max(2, int(v.strip()))
            except ValueError:
                continue
    return out


def _feature_coords(obj: Dict[str, float], dims: List[str], bins: Dict[str, int]) -> Tuple[int, ...]:
    """Map objective vector to MAP-Elites bin coordinates in [0, bins-1]."""
    coords: List[int] = []
    for d in dims:
        v = max(0.0, min(0.999999, float(obj.get(d, 0.0))))
        b = bins.get(d, 8)
        coords.append(min(b - 1, int(v * b)))
    return tuple(coords)


def _save_feature_map(output_dir: str, fmap: Dict[str, Dict]) -> None:
    path = os.path.join(output_dir, "map_elites_feature_map.json")
    with open(path, "w") as f:
        json.dump(fmap, f, indent=2, sort_keys=True)


def _build_frontier_targets(candidates: Dict) -> list:
    points = []
    for commit, c in candidates.items():
        obj = c.get("objective_vector")
        if not obj:
            continue
        points.append((commit, {d: float(obj.get(d, 0.0)) for d in OBJ_DIMS}))

    if not points:
        return []

    frontier = []
    for i, (cid, p) in enumerate(points):
        others = [q for j, (_, q) in enumerate(points) if i != j]
        if not any(_dominates(q, p) for q in others):
            frontier.append((cid, p))

    return [p for _, p in frontier]


def _best_archive_commit(output_dir: str, archive: List[str]) -> Optional[str]:
    """Return best commit in the current archive by accuracy_score."""
    best_commit = None
    best_score = float("-inf")
    for commit in archive:
        try:
            metadata_path = os.path.join(output_dir, commit, "metadata.json")
            metadata = load_json_file(metadata_path)
            score = float(metadata["overall_performance"]["accuracy_score"])
        except Exception:
            continue
        if score > best_score:
            best_score = score
            best_commit = commit
    return best_commit


def _best_lineage_commit(output_dir: str, archive: List[str], root_commit: Optional[str]) -> Optional[str]:
    """
    Return best archive commit by accuracy_score constrained to root lineage.
    If root_commit is None, fallback to global best.
    """
    if not root_commit:
        return _best_archive_commit(output_dir, archive)

    # Build parent map and score map for archive entries.
    parent_of: Dict[str, Optional[str]] = {}
    score_of: Dict[str, float] = {}
    for commit in archive:
        try:
            metadata_path = os.path.join(output_dir, commit, "metadata.json")
            metadata = load_json_file(metadata_path)
            score_of[commit] = float(metadata["overall_performance"]["accuracy_score"])
            parent_of[commit] = metadata.get("parent_commit")
        except Exception:
            continue

    # Root should always be a valid candidate if present.
    if root_commit in score_of:
        parent_of.setdefault(root_commit, None)

    memo: Dict[str, bool] = {}

    def in_lineage(commit: str) -> bool:
        if commit in memo:
            return memo[commit]
        if commit == root_commit:
            memo[commit] = True
            return True
        cur = parent_of.get(commit)
        seen = set()
        while cur and cur not in seen:
            if cur == root_commit:
                memo[commit] = True
                return True
            seen.add(cur)
            cur = parent_of.get(cur)
        memo[commit] = False
        return False

    best_commit = None
    best_score = float("-inf")
    for commit, score in score_of.items():
        if in_lineage(commit) and score > best_score:
            best_score = score
            best_commit = commit

    # Fallback to root itself if present/available; avoid jumping to unrelated global best.
    if best_commit:
        return best_commit
    if root_commit in score_of:
        return root_commit
    root_meta = os.path.join(output_dir, root_commit, "metadata.json")
    if os.path.exists(root_meta):
        return root_commit
    return None


# DGM: DGM_outer.py:50-150
# CHANGED: removed run_baseline/polyglot params, replaced entry selection (lines 111-148)
def choose_selfimproves(output_dir, archive, selfimprove_size, method='random', weights=None,
                        stagnation_triggered=False, bin_visit_counts=None,
                        explore_frac=0.20, exploit_frac=0.50,
                        anchor_best_parent=False, anchor_parent_frac=0.8,
                        force_parent_commit: Optional[str] = None):
    """
    Choose self-improve attempts for the current generation.
    """
    selfimprove_entries = []

    # Get parent candidates
    candidates = {}
    for commit in archive:
        try:
            metadata_path = os.path.join(output_dir, commit, "metadata.json")
            metadata = load_json_file(metadata_path)
            candidates[commit] = {
                'accuracy_score': metadata['overall_performance']['accuracy_score'],
                'total_unresolved_ids': metadata['overall_performance']['total_unresolved_ids'],
                'total_emptypatch_ids': metadata['overall_performance'].get('total_emptypatch_ids', []),
                'total_resolved_ids': metadata['overall_performance']['total_resolved_ids'],
                'children_count': 0,
                # ADDED: per_trace for CCA diagnosis
                'per_trace': metadata['overall_performance'].get('per_trace', {}),
                # ADDED: frontier/robustness-aware fields
                'objective_vector': metadata['overall_performance'].get('objective_vector'),
                'bottleneck_dim': metadata['overall_performance'].get('bottleneck_dim'),
                'bottleneck_gap': metadata['overall_performance'].get('bottleneck_gap', 1.0),
                'selection_score': metadata['overall_performance'].get('selection_score'),
                'confidence_penalty': metadata['overall_performance'].get('confidence_penalty', 0.0),
            }
            # update children count, parent should already be in the archive
            if commit != 'initial':
                parent_commit = metadata['parent_commit']
                candidates[parent_commit]['children_count'] += 1
        except Exception as e:
            # probably because swe-eval failed, generated code did not compile, etc.
            print(f"{commit} not eligible for being a parent: {e}")
            continue

    # Ensure forced parent can be used even if currently pruned from archive.
    if force_parent_commit and force_parent_commit not in candidates:
        try:
            metadata_path = os.path.join(output_dir, force_parent_commit, "metadata.json")
            metadata = load_json_file(metadata_path)
            candidates[force_parent_commit] = {
                'accuracy_score': metadata['overall_performance']['accuracy_score'],
                'total_unresolved_ids': metadata['overall_performance'].get('total_unresolved_ids', []),
                'total_emptypatch_ids': metadata['overall_performance'].get('total_emptypatch_ids', []),
                'total_resolved_ids': metadata['overall_performance'].get('total_resolved_ids', []),
                'children_count': 0,
                'per_trace': metadata['overall_performance'].get('per_trace', {}),
                'objective_vector': metadata['overall_performance'].get('objective_vector'),
                'bottleneck_dim': metadata['overall_performance'].get('bottleneck_dim'),
                'bottleneck_gap': metadata['overall_performance'].get('bottleneck_gap', 1.0),
                'selection_score': metadata['overall_performance'].get('selection_score'),
                'confidence_penalty': metadata['overall_performance'].get('confidence_penalty', 0.0),
            }
        except Exception as e:
            raise RuntimeError(
                f"force_parent_commit={force_parent_commit} unavailable in archive/output_dir: {e}"
            )

    if not candidates:
        print("WARNING: No valid candidates in archive")
        return []

    frontier_targets = _build_frontier_targets(candidates)

    # LeoCC-aligned selection: 2D weighted (util × delay_quality).
    # Matches LeoCC SIGCOMM 2025 evaluation: throughput vs delay Pareto.
    def _candidate_score(commit):
        obj = candidates[commit].get('objective_vector')
        if obj:
            return max(0.001, min(1.0,
                SELECTION_WEIGHTS["util"] * obj["util"]
                + SELECTION_WEIGHTS["delay_quality"] * obj["delay_quality"]
                + SELECTION_WEIGHTS["loss_efficiency"] * obj.get("loss_efficiency", 0)
                + SELECTION_WEIGHTS["robustness"] * obj.get("robustness", 0)
            ))
        s = candidates[commit].get('selection_score')
        if s is not None:
            return max(0.001, min(1.0, float(s)))
        return max(0.001, float(candidates[commit]['accuracy_score']))

    # Optional hard anchor to one known-good parent commit.
    if force_parent_commit:
        if force_parent_commit in candidates:
            parent_commits = [force_parent_commit] * selfimprove_size
            selection_details = [{"method": "forced_parent", "bin": None}] * len(parent_commits)
        else:
            raise RuntimeError(
                f"force_parent_commit={force_parent_commit} not in candidate set; refusing fallback"
            )
    else:
        parent_commits = []
        selection_details = []

    # DGM: DGM_outer.py:78-109 — parent selection
    # CHANGED: accuracy_score → robust _candidate_score
    if not parent_commits and method == 'score_prop':
        commits = list(candidates.keys())
        scores = [_candidate_score(c) for c in commits]
        scores = [1 / (1 + math.exp(-10 * (score - 0.5))) for score in scores]
        probabilities = [score / sum(scores) for score in scores]
        parent_commits = random.choices(commits, probabilities, k=selfimprove_size)
    elif not parent_commits and method == 'score_child_prop':
        commits = list(candidates.keys())
        scores = [_candidate_score(c) for c in commits]
        scores = [1 / (1 + math.exp(-10 * (score - 0.5))) for score in scores]
        children_counts = [candidates[commit]['children_count'] for commit in commits]
        children_counts = [1 / (1 + count) for count in children_counts]
        probabilities = [score * count for score, count in zip(scores, children_counts)]
        probabilities = [prob / sum(probabilities) for prob in probabilities]
        parent_commits = random.choices(commits, probabilities, k=selfimprove_size)
    elif not parent_commits and method == 'best':
        sorted_commits = sorted(candidates, key=lambda x: _candidate_score(x), reverse=True)
        parent_commits = sorted_commits[:min(selfimprove_size, len(sorted_commits))]
        if len(parent_commits) < selfimprove_size:
            parent_commits.extend(random.choices(parent_commits, k=selfimprove_size - len(parent_commits)))
    elif not parent_commits and method == 'three_way':
        # AlphaEvolve-style three-way selection (Novikov et al. 2025)
        # Configurable via --explore_frac / --exploit_frac; diversity = remainder.
        # Stagnation shifts are relative deltas from the configured base fracs,
        # so islands with different base fracs maintain diversity even under stagnation.
        if stagnation_triggered:
            explore_frac = min(explore_frac + 0.10, 1.0)
            exploit_frac = max(exploit_frac - 0.30, 0.0)
        # diversity_frac is the remainder

        commits = list(candidates.keys())
        bvc = bin_visit_counts or {}
        parent_commits = []
        selection_details = []

        for _ in range(selfimprove_size):
            roll = random.random()
            if roll < explore_frac:
                # Exploration: uniform random
                parent_commits.append(random.choice(commits))
                selection_details.append({"method": "exploration", "bin": None})
            elif roll < explore_frac + exploit_frac:
                # Exploitation: top-3 by Tchebycheff with noise
                sorted_by_score = sorted(commits, key=lambda c: _candidate_score(c), reverse=True)
                top_k = sorted_by_score[:min(3, len(sorted_by_score))]
                chosen = random.choice(top_k)
                parent_commits.append(chosen)
                obj = candidates[chosen].get("objective_vector")
                selection_details.append({
                    "method": "exploitation",
                    "bin": list(_get_bin(obj)) if obj else None,
                })
            else:
                # Diversity: MAP-Elites bin, inversely weighted by visit count
                binned: Dict[Tuple[int, int], List[str]] = {}
                for c in commits:
                    obj = candidates[c].get("objective_vector")
                    if obj:
                        b = _get_bin(obj)
                        binned.setdefault(b, []).append(c)
                if binned:
                    bin_weights = {b: 1.0 / (1.0 + bvc.get(str(b), 0)) for b in binned}
                    total_w = sum(bin_weights.values())
                    bins_list = list(binned.keys())
                    probs = [bin_weights[b] / total_w for b in bins_list]
                    chosen_bin = random.choices(bins_list, probs)[0]
                    chosen = random.choice(binned[chosen_bin])
                    parent_commits.append(chosen)
                    selection_details.append({
                        "method": "diversity",
                        "bin": list(chosen_bin),
                    })
                    # Track visit
                    bvc[str(chosen_bin)] = bvc.get(str(chosen_bin), 0) + 1
                else:
                    parent_commits.append(random.choice(commits))
                    selection_details.append({"method": "exploration", "bin": None})
    elif not parent_commits:
        parent_commits = random.choices(list(candidates.keys()), k=selfimprove_size)

    # Exploit-first scaffolding: bias selection toward current best parent.
    if anchor_best_parent and parent_commits and not force_parent_commit:
        sorted_commits = sorted(candidates, key=lambda x: _candidate_score(x), reverse=True)
        if sorted_commits:
            best_parent = sorted_commits[0]
            parent_commits = [
                best_parent if random.random() < max(0.0, min(1.0, anchor_parent_frac)) else p
                for p in parent_commits
            ]

    # Hard guard: when forced parent is requested, never allow silent drift.
    if force_parent_commit:
        mismatches = sorted({p for p in parent_commits if p != force_parent_commit})
        if mismatches:
            raise RuntimeError(
                f"force_parent_commit drift detected: expected={force_parent_commit}, "
                f"selected={mismatches}"
            )

    # DGM: DGM_outer.py:111-148 — entry selection
    # CHANGED: DGM selects SWE-bench issue IDs; CCA selects performance dimension
    # Build selection_details for non-three_way methods
    if method != 'three_way':
        selection_details = [{"method": method, "bin": None}] * len(parent_commits)

    for idx, parent_commit in enumerate(parent_commits):
        perf = {
            'accuracy_score': candidates[parent_commit]['accuracy_score'],
            'per_trace': candidates[parent_commit].get('per_trace', {}),
            'objective_vector': candidates[parent_commit].get('objective_vector'),
            'bottleneck_dim': candidates[parent_commit].get('bottleneck_dim'),
            'bottleneck_gap': candidates[parent_commit].get('bottleneck_gap'),
            'selection_score': candidates[parent_commit].get('selection_score'),
            'confidence_penalty': candidates[parent_commit].get('confidence_penalty', 0.0),
            'total_submitted_instances': (
                len(candidates[parent_commit]['total_resolved_ids'])
                + len(candidates[parent_commit]['total_unresolved_ids'])
                + len(candidates[parent_commit]['total_emptypatch_ids'])
            ),
        }
        entry = choose_entry(perf, frontier_targets=frontier_targets)
        sel_meta = selection_details[idx] if idx < len(selection_details) else {}
        selfimprove_entries.append((parent_commit, entry, sel_meta))

    return selfimprove_entries


# DGM: DGM_outer.py:152-165, CHANGED: added try/except around load_json_file
def filter_compiled(run_ids, output_dir, num_swe_issues=[], logger=None):
    """
    Filter out runs that did not compile or have all empty patches.
    """
    run_ids_compiled = []

    logger.info(f"num_swe_issues: {num_swe_issues}")
    for run_id in run_ids:
        metadata_path = os.path.join(output_dir, run_id, "metadata.json")
        # ADDED: try/except — without Docker isolation, step may crash before writing metadata
        try:
            metadata = load_json_file(metadata_path)
        except Exception:
            continue
        logger.info(f"{run_id} metadata: {metadata}")
        if is_compiled_self_improve(metadata, num_swe_issues=num_swe_issues, logger=logger):
            run_ids_compiled.append(run_id)
    return run_ids_compiled


# DGM: DGM_outer.py:167-172, VERBATIM
def get_original_score(output_dir):
    """
    Get the original score from the initial version.
    """
    metadata = load_json_file(os.path.join(output_dir, "initial", "metadata.json"))
    return metadata["overall_performance"]["accuracy_score"]


# DGM: DGM_outer.py:174-190, VERBATIM
def update_archive(output_dir, archive, new_ids, method='keep_all', noise_leeway=0.1,
                   map_dims: Optional[List[str]] = None,
                   map_bins: Optional[Dict[str, int]] = None,
                   map_max_cells: int = 0):
    """
    Update the archive with the new self-improve runs.
    """
    if method == 'keep_pareto':
        # Keep only non-dominated members in 4D objective space.
        all_ids = []
        for rid in archive + new_ids:
            if rid not in all_ids:
                all_ids.append(rid)

        _logger = logging.getLogger('dgm_cca')
        rows = []
        passthrough = []
        for rid in all_ids:
            try:
                metadata = load_json_file(os.path.join(output_dir, rid, "metadata.json"))
                perf = metadata.get("overall_performance", {})
                # Early-stopped candidates should not enter the Pareto frontier —
                # they were undertested and would mislead parent selection.
                if perf.get("early_stopped", False):
                    _logger.info(f"{rid} skipped for MAP-Elites (early-stopped)")
                    passthrough.append(rid)
                    continue
                obj = perf.get("objective_vector")
                if not obj:
                    passthrough.append(rid)
                    continue
                rows.append((
                    rid,
                    {d: float(obj.get(d, 0.0)) for d in OBJ_DIMS},
                    float(perf.get("selection_score", perf.get("accuracy_score", 0.0))),
                ))
            except Exception:
                continue

        frontier_ids = []
        for i, (rid, obj, score) in enumerate(rows):
            dominated = False
            for j, (rid2, obj2, score2) in enumerate(rows):
                if i == j:
                    continue
                if _dominates(obj2, obj) or (obj2 == obj and score2 > score):
                    dominated = True
                    break
            if not dominated:
                frontier_ids.append(rid)

        archive = passthrough + frontier_ids
    elif method == 'keep_map_elites':
        # OpenEvolve-inspired MAP-Elites archive:
        # keep best score per feature-bin cell over selected objective dimensions.
        dims = map_dims or list(OBJ_DIMS)
        bins = map_bins or {d: 8 for d in dims}
        all_ids = []
        for rid in archive + new_ids:
            if rid not in all_ids:
                all_ids.append(rid)

        _logger = logging.getLogger('dgm_cca')
        feature_map: Dict[str, Dict] = {}
        passthrough = []
        for rid in all_ids:
            try:
                metadata = load_json_file(os.path.join(output_dir, rid, "metadata.json"))
                perf = metadata.get("overall_performance", {})
                # Early-stopped candidates should not occupy MAP-Elites bins —
                # they were undertested and would mislead diversity selection.
                if perf.get("early_stopped", False):
                    _logger.info(f"{rid} skipped for MAP-Elites (early-stopped)")
                    passthrough.append(rid)
                    continue
                obj = perf.get("objective_vector")
                score = float(perf.get("selection_score", perf.get("accuracy_score", 0.0)))
                if not obj:
                    passthrough.append(rid)
                    continue
                coords = _feature_coords(obj, dims, bins)
                key = ",".join(str(x) for x in coords)
                existing = feature_map.get(key)
                if existing is None or score > float(existing.get("score", 0.0)):
                    feature_map[key] = {"run_id": rid, "score": score, "coords": list(coords)}
            except Exception:
                continue

        cell_entries = sorted(feature_map.values(), key=lambda x: float(x["score"]), reverse=True)
        if map_max_cells and map_max_cells > 0:
            cell_entries = cell_entries[:map_max_cells]
        map_ids = [e["run_id"] for e in cell_entries]

        # Keep 'initial' for safety and passthrough entries (non-objective records).
        merged = []
        for rid in (["initial"] + passthrough + map_ids):
            if rid not in merged:
                merged.append(rid)
        archive = merged

        # Persist map for monitoring/debugging.
        _save_feature_map(output_dir, feature_map)

    elif method == 'keep_better':
        # keep only better ones
        original_score = get_original_score(output_dir) - noise_leeway
        for run_id in new_ids:
            metadata = load_json_file(os.path.join(output_dir, run_id, "metadata.json"))
            score = metadata["overall_performance"]["accuracy_score"]
            if score >= original_score:
                archive.append(run_id)
    else:
        # keep everything
        archive += new_ids

    return archive


# REMOVED: get_full_eval_threshold (DGM:192-219) — SWE-bench-specific


# DGM: DGM_outer.py:221-334, CHANGED: SWE-bench args/loading → CCA trace loading
def main():
    parser = argparse.ArgumentParser(description="DGM-CCA: Evolve kernel CCAs with LeoReplayer evaluation")
    parser.add_argument("--max_generation", type=int, default=80, help="Maximum number of evolution iterations.")
    parser.add_argument("--selfimprove_size", type=int, default=2, help="Number of self-improvements attempts per DGM generation.")
    parser.add_argument("--selfimprove_workers", type=int, default=1, help="Number of parallel workers (use 1 for kernel module safety).")
    parser.add_argument(
        "--choose_selfimproves_method", type=str, default='three_way',
        choices=['random', 'score_prop', 'score_child_prop', 'best', 'three_way'],
        help="Method to choose self-improve attempts.",
    )
    parser.add_argument("--continue_from", type=str, default=None, help="Directory to continue the run from.")
    parser.add_argument(
        "--update_archive",
        type=str,
        default='keep_map_elites',
        choices=['keep_better', 'keep_all', 'keep_pareto', 'keep_map_elites'],
        help="Method to update the archive.",
    )
    parser.add_argument(
        "--map_elites_dims",
        type=str,
        default="util,delay_quality,loss_efficiency,robustness",
        help="Comma-separated objective dims used by keep_map_elites.",
    )
    parser.add_argument(
        "--map_elites_bins",
        type=str,
        default="util:8,delay_quality:8,loss_efficiency:6,robustness:8",
        help="Bins per dim for keep_map_elites (or single int like '8').",
    )
    parser.add_argument(
        "--map_elites_max_cells",
        type=int,
        default=64,
        help="If >0, cap MAP-Elites archive cells to top-N by score.",
    )
    parser.add_argument(
        "--migration_interval_gens",
        type=int,
        default=5,
        help="Run cross-pollination script every N generations (0 disables).",
    )
    parser.add_argument(
        "--cross_pollinate_script",
        type=str,
        default="scripts/gcp/cross_pollinate.sh",
        help="Path to cross-pollination script executed periodically.",
    )
    parser.add_argument(
        "--migration_leader",
        action="store_true",
        default=False,
        help="Only the migration leader executes cross-pollination sync script.",
    )
    parser.add_argument(
        "--migration_rate",
        type=float,
        default=0.25,
        help="Fraction of eligible migrants exported per island during cross-pollination.",
    )
    parser.add_argument(
        "--migration_protect_top_k",
        type=int,
        default=1,
        help="Keep top-K candidates local during cross-pollination export.",
    )
    parser.add_argument(
        "--playbook_maint_interval_gens",
        type=int,
        default=10,
        help="Run shared playbook prune/summarize script every N generations (0 disables).",
    )
    parser.add_argument(
        "--playbook_maint_script",
        type=str,
        default="scripts/gcp/maintain_playbook.sh",
        help="Path to shared playbook maintenance script.",
    )
    # CHANGED: SWE-bench args → CCA trace args
    parser.add_argument("--trace_dir", type=str, default="data/starlink_traces")
    parser.add_argument("--num_traces", type=int, default=5)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--eval_noise", type=float, default=0.1, help="Noise leeway for evaluation.")
    # Racing / early-stop (Mechanism 1)
    parser.add_argument("--enable_racing", action="store_true", default=False)
    parser.add_argument("--no_racing", dest="enable_racing", action="store_false")
    parser.add_argument("--racing_traces", type=int, default=3)
    parser.add_argument("--racing_margin", type=float, default=0.15)
    # Mutation-type bandit (Mechanism 2)
    parser.add_argument("--enable_bandit", action="store_true", default=True)
    parser.add_argument("--no_bandit", dest="enable_bandit", action="store_false")
    parser.add_argument("--bandit_exploration_c", type=float, default=1.41)
    parser.add_argument("--bandit_epsilon", type=float, default=0.0,
                        help="Epsilon-greedy probability for bandit (0=pure UCB1, 1=pure random).")
    # Diversity: LLM temperature and selection ratios
    parser.add_argument("--llm_temperature", type=float, default=1.0,
                        help="Temperature for LLM calls (diagnosis + coding agent).")
    parser.add_argument("--explore_frac", type=float, default=0.20,
                        help="Exploration fraction for three-way selection.")
    parser.add_argument("--exploit_frac", type=float, default=0.50,
                        help="Exploitation fraction for three-way selection.")
    parser.add_argument("--anchor_best_parent", action="store_true", default=False,
                        help="Bias parent selection toward current best archive member.")
    parser.add_argument("--anchor_parent_frac", type=float, default=0.85,
                        help="When --anchor_best_parent, fraction of children anchored to best parent.")
    parser.add_argument("--force_parent_commit", type=str, default=None,
                        help="If set, force every child to use this parent commit.")
    parser.add_argument("--rolling_anchor", action="store_true", default=False,
                        help="Enable rolling anchor: bootstrap pin, then local-best anchor with periodic reseed.")
    parser.add_argument("--rolling_anchor_bootstrap_gens", type=int, default=5,
                        help="When --rolling_anchor, number of initial generations to pin to force_parent_commit.")
    parser.add_argument("--rolling_anchor_refresh_gens", type=int, default=10,
                        help="When --rolling_anchor, reseed back to force_parent_commit every N generations (0 disables).")
    parser.add_argument("--max_patch_lines", type=int, default=40,
                        help="Reject oversized mutation patches (>N changed lines) before evaluation.")
    # Stagnation detection (Mechanism 4)
    parser.add_argument("--enable_stagnation", action="store_true", default=True)
    parser.add_argument("--no_stagnation", dest="enable_stagnation", action="store_false")
    parser.add_argument("--stagnation_alpha", type=float, default=0.3)
    parser.add_argument("--stagnation_threshold", type=float, default=0.001)
    parser.add_argument("--stagnation_consecutive", type=int, default=5)
    # Reproducibility
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducibility")
    args = parser.parse_args()
    map_dims = [d.strip() for d in args.map_elites_dims.split(",") if d.strip()]
    if not map_dims:
        map_dims = list(OBJ_DIMS)
    map_bins = _parse_feature_bins(args.map_elites_bins, map_dims, default_bins=8)

    # Variables for this DGM run
    if not args.continue_from:
        run_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S_%f")
    else:
        run_id = os.path.basename(args.continue_from)

    output_dir = os.path.join("output_dgm_cca", run_id)  # CHANGED: output_dgm → output_dgm_cca
    os.makedirs(output_dir, exist_ok=True)

    # Initialize
    archive, start_gen_num = initialize_run(output_dir, prevrun_dir=args.continue_from)

    # Seed RNG for reproducibility
    if args.seed is not None:
        random.seed(args.seed)

    # Initialize mutation bandit (persists across --continue_from)
    bandit = None
    if args.enable_bandit:
        bandit = MutationBandit(
            state_path=os.path.join(output_dir, "bandit_state.json"),
            exploration_c=args.bandit_exploration_c,
            epsilon=args.bandit_epsilon,
        )

    # Initialize stagnation detector (reconstructs state from metadata)
    stagnation = None
    if args.enable_stagnation:
        stagnation = StagnationDetector.from_metadata(
            output_dir,
            alpha=args.stagnation_alpha,
            threshold=args.stagnation_threshold,
            consecutive_threshold=args.stagnation_consecutive,
        )

    # Track bin visit counts for MAP-Elites diversity selection.
    # Accumulate across ALL historical generations (not just last snapshot)
    # to prevent diversity mechanism from resetting on --continue_from.
    bin_visit_counts: Dict[str, int] = {}
    if args.continue_from:
        meta_path = os.path.join(output_dir, "dgm_metadata.jsonl")
        for entry in _parse_metadata_jsonl(meta_path):
            bvc = entry.get("bin_visit_counts")
            if bvc:
                # Each generation saves the running total. The last entry
                # for each key is the cumulative count.
                for k, v in bvc.items():
                    bin_visit_counts[k] = max(bin_visit_counts.get(k, 0), v)

    # CHANGED: SWE issues loading → trace pair discovery
    trace_pairs = find_trace_pairs(args.trace_dir, max_traces=args.num_traces)
    if not trace_pairs:
        print(f"ERROR: No traces found in {args.trace_dir}")
        print("Run: bash scripts/gcp/setup_leo_traces.sh")
        sys.exit(1)

    # CHANGED: setup_logger → logging module (docker_utils not available)
    logger = logging.getLogger('dgm_cca')
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(output_dir, "dgm_outer.log"))
    fh.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(fh)
    logger.addHandler(logging.StreamHandler())

    logger.info(f"Starting DGM-CCA run {run_id} with arguments: {vars(args)}")
    logger.info(f"Archive: {archive}")
    logger.info(f"Archive mode: {args.update_archive}; map_dims={map_dims}; map_bins={map_bins}; max_cells={args.map_elites_max_cells}")
    logger.info(f"Traces: {len(trace_pairs)} pairs from {args.trace_dir}")

    # --- Seed verification: re-evaluate seed CCA on this VM for honest baseline ---
    if start_gen_num == 0 and not args.continue_from:
        logger.info("Verifying seed CCA on this VM...")
        # Restore original seed source — workspace may contain mutated code from prior runs
        _reset = subprocess.run(["git", "checkout", "--", "tcp_evolved.c"],
                         cwd=CCA_WORKSPACE, capture_output=True, text=True)
        if _reset.returncode != 0:
            logger.warning(f"git checkout seed source failed: {_reset.stderr.strip()}")
        build_err = build_and_load_cca(CCA_WORKSPACE, module_name="tcp_evolved")
        if build_err:
            logger.error(f"Seed build/load failed: {build_err}")
            sys.exit(1)
        try:
            seed_result = evaluate_cca(
                cca_name="evolved",
                trace_pairs=trace_pairs,
                duration=args.duration,
                enable_racing=False,
            )
            seed_meta_path = os.path.join(output_dir, "initial", "metadata.json")
            old_meta = load_json_file(seed_meta_path)
            old_score = old_meta.get("overall_performance", {}).get("selection_score", 0)
            new_score = seed_result.get("selection_score", 0)
            old_meta["overall_performance"] = seed_result
            with open(seed_meta_path, "w") as f:
                json.dump(old_meta, f, indent=4, default=str)
            logger.info(f"Verified seed score on this VM: {new_score:.4f} (was {old_score})")
        except Exception as e:
            logger.error(f"Seed evaluation failed: {e}")
            sys.exit(1)
        finally:
            unload_err = unload_cca("tcp_evolved")
            if unload_err:
                logger.warning(f"Seed unload warning: {unload_err}")

    # Track recent mutation categories for stagnation-triggered exclusion (cap at 20)
    recent_categories: List[str] = []
    if args.continue_from:
        meta_path = os.path.join(output_dir, "dgm_metadata.jsonl")
        for entry in _parse_metadata_jsonl(meta_path):
            for child_id in entry.get("children", []):
                try:
                    cm = load_json_file(os.path.join(output_dir, child_id, "metadata.json"))
                    cat = cm.get("mutation_category")
                    if cat:
                        recent_categories.append(cat)
                except Exception:
                    continue
        recent_categories = recent_categories[-20:]

    # Run the DGM
    for gen_num in range(start_gen_num, args.max_generation):
        # --- Stagnation check (before selection) ---
        stagnation_triggered = False
        stagnation_state = {}
        if stagnation is not None:
            stagnation_triggered = stagnation.get_state()["triggered"]
            stagnation_state = stagnation.get_state()
            if stagnation_triggered:
                logger.info(f"Gen {gen_num}: STAGNATION TRIGGERED "
                            f"(EMA={stagnation_state['ema']:.6f}, "
                            f"consecutive={stagnation_state['consecutive_below']})")

        # --- Bandit stats (per-mutation suggestions computed below) ---
        bandit_stats = {}
        if bandit is not None:
            bandit_stats = bandit.get_stats()
            logger.info(f"Gen {gen_num}: Bandit stats: "
                        f"{json.dumps(bandit_stats, default=str)}")

        # --- Stagnation meta for diagnosis prompt ---
        stagnation_meta = None
        if stagnation_triggered and stagnation_state:
            stagnation_meta = {
                "triggered": True,
                "consecutive": stagnation_state["consecutive_below"],
                "ema": stagnation_state["ema"],
                "recent_categories": recent_categories[-5:],
            }

        # Compute effective parent pin for this generation.
        # rolling_anchor policy:
        # - bootstrap: pin to configured force_parent_commit for first K generations
        # - afterward: pin to local best-so-far commit (enables compounding)
        # - optional periodic reseed back to original forced parent every N generations
        effective_force_parent = args.force_parent_commit
        if args.rolling_anchor:
            bootstrap_end = start_gen_num + max(0, int(args.rolling_anchor_bootstrap_gens))
            local_best = _best_lineage_commit(output_dir, archive, args.force_parent_commit)
            if gen_num < bootstrap_end:
                effective_force_parent = args.force_parent_commit or local_best
            else:
                effective_force_parent = local_best or args.force_parent_commit
                refresh = max(0, int(args.rolling_anchor_refresh_gens))
                if (refresh > 0 and args.force_parent_commit and
                        (gen_num - bootstrap_end) > 0 and
                        ((gen_num - bootstrap_end) % refresh == 0)):
                    effective_force_parent = args.force_parent_commit
            logger.info(
                f"Gen {gen_num}: rolling_anchor effective_parent={effective_force_parent} "
                f"(configured={args.force_parent_commit}, local_best={local_best}, "
                f"bootstrap_end={bootstrap_end})"
            )

        # Choose self-improve attempts (returns 3-tuples now)
        selfimprove_entries = choose_selfimproves(
            output_dir, archive, args.selfimprove_size,
            method=args.choose_selfimproves_method,
            stagnation_triggered=stagnation_triggered,
            bin_visit_counts=bin_visit_counts,
            explore_frac=args.explore_frac,
            exploit_frac=args.exploit_frac,
            anchor_best_parent=args.anchor_best_parent,
            anchor_parent_frac=args.anchor_parent_frac,
            force_parent_commit=effective_force_parent,
        )
        logger.info(f"Self-improve entries for generation {gen_num}: "
                    f"{[(pc, e, sm.get('method')) for pc, e, sm in selfimprove_entries]}")

        # --- Stagnation: force entry type rotation ---
        if stagnation_triggered:
            # Read last 3 generations' entry types from metadata
            recent_entries = []
            meta_path = os.path.join(output_dir, "dgm_metadata.jsonl")
            for m in _parse_metadata_jsonl(meta_path):
                for se in m.get("selfimprove_entries", []):
                    if isinstance(se, (list, tuple)) and len(se) >= 2:
                        recent_entries.append(se[1])
            recent_entry_set = set(recent_entries[-3:])
            available_entries = [e for e in ENTRY_TYPES if e not in recent_entry_set]
            if available_entries:
                for i, (pc, entry, sm) in enumerate(selfimprove_entries):
                    if entry in recent_entry_set:
                        new_entry = random.choice(available_entries)
                        selfimprove_entries[i] = (pc, new_entry, sm)
                        logger.info(f"Stagnation: rotated entry '{entry}' → '{new_entry}'")

        # Run self-improvement processes
        selfimprove_ids = []
        # Map future → (parent_commit, entry, sel_meta, suggestion) for post-hoc
        future_to_entry = {}
        with ThreadPoolExecutor(max_workers=args.selfimprove_workers) as executor:
            futures = []
            for parent_commit, entry, sel_meta in selfimprove_entries:
                # Per-mutation bandit suggestion (re-select after each update)
                if bandit is not None:
                    exclude = recent_categories[-3:] if stagnation_triggered else None
                    mut_suggestion = bandit.select(exclude=exclude)
                else:
                    mut_suggestion = None
                fut = executor.submit(
                    self_improve_cca,
                    parent_commit=parent_commit,
                    output_dir=output_dir,
                    entry=entry,
                    trace_pairs=trace_pairs,
                    duration=args.duration,
                    archive=archive,
                    bandit_suggestion=mut_suggestion,
                    stagnation_meta=stagnation_meta,
                    enable_racing=args.enable_racing,
                    racing_traces=args.racing_traces,
                    racing_margin=args.racing_margin,
                    llm_temperature=args.llm_temperature,
                    max_patch_lines=args.max_patch_lines,
                )
                futures.append(fut)
                future_to_entry[fut] = (parent_commit, entry, sel_meta, mut_suggestion)

            for future in as_completed(futures):
                parent_commit, entry, sel_meta, mut_suggestion = future_to_entry[future]
                try:
                    metadata = future.result(timeout=1.5*60*60)
                    run_id = metadata['run_id']
                    selfimprove_ids.append(run_id)

                    # --- Post-hoc: classify diff and update bandit ---
                    if bandit is not None:
                        # Read the git diff for this mutation
                        diff_path = os.path.join(output_dir, run_id, "model_patch.diff")
                        diff_text = ""
                        if os.path.exists(diff_path):
                            diff_text = read_file(diff_path)
                        category = classify_mutation(diff_text)
                        suggestion_followed = (category == mut_suggestion) if mut_suggestion else None
                        # Reward = max(0, child_score - parent_score)
                        child_score = metadata.get("overall_performance", {}).get("accuracy_score", 0.0)
                        try:
                            parent_meta = load_json_file(os.path.join(output_dir, parent_commit, "metadata.json"))
                            parent_score = parent_meta["overall_performance"]["accuracy_score"]
                        except Exception:
                            parent_score = 0.0
                        reward = max(0.0, child_score - parent_score)
                        # Skip bandit update for early-stopped candidates: their accuracy
                        # is penalized by zero-imputation for skipped traces, producing a
                        # near-zero reward that would falsely penalize the mutation category.
                        early_stopped = metadata.get("overall_performance", {}).get("early_stopped", False)
                        if not early_stopped:
                            bandit.update(category, reward)
                        else:
                            logger.info(f"  Bandit: skipping update for early-stopped {run_id}")
                        recent_categories.append(category)
                        recent_categories = recent_categories[-20:]
                        logger.info(f"  Bandit: {run_id} classified as '{category}', "
                                    f"suggestion={mut_suggestion}, followed={suggestion_followed}, "
                                    f"reward={reward:.4f}, early_stopped={early_stopped} "
                                    f"(child={child_score:.4f}, parent={parent_score:.4f})")

                        # Save category + selection info in child's metadata
                        child_meta_path = os.path.join(output_dir, run_id, "metadata.json")
                        try:
                            child_meta = load_json_file(child_meta_path)
                            child_meta["mutation_category"] = category
                            child_meta["mutation_reward"] = round(reward, 5)
                            child_meta["bandit_suggestion"] = mut_suggestion
                            child_meta["suggestion_followed"] = suggestion_followed
                            child_meta["selection_method"] = sel_meta.get("method")
                            child_meta["selection_bin"] = sel_meta.get("bin")
                            child_meta["early_stopped"] = metadata.get("overall_performance", {}).get("early_stopped", False)
                            child_meta["racing_traces_evaluated"] = metadata.get("overall_performance", {}).get("racing_traces_evaluated")
                            with open(child_meta_path, "w") as f:
                                json.dump(child_meta, f, indent=2)
                        except Exception as e:
                            logger.warning(f"Could not update child metadata: {e}")

                except TimeoutError:
                    logger.error("Self-improvement attempt timed out (thread still running).")
                except Exception as e:
                    import traceback
                    logger.error(f"Self-improvement step failed: {e}")
                    logger.error(f"Traceback:\n{traceback.format_exc()}")

        # Update archive
        logger.info(f"Updating archive for generation {gen_num}")
        selfimprove_ids_compiled = filter_compiled(
            selfimprove_ids,
            output_dir,
            num_swe_issues=[len(trace_pairs)],
            logger=logger,
        )
        archive = update_archive(
            output_dir,
            archive,
            selfimprove_ids_compiled,
            method=args.update_archive,
            noise_leeway=args.eval_noise,
            map_dims=map_dims,
            map_bins=map_bins,
            map_max_cells=args.map_elites_max_cells,
        )

        # OpenEvolve-style periodic island migration trigger.
        # Here migration is implemented as an external cross-pollination sync script.
        if (args.migration_leader and args.migration_interval_gens > 0 and gen_num > 0
                and (gen_num % args.migration_interval_gens == 0)):
            try:
                script_path = args.cross_pollinate_script
                if not os.path.isabs(script_path):
                    # Prefer CWD-relative path; fallback to repo-root-relative.
                    cwd_candidate = os.path.abspath(script_path)
                    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
                    repo_candidate = os.path.join(repo_root, script_path)
                    if os.path.exists(cwd_candidate):
                        script_path = cwd_candidate
                    elif os.path.exists(repo_candidate):
                        script_path = repo_candidate
                if os.path.exists(script_path):
                    result = subprocess.run(
                        [
                            "bash", script_path,
                            "--run_rel", output_dir,
                            "--migration_rate", str(args.migration_rate),
                            "--protect_top_k", str(args.migration_protect_top_k),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=20 * 60,
                    )
                    logger.info(
                        f"Migration sync run at gen {gen_num}: rc={result.returncode}, "
                        f"stdout_tail={result.stdout[-500:] if result.stdout else ''}, "
                        f"stderr_tail={result.stderr[-500:] if result.stderr else ''}"
                    )
                else:
                    logger.warning(f"Migration sync skipped: script not found at {script_path}")
            except Exception as e:
                logger.warning(f"Migration sync failed at gen {gen_num}: {e}")

        # Shinka-inspired periodic memory consolidation:
        # prune/summarize shared playbook to keep prompts dense and current.
        if (args.migration_leader and args.playbook_maint_interval_gens > 0 and gen_num > 0
                and (gen_num % args.playbook_maint_interval_gens == 0)):
            try:
                script_path = args.playbook_maint_script
                if not os.path.isabs(script_path):
                    cwd_candidate = os.path.abspath(script_path)
                    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
                    repo_candidate = os.path.join(repo_root, script_path)
                    if os.path.exists(cwd_candidate):
                        script_path = cwd_candidate
                    elif os.path.exists(repo_candidate):
                        script_path = repo_candidate
                if os.path.exists(script_path):
                    result = subprocess.run(
                        ["bash", script_path, "--run_rel", output_dir],
                        capture_output=True,
                        text=True,
                        timeout=20 * 60,
                    )
                    logger.info(
                        f"Playbook maintenance run at gen {gen_num}: rc={result.returncode}, "
                        f"stdout_tail={result.stdout[-500:] if result.stdout else ''}, "
                        f"stderr_tail={result.stderr[-500:] if result.stderr else ''}"
                    )
                else:
                    logger.warning(f"Playbook maintenance skipped: script not found at {script_path}")
            except Exception as e:
                logger.warning(f"Playbook maintenance failed at gen {gen_num}: {e}")

        # --- Cross-pollination: discover externally-injected entries ---
        # Scan output_dir for metadata.json files not yet in the archive.
        # Enables expert mutations and island cross-pollination without restart.
        try:
            external_ids = []
            for entry in os.listdir(output_dir):
                if entry in archive:
                    continue
                meta_path = os.path.join(output_dir, entry, "metadata.json")
                if os.path.isfile(meta_path):
                    m = load_json_file(meta_path)
                    acc = m.get("overall_performance", {}).get("accuracy_score", 0)
                    if acc > 0.75:  # only pick up reasonably-scored entries
                        external_ids.append(entry)
            if external_ids:
                archive = update_archive(
                    output_dir,
                    archive,
                    external_ids,
                    method=args.update_archive,
                    noise_leeway=args.eval_noise,
                    map_dims=map_dims,
                    map_bins=map_bins,
                    map_max_cells=args.map_elites_max_cells,
                )
                logger.info(f"External archive import: added {len(external_ids)} entries (total archive: {len(archive)})")
        except Exception as e:
            logger.warning(f"Cross-pollination scan failed: {e}")

        # --- Stagnation update ---
        best_child_score = 0.0
        best_archive_score = 0.0
        for rid in selfimprove_ids_compiled:
            try:
                m = load_json_file(os.path.join(output_dir, rid, "metadata.json"))
                s = m["overall_performance"]["accuracy_score"]
                best_child_score = max(best_child_score, s)
            except Exception:
                pass
        for rid in archive:
            try:
                m = load_json_file(os.path.join(output_dir, rid, "metadata.json"))
                s = m["overall_performance"]["accuracy_score"]
                best_archive_score = max(best_archive_score, s)
            except Exception:
                pass
        if stagnation is not None:
            stag_triggered = stagnation.update(gen_num, best_child_score, best_archive_score)
            stagnation_state = stagnation.get_state()
            if stag_triggered:
                logger.info(f"Gen {gen_num}: Stagnation update → TRIGGERED")

        # Save DGM state with all new metadata fields
        gen_metadata = {
            "generation": gen_num,
            "effective_force_parent": effective_force_parent,
            "selfimprove_entries": [
                [pc, e, sm] for pc, e, sm in selfimprove_entries
            ],
            "children": selfimprove_ids,
            "children_compiled": selfimprove_ids_compiled,
            "archive": archive,
        }
        if bandit is not None:
            gen_metadata["bandit_stats"] = bandit.get_stats()
        if stagnation is not None:
            gen_metadata["stagnation_state"] = stagnation.get_state()
        if selfimprove_entries:
            gen_metadata["selection_details"] = [
                sm for _, _, sm in selfimprove_entries
            ]
        gen_metadata["bin_visit_counts"] = bin_visit_counts
        gen_metadata["archive_method"] = args.update_archive
        gen_metadata["map_elites_dims"] = map_dims
        gen_metadata["map_elites_bins"] = map_bins

        with open(os.path.join(output_dir, "dgm_metadata.jsonl"), "a") as f:
            f.write(json.dumps(gen_metadata, indent=2) + "\n")

        logger.info(f"Gen {gen_num} complete: {len(selfimprove_ids_compiled)}/{len(selfimprove_ids)} compiled, archive={len(archive)}")

    print("\n=== Evolution Complete ===")
    print(f"Final archive: {archive}")


if __name__ == "__main__":
    main()

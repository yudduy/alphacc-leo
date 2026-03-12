"""CCA self-improvement step.

Copied from dgm/self_improve_step.py via `cp`. Then edited:
- Lines 1-25: imports — removed docker/swe_bench/polyglot, added CCA equivalents
- Lines 27-28: diagnose_model — changed from o1 to o4-mini
- Lines 30-67: diagnose_problem — CCA performance diagnosis instead of SWE-bench
- Lines 69-118: diagnose_improvement — REMOVED (post-improvement, not needed initially)
- Lines 120-123: save_metadata — VERBATIM
- Lines 125-221: run_harness_swe, run_harness_polyglot — REMOVED, replaced inline with CCA harness
- Lines 223-420: self_improve → self_improve_cca:
  - Lines 240-247: dataset loading — REMOVED
  - Lines 249-258: variable init — VERBATIM (root_dir, out_dir_base, output_dir shadow)
  - Lines 263-274: Docker container — CHANGED to git clone workspace
  - Lines 292-300: patch application — same pattern, git_utils instead of Docker
  - Lines 302-309: git commit + hash — same, but git rev-parse instead of parsing output
  - Lines 315-333: diagnosis — CCA performance diagnosis
  - Lines 335-359: coding agent — subprocess instead of Docker exec
  - Lines 361-384: patch extraction — diff_versus_commit instead of Docker copy
  - Lines 386-399: evaluation — CCA build/load/eval/unload instead of SWE-bench harness
  - Lines 401-416: post-improvement diagnosis — REMOVED
  - Lines 418-420: save metadata — VERBATIM
- Lines 422-447: main — REMOVED (called from outer.py)
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

# REMOVED: import docker

# Ensure dgm is importable
import alphacc.dgm_cca  # noqa: F401

# DGM imports (via sys.path from __init__.py)
from llm import create_client, get_response_from_llm, extract_json_between_markers
# REMOVED: from prompts.self_improvement_prompt import get_diagnose_prompt_polyglot, get_diagnose_prompt_swe, get_problem_description_prompt
# REMOVED: from prompts.diagnose_improvement_prompt import get_diagnose_improvement_prompt
# REMOVED: from prompts.testrepo_prompt import get_test_description
# REMOVED: from swe_bench.harness import harness
# REMOVED: from polyglot.harness import harness as polyglot_harness
# REMOVED: from swe_bench.report import make_report
from utils.common_utils import load_json_file, read_file
from utils.evo_utils import get_model_patch_paths, get_all_performance, is_compiled_self_improve
# REMOVED: from utils.docker_utils import (build_dgm_container, cleanup_container, ...)
from utils.git_utils import apply_patch, diff_versus_commit, reset_to_commit

# ADDED: CCA-specific imports
from alphacc.dgm_cca.cca_harness import (
    evaluate_cca,
    build_and_load_cca,
    unload_cca,
)
from alphacc.dgm_cca.cca_diagnosis import (
    get_diagnose_prompt,
    get_problem_description,
    get_test_description,
    choose_entry,
    _get_reference_library,
    get_failure_log,
)

# ADDED: ACE imports — dgm/ and ace/ both have bare `llm` and `utils` modules.
# Temporarily swap conflicting sys.modules entries so ACE's internal bare imports
# (from llm import ..., from utils import ...) resolve to ace/ not dgm/.
_ace_root = os.path.join(os.path.dirname(__file__), '..', '..', 'ace')
_saved_modules = {}
for _mod_name in list(sys.modules):
    if _mod_name == 'llm' or _mod_name == 'utils' or _mod_name.startswith('utils.'):
        _saved_modules[_mod_name] = sys.modules.pop(_mod_name)
sys.path.insert(0, _ace_root)
try:
    from ace.core.reflector import Reflector
    from ace.core.curator import Curator
    from playbook_utils import update_bullet_counts, get_playbook_stats
    import importlib.util as _imputil
    _ace_utils_spec = _imputil.spec_from_file_location(
        "ace_utils", os.path.join(_ace_root, 'utils.py'),
    )
    _ace_utils_mod = _imputil.module_from_spec(_ace_utils_spec)
    _ace_utils_spec.loader.exec_module(_ace_utils_mod)
    ace_initialize_clients = _ace_utils_mod.initialize_clients
finally:
    sys.path.remove(_ace_root)
    # Restore dgm's modules
    for _mod_name, _mod in _saved_modules.items():
        sys.modules[_mod_name] = _mod


# DGM: self_improve_step.py:24 — safe_log
# CHANGED: was from utils.docker_utils import safe_log
def safe_log(msg):
    print(f"[cca_step] {msg}", flush=True)


# DGM: self_improve_step.py:27-28
dataset = None
diagnose_model = 'gpt-5.4'  # Unified: gpt-5.4 beats 5.2-pro on SWE-bench (57.7 vs 55.6) at 1/8th cost

# ADDED: CCA workspace path
CCA_WORKSPACE = os.path.join(os.path.dirname(__file__), 'cca_workspace')


def _get_cca_source(workspace_dir):
    """Read the current CCA C source code."""
    c_files = [f for f in os.listdir(workspace_dir) if f.endswith('.c')]
    if not c_files:
        return ""
    c_path = os.path.join(workspace_dir, c_files[0])
    return read_file(c_path)


# ADDED: ACE playbook helpers
_INITIAL_PLAYBOOK_PATH = os.path.join(os.path.dirname(__file__), 'initial_playbook.txt')
_ACE_REFLECTOR_MODEL = 'gpt-5.4'
_ACE_CURATOR_MODEL = 'gpt-5.4'
_CLAIM_HEADING_RE = re.compile(r"^### \[(B\d+|M\d+)\]")
_SOURCE_TAGS_RE = re.compile(r"^SOURCE_TAGS:\s*(.+)$")
_ACE_BULLET_RE = re.compile(r"^\[([^\]]+)\]\s*helpful=(\d+)\s*harmful=(\d+)\s*::\s*(.*)$")
_SHARED_ACE_DIR_ENV = "ALPHACC_SHARED_ACE_DIR"


def _ace_paths(out_dir_base):
    """Resolve local and optional shared ACE file paths."""
    local_pb = os.path.join(out_dir_base, "playbook.txt")
    local_state = os.path.join(out_dir_base, "playbook_state.json")
    local_fail = os.path.join(out_dir_base, "failure_log.jsonl")

    shared_dir = os.environ.get(_SHARED_ACE_DIR_ENV, "").strip()
    shared_pb = None
    shared_state = None
    shared_fail = None
    if shared_dir:
        os.makedirs(shared_dir, exist_ok=True)
        shared_pb = os.path.join(shared_dir, "playbook.txt")
        shared_state = os.path.join(shared_dir, "playbook_state.json")
        shared_fail = os.path.join(shared_dir, "shared_failure_log.jsonl")
    return {
        "local_pb": local_pb,
        "local_state": local_state,
        "local_fail": local_fail,
        "shared_pb": shared_pb,
        "shared_state": shared_state,
        "shared_fail": shared_fail,
    }


def _normalize_playbook_source_tags(playbook: str):
    """Enforce source-tag discipline for both markdown claims and ACE bullets.

    Returns:
        (normalized_playbook, stats_dict)
    """
    lines = playbook.splitlines()
    out = []
    i = 0
    added_claim_tags = 0
    added_bullet_tags = 0

    while i < len(lines):
        line = lines[i]
        out.append(line)

        # Rule 1: Every ### [B#]/[M#] claim heading gets an immediate SOURCE_TAGS line.
        if _CLAIM_HEADING_RE.match(line.strip()):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                out.append(lines[j])
                j += 1
            if j >= len(lines) or not _SOURCE_TAGS_RE.match(lines[j].strip()):
                out.append("SOURCE_TAGS: SRC:hypothesis(auto_missing_source_tag)")
                added_claim_tags += 1
                i = j - 1
            else:
                i = j - 1

        # Rule 2: Every ACE bullet line must include source tags in content.
        m = _ACE_BULLET_RE.match(line.strip())
        if m:
            bullet_id, helpful, harmful, content = m.groups()
            if "SRC:" not in content:
                content = f"{content} [SOURCE_TAGS: SRC:hypothesis(auto_curator_unsourced)]"
                out[-1] = f"[{bullet_id}] helpful={helpful} harmful={harmful} :: {content}"
                added_bullet_tags += 1

        i += 1

    stats = {
        "added_claim_tags": added_claim_tags,
        "added_bullet_tags": added_bullet_tags,
    }
    return "\n".join(out), stats


def _load_playbook(out_dir_base):
    """Load playbook from DGM run dir, or initialize from ACE template."""
    paths = _ace_paths(out_dir_base)
    pb_path = paths["shared_pb"] if paths["shared_pb"] and os.path.exists(paths["shared_pb"]) else paths["local_pb"]
    if os.path.exists(pb_path):
        with open(pb_path) as f:
            raw = f.read()
        normalized, stats = _normalize_playbook_source_tags(raw)
        if stats["added_claim_tags"] or stats["added_bullet_tags"]:
            safe_log(
                "Playbook source-tag normalization on load: "
                f"+{stats['added_claim_tags']} claim tags, "
                f"+{stats['added_bullet_tags']} bullet tags"
            )
            with open(pb_path, 'w') as wf:
                wf.write(normalized)
            # Keep local copy fresh if shared was loaded.
            if pb_path != paths["local_pb"]:
                with open(paths["local_pb"], "w") as wf:
                    wf.write(normalized)
        return normalized
    # Copy ACE's initial template
    if os.path.exists(_INITIAL_PLAYBOOK_PATH):
        with open(_INITIAL_PLAYBOOK_PATH) as f:
            return f.read()
    return "## STRATEGIES & INSIGHTS\n\n## OTHERS\n"


def _save_playbook(out_dir_base, playbook, next_global_id):
    """Save playbook and state to DGM run dir."""
    playbook, stats = _normalize_playbook_source_tags(playbook)
    if stats["added_claim_tags"] or stats["added_bullet_tags"]:
        safe_log(
            "Playbook source-tag normalization on save: "
            f"+{stats['added_claim_tags']} claim tags, "
            f"+{stats['added_bullet_tags']} bullet tags"
        )
    paths = _ace_paths(out_dir_base)
    with open(paths["local_pb"], 'w') as f:
        f.write(playbook)
    with open(paths["local_state"], 'w') as f:
        json.dump({"next_global_id": next_global_id}, f)
    if paths["shared_pb"] and paths["shared_state"]:
        with open(paths["shared_pb"], "w") as f:
            f.write(playbook)
        with open(paths["shared_state"], "w") as f:
            json.dump({"next_global_id": next_global_id}, f)


def _load_playbook_state(out_dir_base):
    """Load next_global_id from state file."""
    paths = _ace_paths(out_dir_base)
    state_path = paths["shared_state"] if paths["shared_state"] and os.path.exists(paths["shared_state"]) else paths["local_state"]
    if os.path.exists(state_path):
        with open(state_path) as f:
            return json.load(f).get("next_global_id", 1)
    return 1


def _append_failure_log_event(out_dir_base, event):
    """Append one mutation outcome to local and optional shared failure logs."""
    paths = _ace_paths(out_dir_base)
    payload = json.dumps(event, default=str) + "\n"
    with open(paths["local_fail"], "a") as f:
        f.write(payload)
    if paths["shared_fail"]:
        with open(paths["shared_fail"], "a") as f:
            f.write(payload)


def _run_ace_reflection(
    out_dir_base, entry, parent_perf, eval_result, model_patch, log_dir=None,
):
    """Run ACE Reflector + Curator after evaluation.

    Wires ACE directly — no reimplementation of experience accumulation.
    """
    playbook = _load_playbook(out_dir_base)
    next_global_id = _load_playbook_state(out_dir_base)

    try:
        _, reflector_client, curator_client = ace_initialize_clients("openai")
    except Exception as e:
        safe_log(f"ACE client init failed (no OPENAI_API_KEY?): {e}")
        return

    reflector = Reflector(reflector_client, "openai", _ACE_REFLECTOR_MODEL)
    curator = Curator(curator_client, "openai", _ACE_CURATOR_MODEL)

    # Format environment feedback: parent vs child comparison
    parent_score = parent_perf.get("selection_score", parent_perf.get("accuracy_score", 0)) if parent_perf else 0
    child_score = eval_result.get("selection_score", eval_result.get("accuracy_score", 0)) if eval_result else 0
    delta = child_score - parent_score
    direction = "improved" if delta > 0 else "regressed" if delta < 0 else "unchanged"
    env_feedback = (
        f"Parent fitness: {parent_score:.3f}, Child fitness: {child_score:.3f}, "
        f"Delta: {delta:+.3f} ({direction})"
    )

    # Per-dimension objective comparison
    parent_obj = parent_perf.get("objective_vector", {}) if parent_perf else {}
    child_obj = eval_result.get("objective_vector", {}) if eval_result else {}
    if parent_obj and child_obj:
        env_feedback += "\nPer-dimension:"
        for dim in ["util", "delay_quality", "loss_efficiency", "robustness"]:
            p, c = parent_obj.get(dim, 0), child_obj.get(dim, 0)
            env_feedback += f"\n  {dim}: {p:.3f} → {c:.3f} ({c-p:+.3f})"
        bottleneck = eval_result.get("bottleneck_dim", "")
        if bottleneck:
            env_feedback += f"\nBottleneck: {bottleneck} (gap={eval_result.get('bottleneck_gap', 0):.3f})"

    # Per-trace comparison
    child_traces = eval_result.get("per_trace", {}) if eval_result else {}
    parent_traces = parent_perf.get("per_trace", {}) if parent_perf else {}
    trace_lines = []
    for tid, ct in sorted(child_traces.items()):
        pt = parent_traces.get(tid, {})
        pf = pt.get("fitness", 0)
        cf = ct.get("fitness", 0)
        trace_lines.append(f"  {tid}: {pf:.3f} → {cf:.3f} ({cf - pf:+.3f})")
    if trace_lines:
        env_feedback += "\nPer-trace:\n" + "\n".join(trace_lines)

    # Step 1: Reflector
    safe_log("Running ACE Reflector")
    try:
        reflection, bullet_tags, _ = reflector.reflect(
            question=f"CCA evolution task: {entry}",
            reasoning_trace=model_patch[:5000],
            predicted_answer=f"fitness={child_score:.3f}",
            ground_truth=None,
            environment_feedback=env_feedback,
            bullets_used=playbook,
            use_ground_truth=False,
            call_id=f"cca_reflect",
            log_dir=log_dir,
        )
        safe_log(f"Reflector tagged {len(bullet_tags)} bullets")
        if bullet_tags:
            playbook = update_bullet_counts(playbook, bullet_tags)
    except Exception as e:
        safe_log(f"ACE Reflector failed: {e}")
        reflection = f"Reflector failed: {e}"

    # Step 2: Curator
    safe_log("Running ACE Curator")
    try:
        stats = get_playbook_stats(playbook)
        updated_playbook, next_global_id, ops, _ = curator.curate(
            current_playbook=playbook,
            recent_reflection=reflection,
            question_context=(
                f"LEO satellite CCA evolution — entry={entry}. "
                "When adding/updating bullets, include provenance tags in content: "
                "[SOURCE_TAGS: SRC:paper(...); SRC:code(...)] or SRC:hypothesis(...) if unsourced."
            ),
            current_step=1,
            total_samples=100,
            token_budget=80000,
            playbook_stats=stats,
            use_ground_truth=False,
            call_id=f"cca_curate",
            log_dir=log_dir,
            next_global_id=next_global_id,
        )
        safe_log(f"Curator applied {len(ops)} operations")
        playbook, stats = _normalize_playbook_source_tags(updated_playbook)
        if stats["added_claim_tags"] or stats["added_bullet_tags"]:
            safe_log(
                "Playbook source-tag normalization after curate: "
                f"+{stats['added_claim_tags']} claim tags, "
                f"+{stats['added_bullet_tags']} bullet tags"
            )
    except Exception as e:
        safe_log(f"ACE Curator failed: {e}")

    _save_playbook(out_dir_base, playbook, next_global_id)
    safe_log(f"Playbook saved ({get_playbook_stats(playbook)['total_bullets']} bullets)")


# DGM: self_improve_step.py:30-67
# CHANGED: SWE-bench diagnosis → CCA performance diagnosis
# DGM signature: diagnose_problem(entry, commit, root_dir, out_dir, patch_files=[], max_attempts=3, polyglot=False)
# CCA signature: diagnose_problem(entry, performance, workspace_dir, out_dir_base="", archive=[], max_attempts=3)
def diagnose_problem(entry, performance, workspace_dir, out_dir_base="", archive=None,
                     max_attempts=3, bandit_suggestion=None, stagnation_meta=None,
                     llm_temperature=1.0, scaffold_mode=False, max_patch_lines=0):
    c_code = _get_cca_source(workspace_dir)
    # ADDED: Load playbook and reference library for diagnosis prompt
    playbook = _load_playbook(out_dir_base) if out_dir_base else ""
    reference_library = _get_reference_library(out_dir_base, archive or []) if out_dir_base else ""
    # CHANGED: get_diagnose_prompt_swe → get_diagnose_prompt (CCA)
    diagnose_sys_message, diagnose_prompt = get_diagnose_prompt(
        entry, performance, c_code,
        playbook=playbook,
        reference_library=reference_library,
        bandit_suggestion=bandit_suggestion,
        stagnation_meta=stagnation_meta,
        failure_log=get_failure_log(out_dir_base, limit=10) if out_dir_base else "",
        scaffold_mode=scaffold_mode,
        max_patch_lines=max_patch_lines,
    )
    client = create_client(diagnose_model)
    # DGM: self_improve_step.py:42-54 — LLM call, VERBATIM structure
    # CHANGED: pass llm_temperature for island diversity
    try:
        response, msg_history = get_response_from_llm(
            msg=diagnose_prompt,
            client=client[0],
            model=client[1],
            system_message=diagnose_sys_message,
            print_debug=False,
            msg_history=None,
            temperature=llm_temperature,
        )
        safe_log(f"Message history: {msg_history}")
        response_json = extract_json_between_markers(response)
        assert response_json, "empty response json"
        # CHANGED: get_problem_description_prompt → get_problem_description (CCA)
        problem_statement = get_problem_description(response_json)
    except Exception as e:
        # Exception most probably due to not having json in the response
        safe_log(f"Error while diagnosing the problem: {e}")
        if max_attempts > 0:
            return diagnose_problem(
                entry, performance, workspace_dir,
                out_dir_base=out_dir_base, archive=archive,
                max_attempts=max_attempts-1,
                bandit_suggestion=bandit_suggestion,
                stagnation_meta=stagnation_meta,
                llm_temperature=llm_temperature,
                scaffold_mode=scaffold_mode,
                max_patch_lines=max_patch_lines,
            )
        else:
            return None
    return problem_statement


# REMOVED: diagnose_improvement (DGM:69-118) — not needed for CCA initially


# DGM: self_improve_step.py:120-123, VERBATIM
def save_metadata(metadata, output_dir):
    metadata_file = os.path.join(output_dir, "metadata.json")
    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, indent=4, default=str)


# REMOVED: run_harness_swe (DGM:125-178) — replaced inline with CCA harness
# REMOVED: run_harness_polyglot (DGM:180-221)


# DGM: self_improve_step.py:223-420
# CHANGED: Docker → git clone workspace, SWE-bench → CCA harness
# DGM signature: self_improve(parent_commit, output_dir, force_rebuild, num_evals,
#                             post_improve_diagnose, entry, test_task_list,
#                             test_more_threshold, test_task_list_more,
#                             full_eval_threshold, run_baseline, polyglot)
# CCA signature: self_improve_cca(parent_commit, output_dir, entry, trace_pairs, duration)
def self_improve_cca(
    parent_commit='initial',  # 'initial' if starting from original dgm, else the run_id
    output_dir='output_dgm_cca/',
    entry=None,
    # CHANGED: SWE-bench params → CCA params
    trace_pairs=None,
    duration=30,
    # ADDED: archive for reference library
    archive=None,
    # ADDED: selection mechanism params
    bandit_suggestion=None,
    stagnation_meta=None,
    enable_racing=True,
    racing_traces=3,
    racing_margin=0.03,
    # ADDED: LLM temperature for diversity across island VMs
    llm_temperature=1.0,
    # ADDED: exploit-first patch-size guardrail
    max_patch_lines=0,
):

    # REMOVED: DGM lines 240-247 (global dataset, SWE-bench/polyglot loading)

    # DGM: self_improve_step.py:249-258 — variable init, VERBATIM
    metadata = {}
    root_dir = os.path.abspath('./')  # root_dir should be project root
    run_id = datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    out_dir_base = output_dir  # out_dir_base should be output_dgm_cca/{dgm_run_id}/
    output_dir = os.path.join(root_dir, f"{output_dir}/{run_id}/")
    os.makedirs(output_dir, exist_ok=True)
    metadata['run_id'] = run_id
    metadata['parent_commit'] = parent_commit
    # REMOVED: test_task_list_big = load_json_file("./swe_bench/subsets/big.json")

    # REMOVED: DGM line 261 (setup_logger — docker_utils not available)
    safe_log(f"Starting self-improvement {run_id}, parent={parent_commit}, entry={entry}")

    # DGM: self_improve_step.py:263-274 — Docker container creation
    # CHANGED: git clone workspace instead of Docker container
    seed_workspace = os.path.abspath(CCA_WORKSPACE)
    workspace = os.path.join(output_dir, "workspace")
    safe_log(f"Cloning workspace to {workspace}")
    subprocess.run(
        ["git", "clone", seed_workspace, workspace],
        capture_output=True, text=True, check=True,
    )
    # Clean any build artifacts from seed
    subprocess.run(["make", "-C", workspace, "clean"],
                   capture_output=True, timeout=30)

    # Reset to HEAD of the cloned workspace (the seed CCA)
    reset_to_commit(workspace, 'HEAD')

    # REMOVED: DGM lines 276-291 (polyglot Docker setup)

    # DGM: self_improve_step.py:292-300 — find and apply parent patches
    # CHANGED: Prefer parent workspace source reconstruction to avoid patch-chain drift.
    # Some cross-pollinated lineages contain patch hunks that no longer apply cleanly,
    # which can silently create malformed hybrid states. If parent workspace exists,
    # copy its C sources directly; otherwise fall back to patch chain replay.
    patch_files = get_model_patch_paths(root_dir, os.path.join(output_dir, '../'), parent_commit)
    parent_ws = os.path.join(root_dir, out_dir_base, parent_commit, "workspace")
    reconstructed_from_workspace = False
    if parent_commit != "initial" and os.path.isdir(parent_ws):
        try:
            copied = 0
            for name in os.listdir(parent_ws):
                if name.endswith(".c"):
                    src = os.path.join(parent_ws, name)
                    dst = os.path.join(workspace, name)
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                        copied += 1
            if copied > 0:
                reconstructed_from_workspace = True
                safe_log(f"Reconstructed parent from workspace sources ({copied} .c files)")
            else:
                safe_log("Parent workspace had no .c sources; falling back to patch chain")
        except Exception as e:
            safe_log(f"Parent workspace reconstruction failed: {e}; falling back to patch chain")

    if not reconstructed_from_workspace:
        # REMOVED: if run_baseline not in ['no_selfimprove']: (not applicable)
        for patch_file in patch_files:
            safe_log(f"Applying parent patch: {patch_file}")
            patch_content = read_file(patch_file)
            apply_patch(workspace, patch_content)

    # DGM: self_improve_step.py:302-309 — git add + commit, extract hash
    # CHANGED: container.exec_run → subprocess, commit_output.split() → git rev-parse
    subprocess.run(["git", "-C", workspace, "add", "--all"],
                   capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", workspace, "-c", "user.name='user'", "-c", "user.email='you@example.com'",
         "commit", "--allow-empty", "-m", "a nonsense commit message"],
        capture_output=True, text=True, check=True,
    )
    # DGM original: commit_hash = commit_output.split()[1].strip("[]")
    # CHANGED: use git rev-parse HEAD (more robust than parsing commit output)
    result = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    commit_hash = result.stdout.strip()

    # REMOVED: DGM lines 311-313 (pip install in container)

    # DGM: self_improve_step.py:315-333 — diagnosis
    # CHANGED: SWE-bench diagnosis → CCA performance diagnosis
    parent_perf = {}
    try:
        parent_meta_path = os.path.join(root_dir, out_dir_base, parent_commit, "metadata.json")
        parent_meta = load_json_file(parent_meta_path)
        parent_perf = parent_meta.get('overall_performance', {})
    except Exception:
        safe_log("No parent performance data (initial run)")

    if entry is None:
        entry = choose_entry(parent_perf)

    if entry:
        safe_log(f"Task to improve: {entry}")
        problem_statement = diagnose_problem(
            entry, parent_perf, workspace,
            out_dir_base=os.path.join(root_dir, out_dir_base),
            archive=archive or [],
            bandit_suggestion=bandit_suggestion,
            stagnation_meta=stagnation_meta,
            llm_temperature=llm_temperature,
            scaffold_mode=(max_patch_lines and max_patch_lines > 0),
            max_patch_lines=max_patch_lines,
        )
        safe_log(f"problem_statement: {problem_statement}")
    else:
        safe_log("No entry provided. Exiting.")
        save_metadata(metadata, output_dir)
        return metadata

    metadata['entry'] = entry
    metadata['problem_statement'] = problem_statement
    if bandit_suggestion:
        metadata['bandit_suggestion'] = bandit_suggestion
    # If problem statement is not found, exit
    if not problem_statement:
        safe_log("Failed to diagnose the problem statement. Exiting.")
        # REMOVED: cleanup_container(container) — no Docker container
        save_metadata(metadata, output_dir)
        return metadata

    # DGM: self_improve_step.py:335-359 — run coding agent
    # CHANGED: Docker exec → subprocess
    safe_log("Running self-improvement")
    chat_history_file = os.path.join(output_dir, "self_evo.md")
    # CHANGED: get_test_description(swerepo=False) → get_test_description() from CCA diagnosis
    test_description = get_test_description()
    # REMOVED: env_vars dict (Docker environment) — env inherited from parent process
    dgm_root = os.path.join(os.path.dirname(__file__), '..', '..', 'dgm')
    coding_agent_path = os.path.join(dgm_root, 'coding_agent.py')
    cmd = [
        "timeout", "1800",  # 30min timeout (same as DGM line 348)
        sys.executable, coding_agent_path,
        "--problem_statement", problem_statement,
        "--git_dir", workspace,  # CHANGED: "/dgm/" → workspace
        "--chat_history_file", chat_history_file,
        "--base_commit", commit_hash,
        "--outdir", output_dir,  # CHANGED: "/dgm/" → output_dir
        "--test_description", test_description,
        "--self_improve",
    ]
    # CHANGED: container.exec_run → subprocess.run
    env = os.environ.copy()
    dgm_abs = os.path.abspath(dgm_root)
    env['PYTHONPATH'] = dgm_abs + ':' + env.get('PYTHONPATH', '')
    # ADDED: pass LLM temperature to coding agent via env var
    env['LLM_TEMPERATURE'] = str(llm_temperature)
    safe_log(f"Running coding agent (timeout=1800s)")
    try:
        result = subprocess.run(
            cmd, timeout=1860,
            capture_output=True, text=True,
            env=env, cwd=dgm_abs,
        )
        safe_log(f"Coding agent exit code: {result.returncode}")
        if result.stdout:
            safe_log(f"stdout: {result.stdout[-500:]}")
        if result.stderr:
            safe_log(f"stderr: {result.stderr[-500:]}")
    except subprocess.TimeoutExpired:
        safe_log("Coding agent timed out")
    except Exception as e:
        safe_log(f"Coding agent error: {e}")

    # DGM: self_improve_step.py:361-366 — extract patch
    # CHANGED: copy_from_container → diff_versus_commit (no Docker)
    model_patch = diff_versus_commit(workspace, commit_hash)
    model_patch_file = os.path.join(output_dir, "model_patch.diff")
    with open(model_patch_file, 'w') as f:
        f.write(model_patch)

    # DGM: self_improve_step.py:367-379 — validate patch, VERBATIM
    try:
        # Check if patch file exists and is not empty
        if not os.path.exists(model_patch_file):
            raise Exception("Model patch file is empty or does not exist")
        with open(model_patch_file, 'r') as f:
            patch_content = f.read()
            if not patch_content.strip():
                raise Exception("Model patch file is empty")
    except Exception as e:
        safe_log(f"Failed to read model patch file: {str(e)}")
        save_metadata(metadata, output_dir)
        return metadata

    # DGM: self_improve_step.py:381
    patch_files.append(model_patch_file)

    # Exploit-first guardrail: reject oversized edits before expensive eval.
    if max_patch_lines and max_patch_lines > 0:
        changed_lines = 0
        for ln in model_patch.splitlines():
            if ln.startswith("+++") or ln.startswith("---") or ln.startswith("@@"):
                continue
            if ln.startswith("+") or ln.startswith("-"):
                changed_lines += 1
        metadata['patch_changed_lines'] = changed_lines
        if changed_lines > max_patch_lines:
            safe_log(f"Rejecting oversized patch: changed_lines={changed_lines} > max_patch_lines={max_patch_lines}")
            metadata['patch_rejected_reason'] = 'patch_too_large'
            metadata['overall_performance'] = {
                'accuracy_score': 0,
                'total_resolved_instances': 0,
                'total_submitted_instances': 0,
                'total_resolved_ids': [],
                'total_unresolved_ids': [],
                'total_emptypatch_ids': ['patch_too_large'],
            }
            save_metadata(metadata, output_dir)
            return metadata

    # REMOVED: DGM line 383-384 (cleanup_container — no Docker)

    # DGM: self_improve_step.py:386-399 — evaluate
    # CHANGED: SWE-bench harness → CCA build/load/eval/unload
    model_patch_exists = os.path.exists(model_patch_file)
    metadata['model_patch_exists'] = model_patch_exists
    model_patch_notempty = os.path.getsize(model_patch_file) > 0
    metadata['model_patch_notempty'] = model_patch_notempty
    if model_patch_exists and model_patch_notempty:
        build_err = build_and_load_cca(workspace, module_name="tcp_evolved")
        if build_err:
            safe_log(f"Build/load failed: {build_err}")
            metadata['build_error'] = build_err
            metadata['overall_performance'] = {
                'accuracy_score': 0,
                'total_resolved_instances': 0,
                'total_submitted_instances': 0,
                'total_resolved_ids': [],
                'total_unresolved_ids': [],
                'total_emptypatch_ids': ['build_fail'],
            }
        else:
            # ADDED: try/finally ensures module is always unloaded (no Docker cleanup)
            try:
                safe_log("Evaluating through LeoReplayer")
                try:
                    eval_result = evaluate_cca(
                        cca_name="evolved",
                        trace_pairs=trace_pairs or [],
                        duration=duration,
                        work_dir=os.path.join(output_dir, "traces"),
                        parent_score=parent_perf.get("selection_score", parent_perf.get("accuracy_score")) if parent_perf else None,
                        enable_racing=enable_racing,
                        racing_traces=racing_traces,
                        racing_margin=racing_margin,
                    )
                    metadata['overall_performance'] = eval_result
                except Exception as e:
                    safe_log(f"Error while evaluating the self-improvement: {e}")
                    metadata['overall_performance'] = {
                        'accuracy_score': 0,
                        'total_resolved_instances': 0,
                        'total_submitted_instances': 0,
                        'total_resolved_ids': [],
                        'total_unresolved_ids': [],
                        'total_emptypatch_ids': ['eval_fail'],
                    }
            finally:
                safe_log("Unloading module")
                unload_err = unload_cca("tcp_evolved")
                if unload_err:
                    safe_log(f"Unload warning: {unload_err}")
                subprocess.run(["pkill", "-f", "iperf3"], capture_output=True)

    # REMOVED: DGM lines 401-416 (post-improvement diagnosis)

    # ADDED: ACE Reflector + Curator (experience accumulation)
    if metadata.get('overall_performance'):
        # Shared/local failure memory for future diagnosis prompts.
        try:
            child_perf = metadata.get("overall_performance", {})
            parent_score = parent_perf.get("accuracy_score", 0.0) if parent_perf else 0.0
            child_score = child_perf.get("accuracy_score", 0.0)
            failure_event = {
                "run_id": run_id,
                "parent_commit": parent_commit,
                "entry": metadata.get("entry"),
                "bandit_suggestion": metadata.get("bandit_suggestion"),
                "child_score": child_score,
                "parent_score": parent_score,
                "delta": child_score - parent_score,
                "early_stopped": child_perf.get("early_stopped", False),
                "bottleneck_dim": child_perf.get("bottleneck_dim"),
                "reason": (metadata.get("problem_statement", "") or "")[:400],
            }
            _append_failure_log_event(os.path.join(root_dir, out_dir_base), failure_event)
        except Exception as e:
            safe_log(f"Failure log append failed (non-fatal): {e}")

        try:
            _run_ace_reflection(
                out_dir_base=os.path.join(root_dir, out_dir_base),
                entry=entry,
                parent_perf=parent_perf,
                eval_result=metadata['overall_performance'],
                model_patch=model_patch,
                log_dir=os.path.join(output_dir, "ace_logs"),
            )
        except Exception as e:
            safe_log(f"ACE reflection failed (non-fatal): {e}")

    # DGM: self_improve_step.py:418-420, VERBATIM
    save_metadata(metadata, output_dir)
    safe_log(f"Done: accuracy={metadata.get('overall_performance', {}).get('accuracy_score', 'N/A')}")
    return metadata

# REMOVED: DGM lines 422-447 (main function — called from outer.py instead)

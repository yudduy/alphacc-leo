"""UCB1 mutation-type bandit for adaptive operator selection.

Classifies mutations post-hoc from git diffs and maintains per-category
reward statistics. Provides a suggested priority category for the next
diagnosis prompt via UCB1 exploration-exploitation.

Note on epsilon-greedy: When epsilon > 0, a fraction of selections bypass
UCB1 and choose uniformly at random. This breaks the UCB1 regret bound
(O(sqrt(K ln N))) since random pulls waste budget on known-bad arms. The
trade-off is inter-island diversity: different epsilon values across islands
produce different exploration patterns at the cost of per-island optimality.

Note on reward attribution: The bandit suggests a category, but the LLM may
produce changes in a different category. We track whether the suggestion was
followed (suggestion_followed field in metadata) but always credit the
actually-observed category. This means the bandit learns which categories
produce improvements, not whether its suggestions are influential.

References:
    Auer, Cesa-Bianchi, Fischer (2002). "Finite-time Analysis of the
    Multiarmed Bandit Problem." Machine Learning 47(2-3):235-256.
    Li et al. (2014). "Adaptive Operator Selection with Bandits for
    MOEA/D." Applied Soft Computing.
"""

import json
import math
import os
import random
import re
from typing import Dict, List, Optional


MUTATION_CATEGORIES = [
    "loss_discrimination",
    "kalman_tuning",
    "pacing_logic",
    "cwnd_policy",
    "reconfig_handling",
    "state_machine",
    "bw_estimation",
    "other",
]

# Keyword patterns for post-hoc classification (applied to added lines in unified diff)
_CATEGORY_PATTERNS: Dict[str, List[str]] = {
    "loss_discrimination": [
        r"loss", r"ssthresh", r"retransmit", r"reorder", r"ca_state",
        r"TCP_CA_Loss", r"TCP_CA_Recovery", r"cong_loss", r"loss_is_cong",
    ],
    "kalman_tuning": [
        r"kalman", r"var_[RQ]", r"kalman_gain", r"p_post", r"bw_hat_post",
        r"rtt_hat_post", r"p_post_bw", r"p_post_rtt",
    ],
    "pacing_logic": [
        r"pacing_gain", r"probe.*drain", r"pacing_rate", r"sk_pacing",
        r"cycle_idx", r"cycle_len",
    ],
    "cwnd_policy": [
        r"snd_cwnd", r"cwnd_clamp", r"cwnd_gain", r"cwnd_target",
        r"p_lb_cwnd", r"cwnd\s*[=<>+\-]",
    ],
    "reconfig_handling": [
        r"reconfig", r"handoff", r"offset", r"reconfiguration",
        r"local_reconfiguration", r"rtt_jump", r"period_ticks",
    ],
    "state_machine": [
        r"mode\s*=", r"STARTUP", r"DRAIN", r"CRUISE", r"PROBE_RTT",
        r"phase\s*=", r"state.*transition", r"set_mode",
    ],
    "bw_estimation": [
        r"bw_hat", r"delivery_rate", r"minmax.*bw", r"bw_lo", r"bw_hi",
        r"max_filter", r"bw_latest", r"latest_bw",
    ],
}


def classify_mutation(diff_text: str) -> str:
    """Classify a git diff into a mutation category via keyword matching.

    Only inspects added/modified lines (+ prefix) in unified diff format.
    Returns the category with the most keyword hits, or 'other' if no matches.
    """
    if not diff_text:
        return "other"

    scores: Dict[str, int] = {cat: 0 for cat in MUTATION_CATEGORIES}
    added_lines = [
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    text = "\n".join(added_lines)

    for cat, patterns in _CATEGORY_PATTERNS.items():
        for pat in patterns:
            scores[cat] += len(re.findall(pat, text, re.IGNORECASE))

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


class MutationBandit:
    """UCB1 bandit over mutation categories.

    Persists state to JSON. Exploration constant c=sqrt(2) by default.
    """

    def __init__(self, state_path: str, exploration_c: float = 1.41,
                 epsilon: float = 0.0):
        self.state_path = state_path
        self.c = exploration_c
        self.epsilon = epsilon
        self.state = self._load()

    def _load(self) -> Dict:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "total_pulls": 0,
            "arms": {
                cat: {"reward_sum": 0.0, "n_pulls": 0, "max_reward": 0.0}
                for cat in MUTATION_CATEGORIES
            },
        }

    def save(self):
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)

    def select(self, exclude: Optional[List[str]] = None) -> str:
        """Select next category using UCB1 with optional epsilon-greedy.

        Args:
            exclude: categories to exclude (set UCB = -inf). Used during
                stagnation to force exploration of untried categories.

        When epsilon > 0, with probability epsilon a random eligible category
        is returned (ignoring UCB1 scores). Otherwise normal UCB1 is used.
        """
        exclude = set(exclude or [])
        eligible = [cat for cat in MUTATION_CATEGORIES if cat not in exclude]
        if not eligible:
            return "other"

        # Epsilon-greedy: random selection with probability epsilon
        if self.epsilon > 0 and random.random() < self.epsilon:
            return random.choice(eligible)

        total = max(self.state["total_pulls"], 1)

        # Explore unpulled arms first in random order (excluding excluded)
        unpulled = [
            cat for cat in MUTATION_CATEGORIES
            if cat not in exclude
            and self.state["arms"].get(cat, {"n_pulls": 0})["n_pulls"] == 0
        ]
        if unpulled:
            return random.choice(unpulled)

        # UCB1: argmax(mean_reward + c * sqrt(ln(N) / n_i))
        best_cat = None
        best_ucb = -float("inf")
        for cat in MUTATION_CATEGORIES:
            if cat in exclude:
                continue
            arm = self.state["arms"].get(cat, {"reward_sum": 0.0, "n_pulls": 1})
            n = max(arm["n_pulls"], 1)
            mean_r = arm["reward_sum"] / n
            ucb = mean_r + self.c * math.sqrt(math.log(total) / n)
            if ucb > best_ucb:
                best_ucb = ucb
                best_cat = cat

        return best_cat or "other"

    def update(self, category: str, reward: float):
        """Record a pull result. Reward = max(0, child_score - parent_score)."""
        if category not in self.state["arms"]:
            self.state["arms"][category] = {
                "reward_sum": 0.0, "n_pulls": 0, "max_reward": 0.0,
            }
        arm = self.state["arms"][category]
        arm["reward_sum"] += reward
        arm["n_pulls"] += 1
        arm["max_reward"] = max(arm["max_reward"], reward)
        self.state["total_pulls"] += 1
        self.save()

    def get_stats(self) -> Dict[str, Dict]:
        """Return per-arm statistics for logging/analysis."""
        stats = {}
        total = max(self.state["total_pulls"], 1)
        for cat in MUTATION_CATEGORIES:
            arm = self.state["arms"].get(
                cat, {"reward_sum": 0.0, "n_pulls": 0, "max_reward": 0.0}
            )
            n = arm["n_pulls"]
            mean_r = arm["reward_sum"] / n if n > 0 else 0.0
            ucb = (
                mean_r + self.c * math.sqrt(math.log(total) / max(n, 1))
                if n > 0
                else float("inf")
            )
            stats[cat] = {
                "mean_reward": round(mean_r, 5),
                "n_pulls": n,
                "max_reward": round(arm["max_reward"], 5),
                "ucb": round(ucb, 5) if ucb != float("inf") else "inf",
            }
        return stats

#!/usr/bin/env python3
"""Generate publication-quality figures for the workshop paper (v2).

Includes: error bars from multi-seed, BBR baseline, RTT sweep.
"""
import sys, json, csv
import numpy as np
sys.path.insert(0, '/Users/duy/Documents/build/cs244c')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter

OUT = '/Users/duy/Documents/build/cs244c/output_remy_evolve'

# ── Load data ──────────────────────────────────────────────────────

# Multi-seed evaluation (5 seeds)
with open(f'{OUT}/multiseed_eval.json') as f:
    mseed = json.load(f)

# BBR evaluation (5 seeds)
with open(f'{OUT}/bbr_eval.json') as f:
    bbr_eval = json.load(f)

# RTT sweep
with open(f'{OUT}/rtt_sweep.json') as f:
    rtt_sweep = json.load(f)

# Remy CSV (single tree)
remy_data = []
with open('/Users/duy/Documents/build/cs244c/external_repos/remy/combined-plot/data/data-cca.179.csv') as f:
    for row in csv.reader(f):
        remy_data.append([float(x) for x in row])

link_ppts = [r[0] for r in remy_data]
link_mbps = [lp * 10 for lp in link_ppts]

def find_key(d, target):
    """Find closest key in dict to target float."""
    best_k, best_d = None, float('inf')
    for k in d:
        dist = abs(float(k) - target)
        if dist < best_d:
            best_k, best_d = k, dist
    return best_k

# ── Figure 1: Generalization with error bars ────────────────────────

fig, ax = plt.subplots(figsize=(10, 6))

policies = {
    'Remy Tree (1x)': {'color': '#1f77b4', 'marker': 'o', 'ls': '-', 'lw': 2.5, 'ms': 8},
    'PPO Neural Net': {'color': '#e74c3c', 'marker': 's', 'ls': '-', 'lw': 2, 'ms': 7},
    'Copa': {'color': '#2ecc71', 'marker': 'D', 'ls': '-', 'lw': 2, 'ms': 7},
    'AIMD': {'color': '#95a5a6', 'marker': '^', 'ls': '--', 'lw': 1.5, 'ms': 7},
    'BBR (simplified)': {'color': '#f39c12', 'marker': 'v', 'ls': ':', 'lw': 1.5, 'ms': 7},
    'LLM-Evolved': {'color': '#9b59b6', 'marker': '*', 'ls': '-', 'lw': 2.5, 'ms': 10},
}

for name, style in policies.items():
    means, stds = [], []
    for lp in link_ppts:
        if name == 'BBR (simplified)':
            src = bbr_eval
        else:
            key_map = {'Remy Tree (1x)': 'Remy', 'PPO Neural Net': 'PPO',
                       'Copa': 'Copa', 'AIMD': 'AIMD', 'LLM-Evolved': 'LLM'}
            src = mseed[key_map[name]]
        k = find_key(src, lp)
        means.append(src[k]['mean'])
        stds.append(src[k].get('std', 0))

    means = np.array(means)
    stds = np.array(stds)
    ax.plot(link_mbps, means, color=style['color'], marker=style['marker'],
            ls=style['ls'], lw=style['lw'], ms=style['ms'], label=name, zorder=3)
    if any(s > 0 for s in stds):
        ax.fill_between(link_mbps, means - stds, means + stds,
                        color=style['color'], alpha=0.15, zorder=1)

# Training condition marker
ax.axvline(x=9.5, color='gray', ls=':', alpha=0.5, lw=1)
ax.text(9.5, 0.3, 'Training\ncondition', ha='center', va='bottom',
        fontsize=9, color='gray', style='italic')

ax.set_xscale('log')
ax.xaxis.set_major_formatter(ScalarFormatter())
ax.set_xticks([2.4, 5, 10, 20, 50, 95])
ax.get_xaxis().set_major_formatter(ScalarFormatter())
ax.set_xlabel('Link Speed (Mbps)', fontsize=12)
ax.set_ylabel('Normalized Score (higher is better)', fontsize=12)
ax.set_title('CCA Generalization Across Link Rates\n(shaded = ±1 std across 5 seeds)', fontsize=13)
ax.legend(loc='upper left', fontsize=9, framealpha=0.9)
ax.set_ylim(-6, 1)
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig(f'{OUT}/fig1_generalization_v2.png', dpi=150, bbox_inches='tight')
plt.savefig(f'{OUT}/fig1_generalization_v2.pdf', bbox_inches='tight')
plt.close()
print("Fig 1 saved")

# ── Figure 2: RTT Sweep ──────────────────────────────────────────────

fig, axes = plt.subplots(1, 3, figsize=(14, 5), sharey=True)
rtts = [50, 150, 300]
link_labels = ['2.4 Mbps\n(low)', '9.5 Mbps\n(train)', '59.8 Mbps\n(high)']
link_keys = ['0.237', '0.946', '5.983']
policy_names = ['Copa', 'AIMD', 'BBR', 'LLM_evolved']
display_names = ['Copa', 'AIMD', 'BBR', 'LLM']
colors = ['#2ecc71', '#95a5a6', '#f39c12', '#9b59b6']

for i, rtt in enumerate(rtts):
    ax = axes[i]
    x = np.arange(len(link_keys))
    width = 0.2
    for j, (pname, dname, color) in enumerate(zip(policy_names, display_names, colors)):
        vals = [rtt_sweep[pname][f'{rtt}_{lk}']['normalized'] for lk in link_keys]
        ax.bar(x + j * width - 1.5 * width, vals, width, label=dname if i == 0 else '',
               color=color, alpha=0.8, edgecolor='white', lw=0.5)
    ax.set_xticks(x)
    ax.set_xticklabels(link_labels, fontsize=9)
    ax.set_title(f'RTT = {rtt} ms', fontsize=12, fontweight='bold')
    ax.grid(True, axis='y', alpha=0.3)
    ax.set_ylim(-6, 0.5)
    if i == 0:
        ax.set_ylabel('Normalized Score', fontsize=11)

axes[0].legend(fontsize=9, loc='lower left')
fig.suptitle('Generalization Across RTT Values', fontsize=13, y=1.02)
plt.tight_layout()
plt.savefig(f'{OUT}/fig2_rtt_sweep.png', dpi=150, bbox_inches='tight')
plt.savefig(f'{OUT}/fig2_rtt_sweep.pdf', bbox_inches='tight')
plt.close()
print("Fig 2 saved")

# ── Figure 3: Throughput-Delay Scatter (at training condition, consistent) ────

fig, ax = plt.subplots(figsize=(8, 6))

# Use multi-seed data at 0.946 ppt (Remy's eval point) for consistency
# We need per-sender throughput and delay, but multi-seed only has normalized scores.
# Use seed=42 evaluation for the scatter
from alphacc.remy_eval import run_remy_sim, aimd_policy, copa_policy, bbr_policy

ns = {}
with open(f'{OUT}/best_policy.py') as f:
    exec(f.read(), ns)
llm = ns['evolved_policy']

scatter_data = {}
for name, policy in [('Copa', copa_policy), ('AIMD', aimd_policy), ('BBR', bbr_policy)]:
    if hasattr(policy, '_state'): delattr(policy, '_state')
    r = run_remy_sim([policy], 0.946, 150.0, 2, duration_ms=30000, seed=42)
    scatter_data[name] = {'tput_cap': r['throughput_ppt'] / 0.946,
                          'delay_ratio': r['avg_delay_ms'] / 150.0,
                          'norm': r['normalized_score']}

if hasattr(llm, '_state'): del llm._state
r = run_remy_sim([llm], 0.946, 150.0, 2, duration_ms=30000, seed=42)
scatter_data['LLM'] = {'tput_cap': r['throughput_ppt'] / 0.946,
                        'delay_ratio': r['avg_delay_ms'] / 150.0,
                        'norm': r['normalized_score']}

# Remy from CSV
rd = remy_data[3]  # 0.946 ppt
scatter_data['Remy'] = {'tput_cap': (rd[2] + rd[4]) / 2,  # avg of s1, s2 throughput / capacity
                        'delay_ratio': (rd[3] + rd[5]) / 2,
                        'norm': rd[1]}

# PPO — all 6 brains
ppo_csvs = []
import glob
for fp in sorted(glob.glob('/Users/duy/Documents/build/cs244c/external_repos/remy/combined-plot/data/data-brain.*.csv')):
    with open(fp) as f:
        rows = [list(map(float, r)) for r in csv.reader(f)]
    ppo_csvs.append(rows[3])  # index 3 = 0.946 ppt

style_map = {
    'Remy': {'color': '#1f77b4', 'marker': 'o', 'ms': 120, 'zorder': 5},
    'Copa': {'color': '#2ecc71', 'marker': 'D', 'ms': 100, 'zorder': 4},
    'AIMD': {'color': '#95a5a6', 'marker': '^', 'ms': 100, 'zorder': 4},
    'BBR': {'color': '#f39c12', 'marker': 'v', 'ms': 100, 'zorder': 4},
    'LLM': {'color': '#9b59b6', 'marker': '*', 'ms': 150, 'zorder': 5},
}

# Plot PPO brains as a cluster
for prow in ppo_csvs:
    tc = (prow[2] + prow[4]) / 2
    dr = (prow[3] + prow[5]) / 2
    ax.scatter(tc, dr, color='#e74c3c', marker='s', s=60, alpha=0.5, zorder=3)
ax.scatter([], [], color='#e74c3c', marker='s', s=60, label='PPO (6 brains)')

for name, d in scatter_data.items():
    s = style_map[name]
    ax.scatter(d['tput_cap'], d['delay_ratio'], color=s['color'], marker=s['marker'],
               s=s['ms'], label=f"{name} ({d['norm']:.2f})", zorder=s['zorder'],
               edgecolors='black', linewidths=0.5)

ax.set_xlabel('Throughput / Capacity', fontsize=12)
ax.set_ylabel('Delay / Min RTT', fontsize=12)
ax.set_title('Throughput-Delay Tradeoff at 9.5 Mbps\n(all evaluated at 0.946 ppt for consistency)', fontsize=12)
ax.legend(fontsize=9, loc='upper left')
ax.annotate('Ideal\n(high tput,\nlow delay)', xy=(0.95, 1.0), fontsize=9,
            color='gray', style='italic', ha='right')
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig(f'{OUT}/fig3_scatter_v2.png', dpi=150, bbox_inches='tight')
plt.savefig(f'{OUT}/fig3_scatter_v2.pdf', bbox_inches='tight')
plt.close()
print("Fig 3 saved")

# ── Figure 4: Evolution Trajectory (keep existing, just clean up) ────

with open(f'{OUT}/history.json') as f:
    history = json.load(f)

fig, ax = plt.subplots(figsize=(10, 5))

# Group by generation
from collections import defaultdict
gen_scores = defaultdict(list)
for h in history:
    gen_scores[h['gen']].append(h['fitness'])

gens = sorted(gen_scores.keys())
best_per_gen = [max(gen_scores[g]) for g in gens]
running_best = []
rb = -float('inf')
for b in best_per_gen:
    rb = max(rb, b)
    running_best.append(rb)

# Plot all candidates as gray dots
for g in gens:
    for s in gen_scores[g]:
        ax.scatter(g, s, color='gray', alpha=0.3, s=20, zorder=1)

ax.plot(gens, best_per_gen, color='#9b59b6', marker='o', lw=2, ms=6,
        label='Best per generation', zorder=3)
ax.plot(gens, running_best, color='#2ecc71', ls='--', lw=2.5,
        label='Running best', zorder=2)

ax.set_xlabel('Generation', fontsize=12)
ax.set_ylabel('Normalized Score', fontsize=12)
ax.set_title('LLM Evolution Trajectory (15 gens × 5 candidates)', fontsize=13)
ax.legend(fontsize=10)
ax.grid(True, alpha=0.3)
plt.tight_layout()
plt.savefig(f'{OUT}/fig4_evolution_v2.png', dpi=150, bbox_inches='tight')
plt.savefig(f'{OUT}/fig4_evolution_v2.pdf', bbox_inches='tight')
plt.close()
print("Fig 4 saved")

# ── Print summary table for paper (consistent eval at Remy link rates) ────

print("\n=== Table 4.2: Generalization (mean ± std across 5 seeds) ===")
print(f"{'Link (Mbps)':>12} | {'Remy':>10} | {'PPO':>12} | {'Copa':>12} | {'AIMD':>12} | {'BBR':>12} | {'LLM':>12}")
print("-" * 90)
for lp, mbps in zip(link_ppts, link_mbps):
    rk = find_key(mseed['Remy'], lp)
    pk = find_key(mseed['PPO'], lp)
    ck = find_key(mseed['Copa'], lp)
    ak = find_key(mseed['AIMD'], lp)
    bk = find_key(bbr_eval, lp)
    lk = find_key(mseed['LLM'], lp)
    remy_s = f"{mseed['Remy'][rk]['mean']:.2f}"
    ppo_s = f"{mseed['PPO'][pk]['mean']:.2f}±{mseed['PPO'][pk]['std']:.2f}"
    copa_s = f"{mseed['Copa'][ck]['mean']:.2f}±{mseed['Copa'][ck]['std']:.2f}"
    aimd_s = f"{mseed['AIMD'][ak]['mean']:.2f}±{mseed['AIMD'][ak]['std']:.2f}"
    bbr_s = f"{bbr_eval[bk]['mean']:.2f}±{bbr_eval[bk]['std']:.2f}"
    llm_s = f"{mseed['LLM'][lk]['mean']:.2f}±{mseed['LLM'][lk]['std']:.2f}"
    print(f"{mbps:12.1f} | {remy_s:>10} | {ppo_s:>12} | {copa_s:>12} | {aimd_s:>12} | {bbr_s:>12} | {llm_s:>12}")

print("\nDone! All figures saved to output_remy_evolve/")

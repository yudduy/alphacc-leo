"""Generate production-quality generalization comparison plot."""
import sys
import os
sys.path.insert(0, '/Users/duy/Documents/build/cs244c')
os.chdir('/Users/duy/Documents/build/cs244c')

import csv
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from pathlib import Path

LINK_PPT_TO_MBPS = 10

def load_csv(filepath):
    data = []
    with open(filepath) as f:
        for row in csv.reader(f):
            data.append([float(x) for x in row])
    return data

# Load data
remy_dir = Path('external_repos/remy/combined-plot/data')
remy = load_csv(remy_dir / 'data-cca.179.csv')
ppo_best = load_csv(remy_dir / 'data-brain.837.csv')

# Load all PPO runs for range
ppo_all = []
for f in sorted(remy_dir.glob('data-brain.*.csv')):
    ppo_all.append(load_csv(f))

# Evaluate our CCAs at Remy's link rates
from alphacc.remy_eval import run_remy_sim, aimd_policy, copa_policy

remy_link_ppts = [row[0] for row in remy]
remy_mbps = [x * LINK_PPT_TO_MBPS for x in remy_link_ppts]

# Load LLM policy
ns = {}
with open('output_remy_evolve/best_policy.py') as f:
    exec(f.read(), ns)
llm_policy = ns['evolved_policy']

# Evaluate at each link rate
data = {'Copa': [], 'AIMD': [], 'LLM-Evolved': []}
policies = {'Copa': copa_policy, 'AIMD': aimd_policy, 'LLM-Evolved': llm_policy}

for link_ppt in remy_link_ppts:
    for name, policy in policies.items():
        r = run_remy_sim([policy], link_ppt, 150.0, 2, duration_ms=30000, seed=42)
        data[name].append(r['normalized_score'])

# ============ PLOT 1: Generalization comparison ============
fig, ax = plt.subplots(1, 1, figsize=(10, 6))

# PPO range (shaded)
ppo_norms = [[row[1] for row in run] for run in ppo_all]
ppo_lo = [min(run[i] for run in ppo_norms) for i in range(len(remy_mbps))]
ppo_hi = [max(run[i] for run in ppo_norms) for i in range(len(remy_mbps))]
ax.fill_between(remy_mbps, ppo_lo, ppo_hi, color='#FF6B6B', alpha=0.15)

# Remy Tree
remy_norms = [row[1] for row in remy]
ax.plot(remy_mbps, remy_norms, 'o-', color='#2196F3', linewidth=2.5, markersize=7,
        label='Remy Tree (1x-trained)', zorder=5)

# PPO Best
ppo_norms_best = [row[1] for row in ppo_best]
ax.plot(remy_mbps, ppo_norms_best, 's-', color='#F44336', linewidth=2, markersize=6,
        label='PPO Neural Net (best of 6)', zorder=4)

# Copa
ax.plot(remy_mbps, data['Copa'], 'D-', color='#4CAF50', linewidth=2, markersize=7,
        label='Copa (hand-designed)', zorder=3)

# AIMD
ax.plot(remy_mbps, data['AIMD'], '^-', color='#9E9E9E', linewidth=1.5, markersize=6,
        label='AIMD (hand-designed)', zorder=2, alpha=0.7)

# LLM-Evolved
ax.plot(remy_mbps, data['LLM-Evolved'], '*-', color='#9C27B0', linewidth=2.5, markersize=10,
        label='LLM-Evolved (15 gens)', zorder=6)

# Training condition
ax.axvline(x=10, color='gray', linestyle='--', alpha=0.4, linewidth=1)
ax.annotate('Training\ncondition', xy=(10, -0.1), fontsize=9, ha='center',
            color='gray', va='bottom')

ax.set_xscale('log')
ax.set_xlabel('Link Speed (Mbps)', fontsize=13)
ax.set_ylabel('Normalized Score\n(higher is better)', fontsize=12)
ax.set_title('CCA Generalization Across Link Rates', fontsize=14, fontweight='bold')
ax.legend(loc='lower left', fontsize=10, framealpha=0.9)
ax.grid(True, alpha=0.2)
ax.set_ylim(-5.5, 0.5)

plt.tight_layout()
for ext in ['png', 'pdf']:
    plt.savefig(f'output_remy_evolve/fig1_generalization.{ext}', dpi=200, bbox_inches='tight')
print('Saved fig1_generalization')
plt.close()

# ============ PLOT 2: Throughput-Delay scatter ============
fig, ax = plt.subplots(1, 1, figsize=(8, 6))

# At training condition (link_ppt ≈ 0.946)
idx = 3  # 0.946 ppt
remy_tput = (remy[idx][2] + remy[idx][4]) / 2
remy_delay = (remy[idx][3] + remy[idx][5]) / 2
ax.scatter([remy_tput], [remy_delay], s=200, c='#2196F3', marker='o',
           zorder=5, label='Remy Tree', edgecolors='white', linewidth=1.5)

# PPO variants
for i, ppo_run in enumerate(ppo_all):
    tput = (ppo_run[idx][2] + ppo_run[idx][4]) / 2
    delay = (ppo_run[idx][3] + ppo_run[idx][5]) / 2
    label = 'PPO Neural Net' if i == 0 else None
    ax.scatter([tput], [delay], s=80, c='#F44336', marker='s',
               zorder=4, alpha=0.6, label=label, edgecolors='white', linewidth=0.5)

# Our CCAs
for name, policy in policies.items():
    r = run_remy_sim([policy], 0.946, 150.0, 2, duration_ms=30000, seed=42)
    tput = r['throughput_ppt'] / 0.946  # normalize to capacity
    delay = r['avg_delay_ms'] / 150.0  # normalize to min RTT
    colors = {'Copa': '#4CAF50', 'AIMD': '#9E9E9E', 'LLM-Evolved': '#9C27B0'}
    markers = {'Copa': 'D', 'AIMD': '^', 'LLM-Evolved': '*'}
    sizes = {'Copa': 150, 'AIMD': 120, 'LLM-Evolved': 250}
    ax.scatter([tput], [delay], s=sizes[name], c=colors[name], marker=markers[name],
               zorder=3, label=name, edgecolors='white', linewidth=1)

ax.set_xlabel('Throughput / Capacity', fontsize=13)
ax.set_ylabel('Delay / Min RTT', fontsize=12)
ax.set_title('Throughput-Delay Tradeoff\n(link=9.5 Mbps, RTT=150ms, 2 senders)', fontsize=14, fontweight='bold')
ax.legend(loc='upper left', fontsize=10, framealpha=0.9)
ax.grid(True, alpha=0.2)

# Annotate ideal region
ax.annotate('Ideal\n(high tput,\nlow delay)', xy=(0.95, 1.05),
            fontsize=9, ha='center', color='gray', style='italic')

plt.tight_layout()
for ext in ['png', 'pdf']:
    plt.savefig(f'output_remy_evolve/fig2_throughput_delay.{ext}', dpi=200, bbox_inches='tight')
print('Saved fig2_throughput_delay')
plt.close()

# ============ PLOT 3: Evolution trajectory ============
import json
with open('output_remy_evolve/history.json') as f:
    history = json.load(f)

fig, ax = plt.subplots(1, 1, figsize=(8, 4))
gens = [h['gen'] for h in history]
scores = [h['fitness'] for h in history]

# Plot all points
ax.scatter(gens, scores, s=20, alpha=0.5, c='gray', zorder=2)

# Best per generation
gen_bests = {}
for h in history:
    g = h['gen']
    if g not in gen_bests or h['fitness'] > gen_bests[g]:
        gen_bests[g] = h['fitness']

best_gens = sorted(gen_bests.keys())
best_scores = [gen_bests[g] for g in best_gens]
ax.plot(best_gens, best_scores, 'o-', color='#9C27B0', linewidth=2, markersize=6,
        label='Best per generation', zorder=3)

# Overall best line
overall_best = -float('inf')
running_best = []
for g in best_gens:
    overall_best = max(overall_best, gen_bests[g])
    running_best.append(overall_best)
ax.plot(best_gens, running_best, '--', color='#4CAF50', linewidth=2,
        label='Running best', zorder=4)

ax.set_xlabel('Generation', fontsize=12)
ax.set_ylabel('Normalized Score', fontsize=12)
ax.set_title('LLM Evolution Trajectory', fontsize=13, fontweight='bold')
ax.legend(fontsize=10)
ax.grid(True, alpha=0.2)

plt.tight_layout()
for ext in ['png', 'pdf']:
    plt.savefig(f'output_remy_evolve/fig3_evolution.{ext}', dpi=200, bbox_inches='tight')
print('Saved fig3_evolution')
plt.close()

print('\nAll figures generated!')

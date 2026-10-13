"""Run all strategies at 5/10/15/20 households. From the repo root:
    python eval/run_eval.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # so `common`/`scheduler` import

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from common.config import N_MINUTES, build_price_array
from scheduler.strategies import schedule, STRATEGIES
from eval.build_households import load_jobs, build_households

SIZES = [5, 10, 15, 20]
SEEDS = range(10)
OUT = Path("eval/results")


def evaluate(jobs, starts):
    price = build_price_array()
    load = np.zeros(N_MINUTES)
    cost, missed = 0.0, 0
    assert (starts >= jobs["release"].to_numpy()).all(), "job started before it was requested"
    for s, dur, kw, dl in zip(starts, jobs["duration_min"], jobs["kw"], jobs["deadline"]):
        load[s:s + dur] += kw
        cost += kw / 60 * price[s:s + dur].sum()
        missed += (s + dur > dl)
    metrics = {
        "n_jobs": len(jobs),
        "energy_kwh": load.sum() / 60,       # must be identical across strategies
        "cost": cost,
        "peak_kw": load.max(),
        "miss_rate": missed / len(jobs),
    }
    return metrics, load


def plot_summary(df):
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    panels = [("cost", "Total cost (GBP)"), ("peak_kw", "Peak demand (kW)"),
              ("miss_rate", "Deadline-miss rate")]
    for ax, (col, label) in zip(axes, panels):
        for strat, g in df.groupby("strategy"):
            m = g.groupby("n_households")[col].mean()
            ax.plot(m.index, m.values, marker="o", label=strat)
        ax.set_xlabel("Households")
        ax.set_ylabel(label)
        ax.set_xticks(SIZES)
        ax.grid(alpha=0.3)
    axes[0].legend()
    plt.tight_layout()
    plt.savefig(OUT / "summary.png", dpi=150)
    plt.close()


def plot_load_curves(raw, n=10, seed=0):
    jobs = build_households(raw, n, seed)
    fig, axes = plt.subplots(len(STRATEGIES), 1, figsize=(14, 9), sharex=True, sharey=True)
    for ax, strat in zip(axes, STRATEGIES):
        _, load = evaluate(jobs, schedule(jobs, strat))
        ax.plot(np.arange(N_MINUTES) / 60, load, lw=0.7)
        ax.set_ylabel("kW")
        ax.set_title(f"{strat} ({n} households)", loc="left", fontsize=9)
    axes[-1].set_xlabel("Hours from start of week")
    plt.tight_layout()
    plt.savefig(OUT / "load_curves.png", dpi=150)
    plt.close()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    raw = load_jobs()
    rows = []
    for n in SIZES:
        for seed in SEEDS:
            jobs = build_households(raw, n, seed)
            for strat in STRATEGIES:
                metrics, _ = evaluate(jobs, schedule(jobs, strat))
                rows.append({"n_households": n, "seed": seed, "strategy": strat, **metrics})
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "results.csv", index=False)

    summary = (df.groupby(["n_households", "strategy"])[["n_jobs", "energy_kwh", "cost", "peak_kw", "miss_rate"]]
                 .mean().round(3))
    print(summary.to_string())
    summary.to_csv(OUT / "summary.csv")

    plot_summary(df)
    plot_load_curves(raw)
    print(f"\nSaved results and plots to {OUT}/")


if __name__ == "__main__":
    main()
"""Mean and standard deviation across seeds, from eval/results/results.csv.
Run from the repo root after eval/run_eval.py:   python eval/stats.py
Writes eval/results/summary_std.csv and prints the 20-household table plus win counts.
"""
from pathlib import Path

import pandas as pd

SRC = Path("eval/results/results.csv")
OUT = Path("eval/results/summary_std.csv")
METRICS = ["cost", "peak_kw", "miss_rate"]
ORDER = ["no_scheduling", "fixed_offpeak_safe", "cost_only", "dynamic", "fixed_offpeak"]


def main():
    df = pd.read_csv(SRC)
    g = df.groupby(["n_households", "strategy"])[METRICS]
    mean, std = g.mean(), g.std()
    n_seeds = df.groupby(["n_households", "strategy"])["seed"].nunique().iloc[0]

    table = mean.add_suffix("_mean").join(std.add_suffix("_std")).reset_index()
    table.to_csv(OUT, index=False)
    print(f"Seeds per cell: {n_seeds}.  Full table saved to {OUT}\n")

    # The table for the paper: largest neighborhood size
    n = df["n_households"].max()
    print(f"Mean +/- standard deviation at {n} households")
    print(f"{'strategy':<20}{'cost':>18}{'peak (kW)':>18}{'miss rate':>18}")
    for s in ORDER:
        if (n, s) not in mean.index:
            continue
        m, d = mean.loc[(n, s)], std.loc[(n, s)]
        print(f"{s:<20}"
              f"{m['cost']:>10.2f} +/- {d['cost']:<5.2f}"
              f"{m['peak_kw']:>10.2f} +/- {d['peak_kw']:<5.2f}"
              f"{100 * m['miss_rate']:>9.1f}% +/- {100 * d['miss_rate']:<4.1f}")

    # Seed-by-seed comparison: is dynamic better in every seed?
    print("\nSeeds in which dynamic beats no_scheduling (lower is better):")
    for size, grp in df.groupby("n_households"):
        wide = grp.pivot(index="seed", columns="strategy", values=["cost", "peak_kw"])
        k = len(wide)
        cost_wins = int((wide[("cost", "dynamic")] < wide[("cost", "no_scheduling")]).sum())
        peak_wins = int((wide[("peak_kw", "dynamic")] < wide[("peak_kw", "no_scheduling")]).sum())
        print(f"  {size:>2} households: cost {cost_wins} of {k}, peak {peak_wins} of {k}")


if __name__ == "__main__":
    main()
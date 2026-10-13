"""Turn the real job table into N simulated households (reproducible via seed)."""
import numpy as np
import pandas as pd
from common.config import HORIZON_DAYS, SLACK_MIN_H, SLACK_MAX_H


def load_jobs(path="data/processed/jobs.csv"):
    return pd.read_csv(path, parse_dates=["start", "end"])


def build_households(jobs_raw, n_households, seed):
    """Household k = a source house + a random 7-day window, shifted to start at minute 0.
    Each household has its own RNG, so household k is identical whether you ask
    for 5 or 20 households. Scaling up just adds more households."""
    houses = sorted(jobs_raw["house"].unique())
    rows = []
    for k in range(n_households):
        rng = np.random.default_rng([seed, k])
        src = houses[k % len(houses)]
        hj = jobs_raw[jobs_raw["house"] == src]

        first, last = hj["start"].min().normalize(), hj["start"].max().normalize()
        spare_days = max((last - first).days - HORIZON_DAYS, 1)
        d0 = first + pd.Timedelta(days=int(rng.integers(0, spare_days)))
        window = hj[(hj["start"] >= d0) & (hj["start"] < d0 + pd.Timedelta(days=HORIZON_DAYS))]

        for r in window.itertuples():
            release = int((r.start - d0) / pd.Timedelta(minutes=1))
            dur = int(r.duration_min)
            slack = int(rng.uniform(SLACK_MIN_H, SLACK_MAX_H) * 60)
            rows.append({
                "household": f"S{k + 1:02d}",
                "source_house": src,
                "appliance": r.appliance,
                "release": release,
                "duration_min": dur,
                "kw": r.avg_kw,                      # rectangular profile (simplification)
                "deadline": release + dur + slack,
            })
    return pd.DataFrame(rows)
import numpy as np
import pandas as pd
from scheduler.strategies import OnlineScheduler, schedule, STRATEGIES


def random_jobs(n=60, seed=1):
    rng = np.random.default_rng(seed)
    release = np.sort(rng.integers(0, 7 * 1440, n))
    dur = rng.integers(30, 150, n)
    return pd.DataFrame({
        "release": release, "duration_min": dur,
        "kw": rng.uniform(0.5, 2.5, n),
        "deadline": release + dur + rng.integers(60, 720, n),
    })


def test_online_matches_batch_for_every_strategy():
    jobs = random_jobs()
    for name in STRATEGIES:
        batch = list(schedule(jobs, name))
        online = OnlineScheduler(name)
        starts = [online.place(int(r.release), int(r.duration_min), float(r.kw), int(r.deadline))[0]
                  for r in jobs.itertuples()]
        assert batch == starts, name
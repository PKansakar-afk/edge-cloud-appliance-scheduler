import numpy as np
import pandas as pd
from common.config import build_price_array
from scheduler.strategies import schedule, STRATEGIES


def make_jobs():
    return pd.DataFrame({
        "release": [600, 610, 1500],
        "duration_min": [60, 90, 45],
        "kw": [2.0, 1.0, 2.0],
        "deadline": [600 + 60 + 300, 610 + 90 + 600, 1500 + 45 + 120],
    })


def cost(jobs, starts):
    price = build_price_array()
    return sum(kw / 60 * price[s:s + d].sum()
               for s, d, kw in zip(starts, jobs.duration_min, jobs.kw))


def test_no_scheduling_starts_at_release():
    jobs = make_jobs()
    assert (schedule(jobs, "no_scheduling") == jobs.release.to_numpy()).all()


def test_dynamic_respects_release_and_deadline():
    jobs = make_jobs()
    s = schedule(jobs, "dynamic")
    assert (s >= jobs.release.to_numpy()).all()
    assert (s + jobs.duration_min.to_numpy() <= jobs.deadline.to_numpy()).all()


def test_dynamic_not_more_expensive_than_baseline():
    jobs = make_jobs()
    assert cost(jobs, schedule(jobs, "dynamic")) <= cost(jobs, schedule(jobs, "no_scheduling")) + 1e-9


def test_all_strategies_run():
    jobs = make_jobs()
    for name in STRATEGIES:
        assert len(schedule(jobs, name)) == len(jobs)
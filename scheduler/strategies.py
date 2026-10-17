"""Scheduling strategies.

Batch:  schedule(jobs, strategy) -> start minutes   (used by eval/run_eval.py)
Online: OnlineScheduler(strategy).place(...)         (used by the MQTT service)
Both call the same place_job() for dynamic / cost_only, so results match.
"""
import numpy as np
import pandas as pd
from common.config import (N_MINUTES, OFFPEAK_START_H, SLOT_MIN, LAMBDA,
                           build_price_array, is_offpeak)


def no_scheduling(jobs):
    """Baseline 1: every job starts when the user requested it."""
    return jobs["release"].to_numpy().copy()


def fixed_offpeak(jobs, respect_deadline=False):
    """Baseline 2: an off-peak timer. Deferrable jobs wait for 22:00."""
    starts = []
    for r, dur, dl in zip(jobs["release"], jobs["duration_min"], jobs["deadline"]):
        if is_offpeak(r):
            s = r
        else:
            s = (r // 1440) * 1440 + OFFPEAK_START_H * 60
        if respect_deadline and s + dur > dl:
            s = r
        starts.append(s)
    return np.array(starts)


def place_job(load, price_cum, r, dur, kw, dl, lam=LAMBDA, slot=SLOT_MIN):
    """Best start for one job: energy cost + lam * (kW x existing load over its window)."""
    dl = min(dl, N_MINUTES)
    cand = np.arange(r, max(dl - dur, r) + 1, slot)      # always includes r
    ends = cand + dur
    load_cum = np.concatenate([[0], np.cumsum(load)])
    energy_cost = kw / 60 * (price_cum[ends] - price_cum[cand])
    overlap = kw / 60 * (load_cum[ends] - load_cum[cand])
    return int(cand[np.argmin(energy_cost + lam * overlap)])


def dynamic(jobs, lam=LAMBDA, slot=SLOT_MIN):
    """Cost- and peak-aware greedy scheduler (jobs handled in request order)."""
    price_cum = np.concatenate([[0], np.cumsum(build_price_array())])
    load = np.zeros(N_MINUTES)
    starts = np.zeros(len(jobs), dtype=int)

    release = jobs["release"].to_numpy()
    dur_all = jobs["duration_min"].to_numpy()
    kw_all = jobs["kw"].to_numpy()
    dl_all = jobs["deadline"].to_numpy()

    for i in np.argsort(release, kind="stable"):
        r, dur, kw, dl = int(release[i]), int(dur_all[i]), kw_all[i], int(dl_all[i])
        best = place_job(load, price_cum, r, dur, kw, dl, lam, slot)
        starts[i] = best
        load[best:best + dur] += kw
    return starts


STRATEGIES = {
    "no_scheduling": no_scheduling,
    "fixed_offpeak": fixed_offpeak,
    "fixed_offpeak_safe": lambda j: fixed_offpeak(j, respect_deadline=True),
    "cost_only": lambda j: dynamic(j, lam=0.0),
    "dynamic": dynamic,
}


def schedule(jobs, strategy):
    return STRATEGIES[strategy](jobs)


class OnlineScheduler:
    """Stateful scheduler: place one job at a time and remember the committed load."""

    def __init__(self, strategy="dynamic"):
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy}")
        self.strategy = strategy
        self.price = build_price_array()
        self.price_cum = np.concatenate([[0], np.cumsum(self.price)])
        self.load = np.zeros(N_MINUTES)

    def place(self, release, duration, kw, deadline):
        """All times in minutes since EPOCH. Returns (start_minute, estimated_cost)."""
        if release < 0 or release + duration > N_MINUTES:
            raise ValueError("job is outside the scheduler horizon")
        if self.strategy in ("dynamic", "cost_only"):
            lam = LAMBDA if self.strategy == "dynamic" else 0.0
            start = place_job(self.load, self.price_cum, release, duration, kw, deadline, lam)
        else:
            row = pd.DataFrame({"release": [release], "duration_min": [duration],
                                "kw": [kw], "deadline": [deadline]})
            start = int(STRATEGIES[self.strategy](row)[0])
        if start + duration > N_MINUTES:
            raise ValueError("scheduled start falls outside the horizon")
        self.load[start:start + duration] += kw
        cost = kw / 60 * self.price[start:start + duration].sum()
        return start, float(cost)
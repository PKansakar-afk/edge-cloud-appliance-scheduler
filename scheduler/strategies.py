"""Scheduling strategies. Pure functions: schedule(jobs, strategy) -> start minutes.

`jobs` is a DataFrame with columns: release, duration_min, kw, deadline
(all times are integer minutes from the start of the horizon).
"""
import numpy as np
from common.config import (N_MINUTES, OFFPEAK_START_H, SLOT_MIN, LAMBDA,
                           build_price_array, is_offpeak)


def no_scheduling(jobs):
    """Baseline 1: every job starts when the user requested it."""
    return jobs["release"].to_numpy().copy()


def fixed_offpeak(jobs, respect_deadline=False):
    """Baseline 2: a dumb off-peak timer. Deferrable jobs wait for 22:00.
    A timer doesn't know about deadlines, so it can miss them. Set
    respect_deadline=True to make it fall back to 'start now' instead."""
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


def dynamic(jobs, lam=LAMBDA, slot=SLOT_MIN):
    """Cost- and peak-aware greedy scheduler.

    Jobs are handled in request order (online). For each job, every feasible
    start in [release, deadline - duration] is scored as:
        energy cost + lam * (kW x existing aggregate load over the job's window)
    The second term is the increase in sum-of-squares of the load curve, so it
    pushes jobs away from already-busy periods. lam=0 gives a cost-only scheduler.
    """
    price = build_price_array()
    price_cum = np.concatenate([[0], np.cumsum(price)])
    load = np.zeros(N_MINUTES)                       # kW already committed
    starts = np.zeros(len(jobs), dtype=int)

    release = jobs["release"].to_numpy()
    dur_all = jobs["duration_min"].to_numpy()
    kw_all = jobs["kw"].to_numpy()
    dl_all = jobs["deadline"].to_numpy()

    for i in np.argsort(release, kind="stable"):
        r, dur, kw, dl = int(release[i]), int(dur_all[i]), kw_all[i], int(dl_all[i])
        cand = np.arange(r, max(dl - dur, r) + 1, slot)   # always includes r
        ends = cand + dur
        load_cum = np.concatenate([[0], np.cumsum(load)])

        energy_cost = kw / 60 * (price_cum[ends] - price_cum[cand])
        overlap = kw / 60 * (load_cum[ends] - load_cum[cand])
        best = int(cand[np.argmin(energy_cost + lam * overlap)])

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
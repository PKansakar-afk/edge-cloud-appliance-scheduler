"""Shared assumptions. Every value here must be stated as an assumption in the paper."""
import numpy as np

MIN_PER_DAY = 1440
HORIZON_DAYS = 7
BUFFER_DAYS = 2                      # room for jobs that finish after the last day
N_MINUTES = (HORIZON_DAYS + BUFFER_DAYS) * MIN_PER_DAY

# Time-of-use tariff, currency per kWh (assumed)
PRICE_OFFPEAK = 0.10                 # 22:00-06:00
PRICE_SHOULDER = 0.20                # everything else
PRICE_PEAK = 0.35                    # 17:00-21:00
OFFPEAK_START_H, OFFPEAK_END_H = 22, 6
PEAK_START_H, PEAK_END_H = 17, 21

# Deadline = release + duration + slack, slack ~ Uniform(MIN, MAX) hours
SLACK_MIN_H, SLACK_MAX_H = 1, 12

# Dynamic scheduler
SLOT_MIN = 15                        # candidate start times every 15 min
LAMBDA = 0.02                        # weight on the load-overlap (peak) term


def build_price_array(n=N_MINUTES):
    """Price per kWh for every minute of the horizon (minute 0 = midnight)."""
    hour = (np.arange(n) // 60) % 24
    price = np.full(n, PRICE_SHOULDER)
    price[(hour >= PEAK_START_H) & (hour < PEAK_END_H)] = PRICE_PEAK
    price[(hour >= OFFPEAK_START_H) | (hour < OFFPEAK_END_H)] = PRICE_OFFPEAK
    return price


def is_offpeak(minute):
    h = (minute // 60) % 24
    return h >= OFFPEAK_START_H or h < OFFPEAK_END_H


# ---- experiment clock ----
import os
from datetime import datetime, timedelta

# Minute 0 of the scheduler's timeline = local midnight on this date (a Monday).
# Tariff hours are computed relative to it, so keep it at LOCAL midnight (+07:00 for Thailand).
EPOCH = datetime.fromisoformat(os.getenv("EPOCH", "2026-10-19T00:00:00+07:00"))


def to_minute(dt):
    """Timezone-aware datetime -> integer minutes since EPOCH (rounded down)."""
    return int((dt - EPOCH).total_seconds() // 60)


def from_minute(m):
    return EPOCH + timedelta(minutes=int(m))
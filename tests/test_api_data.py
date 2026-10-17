from datetime import timedelta, timezone

import pandas as pd

from api.data import load_curves, run_metrics


def make_df():
    df = pd.DataFrame({
        "run_id": "r1", "strategy": "dynamic",
        "house": ["S01", "S02", "S01"], "device": "washing_machine",
        "delay_min": [0, 60, 0], "duration_min": [60, 60, 30],
        "kw": [2.0, 1.0, 1.5], "cost": [0.5, 0.2, 0.3], "deadline_met": [1, 1, 0],
    })
    df["release"] = pd.to_datetime(["2026-10-19T08:00:00+07:00", "2026-10-19T08:00:00+07:00",
                                    "2026-10-19T20:00:00+07:00"], utc=True)
    df["start"] = df["release"] + pd.to_timedelta(df["delay_min"], unit="min")
    df["end"] = df["start"] + pd.to_timedelta(df["duration_min"], unit="min")
    return df


def test_run_metrics():
    m = run_metrics(make_df())
    assert m["jobs"] == 3 and m["households"] == 2
    assert m["peak_kw"] == 2.0            # jobs 1 and 2 run back to back, never together
    assert m["cost"] == 1.0
    assert abs(m["miss_rate"] - 1 / 3) < 1e-3
    assert m["avg_delay_min"] == 20.0
    assert m["energy_kwh"] == 3.75


def test_load_curves():
    c = load_curves(make_df(), step=15, tz=timezone(timedelta(hours=7)))
    assert c["start"].startswith("2026-10-19T00:00:00")
    assert max(c["aggregate_kw"]) == 2.0
    assert set(c["households_kw"]) == {"S01", "S02"}
"""Read side: query InfluxDB and turn scheduling decisions into metrics and load curves.
The pure functions (run_metrics, load_curves) need no database, so they can be unit tested."""
import re

import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient

WIDE_RANGE = "start: 2020-01-01T00:00:00Z, stop: 2100-01-01T00:00:00Z"   # data is stamped with simulated time
SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
NEEDED = ["_time", "run_id", "strategy", "house", "device",
          "delay_min", "duration_min", "kw", "cost", "deadline_met"]


def _safe(value):
    if not SAFE.match(value):
        raise ValueError(f"unsafe identifier: {value!r}")
    return value


class Reader:
    def __init__(self, url, token, org, bucket):
        self.client = InfluxDBClient(url=url, token=token, org=org, timeout=15_000)
        self.q = self.client.query_api()
        self.bucket = _safe(bucket)

    def ping(self):
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def decisions(self, run_id=None, root="sim"):
      """Scheduling decisions as one DataFrame (one row per job).
      Only simulated jobs by default, so live demo requests never pollute experiment runs."""
      run = f'|> filter(fn: (r) => r.run_id == "{_safe(run_id)}")' if run_id else ""
      flux = f'''
      from(bucket: "{self.bucket}")
        |> range({WIDE_RANGE})
        |> filter(fn: (r) => r._measurement == "schedule_decision" and r.root == "{_safe(root)}")
        {run}
        |> filter(fn: (r) => contains(value: r._field, set: ["delay_min", "duration_min", "kw", "cost", "deadline_met"]))
        |> toFloat()
        |> group()
        |> pivot(rowKey: ["_time", "run_id", "strategy", "house", "device"], columnKey: ["_field"], valueColumn: "_value")
      '''
      rows = [rec.values for table in self.q.query(flux) for rec in table.records]
      return prepare(pd.DataFrame(rows))

    def live_power(self, house, device, minutes):
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: -{int(minutes)}m)
  |> filter(fn: (r) => r._measurement == "power" and r._field == "watts" and r.root == "home"
                       and r.house == "{_safe(house)}" and r.device == "{_safe(device)}")
  |> keep(columns: ["_time", "_value"])
'''
        pts = [{"t": rec.get_time().isoformat(), "watts": float(rec.get_value())}
               for table in self.q.query(flux) for rec in table.records]
        return sorted(pts, key=lambda p: p["t"])


def prepare(df):
    """Add release/start/end timestamps. `_time` is the release time (the scheduler stamps it that way)."""
    cols = NEEDED + ["release", "start", "end"]
    if df is None or df.empty or "_time" not in df:
        return pd.DataFrame(columns=cols)
    df = df[[c for c in NEEDED if c in df.columns]].copy()
    df["release"] = pd.to_datetime(df["_time"], utc=True)
    df["start"] = df["release"] + pd.to_timedelta(df["delay_min"].astype(int), unit="min")
    df["end"] = df["start"] + pd.to_timedelta(df["duration_min"].astype(int), unit="min")
    return df.drop(columns="_time").sort_values("release").reset_index(drop=True)


def _offsets(df, t0):
    off = ((df["start"] - t0) / pd.Timedelta(minutes=1)).round().astype(int).to_numpy()
    dur = df["duration_min"].astype(int).to_numpy()
    return off, dur


def span(df, t0):
    off, dur = _offsets(df, t0)
    return int((off + dur).max()) + 1


def load_array(df, t0, n):
    """1-minute aggregate load (kW) built from the jobs' start times, durations and power."""
    off, dur = _offsets(df, t0)
    kw = df["kw"].astype(float).to_numpy()
    load = np.zeros(n)
    for o, d, k in zip(off, dur, kw):
        load[o:o + d] += k
    return load


def run_metrics(df):
    """Cost, peak, deadline-miss rate, and delay for one run (df = that run's decisions)."""
    t0 = df["release"].min()
    load = load_array(df, t0, span(df, t0))
    return {
        "run_id": str(df["run_id"].iloc[0]),
        "strategy": str(df["strategy"].iloc[0]),
        "households": int(df["house"].nunique()),
        "jobs": int(len(df)),
        "energy_kwh": round(float((df["kw"].astype(float) * df["duration_min"].astype(float)).sum() / 60), 3),
        "cost": round(float(df["cost"].astype(float).sum()), 3),
        "peak_kw": round(float(load.max()), 3),
        "miss_rate": round(float(1 - df["deadline_met"].astype(int).mean()), 4),
        "avg_delay_min": round(float(df["delay_min"].astype(float).mean()), 1),
    }


def load_curves(df, step=15, tz=None):
    """Aggregate and per-household load, bucketed to `step` minutes (peak within each bucket).
    The time axis starts at local midnight of the first release, so runs line up."""
    first = df["release"].min()
    t0 = (first.tz_convert(tz) if tz else first).normalize()
    n = span(df, t0)
    padded_len = -(-n // step) * step

    def bucket(arr):
        padded = np.zeros(padded_len)
        padded[:len(arr)] = arr
        return padded.reshape(-1, step).max(axis=1)

    return {
        "start": t0.isoformat(),
        "step_min": step,
        "aggregate_kw": np.round(bucket(load_array(df, t0, n)), 3).tolist(),
        "households_kw": {h: np.round(bucket(load_array(g, t0, n)), 3).tolist()
                          for h, g in df.groupby("house")},
    }


def decisions_table(df, limit):
    rows = df.sort_values("release", ascending=False).head(limit)
    return [{
        "house": r.house, "device": r.device,
        "release": r.release.isoformat(), "start": r.start.isoformat(), "end": r.end.isoformat(),
        "delay_min": int(r.delay_min), "kw": round(float(r.kw), 3),
        "cost": round(float(r.cost), 4), "deadline_met": bool(int(r.deadline_met)),
    } for r in rows.itertuples()]
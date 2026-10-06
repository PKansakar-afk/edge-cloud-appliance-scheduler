"""Process raw UK-DALE .dat files into 1-minute series and a job table.
Run from the repo root:  python data/process_data.py
"""
from pathlib import Path
import pandas as pd

RAW = Path("data/raw/ukdale")
OUT = Path("data/processed")
HOUSES = [1]                  # start with one house, add more later
INCLUDE_MAINS = False         # mains files are huge; turn on once the rest works

# appliance name -> keywords to look for in labels.dat
FLEXIBLE = {
    "washing_machine": ["washing"],
    "dishwasher": ["dish"],
}

# job detection settings (state these as assumptions in your paper)
ON_WATTS = 10      # above this = appliance is running
MIN_LEN_MIN = 10   # ignore runs shorter than this
MAX_GAP_MIN = 5    # merge runs separated by short pauses (wash cycles pause)


def read_labels(house_dir):
    labels = pd.read_csv(house_dir / "labels.dat", sep=r"\s+",
                         header=None, names=["ch", "name"])
    return dict(zip(labels["name"], labels["ch"]))


def find_channel(labels, keywords):
    for name, ch in labels.items():
        if any(k in name.lower() for k in keywords):
            return name, ch
    return None, None


def load_channel(path, chunksize=2_000_000):
    """Read a .dat file in chunks and return a 1-minute mean series (watts)."""
    parts = []
    reader = pd.read_csv(path, sep=" ", header=None, usecols=[0, 1],
                         names=["ts", "watts"], chunksize=chunksize)
    for chunk in reader:
        chunk["ts"] = pd.to_datetime(chunk["ts"], unit="s")
        parts.append(chunk.set_index("ts")["watts"].resample("1min").mean())
    return pd.concat(parts).groupby(level=0).mean()


def extract_jobs(s, house, appliance):
    on = s.fillna(0) > ON_WATTS
    run_id = (on != on.shift()).cumsum()
    runs = [(g.index[0], g.index[-1]) for _, g in on.groupby(run_id) if g.iloc[0]]

    merged = []
    for st, en in runs:
        if merged and (st - merged[-1][1]) <= pd.Timedelta(minutes=MAX_GAP_MIN + 1):
            merged[-1] = (merged[-1][0], en)
        else:
            merged.append((st, en))

    rows = []
    for st, en in merged:
        duration = int((en - st) / pd.Timedelta(minutes=1)) + 1
        if duration < MIN_LEN_MIN:
            continue
        seg = s[st:en].fillna(0)
        rows.append({
            "house": f"H{house:02d}",
            "appliance": appliance,
            "start": st,
            "end": en,
            "duration_min": duration,
            "energy_kwh": round(seg.sum() / 60 / 1000, 4),
            "avg_kw": round(seg.mean() / 1000, 4),
            "peak_kw": round(seg.max() / 1000, 4),
        })
    return rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    all_jobs = []

    for h in HOUSES:
        house_dir = RAW / f"house_{h}"
        labels = read_labels(house_dir)
        print(f"House {h} labels:", labels)

        if INCLUDE_MAINS:
            print("  loading mains (slow)...")
            mains = load_channel(house_dir / "channel_1.dat")
            mains.to_frame("watts").to_parquet(OUT / f"house{h}_mains.parquet")

        for appliance, keywords in FLEXIBLE.items():
            name, ch = find_channel(labels, keywords)
            if ch is None:
                print(f"  {appliance}: not found in house {h}, skipping")
                continue
            print(f"  {appliance}: channel {ch} ({name})")
            s = load_channel(house_dir / f"channel_{ch}.dat")
            s.to_frame("watts").to_parquet(OUT / f"house{h}_{appliance}.parquet")
            jobs = extract_jobs(s, h, appliance)
            print(f"    -> {len(jobs)} jobs")
            all_jobs += jobs

    jobs_df = pd.DataFrame(all_jobs)
    jobs_df.to_csv(OUT / "jobs.csv", index=False)
    print(f"Saved {len(jobs_df)} jobs to {OUT / 'jobs.csv'}")


if __name__ == "__main__":
    main()
"""Chat-spike detection. Run alone to replay a chat CSV with other settings:
    python spikes.py chat/<file>.csv --k 2.0 --min-count 10
"""
import argparse
import csv
import statistics
import time
from pathlib import Path

BUCKET = 10   # seconds per chat bucket
MERGE = 30    # spikes closer than this (seconds) are one moment


def evaluate(history, count, k=2.5, min_count=15, warmup=12, window=30):
    """history = message counts of earlier buckets. Returns (mean, std, threshold, spike)."""
    base = history[-window:]
    mean = statistics.fmean(base) if base else 0.0
    std = statistics.pstdev(base) if base else 0.0
    thr = mean + k * std
    spike = len(history) >= warmup and count > thr and count >= min_count
    return mean, std, thr, spike


class Merger:
    """Feed buckets in order; returns a finished moment {epoch, count} once no spike can join it."""

    def __init__(self):
        self.cur = None

    def feed(self, epoch, count, spike):
        done = None
        if self.cur and epoch - self.cur["last"] >= MERGE:
            done, self.cur = self.cur, None
        if spike:
            if self.cur is None:
                self.cur = {"epoch": epoch, "count": count, "last": epoch}
            else:
                self.cur["last"] = epoch
                if count > self.cur["count"]:
                    self.cur.update(epoch=epoch, count=count)
        return done

    def flush(self):
        done, self.cur = self.cur, None
        return done


def replay(rows, k, min_count, warmup, window):
    """rows = [(epoch, messages)]. Returns (moments, thresholds by epoch)."""
    history, moments, thr_at, m = [], [], {}, Merger()
    for epoch, n in rows:
        _, _, thr, spike = evaluate(history, n, k, min_count, warmup, window)
        history.append(n)
        thr_at[epoch] = thr
        done = m.feed(epoch, n, spike)
        if done:
            moments.append(done)
    if m.cur:
        moments.append(m.flush())
    return moments, thr_at


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--k", type=float, default=2.5)
    ap.add_argument("--min-count", type=int, default=15)
    ap.add_argument("--baseline-min", type=float, default=5, help="baseline window in minutes")
    ap.add_argument("--warmup-buckets", type=int, default=12)
    a = ap.parse_args()

    with open(a.csv, newline="") as f:
        rows = [(float(r["bucket_start_epoch"]), int(r["messages"])) for r in csv.DictReader(f)]
    moments, thr_at = replay(rows, a.k, a.min_count, a.warmup_buckets, int(a.baseline_min * 60 / BUCKET))
    print(f"{len(rows)} buckets, {len(moments)} spike moment(s)  (k={a.k}, min-count={a.min_count})")

    # recordings/<same name as the chat csv> lets us print the clip command for each moment
    rec = Path(a.csv).resolve().parent.parent / "recordings" / Path(a.csv).stem
    samples = None
    if (rec / "progress.csv").exists():
        import clip
        samples = clip.load_progress(rec)
    for mo in sorted(moments, key=lambda x: -x["count"]):
        line = (f"  {time.strftime('%H:%M:%S', time.localtime(mo['epoch']))}  "
                f"{mo['count']} msgs (threshold {thr_at[mo['epoch']]:.1f})")
        if samples:
            at = max(0.0, clip.rec_seconds(samples, mo["epoch"]) - 20)
            line += f"  ->  python clip.py \"{rec}\" --at {at:.1f}"
        print(line)


if __name__ == "__main__":
    main()

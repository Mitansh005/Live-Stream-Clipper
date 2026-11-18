"""Watch a Twitch channel or YouTube live stream, record it while live, and auto-clip chat spikes.
    python watch.py <twitch channel | youtube url> [options]
"""
import argparse
import csv
import json
import logging
import math
import os
import queue
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from streamlink import Streamlink

import clip
import spikes

HERE = Path(__file__).resolve().parent
STREAMLINK = Path(sys.executable).with_name("streamlink.exe" if os.name == "nt" else "streamlink")  # the venv's own streamlink
log = logging.getLogger("clipper")


def pipe_to_log(stream, name):
    for line in iter(stream.readline, b""):
        if line.strip():
            log.info("[%s] %s", name, line.decode(errors="replace").rstrip())


class Recording:
    """streamlink -> (python pump) -> ffmpeg segment muxer, with a wall clock -> recorded seconds map."""

    def __init__(self, url, rec_dir, quality):
        self.dir = rec_dir
        self.samples = []  # (wall clock, seconds recorded), appended in time order
        self.sl = subprocess.Popen(
            [str(STREAMLINK), "--twitch-low-latency", "--stdout", url, quality],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.ff = subprocess.Popen(
            [clip.FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
             "-f", "segment", "-segment_time", "30", "-segment_format", "mpegts",
             "-segment_list", "segments.csv", "-segment_list_type", "csv",
             "-progress", "pipe:1", "-stats_period", "1", "seg_%05d.ts"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=rec_dir)
        for target, args in ((self._pump, ()), (self._progress, ()),
                             (pipe_to_log, (self.sl.stderr, "streamlink")), (pipe_to_log, (self.ff.stderr, "ffmpeg"))):
            threading.Thread(target=target, args=args, daemon=True).start()

    def _pump(self):
        first = True
        try:
            while chunk := self.sl.stdout.read1(65536):
                if first:
                    self.samples.append((time.time(), 0.0))
                    log.info("first video bytes arrived")
                    first = False
                self.ff.stdin.write(chunk)
        except (BrokenPipeError, OSError) as e:
            log.warning("pump stopped: %s", e)
        finally:
            try:
                self.ff.stdin.close()
            except OSError:
                pass

    def _progress(self):
        with open(self.dir / "progress.csv", "a") as f:
            for line in self.ff.stdout:
                if line.startswith(b"out_time_us="):
                    try:
                        secs = int(line[12:]) / 1e6
                    except ValueError:  # "N/A" before the first packet
                        continue
                    now = time.time()
                    self.samples.append((now, secs))
                    f.write(f"{now:.3f},{secs:.3f}\n")
                    f.flush()

    def seconds(self):
        return self.samples[-1][1] if self.samples else 0.0

    def to_rec(self, epoch):
        return clip.rec_seconds(self.samples, epoch)

    def alive(self):
        return self.ff.poll() is None

    def stop(self):
        if self.sl.poll() is None:
            self.sl.terminate()
        try:
            self.ff.wait(60)
        except subprocess.TimeoutExpired:
            self.ff.kill()


class ChatCounter(threading.Thread):
    def __init__(self, target):
        super().__init__(daemon=True)
        self.target, self.count, self.lock, self.stopped = target, 0, threading.Lock(), threading.Event()

    def add(self, n):
        with self.lock:
            self.count += n

    def take(self):
        with self.lock:
            n, self.count = self.count, 0
        return n


class TwitchChat(ChatCounter):
    """Anonymous Twitch IRC reader that counts chat lines. target = channel name."""

    def run(self):
        self.channel = self.target
        while not self.stopped.is_set():
            try:
                with socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=15) as s:
                    s.sendall(f"NICK justinfan{random.randint(0, 99999):05d}\r\nJOIN #{self.channel}\r\n".encode())
                    log.info("chat connected to #%s", self.channel)
                    s.settimeout(1)
                    buf, last = b"", time.time()
                    while not self.stopped.is_set():
                        try:
                            data = s.recv(65536)
                        except socket.timeout:
                            if time.time() - last > 360:  # Twitch PINGs every ~5 min
                                raise ConnectionError("no data for 6 minutes")
                            continue
                        if not data:
                            raise ConnectionError("server closed the connection")
                        last = time.time()
                        *lines, buf = (buf + data).split(b"\r\n")
                        n = 0
                        for line in lines:
                            if line.startswith(b"PING"):
                                s.sendall(b"PONG" + line[4:] + b"\r\n")
                            elif b" PRIVMSG #" in line:
                                n += 1
                        self.add(n)
            except OSError as e:
                if not self.stopped.is_set():
                    log.warning("chat disconnected (%s), reconnecting in 5 s", e)
                    self.stopped.wait(5)


YT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}


def yt_get(url, data=None):
    headers = {**YT_HEADERS, "Content-Type": "application/json"} if data else YT_HEADERS
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=15) as r:
        return r.read().decode()


def youtube_id(url):
    """Video id from a watch / youtu.be URL; a channel's /live URL is resolved to the current stream."""
    u = urlparse(url)
    if "v" in parse_qs(u.query):
        return parse_qs(u.query)["v"][0]
    if u.netloc.endswith("youtu.be"):
        return u.path.strip("/")
    return re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', yt_get(url)).group(1)


class YouTubeChat(ChatCounter):
    """Counts YouTube live chat messages by polling the public live-chat feed (no key, no account).
    target = YouTube URL."""

    def run(self):
        while not self.stopped.is_set():
            try:
                vid = youtube_id(self.target)
                page = yt_get(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}")
                ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page).group(1)
                token = re.search(r'"continuation":"([^"]+)"', page).group(1)
                log.info("chat connected to YouTube live chat of %s", vid)
                while not self.stopped.is_set():
                    body = json.dumps({"context": {"client": {"clientName": "WEB", "clientVersion": ver}},
                                       "continuation": token}).encode()
                    lc = json.loads(yt_get("https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false",
                                           body))["continuationContents"]["liveChatContinuation"]
                    # shortcut: counted when the ~5 s batch arrives, not by each message's timestamp;
                    # fine for 10 s buckets, switch to timestampUsec if buckets get shorter
                    self.add(sum("liveChatTextMessageRenderer" in a.get("addChatItemAction", {}).get("item", {})
                                 for a in lc.get("actions", [])))
                    cont = next(iter(lc["continuations"][0].values()))
                    token = cont["continuation"]
                    self.stopped.wait(min(cont.get("timeoutMs", 5000), 5000) / 1000)
            except (OSError, KeyError, ValueError, AttributeError) as e:
                if not self.stopped.is_set():
                    log.warning("YouTube chat dropped (%r), retrying in 5 s", e)
                    self.stopped.wait(5)


class Clipper(threading.Thread):
    """Makes clips one by one so Whisper never blocks recording or chat."""

    def __init__(self, rec, model):
        super().__init__(daemon=True)
        self.rec, self.model, self.q, self.needed = rec, model, queue.Queue(), []

    def add(self, start, out):
        self.needed.append(start)
        self.q.put((start, out))

    def run(self):
        while (job := self.q.get()) is not None:
            start, out = job
            end = start + 30
            while self.rec.alive() and not (self.rec.seconds() >= end and clip.covered(self.rec.dir, end)):
                time.sleep(2)
            log.info("making clip %s", out.name)
            try:
                _, caps = clip.make_clip(self.rec.dir, start, 30, out, self.model)
                log.info("clip done: %s (%d captions)", out, len(caps))
            except subprocess.CalledProcessError as e:
                log.error("clip failed: %s", e.stderr.decode(errors="replace")[-500:])
            except Exception:
                log.exception("clip failed")
            finally:
                self.needed.remove(start)

    def finish(self):
        self.q.put(None)
        self.join()


def prune(rec, keep_minutes, protect):
    """Delete pieces older than keep_minutes, except ones a pending clip (starting at >= protect) needs."""
    cutoff = min(rec.seconds() - keep_minutes * 60, protect)
    for name, _, end in clip.segments(rec.dir):
        p = rec.dir / name
        if end < cutoff and p.exists():
            p.unlink()


def run_session(a):
    name = f"{a.slug}-{datetime.now():%Y%m%d-%H%M%S}"
    rec_dir = HERE / "recordings" / name
    rec_dir.mkdir(parents=True)
    (HERE / "chat").mkdir(exist_ok=True)
    (HERE / "clips").mkdir(exist_ok=True)
    log.info("live! recording to %s", rec_dir)

    rec = Recording(a.url, rec_dir, a.quality)
    chat = a.chat_cls(a.target)
    chat.start()
    clipper = Clipper(rec, a.model)
    clipper.start()
    merger, history = spikes.Merger(), []
    window = int(a.baseline_min * 60 / spikes.BUCKET)
    deadline = time.time() + a.minutes * 60 if a.minutes else math.inf
    buckets = n_spikes = 0

    def queue_clip(mo):
        start = max(0.0, rec.to_rec(mo["epoch"]) - 20)
        out = HERE / "clips" / f"{name}-{datetime.fromtimestamp(mo['epoch']):%H%M%S}-{mo['count']}msgs.mp4"
        log.info("spike moment at %s (%d msgs) -> clip from %.1fs", out.name, mo["count"], start)
        clipper.add(start, out)

    next_t = math.ceil(time.time() / spikes.BUCKET) * spikes.BUCKET
    first = True
    with open(HERE / "chat" / f"{name}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bucket_start", "bucket_start_epoch", "messages", "baseline_mean", "baseline_std", "threshold", "spike"])
        while rec.alive() and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, next_t - time.time())))
            if time.time() < next_t:
                continue
            count, bstart = chat.take(), next_t - spikes.BUCKET
            next_t += spikes.BUCKET
            if first:  # chat joined mid-bucket: drop the partial one
                first = False
                continue
            mean, std, thr, spike = spikes.evaluate(history, count, a.k, a.min_count, a.warmup_buckets, window)
            history.append(count)
            buckets += 1
            n_spikes += spike
            w.writerow([f"{datetime.fromtimestamp(bstart):%Y-%m-%d %H:%M:%S}", bstart, count,
                        f"{mean:.2f}", f"{std:.2f}", f"{thr:.2f}", int(spike)])
            f.flush()
            if spike:
                log.info("SPIKE bucket %s: %d msgs > threshold %.1f", f"{datetime.fromtimestamp(bstart):%H:%M:%S}", count, thr)
            done = merger.feed(bstart, count, spike)
            if done:
                queue_clip(done)
            if buckets % 6 == 0:
                log.info("last bucket %d msgs, spike threshold %.1f (mean %.1f, std %.1f), recorded %.0fs",
                         count, thr, mean, std, rec.seconds())
                pending = clipper.needed + ([rec.to_rec(merger.cur["epoch"]) - 20] if merger.cur else [])
                prune(rec, a.keep_minutes, min(pending, default=math.inf))

    log.info("stream ended or time limit reached; stopping recorder")
    rec.stop()
    chat.stopped.set()
    if merger.cur:
        queue_clip(merger.flush())
    clipper.finish()
    log.info("session done: %d buckets, %d spike buckets", buckets, n_spikes)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("channel", help="Twitch channel name, or a YouTube live URL (watch?v=... or youtube.com/@handle/live)")
    ap.add_argument("--k", type=float, default=2.5, help="spike = count > mean + k*std")
    ap.add_argument("--min-count", type=int, default=15, help="a spike needs at least this many msgs per 10 s")
    ap.add_argument("--baseline-min", type=float, default=5, help="baseline window in minutes")
    ap.add_argument("--warmup-buckets", type=int, default=12, help="no spikes before this many buckets")
    ap.add_argument("--quality", default="720p60,720p,best")
    ap.add_argument("--model", default="base", help="faster-whisper model")
    ap.add_argument("--keep-minutes", type=float, default=15, help="delete recording pieces older than this")
    ap.add_argument("--minutes", type=float, default=0, help="stop after N minutes and exit (0 = until offline)")
    ap.add_argument("--poll", type=float, default=60, help="seconds between live checks")
    ap.add_argument("--once", action="store_true", help="exit after one stream")
    a = ap.parse_args()
    if "youtu" in a.channel:
        a.url = a.target = a.channel
        m = re.search(r"(?:v=|youtu\.be/|@)([\w.-]+)", a.channel)
        a.slug, a.chat_cls = "yt-" + (m.group(1) if m else "stream"), YouTubeChat
    else:
        a.target = a.slug = a.channel.lower().lstrip("#")
        a.url, a.chat_cls = f"https://twitch.tv/{a.target}", TwitchChat

    (HERE / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "logs" / f"{a.slug}.log", encoding="utf-8")])
    session = Streamlink()
    log.info("watching %s", a.url)
    while True:
        try:
            live = bool(session.streams(a.url))
        except Exception as e:
            log.warning("live check failed: %s", e)
            live = False
        if live:
            run_session(a)
            if a.once or a.minutes:
                break
            log.info("back to waiting")
        else:
            log.info("offline, checking again in %.0fs", a.poll)
        time.sleep(a.poll)


if __name__ == "__main__":
    main()

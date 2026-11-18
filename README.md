# Live-Stream-Clipper

Auto-clipper for Twitch and YouTube live streams.

Watches a Twitch channel or a YouTube live stream. While it's live, it records the stream, counts chat messages every 10 seconds, and turns every chat spike into a 35-second vertical 9:16 (1080x1920) clip with burned-in captions. No API keys, no accounts, no paid services.

## Setup (once)

Needs Python 3.9+ and ffmpeg (`winget install ffmpeg` on Windows, `brew install ffmpeg` on Mac).

```bash
git clone https://github.com/Mitansh005/Live-Stream-Clipper.git
cd Live-Stream-Clipper
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt      # Windows
.venv/bin/python -m pip install -r requirements.txt          # Mac/Linux
```

On Mac/Linux, use `.venv/bin/python` wherever these docs say `.venv\Scripts\python`.

## Run

```bash
.venv\Scripts\python watch.py xqc
```

It checks every 60 s whether the channel is live. When it is, it records and clips, then goes back to waiting after the stream ends.

YouTube: pass a URL instead of a channel name.

```bash
.venv\Scripts\python watch.py "https://www.youtube.com/@CarryisLive/live"
.venv\Scripts\python watch.py "https://www.youtube.com/watch?v=irNhWMG560k"
```

Use the channel's `/live` URL for overnight runs: each new stream gets a new video id, and `/live` always points to the current one. YouTube chat is read from the public live-chat feed that youtube.com's own chat window uses, with no key or account. It's unofficial, so it can break if YouTube changes its page. If that happens, the log shows `YouTube chat dropped` and it keeps retrying. Files are named `yt-<id or handle>-<date-time>`.

| Option | Default | Meaning |
|---|---|---|
| `--k` | 2.5 | A spike is a count above mean + k × std of the last 5 minutes |
| `--min-count` | 15 | A spike also needs at least this many messages in its 10 s bucket |
| `--baseline-min` | 5 | Length of the baseline window, in minutes (5 = 30 buckets) |
| `--warmup-buckets` | 12 | No spikes until this many buckets (2 minutes) have been counted |
| `--quality` | `720p60,720p,best` | Streamlink quality list (first one available wins) |
| `--clip-seconds` | 35 | Clip length in seconds |
| `--model` | base | faster-whisper model (tiny, base, small, medium…) |
| `--keep-minutes` | 15 | Delete recording pieces older than this, except ones a pending clip still needs |
| `--minutes` | 0 | Stop after N minutes and exit (for tests). 0 = run until the stream goes offline |
| `--poll` | 60 | Seconds between "is it live?" checks |
| `--once` | off | Exit after one stream instead of waiting for the next one |

Where things go:
- `recordings/<channel>-<date-time>/`: 30 s `.ts` pieces, `segments.csv`, `progress.csv` (wall clock → seconds recorded)
- `chat/<channel>-<date-time>.csv`: one row per 10 s bucket: count, baseline mean/std, threshold, spike flag
- `clips/<channel>-<date-time>-<HHMMSS>-<N>msgs.mp4` plus an `.srt` with the same name
- `logs/<channel>.log`: everything that was printed, with timestamps

A clip ends at the end of the spike's 10 s bucket and starts `--clip-seconds` earlier (default 35 s, so 25 s of lead-in before the bucket). Spikes less than 30 s apart count as one moment, and the loudest bucket is used.

## Tune the spike settings afterwards

Replay a chat CSV with other settings. If the recording still exists, it also prints the `clip.py` command for each moment:

```bash
.venv\Scripts\python spikes.py chat/xqc-20261118-064209.csv --k 2.0 --min-count 10
```

Options: `--k`, `--min-count`, `--baseline-min`, `--warmup-buckets` (same meaning as above).

## Make a clip by hand

```bash
.venv\Scripts\python clip.py recordings/xqc-20261118-064209 --at 300 --out clips/manual.mp4
```

`--at` is the clip start in seconds into the recording. Other options: `--duration` (default 35), `--model` (default base).

## Leave it running overnight

Windows: open a terminal in this folder and run the command below. Leave the window open and stop Windows from sleeping (Settings → System → Power → Sleep: Never). Close the window or press Ctrl+C to stop.

```bash
.venv\Scripts\python watch.py xqc
```

To have it start at logon, use Task Scheduler. Create a task with the action Program `<this folder>\.venv\Scripts\python.exe`, Arguments `watch.py xqc`, Start in `<this folder>`.

Mac/Linux: `nohup .venv/bin/python watch.py xqc > /dev/null 2>&1 &`. On a Mac, prefix it with `caffeinate -i` so the machine stays awake. Stop it with `pkill -f watch.py`.

Disk use stays small, because only the last `--keep-minutes` of video are kept (about 400 MB per 15 min at 720p60). Clips and chat CSVs are kept forever.

## Self-check

```bash
.venv\Scripts\python tests.py
```

## Be fair to streamers

This tool saves other people's streams. Get the streamer's permission before you post clips, and follow Twitch's and YouTube's terms. Nothing is uploaded anywhere: everything stays in this folder.

## License

MIT, see [LICENSE](LICENSE).

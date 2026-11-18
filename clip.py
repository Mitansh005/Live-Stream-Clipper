"""Cut a captioned vertical 9:16 (1080x1920) clip out of a recording folder.
    python clip.py recordings/<session> --at <seconds> --out clips/manual.mp4
--at is the clip start in seconds into the recording.
"""
import argparse
import bisect
import csv
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FFMPEG = "ffmpeg"
CAPTION_Y = 1340
DURATION = 35  # default clip length, seconds
_models = {}


def segments(rec_dir):
    """[(filename, start, end)] from ffmpeg's segments.csv."""
    p = Path(rec_dir) / "segments.csv"
    if not p.exists():
        return []
    with open(p, newline="") as f:
        return [(r[0], float(r[1]), float(r[2])) for r in csv.reader(f) if len(r) >= 3]


def covered(rec_dir, t):
    return any(end >= t for _, _, end in segments(rec_dir))


def load_progress(rec_dir):
    with open(Path(rec_dir) / "progress.csv") as f:
        return [tuple(map(float, line.split(","))) for line in f if "," in line]


def rec_seconds(samples, epoch):
    """samples = [(wall clock, seconds recorded)] sorted. Wall time -> seconds into the recording."""
    i = bisect.bisect_right(samples, (epoch, float("inf"))) - 1
    if i < 0:
        return 0.0
    wall, secs = samples[i]
    return secs + min(max(epoch - wall, 0.0), 1.0)


def clip_start(moment, duration=DURATION):
    """A clip ends at the end of the spike's 10 s bucket (moment = bucket start, in recording seconds)."""
    return max(0.0, moment + 10 - duration)


def group_words(words, max_words=3, gap=0.45):
    """words = [(start, end, text)] -> captions [(start, end, text)]."""
    caps, cur = [], []
    for i, (s, e, t) in enumerate(words):
        t = t.strip()
        if not t:
            continue
        cur.append((s, e, t))
        nxt = words[i + 1][0] if i + 1 < len(words) else None
        if len(cur) == max_words or t[-1] in ".,!?;:" or nxt is None or nxt - e > gap:
            caps.append((cur[0][0], cur[-1][1], " ".join(w for _, _, w in cur)))
            cur = []
    return caps


def _srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def _font(size):
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def caption_png(text, path, size=66, max_w=1000, stroke=6):
    draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while True:
        font = _font(size)
        x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        if x1 - x0 <= max_w or size <= 24:
            break
        size -= 4
    pad = 4
    img = Image.new("RGBA", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - x0, pad - y0), text, font=font, fill="white",
                             stroke_width=stroke, stroke_fill="black")
    img.save(path)


def transcribe(audio, model="base"):
    from faster_whisper import WhisperModel  # slow import, only when needed
    if model not in _models:
        _models[model] = WhisperModel(model, device="cpu", compute_type="int8")
    segs, _ = _models[model].transcribe(audio, word_timestamps=True, vad_filter=True)
    return [(w.start, w.end, w.word) for s in segs for w in (s.words or [])]


def make_clip(rec_dir, start, duration, out, model="base"):
    rec_dir, out = Path(rec_dir), Path(out).resolve()
    end = start + duration
    pieces = [s for s in segments(rec_dir) if s[2] > start and s[1] < end and (rec_dir / s[0]).exists()]
    if not pieces:
        raise RuntimeError(f"no recording pieces cover {start:.1f}-{end:.1f}s in {rec_dir}")
    src = ["-ss", f"{max(0.0, start - pieces[0][1]):.3f}", "-i", "concat:" + "|".join(p[0] for p in pieces)]
    run = lambda args, **kw: subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                                            cwd=rec_dir, check=True, capture_output=True, **kw)

    pcm = run([*src, "-t", str(duration), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]).stdout
    caps = group_words(transcribe(np.frombuffer(pcm, np.float32), model))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".srt").write_text("".join(
        f"{i}\n{_srt_time(s)} --> {_srt_time(e)}\n{t}\n\n" for i, (s, e, t) in enumerate(caps, 1)), encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        inputs, chain, last = [], [], "v0"
        for i, (s, e, t) in enumerate(caps, 1):
            png = Path(tmp) / f"cap{i}.png"
            caption_png(t, png)
            inputs += ["-i", str(png)]
            chain.append(f"[{last}][{i}:v]overlay=(W-w)/2:{CAPTION_Y}:enable='between(t,{s:.3f},{e:.3f})'[v{i}]")
            last = f"v{i}"
        graph = ";".join([
            "[0:v]split=2[a][b]",
            # blur at quarter size: same look, much faster
            "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=8,"
            "scale=1080:1920,eq=brightness=-0.12[bg]",
            "[b]scale=1080:-2[fg]",
            "[bg][fg]overlay=0:(H-h)/2[v0]",
            *chain,
            f"[{last}]fps=30,format=yuv420p[vout]",
        ])
        run([*src, *inputs, "-filter_complex", graph, "-map", "[vout]", "-map", "0:a:0?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30",
             "-c:a", "aac", "-b:a", "160k", "-t", str(duration), "-movflags", "+faststart", str(out)])
    return out, caps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording")
    ap.add_argument("--at", type=float, required=True, help="clip start, seconds into the recording")
    ap.add_argument("--duration", type=float, default=DURATION)
    ap.add_argument("--out", default="clips/manual.mp4")
    ap.add_argument("--model", default="base")
    a = ap.parse_args()
    out, caps = make_clip(a.recording, a.at, a.duration, a.out, a.model)
    print(f"wrote {out} ({len(caps)} captions)")


if __name__ == "__main__":
    main()

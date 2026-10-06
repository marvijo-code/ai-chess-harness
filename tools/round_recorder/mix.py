#!/usr/bin/env python3
"""Build the audio of a recorded round (speech clips + move clicks), mux the lossless master, verify it.

Runs on the VPS after rec.py finished.
Usage: mix.py <run_dir> <out_name> [--format mkv|mp4] [--port 18770]

  mkv (fast): the raw lossless take (H.264 CRF 0, yuv444p) is stream-copied, audio is FLAC.
  mp4       : video re-encoded to H.264 CRF 0 yuv420p (slow), audio AAC 320k.

Audio: each clip is trimmed to what the page actually played, placed at its logged start (adelay) and
mixed with amix normalize=0 (no ducking); speech is set to about -16 LUFS integrated with a limiter so
true peak stays under -1.5 dBTP; clicks (click_sound.py, -20 dBFS peak) sit at each logged move click.
Verification (verify.txt): video vs audio duration, frame count vs duration x 30, EBU R128 summary,
volumedetect at 3 clip offsets, 4 frame grabs at clip times (grabs/).
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from click_sound import SR, make_click, write_wav  # noqa: E402

CLICK_PEAK_DB = -20.0
LIMIT_DB = -2.5


def sh(cmd):
    print("+", " ".join(map(str, cmd))[:300], flush=True)
    return subprocess.run(list(map(str, cmd)), check=True, text=True, capture_output=True)


def probe_dur(path, sel):
    j = json.loads(sh(["ffprobe", "-v", "error", "-select_streams", sel, "-show_entries",
                       "stream=duration:format=duration", "-of", "json", path]).stdout)
    d = (j.get("streams") or [{}])[0].get("duration")
    return float(d if d not in (None, "N/A") else j["format"]["duration"])


def ebur(path):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true",
                          "-f", "null", "-"], text=True, capture_output=True).stderr
    summ = err[err.rfind("Summary:"):]
    i = float(re.search(r"I:\s+(-?[0-9.]+|-inf) LUFS", summ).group(1))
    tp = float(re.search(r"Peak:\s+(-?[0-9.]+|-inf) dBFS", summ).group(1))
    return i, tp, summ.strip()


def plan_events(run, vdur, meta):
    t0 = meta.get("ffmpeg_input_start")
    t0_src = "x11grab start"
    if not t0 or abs(t0 - meta["ffmpeg_popen_epoch"]) > 5:
        t0, t0_src = meta["ffmpeg_popen_epoch"], "popen"
    evs = [json.loads(l) for l in (run / "events.jsonl").read_text().splitlines() if l.strip()]
    page = [e for e in evs if "t" in e]
    clips = []
    for i, e in enumerate(page):
        if e["kind"] != "clip_playing":
            continue
        played = None
        for f in page[i + 1:]:
            if f["kind"] in ("clip_pause", "clip_ended") and f.get("src") == e["src"]:
                played = float(f.get("ct") or 0)
                break
            if f["kind"] == "clip_playing":
                break
        off = e["t"] / 1000.0 - t0
        if 0 <= off < vdur:
            clips.append({"name": e["src"].rsplit("/", 1)[-1], "offset": round(off, 4), "played": played})
    clicks = []
    for e in page:
        if e["kind"] == "click" and e.get("state") == "running":
            off = e["t"] / 1000.0 + float(e.get("delay") or 0) - t0
            if 0 <= off < vdur - 0.06:
                clicks.append(round(off, 4))
    return t0, t0_src, clips, clicks


def build_mix(work, vdur, clips, clicks, gain):
    lim = 10 ** (LIMIT_DB / 20)
    sh(["ffmpeg", "-hide_banner", "-y", "-i", work / "speech_raw.wav", "-af",
        f"volume={gain:.3f}dB,alimiter=limit={lim:.4f}:attack=1:release=50:level=0:asc=1",
        "-c:a", "pcm_f32le", work / "speech_norm.wav"])
    sh(["ffmpeg", "-hide_banner", "-y", "-i", work / "speech_norm.wav", "-i", work / "clicks.wav", "-filter_complex",
        f"[0:a][1:a]amix=inputs=2:normalize=0:duration=longest,atrim=0:{vdur:.3f},aformat=channel_layouts=stereo[m]",
        "-map", "[m]", "-ar", SR, "-c:a", "pcm_f32le", work / "mix.wav"])
    return ebur(work / "mix.wav")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("out_name")
    ap.add_argument("--format", choices=["mkv", "mp4"], default="mkv")
    ap.add_argument("--port", type=int, default=18770)
    a = ap.parse_args()
    run = Path(a.run).expanduser()
    api = f"http://127.0.0.1:{a.port}"
    meta = json.loads((run / "meta.json").read_text())
    raw = run / "raw.mkv"
    try:
        vdur = probe_dur(raw, "v:0")
    except (KeyError, ValueError):
        # A take whose ffmpeg was killed has no duration in its header: remux it (stream copy) first.
        fixed = run / "raw-remux.mkv"
        sh(["ffmpeg", "-hide_banner", "-y", "-i", raw, "-c", "copy", fixed])
        raw = fixed
        vdur = probe_dur(raw, "v:0")
    t0, t0_src, clips, clicks = plan_events(run, vdur, meta)
    print(f"video {vdur:.3f}s, t0={t0:.3f} ({t0_src}); {len(clips)} clips, {len(clicks)} clicks", flush=True)
    if not clips:
        raise SystemExit("no commentary clips were played during the take")

    work = run / "mix"
    cdir = work / "clips"
    cdir.mkdir(parents=True, exist_ok=True)
    for c in clips:
        p = cdir / c["name"]
        if not p.exists():
            urllib.request.urlretrieve(f"{api}/api/commentary/audio/{c['name']}", p)
        with wave.open(str(p)) as w:
            c["wav_seconds"] = w.getnframes() / w.getframerate()
        if not c["played"] or c["played"] <= 0:
            c["played"] = c["wav_seconds"]
        c["played"] = min(c["played"], c["wav_seconds"])

    click = make_click(CLICK_PEAK_DB)
    write_wav(work / "click.wav", click)
    total = int(round(vdur * SR))
    track = np.zeros(total, dtype=np.float32)
    for off in clicks:
        s = int(round(off * SR))
        e = min(total, s + len(click))
        track[s:e] += click[: e - s]
    write_wav(work / "clicks.wav", track)
    del track

    inputs, parts = [], []
    for k, c in enumerate(clips):
        inputs += ["-i", cdir / c["name"]]
        ms = int(round(c["offset"] * 1000))
        parts.append(f"[{k}:a]aresample={SR},atrim=0:{c['played']:.3f},asetpts=PTS-STARTPTS,adelay={ms}:all=1[c{k}]")
    labels = "".join(f"[c{k}]" for k in range(len(clips)))
    parts.append(f"{labels}amix=inputs={len(clips)}:normalize=0:duration=longest,apad=whole_dur={vdur:.3f},"
                 f"atrim=0:{vdur:.3f}[sp]")
    (work / "speech.filter").write_text(";\n".join(parts))
    sh(["ffmpeg", "-hide_banner", "-y", *inputs, "-/filter_complex", work / "speech.filter", "-map", "[sp]",
        "-ac", "1", "-ar", SR, "-c:a", "pcm_f32le", work / "speech_raw.wav"])
    si, stp, _ = ebur(work / "speech_raw.wav")
    gain = -16.0 - si
    mi, mtp, _ = build_mix(work, vdur, clips, clicks, gain)
    for _pass in range(3):
        if abs(mi + 16) <= 0.3 and mtp <= -1.5:
            break
        gain += -16.0 - mi
        mi, mtp, _ = build_mix(work, vdur, clips, clicks, gain)
    print(f"speech raw I={si} TP={stp}; gain {gain:.2f} dB; mix I={mi} TP={mtp}", flush=True)
    (work / "plan.json").write_text(json.dumps({
        "t0": t0, "t0_source": t0_src, "video_seconds": vdur, "clips": clips, "clicks": clicks,
        "speech_gain_db": gain, "mix_I": mi, "mix_TP": mtp, "click_peak_dbfs": CLICK_PEAK_DB}, indent=2))

    master = run / a.out_name
    if a.format == "mkv":
        sh(["ffmpeg", "-hide_banner", "-y", "-i", raw, "-i", work / "mix.wav", "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "flac", "-sample_fmt", "s16", "-t", f"{vdur:.3f}", master])
    else:
        sh(["ffmpeg", "-hide_banner", "-y", "-i", raw, "-i", work / "mix.wav", "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "0", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "320k", "-ar", SR, "-t", f"{vdur:.3f}", "-movflags", "+faststart", master])

    # ---- verification ---------------------------------------------------------------------------
    lines = []
    md_v, md_a = probe_dur(master, "v:0"), probe_dur(master, "a:0")
    lines.append(f"duration video={md_v:.3f}s audio={md_a:.3f}s diff={abs(md_v - md_a):.3f}s "
                 f"({'OK' if abs(md_v - md_a) <= 0.2 else 'FAIL'})")
    nfr = int(sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                  "stream=nb_read_packets", "-of", "csv=p=0", master]).stdout.strip().split(",")[0])
    exp = round(md_v * 30)
    lines.append(f"frames={nfr} expected={exp} (duration x 30) ({'OK' if abs(nfr - exp) <= 1 else 'CHECK'})")
    lines.append(sh(["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,sample_rate,channels",
                     "-of", "compact", master]).stdout.strip())
    mi2, mtp2, summ = ebur(master)
    lines.append(f"loudness master I={mi2} LUFS true_peak={mtp2} dBTP "
                 f"({'OK' if abs(mi2 + 16) <= 1 and mtp2 < -1.5 else 'CHECK'})")
    lines.append(re.sub(r"\s+", " ", summ))
    picks = sorted({0, len(clips) // 3, (2 * len(clips)) // 3, len(clips) - 1})
    for k in picks[:3]:
        c = clips[k]
        err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{c['offset'] + 1:.2f}", "-t", "3",
                              "-i", str(master), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                             text=True, capture_output=True).stderr
        mv = re.search(r"mean_volume: (-?[0-9.]+|-inf) dB", err)
        xv = re.search(r"max_volume: (-?[0-9.]+|-inf) dB", err)
        lines.append(f"volumedetect clip {c['name']} at {c['offset']:.1f}s: mean={mv and mv.group(1)} dB "
                     f"max={xv and xv.group(1)} dB")
    gdir = run / "grabs"
    gdir.mkdir(exist_ok=True)
    for k in picks:
        c = clips[k]
        at = c["offset"] + min(2.0, c["played"] / 2)
        out = gdir / f"grab-{at:07.1f}s-{c['name'].replace('.wav', '')}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(master),
                        "-frames:v", "1", str(out)], check=False)
        lines.append(f"grab {out.name}")
    lines.append(f"clips={len(clips)} clicks={len(clicks)} t0_source={t0_src}")
    (run / "verify.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("MASTER", master)


if __name__ == "__main__":
    main()

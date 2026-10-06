#!/usr/bin/env python3
"""Build the audio of a recorded round (speech clips + move clicks), mux the lossless master, verify it.

Runs on the VPS after rec.py finished.
Usage: mix.py <run_dir> <out_name> [--format mkv|mp4] [--port 18770]
       mix.py <run_dir> --round R [--port 18770]     (series take: rounds.json + raw-r<R>.mkv, time-lapse edit)

Series round mode: the round's window from rounds.json, board-tour spans (director events) sped up, one
lossless video pass (trim + setpts/S + fps=30 per segment, concat; CRF 0 yuv444p, nice 19, 2 threads),
audio built on the OUTPUT timeline (speech and clicks remapped, clicks inside lapse spans dropped, ducked
music bed, two-pass loudnorm to -16 LUFS), FLAC in MKV: round<R>-live-DRAFT001.mkv + round<R>-verify.txt.

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
import os
import re
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timeline as tl  # noqa: E402
from click_sound import SR, make_click, write_wav  # noqa: E402

CLICK_PEAK_DB = -20.0
# Channel music bed under the whole round (owner 2026-10-06: "i don't like silences without at least background
# music"): about -30 LUFS on its own, ducked ~10 dB under the commentary, then the mix is set to -16 LUFS.
MUSIC_LUFS = -30.0
DEFAULT_BED = Path.home() / "acl-chess-round-rec" / "bed-cinematic-ambient.mp3"
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


def add_music(work, vdur, bed):
    """Replace mix.wav with speech + clicks + a looped, ducked music bed, two-pass loudness to -16 LUFS."""
    (work / "mix.wav").replace(work / "mix_nomusic.wav")
    fade_out = max(0.0, vdur - 4)
    sh(["ffmpeg", "-hide_banner", "-y", "-stream_loop", "-1", "-i", bed, "-t", f"{vdur:.3f}", "-af",
        f"aresample={SR},aformat=channel_layouts=stereo,loudnorm=I={MUSIC_LUFS}:TP=-6:LRA=11,"
        f"afade=t=in:d=3,afade=t=out:st={fade_out:.3f}:d=4", "-c:a", "pcm_f32le", work / "music_bed.wav"])
    sh(["ffmpeg", "-hide_banner", "-y", "-i", work / "mix_nomusic.wav", "-i", work / "music_bed.wav",
        "-i", work / "speech_norm.wav", "-filter_complex",
        "[2:a]aformat=channel_layouts=stereo[key];"
        "[1:a][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=450:makeup=1[duck];"
        f"[0:a][duck]amix=inputs=2:normalize=0:duration=first,atrim=0:{vdur:.3f}[m]",
        "-map", "[m]", "-ar", SR, "-c:a", "pcm_f32le", work / "mix_music_raw.wav"])
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(work / "mix_music_raw.wav"), "-af",
                          "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = json.loads(out[out.rindex("{"):out.rindex("}") + 1])
    sh(["ffmpeg", "-hide_banner", "-y", "-i", work / "mix_music_raw.wav", "-af",
        f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
        f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,"
        f"aresample={SR}", "-c:a", "pcm_f32le", work / "mix.wav"])
    return ebur(work / "mix.wav")


# ---- series round mode: one round of a series take, with time-lapse spans ---------------------------

FINAL_TP = -1.7          # loudnorm true-peak target: a margin under the -1.5 dBTP ceiling
PREMIX_HEADROOM_DB = -6  # the 24-bit premix keeps headroom; the final loudnorm restores the level


def ebur_text(err):
    summ = err[err.rfind("Summary:"):]
    m_i = re.search(r"I:\s+(-?[0-9.]+|-inf) LUFS", summ)
    m_tp = re.search(r"Peak:\s+(-?[0-9.]+|-inf) dBFS", summ)
    return (float(m_i.group(1)) if m_i else float("-inf")), (float(m_tp.group(1)) if m_tp else float("-inf"))


def round_clips(events, t0, raw_start, raw_end):
    """Clips that play inside [raw_start, raw_end] (raw time); a clip still playing at the window start is
    kept from that point (seek), so speech continues across round files."""
    page = [e for e in events if "t" in e]
    out = []
    for i, e in enumerate(page):
        if e.get("kind") != "clip_playing":
            continue
        played = None
        for f in page[i + 1:]:
            if f.get("kind") in ("clip_pause", "clip_ended") and f.get("src") == e.get("src"):
                played = float(f.get("ct") or 0)
                break
            if f.get("kind") == "clip_playing":
                break
        off = e["t"] / 1000.0 - t0
        c = {"name": str(e.get("src", "")).rsplit("/", 1)[-1], "raw_offset": round(off, 4), "played": played}
        if raw_start <= off < raw_end:
            out.append(c)
        elif off < raw_start and played and off + played > raw_start + 0.3:
            c["seek"] = round(raw_start - off, 4)
            out.append(c)
    return out


def round_clicks(events, t0, raw_start, raw_end, segs):
    kept, dropped = [], 0
    for e in events:
        if e.get("kind") == "click" and e.get("state") == "running" and "t" in e:
            off = e["t"] / 1000.0 + float(e.get("delay") or 0) - t0
            if not raw_start <= off < raw_end:
                continue
            if tl.in_lapse(off, segs):
                dropped += 1
                continue
            kept.append(round(off, 4))
    return kept, dropped


def speech_parts(clips, dur):
    """Filter lines placing each clip at its output offset; label [sp] (mono, dur seconds)."""
    if not clips:
        return [f"anullsrc=r={SR}:cl=mono,atrim=0:{dur:.3f}[sp]"], 0
    parts = []
    for k, c in enumerate(clips):
        ms = int(round(c["out_offset"] * 1000))
        seek = c.get("seek", 0.0)
        parts.append(f"[{k}:a]aresample={SR},aformat=channel_layouts=mono,atrim={seek:.3f}:{seek + c['play']:.3f},"
                     f"asetpts=PTS-STARTPTS,adelay={ms}:all=1[c{k}]")
    labels = "".join(f"[c{k}]" for k in range(len(clips)))
    parts.append(f"{labels}amix=inputs={len(clips)}:normalize=0:duration=longest,apad=whole_dur={dur:.3f},"
                 f"atrim=0:{dur:.3f}[sp]")
    return parts, len(clips)


def mix_round(run, key, music, port, out_name=None):
    t_start = time.time()
    try:
        os.nice(19 - os.nice(0))            # the next round is recording on the same 4 cores
    except OSError:
        pass
    api = f"http://127.0.0.1:{port}"
    rounds = json.loads((run / "rounds.json").read_text())
    entry = tl.find_entry(rounds, key)
    if not entry:
        raise SystemExit(f"round {key} is not in rounds.json")
    key = str(entry.get("key", entry["round"]))
    out_name = out_name or f"round{key}-live-DRAFT001.mkv"
    raw = run / entry["raw"]
    work = run / f"mix-r{key}"
    work.mkdir(parents=True, exist_ok=True)
    cdir = run / "clips"                     # clip cache shared by every round of the series
    cdir.mkdir(exist_ok=True)
    temp = []
    try:
        vdur = probe_dur(raw, "v:0")
    except (KeyError, ValueError, subprocess.CalledProcessError):
        fixed = work / "raw-remux.mkv"
        sh(["ffmpeg", "-hide_banner", "-y", "-i", raw, "-c", "copy", fixed])
        raw = fixed
        temp.append(fixed)
        vdur = probe_dur(raw, "v:0")
    t0, t0_src = tl.take_t0(entry)
    end_epoch = entry.get("end_epoch") or (t0 + vdur)
    nfr_raw = int(vdur * tl.FPS + 1e-6)
    raw_start = min(max(0.0, entry["start_epoch"] - t0), vdur)
    raw_end = min(max(0.0, end_epoch - t0), nfr_raw / tl.FPS)
    events = tl.load_events(run / "events.jsonl")
    spans, rejected = tl.lapse_spans(events, entry["start_epoch"], end_epoch)
    segs = tl.build_segments(raw_start, raw_end, [(s["start"] - t0, s["end"] - t0, s["speed"]) for s in spans])
    rows = tl.output_table(segs)
    dur = tl.output_duration(segs)
    total_frames = sum(r[5] for r in rows)
    print(f"round {key}: raw {vdur:.3f}s, window raw {raw_start:.3f}-{raw_end:.3f}, t0={t0:.3f} ({t0_src}); "
          f"{len(spans)} lapse spans, {len(rejected)} kept normal; output {dur:.3f}s", flush=True)
    print(tl.fmt_table(rows), flush=True)

    clips = round_clips(events, t0, raw_start, raw_end)
    for c in clips:
        p = cdir / c["name"]
        if not p.exists():
            urllib.request.urlretrieve(f"{api}/api/commentary/audio/{c['name']}", p)
        with wave.open(str(p)) as w:
            c["wav_seconds"] = w.getnframes() / w.getframerate()
        if not c["played"] or c["played"] <= 0:
            c["played"] = c["wav_seconds"]
        c["played"] = min(c["played"], c["wav_seconds"])
        seek = c.get("seek", 0.0)
        c["play"] = max(0.0, c["played"] - seek)
        c["out_offset"] = 0.0 if seek else round(tl.remap(c["raw_offset"], segs), 4)
    clips = [c for c in clips if c["play"] > 0.05 and c["out_offset"] < dur]
    clicks_raw, dropped = round_clicks(events, t0, raw_start, raw_end, segs)
    clicks = [round(tl.remap(x, segs), 4) for x in clicks_raw]
    clicks = [x for x in clicks if 0 <= x < dur - 0.06]
    print(f"{len(clips)} clips, {len(clicks)} clicks kept, {dropped} clicks dropped inside lapse spans", flush=True)

    inputs = []
    for c in clips:
        inputs += ["-i", cdir / c["name"]]
    # 1) speech loudness, measured from the graph (no file)
    sp_parts, n_in = speech_parts(clips, dur)
    gain = 0.0
    if clips:
        (work / "speech-measure.filter").write_text(";\n".join(sp_parts + ["[sp]ebur128=peak=true[spm]"]))
        err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *map(str, inputs), "-/filter_complex",
                              str(work / "speech-measure.filter"), "-map", "[spm]", "-f", "null", "-"],
                             text=True, capture_output=True).stderr
        si, stp = ebur_text(err)
        gain = (-16.0 - si) if si > -70 else 0.0
        print(f"speech I={si} TP={stp}; gain {gain:.2f} dB", flush=True)
    # 2) clicks on the output timeline (16-bit, small)
    click = make_click(CLICK_PEAK_DB)
    total = int(round(dur * SR)) + 1
    track = np.zeros(total, dtype=np.float32)
    for off in clicks:
        s = int(round(off * SR))
        e = min(total, s + len(click))
        track[s:e] += click[: e - s]
    clicks_wav = work / "clicks.wav"
    write_wav(clicks_wav, track)
    temp.append(clicks_wav)
    del track
    # 3) one graph: speech (gain + limiter) + clicks + looped music ducked under the speech -> 24-bit FLAC premix
    lim = 10 ** (LIMIT_DB / 20)
    n_clk = n_in
    parts = list(sp_parts)
    parts.append(f"[sp]volume={gain:.3f}dB,alimiter=limit={lim:.4f}:attack=1:release=50:level=0:asc=1,"
                 f"aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[spn][key]")
    parts.append(f"[{n_clk}:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}[clk]")
    parts.append("[spn][clk]amix=inputs=2:normalize=0:duration=first[sc]")
    bed_in = []
    if music and Path(music).exists():
        fade_out = max(0.0, dur - 4)
        bed_in = ["-stream_loop", "-1", "-t", f"{dur + 1:.3f}", "-i", music]
        parts.append(f"[{n_clk + 1}:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"loudnorm=I={MUSIC_LUFS}:TP=-6:LRA=11,aresample={SR},afade=t=in:d=3,"
                     f"afade=t=out:st={fade_out:.3f}:d=4,apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}[mus]")
        parts.append("[mus][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=450:makeup=1[duck]")
        parts.append(f"[sc][duck]amix=inputs=2:normalize=0:duration=first,atrim=0:{dur:.3f},"
                     f"volume={PREMIX_HEADROOM_DB}dB[m]")
    else:
        if music:
            print(f"WARNING: music bed {music} not found; mixing without music", flush=True)
        parts.append("[key]anullsink")
        parts.append(f"[sc]atrim=0:{dur:.3f},volume={PREMIX_HEADROOM_DB}dB[m]")
    (work / "premix.filter").write_text(";\n".join(parts))
    premix = work / "premix.flac"
    temp.append(premix)
    sh(["ffmpeg", "-hide_banner", "-y", *inputs, "-i", clicks_wav, *bed_in, "-/filter_complex", work / "premix.filter",
        "-map", "[m]", "-ar", SR, "-c:a", "flac", "-sample_fmt", "s32", premix])
    # 4) loudness pass 1 (measure)
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(premix), "-af",
                          f"loudnorm=I=-16:TP={FINAL_TP}:LRA=11:print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = json.loads(out[out.rindex("{"):out.rindex("}") + 1])
    # 5) ONE video pass (lapse edit, lossless) + loudness pass 2 + mux
    vf = tl.video_filter(segs)
    af = (f"[1:a]loudnorm=I=-16:TP={FINAL_TP}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true,"
          f"aresample={SR},apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}[a]")
    (work / "final.filter").write_text(vf + ";\n" + af)
    master = run / out_name
    t_video = time.time()
    sh(["ffmpeg", "-hide_banner", "-y", "-threads", "2", "-i", raw, "-i", premix, "-/filter_complex",
        work / "final.filter", "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv444p", "-r", "30", "-threads", "2",
        "-c:a", "flac", "-sample_fmt", "s16", "-t", f"{dur:.3f}", master])
    video_secs = time.time() - t_video

    # ---- verification ---------------------------------------------------------------------------
    lines = [f"round {key} ({entry.get('stop_reason')}) raw={entry['raw']} t0={t0:.3f} ({t0_src})",
             f"window epoch {entry['start_epoch']:.3f} - {end_epoch:.3f}; raw span {raw_start:.3f} - {raw_end:.3f} s "
             f"of {vdur:.3f} s"]
    md_v, md_a = probe_dur(master, "v:0"), probe_dur(master, "a:0")
    lines.append(f"duration video={md_v:.3f}s audio={md_a:.3f}s planned={dur:.3f}s diff_av={abs(md_v - md_a):.3f}s "
                 f"({'OK' if abs(md_v - md_a) <= 0.2 and abs(md_v - dur) <= 0.2 else 'FAIL'})")
    nfr = int(sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                  "stream=nb_read_packets", "-of", "csv=p=0", master]).stdout.strip().split(",")[0])
    exp = round(md_v * 30)
    lines.append(f"frames={nfr} expected={exp} (duration x 30) planned={total_frames} "
                 f"({'OK' if abs(nfr - exp) <= 1 and abs(nfr - total_frames) <= 2 else 'CHECK'})")
    lines.append(sh(["ffprobe", "-v", "error", "-show_entries",
                     "stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,sample_rate,channels",
                     "-of", "compact", master]).stdout.strip())
    mi2, mtp2, summ = ebur(master)
    lines.append(f"loudness master I={mi2} LUFS true_peak={mtp2} dBTP "
                 f"({'OK' if abs(mi2 + 16) <= 0.5 and mtp2 < -1.5 else 'CHECK'})")
    lines.append(re.sub(r"\s+", " ", summ))
    lines.append("segments (raw seconds -> output seconds):")
    lines.append(tl.fmt_table(rows))
    for s in spans:
        lines.append(f"lapse span raw {s['start'] - t0:.3f}-{s['end'] - t0:.3f} x{s['speed']:g}")
    for s in rejected:
        lines.append(f"normal-speed tour raw {s['start'] - t0:.3f}-{s['end'] - t0:.3f} ({s['why_normal']})")
    lines.append(f"clips={len(clips)} clicks_kept={len(clicks)} clicks_dropped_in_lapse={dropped}")
    picks = sorted({0, len(clips) // 3, (2 * len(clips)) // 3, len(clips) - 1}) if clips else []
    for k in picks[:3]:
        c = clips[k]
        err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{c['out_offset'] + 0.5:.2f}", "-t", "2",
                              "-i", str(master), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                             text=True, capture_output=True).stderr
        mv = re.search(r"mean_volume: (-?[0-9.]+|-inf) dB", err)
        lines.append(f"volumedetect clip {c['name']} at out {c['out_offset']:.1f}s (raw {c['raw_offset']:.1f}s): "
                     f"mean={mv and mv.group(1)} dB")
    gdir = run / f"grabs-r{key}"
    gdir.mkdir(exist_ok=True)
    grabs = []
    lapse_rows = [r for r in rows if r[2] > 1]
    if lapse_rows:
        r = max(lapse_rows, key=lambda r: r[4] - r[3])
        grabs.append(((r[3] + r[4]) / 2, f"lapse-x{r[2]}"))
    for k in picks:
        c = clips[k]
        grabs.append((c["out_offset"] + min(2.0, c["play"] / 2), c["name"].replace(".wav", "")))
    if len(grabs) < 4:
        grabs += [(dur * f, "even") for f in (0.1, 0.35, 0.6, 0.85)][: 4 - len(grabs)]
    for at, tag in grabs[:4]:
        at = min(max(0.0, at), max(0.0, md_v - 0.1))
        out_png = gdir / f"grab-{at:07.1f}s-{tag}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(master),
                        "-frames:v", "1", str(out_png)], check=False)
        lines.append(f"grab {out_png.name}")
    lines.append(f"mix wall time {time.time() - t_start:.0f}s (video pass {video_secs:.0f}s)")
    (work / "plan.json").write_text(json.dumps({
        "key": key, "t0": t0, "t0_source": t0_src, "raw_seconds": vdur, "raw_start": raw_start, "raw_end": raw_end,
        "segments": rows, "lapse_spans": spans, "normal_tours": rejected, "output_seconds": dur, "clips": clips,
        "clicks": clicks, "clicks_dropped_in_lapse": dropped, "speech_gain_db": gain, "loudnorm_pass1": m,
        "master_I": mi2, "master_TP": mtp2, "video_pass_seconds": video_secs}, indent=2))
    (run / f"round{key}-verify.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    for p in temp:                          # the VPS disk is tight: keep plan/filters, drop big intermediates
        Path(p).unlink(missing_ok=True)
    print("MASTER", master)
    bad = [ln for ln in lines if ln.endswith("(FAIL)")]
    if bad:
        raise SystemExit("verification failed: " + "; ".join(bad))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("out_name", nargs="?", default=None)
    ap.add_argument("--round", dest="round_key", default=None,
                    help="series mode: mix this round (key from rounds.json, e.g. 5) with time-lapse spans")
    ap.add_argument("--format", choices=["mkv", "mp4"], default="mkv")
    ap.add_argument("--music", default=str(DEFAULT_BED), help="music bed (looped, ducked); '' for none")
    ap.add_argument("--port", type=int, default=18770)
    a = ap.parse_args()
    run = Path(a.run).expanduser()
    if a.round_key is not None:
        mix_round(run, a.round_key, a.music, a.port, a.out_name)
        return
    if not a.out_name:
        raise SystemExit("usage: mix.py <run_dir> <out_name> | mix.py <run_dir> --round R")
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
    if a.music and Path(a.music).exists():
        mi, mtp, _ = add_music(work, vdur, a.music)
        print(f"music bed {a.music}: mix with music I={mi} TP={mtp}", flush=True)
    elif a.music:
        print(f"WARNING: music bed {a.music} not found; mixing without music", flush=True)
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
    for p in work.glob("*.wav"):            # the VPS disk is tight: drop the big intermediates, keep plan.json
        if p.name != "click.wav":
            p.unlink(missing_ok=True)
    print("MASTER", master)


if __name__ == "__main__":
    main()

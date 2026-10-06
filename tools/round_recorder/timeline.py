#!/usr/bin/env python3
"""Pure-python timeline logic of the series recorder (no ffmpeg, no Playwright; unit tested).

Epochs are wall-clock seconds. Page events carry the page epoch `t` in milliseconds; python-side events carry
`py` in seconds. A round window is [start_epoch, end_epoch] of one per-round raw take.

- lapse spans: `director` tour start .. next tour end, clamped to the window; a span in which a commentary
  clip starts plays at normal speed; spans shorter than MIN_SPAN_S are ignored.
- fast-forward spans: every gap longer than FF_MIN_GAP that is neither busy (a host clip playing with a
  CLIP_LEAD_S lead and CLIP_TAIL_S tail, a round card or the champion overlay on screen) nor a tour span
  plays at FF_SPEED.
- segments: [(raw_start, raw_end, speed)] covering the round on the raw file's own time axis, frame aligned.
- remap(t): raw time -> output time, piecewise linear over the segments.
- RoundBoundary: decides when a round's take ends (series mode of rec.py).
"""
import json
import math

FPS = 30
MIN_SPAN_S = 8.0
DEFAULT_SPEED = 10
RR_RESULTS_PAD_S = 0.5      # cut this long after the results card closed
RR_FALLBACK_S = 90.0        # round robin round with no results-card event: cut this long after round_done
KO_TAIL_S = 20.0            # knockout round (not the last one): tail after round_done
FINAL_TAIL_S = 12.0         # after the champion line played and no clip is playing
FINAL_CAP_S = 180.0         # cap after the champion first appeared
FF_SPEED = 5                # auto fast-forward of quiet gaps between host lines
FF_MIN_GAP = 10.0           # only gaps longer than this are fast-forwarded
CLIP_LEAD_S = 1.0           # busy lead before each host clip starts
CLIP_TAIL_S = 0.5           # busy tail after each host clip ends
FF_CUE = ">> x5"            # drawn bottom centre during fast-forward segments


def ev_epoch(e):
    """Wall-clock seconds of an event (page `t` ms preferred, else python `py`)."""
    if "t" in e and e["t"] is not None:
        return float(e["t"]) / 1000.0
    if "py" in e and e["py"] is not None:
        return float(e["py"])
    return None


def load_events(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def in_window(e, start, end):
    t = ev_epoch(e)
    return t is not None and start <= t <= end


def tour_spans(events):
    """All tour spans as (start_epoch, end_epoch_or_None, speed), in time order.

    A start while a span is open closes the open span there and opens a new one (a speed change);
    an end with no open span is ignored here (window clamping handles spans that began before a window).
    """
    evs = sorted((e for e in events if e.get("kind") == "director" and e.get("k") == "tour"
                  and ev_epoch(e) is not None), key=ev_epoch)
    spans, open_ = [], None
    for e in evs:
        t = ev_epoch(e)
        if e.get("a") == "start":
            if open_ is not None:
                spans.append((open_[0], t, open_[1]))
            sp = e.get("speed")
            try:
                sp = float(sp) if sp not in (None, "") else DEFAULT_SPEED
            except (TypeError, ValueError):
                sp = DEFAULT_SPEED
            open_ = (t, sp if sp > 1 else DEFAULT_SPEED)
        elif e.get("a") == "end" and open_ is not None:
            spans.append((open_[0], t, open_[1]))
            open_ = None
    if open_ is not None:
        spans.append((open_[0], None, open_[1]))
    return spans


def clip_starts(events):
    return sorted(ev_epoch(e) for e in events if e.get("kind") == "clip_playing" and ev_epoch(e) is not None)


def lapse_spans(events, start, end, min_span=MIN_SPAN_S):
    """Valid lapse spans inside [start, end] as dicts {start, end, speed, ...}; rejected ones listed too."""
    starts = clip_starts(events)
    valid, rejected = [], []
    for s, e, sp in tour_spans(events):
        e = end if e is None else e
        cs, ce = max(s, start), min(e, end)
        if ce <= cs:
            continue
        span = {"start": cs, "end": ce, "speed": sp}
        if any(cs <= c < ce for c in starts):
            span["why_normal"] = "clip starts inside"
            rejected.append(span)
        elif ce - cs < min_span:
            span["why_normal"] = f"shorter than {min_span:g} s"
            rejected.append(span)
        else:
            valid.append(span)
    return valid, rejected


def merge_intervals(iv, join=0.0):
    """Union of (a, b) intervals; intervals closer than `join` are merged."""
    out = []
    for a, b in sorted((a, b) for a, b in iv if b > a):
        if out and a <= out[-1][1] + join:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def subtract_intervals(span, busy):
    """Pieces of span (a, b) not covered by the (merged) busy intervals."""
    a, b = span
    pieces, cur = [], a
    for x, y in merge_intervals(busy):
        if y <= cur or x >= b:
            continue
        if x > cur:
            pieces.append((cur, x))
        cur = max(cur, y)
    if cur < b:
        pieces.append((cur, b))
    return pieces


def overlay_intervals(events, start, end):
    """Round cards (rcard show..hide) and the champion overlay (show..hide), as epochs clamped to the window.

    A show while one is open keeps the first; a show with no hide lasts to the window end; a hide with no
    open show is ignored."""
    out = []
    for kind in ("rcard", "champion"):
        evs = sorted((e for e in events if e.get("kind") == "director" and e.get("k") == kind
                      and ev_epoch(e) is not None), key=ev_epoch)
        open_ = None
        for e in evs:
            t = ev_epoch(e)
            if e.get("a") == "show" and open_ is None:
                open_ = t
            elif e.get("a") == "hide" and open_ is not None:
                out.append((open_, t))
                open_ = None
        if open_ is not None:
            out.append((open_, max(end, open_)))
    return [(max(a, start), min(b, end)) for a, b in out if min(b, end) > max(a, start)]


def clip_busy(clips, lead=CLIP_LEAD_S, tail=CLIP_TAIL_S):
    """(start, end) of host clips -> busy intervals with a lead before and a tail after."""
    return [(s - lead, e + tail) for s, e in clips]


def trim_tours(tours, clip_iv, min_span=MIN_SPAN_S):
    """Tour spans (a, b, speed) minus the clip busy intervals: a tour never starts inside speech."""
    out = []
    for a, b, sp in tours:
        for x, y in subtract_intervals((a, b), clip_iv):
            if y - x >= min_span:
                out.append((x, y, sp))
    return out


def ff_spans(start, end, busy, tours, speed=FF_SPEED, min_gap=FF_MIN_GAP):
    """Fast-forward spans (a, b, speed): gaps of [start, end] outside busy intervals and tour spans that
    are longer than min_gap."""
    blocked = list(busy) + [(a, b) for a, b, *_ in tours]
    return [(a, b, speed) for a, b in subtract_intervals((start, end), blocked) if b - a > min_gap]


def plan_spans(events, t0, start_epoch, end_epoch, raw_start, raw_end, clip_raw, fast_forward=True):
    """All sped-up spans of a round on the raw axis.

    clip_raw: [(start, end)] of the host clips on the raw axis. Returns a dict with tours (x10, trimmed so
    none starts inside speech), rejected tours, ff spans (FF_SPEED), the busy intervals and the segments
    [(a, b, speed, kind)]."""
    valid, rejected = lapse_spans(events, start_epoch, end_epoch)
    civ = merge_intervals(clip_busy(clip_raw))
    tours = trim_tours([(s["start"] - t0, s["end"] - t0, s["speed"]) for s in valid], civ)
    busy = merge_intervals(civ + [(a - t0, b - t0) for a, b in overlay_intervals(events, start_epoch, end_epoch)])
    ffs = ff_spans(raw_start, raw_end, busy, tours) if fast_forward else []
    segk = build_segments_k(raw_start, raw_end, [(a, b, s, "tour") for a, b, s in tours]
                            + [(a, b, s, "ff") for a, b, s in ffs])
    return {"tours": tours, "rejected": rejected, "ff": ffs, "busy": busy, "segments": segk}


def _snap(x, fps=FPS):
    return round(x * fps) / fps


def build_segments_k(raw_start, raw_end, spans_raw, fps=FPS):
    """Segments [(a, b, speed, kind)] covering [raw_start, raw_end] on the raw time axis.

    spans_raw: [(a, b, speed[, kind])] on the raw axis (already clamped, valid); kind defaults to "tour",
    normal-speed segments have kind "normal". All boundaries are snapped to frames; a sped-up segment's frame
    count is trimmed to a multiple of its (integer) speed so it decimates to a whole number of output frames.
    Overlapping spans: the later one starts after the earlier ends.
    """
    raw_start, raw_end = _snap(raw_start, fps), _snap(raw_end, fps)
    segs, cur = [], raw_start
    for span in sorted(spans_raw, key=lambda s: (s[0], s[1])):
        a, b, sp = span[0], span[1], span[2]
        kind = span[3] if len(span) > 3 else "tour"
        a, b = max(_snap(a, fps), cur), min(_snap(b, fps), raw_end)
        sp = max(1, int(round(sp)))
        if sp > 1:
            n = int(round((b - a) * fps))
            n -= n % sp
            b = a + n / fps
        if b - a <= 0 or sp <= 1:
            continue
        if a > cur:
            segs.append((cur, a, 1, "normal"))
        segs.append((a, b, sp, kind))
        cur = b
    if raw_end > cur:
        segs.append((cur, raw_end, 1, "normal"))
    return [(round(a, 6), round(b, 6), s, k) for a, b, s, k in segs]


def build_segments(raw_start, raw_end, spans_raw, fps=FPS):
    """Segments [(a, b, speed)] (see build_segments_k)."""
    return [s[:3] for s in build_segments_k(raw_start, raw_end, spans_raw, fps)]


def seg_frames(seg, fps=FPS):
    a, b, s = seg
    return int(round((b - a) * fps)) // s


def output_table(segs, fps=FPS):
    """[(a, b, speed, out_a, out_b, frames)] with frame-exact output spans."""
    rows, out = [], 0
    for seg in segs:
        n = seg_frames(seg, fps)
        rows.append((seg[0], seg[1], seg[2], out / fps, (out + n) / fps, n))
        out += n
    return rows


def output_duration(segs, fps=FPS):
    return sum(seg_frames(s, fps) for s in segs) / fps


def remap(t, segs, fps=FPS):
    """Raw time -> output time. Before the first segment: negative offset kept; after the last: past the end."""
    if not segs:
        return t
    rows = output_table(segs, fps)
    if t <= rows[0][0]:
        return t - rows[0][0]
    for a, b, s, oa, ob, _n in rows:
        if t <= b:
            return oa + (t - a) / s
    a, b, s, oa, ob, _n = rows[-1]
    return ob + (t - b)


def in_lapse(t, segs):
    return any(s > 1 and a <= t < b for a, b, s in segs)


def cue_filter(text, fontfile):
    """drawtext in the page's gold ribbon style, bottom centre."""
    txt = text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return (f"drawtext=fontfile='{fontfile}':text='{txt}':fontsize=30:fontcolor=0xffd479:"
            f"box=1:boxcolor=0x0e1013@0.94:boxborderw=12|26:x=(w-text_w)/2:y=h-text_h-34")


def video_filter(segs, fps=FPS, in_label="0:v", out_label="v", kinds=None, cue=None):
    """filter_complex text: split + per-segment trim (frame exact) + setpts/S (+ fps for S>1) + concat.
    kinds: per segment kind; `cue` (a filter string) is appended to the "ff" segments only."""
    n = len(segs)
    lines = [f"[{in_label}]split={n}" + "".join(f"[s{k}]" for k in range(n))] if n > 1 else []
    for k, seg in enumerate(segs):
        a, b, s = seg[0], seg[1], seg[2]
        src = f"[s{k}]" if n > 1 else f"[{in_label}]"
        fa, fb = int(round(a * fps)), int(round(b * fps))
        chain = f"{src}trim=start_frame={fa}:end_frame={fb},setpts=(PTS-STARTPTS)/{s}"
        if s > 1:
            chain += f",fps={fps}"
        if cue and kinds and kinds[k] == "ff":
            chain += "," + cue
        lines.append(chain + f"[g{k}]")
    lines.append("".join(f"[g{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[{out_label}]")
    return ";\n".join(lines)


# ---- round windows ----------------------------------------------------------------------------

def take_t0(entry):
    """Raw time zero of a take (x11grab input start, else the ffmpeg popen epoch)."""
    t0 = entry.get("ffmpeg_input_start")
    p = entry.get("ffmpeg_popen_epoch")
    if not t0 or (p and abs(t0 - p) > 5):
        return p, "popen"
    return t0, "x11grab start"


def find_entry(rounds, key):
    key = str(key)
    for e in rounds:
        if str(e.get("key", e.get("round"))) == key:
            return e
    return None


def window_events(events, entry):
    s, e = entry["start_epoch"], entry["end_epoch"]
    return [x for x in events if in_window(x, s, e)]


# ---- round boundary state machine ---------------------------------------------------------------

def round_done(st):
    """Every game of the round has a result and the round status is finished."""
    if not st or not st.get("exists") or not st.get("games"):
        return False
    games_done = all(g.get("status") != "live" and g.get("result") not in (None, "", "*") for g in st["games"])
    return games_done and st.get("status") == "finished"


class RoundBoundary:
    """When does round `rnd`'s take end? Feed polls (state dicts) and director events; ask cut_at().

    Round robin round (no stage): at the results card hide for this round + 0.5 s; fallback round_done + 90 s.
    Knockout round that is not the final: round_done + 20 s (a reopen, e.g. an Armageddon decider, resets).
    Final round (stage "final"): champion set, champion overlay shown, champion line heard, no clip playing,
    then 12 s; cap 180 s after the champion first appeared.
    A round robin round after which the tournament is finished (no knockouts) also ends the series.
    """

    def __init__(self, rnd):
        self.rnd = rnd
        self.stage = None
        self.done_at = None
        self.results_hide_at = None
        self.champ_at = None
        self.champ_shown_at = None
        self.quiet_since = None
        self.events = []          # (epoch, name) log for rounds.json / debugging
        self.reopened = 0

    @property
    def is_final(self):
        return self.stage == "final"

    def on_director(self, d, epoch):
        k, a = d.get("k"), d.get("a")
        if k == "rcard" and a == "hide" and d.get("mode") == "results" and _int(d.get("round")) == self.rnd:
            if self.results_hide_at is None:
                self.results_hide_at = epoch
                self.events.append((epoch, "results_hide"))
        elif k == "champion" and a == "show" and self.champ_shown_at is None:
            self.champ_shown_at = epoch
            self.events.append((epoch, "champion_show"))

    def on_poll(self, st, now):
        """st: {exists, status, games, stage, champion, heard_champion, clip_playing, finished, last_round}.
        Returns 'round_done' / 'round_reopened' / None (a transition to log)."""
        if not st:
            return None
        if st.get("exists"):
            self.stage = st.get("stage") or None
        if st.get("champion") and self.champ_at is None and self.is_final:
            self.champ_at = now
            self.events.append((now, "champion_set"))
        done = round_done(st)
        if self.done_at is None and done:
            self.done_at = now
            self.events.append((now, "round_done"))
            return "round_done"
        if self.done_at is not None and not done and st.get("exists"):
            self.done_at = None
            self.reopened += 1
            self.events.append((now, "round_reopened"))
            return "round_reopened"
        if self.is_final and self.champ_at is not None:
            quiet = (self.champ_shown_at is not None and st.get("heard_champion") and not st.get("clip_playing"))
            if quiet and self.quiet_since is None:
                self.quiet_since = now
            elif not quiet:
                self.quiet_since = None
        return None

    def cut_at(self, st, now):
        """(epoch, reason) when the take should end, or None. The epoch may be slightly in the future."""
        if self.is_final:
            if self.champ_at is None:
                return None
            if self.quiet_since is not None and now - self.quiet_since >= FINAL_TAIL_S:
                return now, "tournament_over"
            if now - self.champ_at >= FINAL_CAP_S:
                return now, "tournament_over_cap"
            return None
        if self.stage:                                   # knockout round, not the final
            if self.done_at is not None and now - self.done_at >= KO_TAIL_S:
                return now, "ko_round_done"
            return None
        if self.results_hide_at is not None:
            return max(now, self.results_hide_at + RR_RESULTS_PAD_S), "results_card_closed"
        if self.done_at is not None and now - self.done_at >= RR_FALLBACK_S:
            return now, "round_done_fallback"
        return None

    def series_over_after(self, st):
        """True when no take should follow this round."""
        if self.is_final:
            return True
        return bool(st and st.get("finished") and not self.stage and (st.get("last_round") or 0) <= self.rnd)


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def fmt_table(rows, kinds=None):
    out = ["raw_start  raw_end    speed  out_start  out_end    frames" + ("  kind" if kinds else "")]
    for k, (a, b, s, oa, ob, n) in enumerate(rows):
        out.append(f"{a:9.3f}  {b:9.3f}  {s:>5}  {oa:9.3f}  {ob:9.3f}  {n:>6}" + (f"  {kinds[k]}" if kinds else ""))
    return "\n".join(out)


def isclose(a, b, tol=1e-6):
    return math.isclose(a, b, abs_tol=tol)

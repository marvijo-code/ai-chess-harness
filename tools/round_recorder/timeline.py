#!/usr/bin/env python3
"""Pure-python timeline logic of the series recorder (no ffmpeg, no Playwright; unit tested).

Epochs are wall-clock seconds. Page events carry the page epoch `t` in milliseconds; python-side events carry
`py` in seconds. A round window is [start_epoch, end_epoch] of one per-round raw take.

- lapse spans: `director` tour start .. next tour end, clamped to the window; a span in which a commentary
  clip starts plays at normal speed; spans shorter than MIN_SPAN_S are ignored.
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


def _snap(x, fps=FPS):
    return round(x * fps) / fps


def build_segments(raw_start, raw_end, spans_raw, fps=FPS):
    """Segments [(a, b, speed)] covering [raw_start, raw_end] on the raw time axis.

    spans_raw: [(a, b, speed)] on the raw axis (already clamped, valid). All boundaries are snapped to
    frames; a lapse segment's frame count is trimmed to a multiple of its (integer) speed so it decimates
    to a whole number of output frames. Overlapping spans: the later one starts after the earlier ends.
    """
    raw_start, raw_end = _snap(raw_start, fps), _snap(raw_end, fps)
    segs, cur = [], raw_start
    for a, b, sp in sorted(spans_raw):
        a, b = max(_snap(a, fps), cur), min(_snap(b, fps), raw_end)
        sp = max(1, int(round(sp)))
        if sp > 1:
            n = int(round((b - a) * fps))
            n -= n % sp
            b = a + n / fps
        if b - a <= 0 or sp <= 1:
            continue
        if a > cur:
            segs.append((cur, a, 1))
        segs.append((a, b, sp))
        cur = b
    if raw_end > cur:
        segs.append((cur, raw_end, 1))
    return [(round(a, 6), round(b, 6), s) for a, b, s in segs]


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


def video_filter(segs, fps=FPS, in_label="0:v", out_label="v"):
    """filter_complex text: split + per-segment trim (frame exact) + setpts/S (+ fps for S>1) + concat."""
    n = len(segs)
    lines = [f"[{in_label}]split={n}" + "".join(f"[s{k}]" for k in range(n))] if n > 1 else []
    for k, (a, b, s) in enumerate(segs):
        src = f"[s{k}]" if n > 1 else f"[{in_label}]"
        fa, fb = int(round(a * fps)), int(round(b * fps))
        chain = f"{src}trim=start_frame={fa}:end_frame={fb},setpts=(PTS-STARTPTS)/{s}"
        if s > 1:
            chain += f",fps={fps}"
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


def fmt_table(rows):
    out = ["raw_start  raw_end    speed  out_start  out_end    frames"]
    for a, b, s, oa, ob, n in rows:
        out.append(f"{a:9.3f}  {b:9.3f}  {s:>5}  {oa:9.3f}  {ob:9.3f}  {n:>6}")
    return "\n".join(out)


def isclose(a, b, tol=1e-6):
    return math.isclose(a, b, abs_tol=tol)

#!/usr/bin/env python3
"""FirstNaz video edit pipeline. Free local tools only: ffmpeg, numpy, Whisper.

Commands (run from anywhere; PROJECT is a project folder with raw/ and broll/):
  new NAME                   make projects/NAME with raw/, broll/, brief.md
  probe PROJECT              inventory every file, guess which camera has the lav audio
  sync PROJECT               line up every angle against the master audio
  transcribe PROJECT         word-level transcript (mlx-whisper on Apple Silicon)
  color PROJECT              match each angle's color to the master angle
  stills PROJECT [T ...]     contact sheet of all angles at master times T (for a quick look)
  check PROJECT              validate work/plan.json
  render PROJECT [--short] [--preview]
                             build out/final.mp4 (or out/short.mp4) from work/plan.json
  music [seed|fetch MOOD|scan|pick MOOD]   self-managing music library (see music.py)
  upload login | upload PATH [--dry-run]   send finished videos to YouTube (see upload.py)

All times in plan.json are MASTER times: seconds on the master angle's clock,
the same clock as the transcript.
"""
import json, math, os, re, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())
# Data root (projects, music). The `edit` wrapper on the drive sets FIRSTNAZ_VIDEO_ROOT to its own folder,
# so footage lives on the external drive and nothing breaks if the drive is renamed.
ROOT = Path(os.path.expanduser(os.environ.get("FIRSTNAZ_VIDEO_ROOT") or CONFIG["root"]))
if str(ROOT).startswith("/Volumes/") and not ROOT.exists():
    sys.exit(f"{ROOT} not found. Is the external drive plugged in and mounted?")
VIDEO_EXT = {".mp4", ".mov", ".mxf", ".m4v", ".mts", ".avi", ".mkv"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic"}


# ---------- helpers ----------
def run(cmd, quiet=True):
    cmd = [str(c) for c in cmd]
    if cmd[0] == "ffmpeg":
        cmd[1:1] = ["-hide_banner", "-y", "-loglevel", "error", "-stats"] if not quiet else ["-hide_banner", "-y", "-loglevel", "error"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd[:12])}...\n{r.stderr[-3000:]}")
    return r.stdout


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    return json.loads(out)


def project_dirs(p):
    p = Path(p).expanduser()
    if not p.is_absolute() and not p.exists():
        p = ROOT / "projects" / p
    work = p / "work"; out = p / "out"
    work.mkdir(exist_ok=True); out.mkdir(exist_ok=True)
    return p, work, out


def load(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def save(path, data):
    Path(path).write_text(json.dumps(data, indent=2))


def hms(t):
    t = max(0, t)
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def ass_time(t):
    t = max(0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def pcm(path, start=0.0, dur=None, rate=8000):
    """Mono float32 samples from any file's first audio stream."""
    import numpy as np
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, start):.3f}"]
    if dur: cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.float32)


def angle_file(work, plan_or_inv, angle):
    """Each angle is one continuous stream; chunked camera files are joined once into work/."""
    files = plan_or_inv["angles"][angle]["files"]
    if len(files) == 1:
        return Path(files[0])
    joined = work / f"angle_{angle}{Path(files[0]).suffix}"
    if not joined.exists():
        lst = work / f"angle_{angle}.txt"
        lst.write_text("".join(f"file '{Path(f).as_posix()}'\n" for f in files))
        run(["ffmpeg", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
    return joined


# ---------- new ----------
def cmd_new(name):
    p = ROOT / "projects" / name
    for d in ("raw", "broll", "work", "out"): (p / d).mkdir(parents=True, exist_ok=True)
    brief = p / "brief.md"
    if not brief.exists():
        brief.write_text((HERE / "brief-template.md").read_text())
    print(p)


# ---------- probe ----------
def cmd_probe(project):
    p, work, _ = project_dirs(project)
    items = []
    files = [f for d in ("raw", "broll") if (p / d).exists() for f in sorted((p / d).rglob("*"))]
    for f in files:
        if not f.is_file() or f.name.startswith("."): continue
        ext = f.suffix.lower()
        kind = "image" if ext in IMAGE_EXT else "audio" if ext in AUDIO_EXT else "video" if ext in VIDEO_EXT else None
        if not kind: continue
        info = probe(f)
        v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
        a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
        item = {"file": str(f), "folder": f.parent.name, "kind": kind,
                "duration": round(float(info["format"].get("duration", 0)), 2),
                "created": info["format"].get("tags", {}).get("creation_time")}
        if v and kind == "video":
            num, den = (v.get("avg_frame_rate") or "0/1").split("/")
            item.update(width=v["width"], height=v["height"], codec=v["codec_name"],
                        fps=round(int(num) / int(den), 3) if int(den) else None,
                        rotation=next((sd.get("rotation") for sd in v.get("side_data_list", []) if "rotation" in sd), 0),
                        transfer=v.get("color_transfer"))
        if a and kind in ("video", "audio"):
            item["audio_clarity"] = clarity(f, item["duration"])
        items.append(item)
    raw = [i for i in items if "/raw/" in i["file"].replace("\\", "/") and i["kind"] == "video"]
    guess = max(raw, key=lambda i: i.get("audio_clarity") or 0)["file"] if raw else None
    inv = {"items": items, "master_audio_guess": guess}
    save(work / "inventory.json", inv)
    for i in items:
        extra = f"{i.get('width')}x{i.get('height')} {i.get('fps')}fps {i.get('codec')}" if i["kind"] == "video" else ""
        print(f"{i['kind']:5} {hms(i['duration']):>8}  clarity={i.get('audio_clarity', '-'):<5} {extra}  {i['file']}")
    print(f"\nmaster audio guess (cleanest speech): {guess}")
    print(f"wrote {work / 'inventory.json'}")


def clarity(path, duration):
    """Speech clarity score: loud-frame RMS vs noise-floor RMS (dB). A lav scores much higher than a camera mic."""
    import numpy as np
    x = pcm(path, start=min(30, duration * 0.1), dur=min(180, duration))
    if len(x) < 8000: return 0
    frames = x[: len(x) // 400 * 400].reshape(-1, 400)
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-7
    return round(float(20 * np.log10(np.percentile(rms, 95) / np.percentile(rms, 10))), 1)


# ---------- sync ----------
def cmd_sync(project):
    import numpy as np
    p, work, _ = project_dirs(project)
    plan = load(work / "plan.json")
    if not plan: sys.exit("work/plan.json missing: write the angles block first (see SKILL.md)")
    master = plan["master"]
    m_file = angle_file(work, plan, master)
    rate = 8000
    ref = pcm(m_file, 0, 600, rate)
    for ang, a in plan["angles"].items():
        if ang == master:
            a["offset"] = 0.0; continue
        sig = pcm(angle_file(work, plan, ang), 0, 900, rate)
        n = 1 << int(math.ceil(math.log2(len(ref) + len(sig))))
        R = np.fft.rfft(ref, n); S = np.fft.rfft(sig, n)
        X = S * np.conj(R)
        cc = np.fft.irfft(X / (np.abs(X) + 1e-9), n)  # GCC-PHAT: sharp peak, robust to mic differences
        cc = np.concatenate([cc[-(len(ref) - 1):], cc[: len(sig)]])
        k = int(np.argmax(cc)); lag = (k - (len(ref) - 1)) / rate
        conf = float(cc[k] / (np.abs(cc).mean() + 1e-12))
        # refine at 48k around the coarse lag
        fine = refine(m_file, angle_file(work, plan, ang), lag)
        a["offset"] = round(fine, 4); a["sync_confidence"] = round(conf, 1)
        flag = "OK" if conf > 15 else "LOW: check by eye/ear"
        print(f"angle {ang}: offset {a['offset']:+.3f}s (source time = master time + offset)  confidence {conf:.0f} {flag}")
    save(work / "plan.json", plan)


def refine(m_file, s_file, lag, rate=48000, win=20.0):
    """Sample-accurate pass in a window both files cover, +-0.25s around the coarse lag."""
    import numpy as np
    md = float(probe(m_file)["format"]["duration"]); sd = float(probe(s_file)["format"]["duration"])
    lo, hi = max(0.3, 0.3 - lag), min(md, sd - lag) - 0.3   # master times covered by both
    if hi - lo < 3: return lag
    win = min(win, hi - lo)
    t0 = lo + (hi - lo - win) * 0.4
    ref = pcm(m_file, t0, win, rate)
    sig = pcm(s_file, t0 + lag - 0.25, win + 0.5, rate)
    if len(ref) < rate or len(sig) < len(ref) + rate // 4: return lag
    n = 1 << int(math.ceil(math.log2(len(ref) + len(sig))))
    X = np.fft.rfft(sig, n) * np.conj(np.fft.rfft(ref, n))
    cc = np.fft.irfft(X / (np.abs(X) + 1e-9), n)[: len(sig) - len(ref) + 1]
    return lag - 0.25 + int(np.argmax(cc)) / rate


# ---------- transcribe ----------
def cmd_transcribe(project):
    p, work, _ = project_dirs(project)
    plan = load(work / "plan.json")
    src = angle_file(work, plan, plan["master"])
    wav = work / "master.wav"
    run(["ffmpeg", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
    model = CONFIG.get("whisper_model", "mlx-community/whisper-large-v3-turbo")
    words, segs = [], []
    try:
        import mlx_whisper
        res = mlx_whisper.transcribe(str(wav), path_or_hf_repo=model, word_timestamps=True,
                                     initial_prompt=CONFIG.get("whisper_prompt"))
        for s in res["segments"]:
            segs.append({"start": s["start"], "end": s["end"], "text": s["text"].strip()})
            words += [{"w": w["word"].strip(), "s": w["start"], "e": w["end"]} for w in s.get("words", [])]
    except ImportError:
        from faster_whisper import WhisperModel  # fallback for non-Apple machines
        m = WhisperModel(CONFIG.get("whisper_fallback_model", "small"), compute_type="int8")
        it, _ = m.transcribe(str(wav), word_timestamps=True, initial_prompt=CONFIG.get("whisper_prompt"))
        for s in it:
            segs.append({"start": s.start, "end": s.end, "text": s.text.strip()})
            words += [{"w": w.word.strip(), "s": w.start, "e": w.end} for w in (s.words or [])]
    save(work / "transcript.json", {"segments": segs, "words": words})
    write_transcript_txt(work, segs)
    print(f"{len(segs)} lines, {len(words)} words -> {work / 'transcript.txt'}")


def write_transcript_txt(work, segs):
    """Compact transcript Claude reads to plan the edit. Gaps > 1.5s are marked (dead air, resets, flubs)."""
    lines, prev = [], 0.0
    for s in segs:
        if s["start"] - prev > 1.5:
            lines.append(f"   ... {s['start'] - prev:.1f}s gap ...")
        lines.append(f"[{s['start']:7.1f}-{s['end']:7.1f}] {s['text']}")
        prev = s["end"]
    (work / "transcript.txt").write_text("\n".join(lines) + "\n")


# ---------- color ----------
def frames_rgb(path, times, w=160, h=90):
    import numpy as np
    out = []
    for t in times:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, t):.2f}", "-i", str(path),
                            "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True)
        if len(r.stdout) == w * h * 3:
            out.append(np.frombuffer(r.stdout, np.uint8).reshape(h, w, 3).astype(float))
    return np.stack(out) if out else None


def cmd_color(project):
    """Per-channel tone match (quantile fit), measured at the same moments on every angle.
    Fixes white balance, exposure and contrast differences. Clamped so a framing difference
    (wide vs tight) can't produce a wild correction."""
    p, work, _ = project_dirs(project)
    plan = load(work / "plan.json")
    master = plan["master"]
    mf = angle_file(work, plan, master)
    dur = float(probe(mf)["format"]["duration"])
    times = [dur * k / 9 for k in range(1, 9)]
    kept = [(s["start"], s["end"]) for s in (plan.get("main") or {}).get("segments", [])]
    if kept:  # once a cut exists, sample only what's kept: false starts and empty frames skew the match
        total = sum(e - s for s, e in kept)
        times = []
        for k in range(1, 9):
            pos = total * k / 9
            for s, e in kept:
                if pos <= e - s: times.append(s + pos); break
                pos -= e - s
    for ang, a in plan["angles"].items():
        if ang == master: continue
        f = angle_file(work, plan, ang)
        sd = float(probe(f)["format"]["duration"])
        ts = [t for t in times if 0 <= t + a.get("offset", 0) < sd]
        img = frames_rgb(f, [t + a.get("offset", 0) for t in ts])
        r = frames_rgb(mf, ts)
        if img is None or r is None: continue
        import numpy as np
        lut, q = {}, np.linspace(3, 97, 33)
        for c, name in enumerate("rgb"):
            qa, qb = np.percentile(r[..., c], q), np.percentile(img[..., c], q)
            gain, off = np.polyfit(qb, qa, 1)   # least squares across the tonal range
            gain = float(min(1.25, max(0.8, gain)))
            off = float(np.median(qa - gain * qb))
            lut[name] = [round(gain, 4), round(max(-40, min(40, off)), 2)]
        a["color_match"] = lut
        print(f"angle {ang} -> match {master}: " + ", ".join(f"{k}: x{v[0]} {v[1]:+}" for k, v in lut.items()))
    save(work / "plan.json", plan)
    print("Run `stills` to eyeball it. Fine-tune with angles.<X>.adjust in plan.json.")


def color_filter(angle_cfg, look=None):
    f = []
    cm = angle_cfg.get("color_match")
    if cm:
        f.append("lutrgb=" + ":".join(f"{c}='clip(val*{g}+({o}),0,255)'" for c, (g, o) in cm.items()))
    for adj in (angle_cfg.get("adjust"), look):
        if not adj: continue
        eq = {k: adj[k] for k in ("brightness", "contrast", "saturation", "gamma") if k in adj}
        if eq: f.append("eq=" + ":".join(f"{k}={v}" for k, v in eq.items()))
        if adj.get("warmth"):  # -1 cool .. +1 warm
            w = adj["warmth"] * 0.12
            f.append(f"colorbalance=rm={w:.3f}:bm={-w:.3f}:rh={w/2:.3f}:bh={-w/2:.3f}")
    return f


# ---------- stills ----------
def cmd_stills(project, times):
    p, work, _ = project_dirs(project)
    plan = load(work / "plan.json")
    mf = angle_file(work, plan, plan["master"])
    if not times:
        dur = float(probe(mf)["format"]["duration"]); times = [dur * 0.3, dur * 0.6]
    rows = []
    for i, t in enumerate(map(float, times)):
        tiles = []
        for ang, a in plan["angles"].items():
            for graded in (False, True):
                if graded and ang == plan["master"]: continue
                img = work / f"_still_{i}_{ang}_{int(graded)}.png"
                vf = ["scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2"]
                if graded: vf = color_filter(a, plan.get("look")) + vf
                vf.append(f"drawtext=text='{ang}{' graded' if graded else ''}  {t:.1f}s':x=10:y=10:fontsize=22:fontcolor=white:box=1:boxcolor=black@0.6"
                          + font_opt())
                run(["ffmpeg", "-ss", f"{t + a.get('offset', 0):.2f}", "-i", angle_file(work, plan, ang),
                     "-frames:v", "1", "-vf", ",".join(vf), img])
                if not img.exists(): sys.exit(f"no frame for angle {ang} at master {t:.1f}s (outside its footage?)")
                tiles.append(img)
        row = work / f"_row_{i}.png"
        run(["ffmpeg", *sum((["-i", x] for x in tiles), []), "-filter_complex", f"hstack=inputs={len(tiles)}" if len(tiles) > 1 else "null", row])
        rows.append(row)
    sheet = work / "stills.jpg"
    run(["ffmpeg", *sum((["-i", x] for x in rows), []), "-filter_complex", f"vstack=inputs={len(rows)}" if len(rows) > 1 else "null", "-q:v", "3", sheet])
    for f in work.glob("_*.png"): f.unlink()
    print(sheet)


def font_opt():
    ff = CONFIG.get("font_file")
    for cand in [ff, "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if cand and Path(cand).exists():
            return f":fontfile='{cand}'"
    return ""


# ---------- timeline ----------
class Timeline:
    """Maps master time -> output time across the kept segments (plus an intro offset)."""
    def __init__(self, segments, lead=0.0):
        self.map, t = [], lead
        for s in segments:
            self.map.append((s["start"], s["end"], t)); t += s["end"] - s["start"]
        self.end = t

    def out(self, mt):
        for a, b, o in self.map:
            if a - 1e-3 <= mt <= b + 1e-3: return o + (mt - a)
        return None


def check(plan, section, work):
    errs = []
    segs = section["segments"]
    for i, s in enumerate(segs):
        if s["end"] <= s["start"]: errs.append(f"segment {i}: end before start")
        ang = s.get("angle", plan["master"])
        if ang not in plan["angles"]: errs.append(f"segment {i}: unknown angle {ang}"); continue
        a = plan["angles"][ang]
        dur = float(probe(angle_file(work, plan, ang))["format"]["duration"])
        if s["start"] + a.get("offset", 0) < 0 or s["end"] + a.get("offset", 0) > dur + 0.05:
            errs.append(f"segment {i} ({hms(s['start'])}-{hms(s['end'])}): angle {ang} has no footage there")
        if i and s["start"] < segs[i - 1]["end"] - 1e-3: errs.append(f"segment {i}: overlaps the previous one")
        if i and abs(s["start"] - segs[i - 1]["end"]) > 0.05 and ang == segs[i - 1].get("angle", plan["master"]) \
                and not s.get("zoom") and not segs[i - 1].get("zoom"):
            errs.append(f"WARN segment {i}: jump cut on the same angle at {hms(s['start'])} (switch angle, punch in, or cover with b-roll)")
    for b in section.get("broll", []):
        if not Path(b["file"]).exists(): errs.append(f"b-roll missing: {b['file']}")
    m = section.get("music")
    if m and m.get("file"):
        mf = Path(m["file"]); mf = mf if mf.is_absolute() else ROOT / "music" / mf
        if not mf.exists(): errs.append(f"music missing: {m['file']}")
    return errs


def cmd_check(project):
    p, work, _ = project_dirs(project)
    plan = load(work / "plan.json")
    ok = True
    for key in ("main", "short"):
        if key in plan:
            errs = check(plan, plan[key], work)
            tl = Timeline(plan[key]["segments"])
            print(f"{key}: {len(plan[key]['segments'])} segments, {hms(tl.end)} before intro/outro")
            for e in errs:
                print("  !", e); ok = ok and e.startswith("WARN")
    print("plan OK" if ok else "fix the issues above")


# ---------- captions / lower thirds (one ASS file) ----------
def build_ass(plan, sec, tl, W, H, path, vertical):
    brand = CONFIG["brand"]
    font = brand.get("caption_font", "Helvetica Neue")
    def col(hexrgb, alpha="00"):
        h = hexrgb.lstrip("#"); return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}"
    cap_size = int(H * (0.042 if vertical else 0.048))
    styles = [
        # Caption: white bold text, soft black box (BorderStyle 3)
        f"Style: Cap,{font},{cap_size},{col('#FFFFFF')},{col('#FFFFFF')},{col('#000000','60')},{col('#000000','60')},"
        f"-1,0,0,0,100,100,0,0,{'3,10,0' if not vertical else '1,6,2'},2,60,60,{int(H*(0.30 if vertical else 0.07))},1",
        f"Style: LTName,{font},{int(H*0.045)},{col('#FFFFFF')},{col('#FFFFFF')},{col(brand['accent'])},{col('#000000','A0')},"
        f"-1,0,0,0,100,100,0,0,3,10,0,1,{int(W*0.06)},60,{int(H*(0.22 if vertical else 0.16))},1",
        f"Style: LTTitle,{font},{int(H*0.030)},{col('#000000')},{col('#000000')},{col('#FFFFFF')},{col('#000000','A0')},"
        f"0,0,0,0,100,100,0,0,3,8,0,1,{int(W*0.06)},60,{int(H*(0.22 if vertical else 0.16)) - int(H*0.07)},1",
    ]
    ev = []
    for lt in sec.get("lower_thirds", []):
        t = tl.out(lt["at"])
        if t is None: continue
        d = lt.get("dur", 5)
        fade = r"{\fad(300,300)}"
        ev.append(f"Dialogue: 1,{ass_time(t)},{ass_time(t+d)},LTName,,0,0,0,,{fade}{lt['name']}")
        if lt.get("title"):  # own layer so libass doesn't restack it above the name
            ev.append(f"Dialogue: 2,{ass_time(t+0.15)},{ass_time(t+d)},LTTitle,,0,0,0,,{fade}{lt['title']}")
    if sec.get("captions"):
        tr = load(Path(path).parent / "transcript.json", {"words": []})
        max_words = 3 if vertical else 7
        chunk = []
        def flush():
            if not chunk: return
            s, e = tl.out(chunk[0]["s"]), tl.out(chunk[-1]["e"])
            if s is not None and e is not None and e > s:
                text = " ".join(w["w"] for w in chunk)
                if vertical: text = text.upper()
                ev.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e + 0.05)},Cap,,0,0,0,,{text}")
            chunk.clear()
        for w in tr["words"]:
            if tl.out(w["s"]) is None or tl.out(w["e"]) is None:
                flush(); continue
            if chunk and (w["s"] - chunk[-1]["e"] > 0.6 or len(chunk) >= max_words
                          or len(" ".join(x["w"] for x in chunk)) > (18 if vertical else 40)):
                flush()
            chunk.append(w)
            if re.search(r"[.?!,]$", w["w"]): flush()
        flush()
    if not ev: return None
    Path(path).write_text(
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles) + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(ev) + "\n")
    return path


# ---------- render ----------
def frame_filter(W, H, zoom=1.0, cx=0.5, cy=0.5):
    """Fill the frame (crop, never letterbox). zoom>1 punches in, a free 'third angle' on 4K or wide shots."""
    z = max(1.0, zoom)
    return (f"scale={W}*{z}:{H}*{z}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H}:(iw-{W})*{cx}:(ih-{H})*{cy},setsar=1")


def card(path, W, H, fps, dur, enc, text=None):
    """Logo card on brand background, fades in and out. Silent audio track so concat stays uniform."""
    brand = CONFIG["brand"]
    logo = Path(os.path.expanduser(brand["logo"]))
    if not logo.is_absolute(): logo = HERE.parent / logo
    size = int(min(W, H) * 0.32)
    inv = ",lutrgb=r=negval:g=negval:b=negval" if brand.get("logo_invert") else ""
    vf = (f"[1:v]scale={size}:-1,format=rgba{inv}[l];[0:v][l]overlay=(W-w)/2:(H-h)/2-{int(H*0.03) if text else 0}")
    if text:
        vf += f",drawtext=text='{text}':x=(w-text_w)/2:y=(h/2)+{size//2}:fontsize={int(H*0.035)}:fontcolor=white{font_opt()}"
    vf += f",fade=in:st=0:d=0.6,fade=out:st={dur-0.6}:d=0.6,fps={fps},format=yuv420p[v]"
    run(["ffmpeg", "-f", "lavfi", "-i", f"color=c={brand['background']}:s={W}x{H}:r={fps}:d={dur}",
         "-loop", "1", "-t", dur, "-i", logo, "-f", "lavfi", "-t", dur, "-i", "anullsrc=r=48000:cl=stereo",
         "-filter_complex", vf, "-map", "[v]", "-map", "2:a", *enc, "-c:a", "pcm_s16le", "-t", dur, path])


def cmd_render(project, short=False, preview=False):
    p, work, out = project_dirs(project)
    plan = load(work / "plan.json")
    key = "short" if short else "main"
    sec = plan[key]
    errs = [e for e in check(plan, sec, work) if not e.startswith("WARN")]
    if errs: sys.exit("plan has problems:\n  " + "\n  ".join(errs))
    vertical = short
    W, H = (1080, 1920) if vertical else (1920, 1080)
    if preview: W, H = (360, 640) if vertical else (640, 360)
    fps = plan.get("fps", 30)
    tag = f"{key}{'_preview' if preview else ''}"
    tmp = work / f"render_{tag}"
    if tmp.exists(): shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(exist_ok=True)
    # intermediates: near-lossless so the final encode is the only real generation
    enc = ["-c:v", "libx264", "-preset", "ultrafast" if preview else "veryfast", "-crf", "28" if preview else "12",
           "-pix_fmt", "yuv420p", "-r", str(fps)]
    clips = []
    intro = sec.get("intro", False); outro = sec.get("outro", False)
    lead = 0.0
    if intro:
        d = CONFIG["brand"].get("intro_seconds", 3.0)
        card(tmp / "intro.mov", W, H, fps, d, enc); clips.append(tmp / "intro.mov"); lead = d
    master = plan["master"]
    mfile = angle_file(work, plan, master)
    look = plan.get("look")
    segs = sec["segments"]
    for i, s in enumerate(segs):
        ang = s.get("angle", master); a = plan["angles"][ang]
        d = s["end"] - s["start"]
        # tiny audio fades only where time actually jumps (a real cut), never at a plain angle switch
        fi = i == 0 or abs(s["start"] - segs[i - 1]["end"]) > 0.05
        fo = i == len(segs) - 1 or abs(segs[i + 1]["start"] - s["end"]) > 0.05
        af = ["aresample=48000", "aformat=channel_layouts=stereo"]
        if fi: af.append("afade=in:d=0.02")
        if fo: af.append(f"afade=out:st={max(0, d - 0.03):.3f}:d=0.03")
        vf = color_filter(a, look) + [frame_filter(W, H, s.get("zoom", 1.0), *s.get("center", [0.5, 0.5])
                                                    if not vertical else (s.get("crop_x", a.get("crop_x", 0.5)), 0.5)),
                                       f"fps={fps}", "format=yuv420p"]
        clip = tmp / f"seg_{i:03d}.mov"
        run(["ffmpeg", "-ss", f"{s['start'] + a.get('offset', 0):.3f}", "-i", angle_file(work, plan, ang),
             "-ss", f"{s['start']:.3f}", "-i", mfile, "-t", f"{d:.3f}",
             "-map", "0:v:0", "-map", "1:a:0", "-vf", ",".join(vf), "-af", ",".join(af),
             *enc, "-c:a", "pcm_s16le", clip])
        clips.append(clip)
        print(f"  segment {i+1}/{len(segs)} {ang} {hms(s['start'])}-{hms(s['end'])}", flush=True)
    if outro:
        d = CONFIG["brand"].get("outro_seconds", 4.0)
        card(tmp / "outro.mov", W, H, fps, d, enc, sec.get("outro_text", CONFIG["brand"].get("outro_text"))); clips.append(tmp / "outro.mov")
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{c.name}'\n" for c in clips))
    joined = tmp / "joined.mov"
    run(["ffmpeg", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
    total = float(probe(joined)["format"]["duration"])
    tl = Timeline(segs, lead)

    # final pass: b-roll, captions/lower thirds, music bed with ducking, loudness
    inputs = ["-i", joined]; fc = []; vlab = "0:v"; idx = 1
    for j, b in enumerate(sec.get("broll", [])):
        t = tl.out(b["at"])
        if t is None: print(f"  skip b-roll {j}: {hms(b['at'])} is cut out of the edit"); continue
        dur = b.get("dur", 4.0)
        bf = Path(b["file"])
        if bf.suffix.lower() in IMAGE_EXT:
            inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", bf]
            # slow push-in on stills so they don't sit dead
            move = f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,crop={W*2}:{H*2},zoompan=z='1+0.0008*on':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}"
            fc.append(f"[{idx}:v]{move},setsar=1,format=yuva420p,fade=in:st=0:d=0.3:alpha=1,fade=out:st={dur-0.3:.2f}:d=0.3:alpha=1,setpts=PTS-STARTPTS+{t:.3f}/TB[b{j}]")
        else:
            inputs += ["-ss", f"{b.get('src_in', 0):.3f}", "-t", f"{dur:.3f}", "-i", bf]
            bc = b.get("adjust")
            fc.append(f"[{idx}:v]{','.join(color_filter({'adjust': bc}, look) + [frame_filter(W, H, b.get('zoom', 1.0))])},fps={fps},format=yuva420p,"
                      f"fade=in:st=0:d=0.25:alpha=1,fade=out:st={dur-0.25:.2f}:d=0.25:alpha=1,setpts=PTS-STARTPTS+{t:.3f}/TB[b{j}]")
        fc.append(f"[{vlab}][b{j}]overlay=eof_action=pass:enable='between(t,{t:.3f},{t+dur:.3f})'[v{j}]")
        vlab = f"v{j}"; idx += 1
    ass = build_ass(plan, sec, tl, W, H, work / f"{tag}.ass", vertical)
    if ass:
        esc = str(ass).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        fc.append(f"[{vlab}]subtitles='{esc}'[vs]"); vlab = "vs"
    # "voice": "raw" keeps the recorded sound (no high-pass, no denoise); default cleans it up a little
    raw_voice = plan.get("voice") == "raw"
    voice = "[0:a]anull" if raw_voice else \
        "[0:a]highpass=f=80" + (",afftdn=nr=8:nf=-40" if plan.get("denoise", True) else "")
    afc = []  # audio graph, kept apart so it can be measured on its own
    m = sec.get("music")
    credit = None
    if m and (m.get("file") or m.get("mood")):
        sys.path.insert(0, str(HERE)); import music
        music.init(ROOT)
        mfile, credit = music.resolve(m, total, p.name)
        save(work / "plan.json", plan)  # pins an auto-picked track so the final matches the preview
        print(f"  music: {mfile.name}")
        inputs += ["-stream_loop", "-1", "-ss", f"{m.get('start', 0):.3f}", "-i", mfile]
        fo = m.get("fade_out", 3.0)
        afc.append(voice + ",asplit=2[vo][sc]")
        afc.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},volume={m.get('gain_db', -14)}dB,"
                   f"afade=in:d={m.get('fade_in', 1.5)},afade=out:st={max(0, total - fo):.3f}:d={fo}[mu]")
        if m.get("duck", True):
            # music sits under the voice and swells back up in pauses, intro and outro
            afc.append("[mu][sc]sidechaincompress=threshold=0.015:ratio=8:attack=60:release=900:makeup=1[md]")
        else:
            afc.append("[sc]anullsink;[mu]anull[md]")
        afc.append("[vo][md]amix=inputs=2:normalize=0:duration=first[mx]")
    else:
        afc.append(voice + "[mx]")
    lufs = -14 if vertical else -16
    norm = f"loudnorm=I={lufs}:TP=-1.5:LRA=11"
    if raw_voice:
        # two-pass: measure the mix, then apply one fixed gain (linear) so the voice isn't reshaped
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *map(str, inputs), "-filter_complex",
                            ";".join(afc + [f"[mx]{norm}:print_format=json[aout]"]), "-map", "[aout]", "-t", f"{total:.3f}",
                            "-f", "null", "-"], capture_output=True, text=True)
        st = json.loads(r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1])
        gain = lufs - float(st["input_i"])
        # one fixed gain; a fast limiter catches only the few peaks it would push past -1.5 dBTP
        # (loudnorm's own linear mode silently turns dynamic in that case, which reshapes the voice)
        norm = f"volume={gain:.2f}dB,alimiter=limit=0.84:attack=1:release=50:level=disabled"
        peaks = float(st["input_tp"]) + gain > -1.5
        print(f"  loudness: {st['input_i']} LUFS measured, fixed gain {gain:+.1f} dB to {lufs}"
              + (" (peak limiter catches the loudest moments)" if peaks else ""))
    fc += afc + [f"[mx]{norm},aresample=48000[aout]"]
    final = out / (f"{tag}.mp4")
    venc = ["-c:v", "libx264", "-preset", "ultrafast" if preview else CONFIG.get("x264_preset", "slow"),
            "-crf", "30" if preview else str(CONFIG.get("crf", 18)), "-pix_fmt", "yuv420p", "-r", str(fps),
            "-movflags", "+faststart"]
    if vlab == "0:v":
        fc.append("[0:v]null[vout]"); vlab = "vout"
    run(["ffmpeg", *inputs, "-filter_complex", ";".join(fc), "-map", f"[{vlab}]", "-map", "[aout]",
         *venc, "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}", final], quiet=False)
    if not preview: shutil.rmtree(tmp, ignore_errors=True)
    if credit and not preview:
        (out / "music-credit.txt").write_text(credit + "\n")
    if m and m.get("file") and not preview:
        music.record_use(Path(m["file"]).name, p.name)
    print(f"\n{final}  ({hms(total)})")


# ---------- music library (see music.py) ----------
def cmd_music(args):
    sys.path.insert(0, str(HERE)); import music
    music.init(ROOT); music.main(args)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"): sys.exit(__doc__)
    c = a[0]
    if c == "new": cmd_new(a[1])
    elif c == "music": cmd_music(a[1:])
    elif c == "upload":
        sys.path.insert(0, str(HERE)); import upload; upload.main(a[1:])
    elif c == "probe": cmd_probe(a[1])
    elif c == "sync": cmd_sync(a[1])
    elif c == "transcribe": cmd_transcribe(a[1])
    elif c == "color": cmd_color(a[1])
    elif c == "stills": cmd_stills(a[1], a[2:])
    elif c == "check": cmd_check(a[1])
    elif c == "render": cmd_render(a[1], "--short" in a, "--preview" in a)
    else: sys.exit(__doc__)

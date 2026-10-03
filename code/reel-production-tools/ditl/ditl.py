"""ditl.py - day-in-the-life (DITL) variant renderer: beat-synced hard cuts over a footage library, with a
time + label caption on every shot after the hook card.

Default grammar (all values are plain constants below; tune them for your own format):
  * 1080x1920, 24 fps, hard cuts only, every cut 1 or 2 beats (hook 4 beats), cut lands 2 frames BEFORE the beat
  * caption on every shot after the hook: time (bold) above a small CAPS label, centred, white, soft shadow
  * hook card: 2 lines, sentence case
  * grade: house LC-709 pipe (onetoone.housechain); day shots may add an optional DAY_POST filter string

Usage:  python3 ditl.py spec.json        (spec format: see make_specs.py)
"""
from __future__ import annotations
import json, os, random, sys, tempfile, shutil
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import FF, WB, LIB_JSON, LIBROOT, IDENTITY_POOL, read_text  # noqa: E402
from onetoone import looksheet, housechain, grades as G, ffx  # noqa: E402

W, H, FPS = 1080, 1920, 24
BEATS_JSON = WB + '/beats_verified.json'
DAY_POST = read_text(WB + '/look/DAY_POST.txt')          # optional extra ffmpeg filter for day shots
# Caption font: an open-licensed or licensed font file you supply (TTF/TTC), e.g. an SIL OFL face.
# Placeholder default: $DITL_WORKDIR/fonts/caption-font.ttf. For a TTC, set the face indices.
CAPTION_FONT = os.environ.get('DITL_FONT') or os.path.join(WB, 'fonts', 'caption-font.ttf')
FONT_BOLD_INDEX = int(os.environ.get('DITL_FONT_BOLD_INDEX', '0'))
FONT_LIGHT_INDEX = int(os.environ.get('DITL_FONT_LIGHT_INDEX', '0'))
CUT_LEAD = 2 / FPS  # cuts land 2 frames before the beat


# ---------------------------------------------------------------- music
def beat_grid(song: str, start: float, dur: float, force_bl: float | None = None):
    import librosa
    y, sr = librosa.load(song, sr=22050, offset=start, duration=dur + 2)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units='time')
    beats = np.asarray(beats, float)
    # fill to a regular grid from the tracked phase so we never run out of beats
    bl = float(np.median(np.diff(beats))) if len(beats) > 4 else 60 / float(np.atleast_1d(tempo)[0])
    if force_bl:  # make_specs doubles the grid on very fast tracks; keep the tracked phase
        k = max(1, round(force_bl / bl))
        bl = bl * k if abs(bl * k - force_bl) < 0.08 else force_bl
    phase = beats[0] % bl
    grid = np.arange(phase, dur + 2, bl)
    return grid, bl


# ---------------------------------------------------------------- captions
def caption_png(path: str, time_txt: str | None, label: str | None, hook: list[str] | None = None):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)

    def put(text, font, cap_top, track=0.0):
        # place so the CAP top (not the bbox top) sits at cap_top, centred on x=540
        asc = font.getbbox('H')[1]
        widths = [font.getlength(c) for c in text]
        total = sum(widths) + track * (len(text) - 1)
        x = W / 2 - total / 2
        for c, w in zip(text, widths):
            # soft shadow keeps it readable on bright plates
            d.text((x + 1, cap_top - asc + 2), c, font=font, fill=(0, 0, 0, 70))
            d.text((x, cap_top - asc), c, font=font, fill=(255, 255, 255, 255))
            x += w + track
    if hook:
        f = ImageFont.truetype(CAPTION_FONT, 71, index=FONT_BOLD_INDEX)
        for i, line in enumerate(hook):
            put(line, f, 899 + i * 79)
    else:
        if time_txt:
            put(time_txt, ImageFont.truetype(CAPTION_FONT, 65, index=FONT_BOLD_INDEX), 901)
        if label:
            put(label.upper(), ImageFont.truetype(CAPTION_FONT, 36, index=FONT_LIGHT_INDEX), 980, track=1.0)
    im.save(path)


# ---------------------------------------------------------------- footage
_LIB = None


def library():
    global _LIB
    if _LIB is None:
        _LIB = json.load(open(LIB_JSON))['clips']
    return _LIB


def load_pool():
    v = json.load(open(BEATS_JSON))
    pool = {}
    for k, c in v.items():
        if not c.get('master'):
            continue
        b = c['beat']
        if b.startswith('X') or b in ('SKIP',):
            continue
        pool.setdefault(b, []).append(dict(c, key=k, stem=os.path.splitext(os.path.basename(c['path']))[0],
                                         proxy=os.path.join(LIBROOT, library()[c['cid']]['proxy'])))
    return pool


def person_x(master: str, t: float) -> float:
    """Horizontal centre (0..1) of the main person: macOS Vision human-rectangles, then faces; falls back to 0.5."""
    tmpj = tempfile.mktemp(suffix='.png', dir=WB)
    ffx.run(['-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', master, '-frames:v', '1', '-vf', 'scale=640:360', tmpj])
    try:
        import Vision
        from Foundation import NSURL
        best = None
        for req in (Vision.VNDetectHumanRectanglesRequest.alloc().init(), Vision.VNDetectFaceRectanglesRequest.alloc().init()):
            h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(tmpj), None)
            ok, _ = h.performRequests_error_([req], None)
            res = req.results() or []
            if res:
                r = max(res, key=lambda o: o.boundingBox().size.width * o.boundingBox().size.height)
                bb = r.boundingBox(); best = bb.origin.x + bb.size.width / 2
                break
        return float(np.clip(best, 0.16, 0.84)) if best is not None else 0.5
    except Exception:
        return 0.5
    finally:
        try: os.remove(tmpj)
        except OSError: pass


def probe(master: str, t: float) -> dict:
    """One frame at t (LC-709 graded so Vision sees contrast): faces + full-body humans; crop chosen on them;
    luma/sharpness measured INSIDE the 9:16 crop that will actually ship."""
    import cv2
    tmpj = tempfile.mktemp(suffix='.png', dir=WB)
    # proxy (640x360 S-Log) + a cheap contrast lift is ~20x faster than the 4K LUT pipe and close enough for Vision/luma
    ffx.run(['-v', 'error', '-y', '-ss', f'{t:.2f}', '-i', master, '-frames:v', '1', '-vf',
             'eq=contrast=1.55:brightness=-0.06:saturation=1.4', tmpj])
    out = dict(n=0, faces=0, face_area=0.0, area=0.0, top=1.0, cx=0.5, luma=0.0, sharp=0.0)
    try:
        img = cv2.imread(tmpj)
        if img is None:
            return out
        import Vision
        from Foundation import NSURL
        def run(req):
            h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(tmpj), None)
            h.performRequests_error_([req], None)
            return [r.boundingBox() for r in (req.results() or [])]
        fr = Vision.VNDetectFaceRectanglesRequest.alloc().init()
        hr = Vision.VNDetectHumanRectanglesRequest.alloc().init()
        try: hr.setUpperBodyOnly_(False)
        except Exception: pass
        faces = [b for b in run(fr) if b.size.width * b.size.height > 0.0015]
        hums = [b for b in run(hr) if b.size.width * b.size.height > 0.01]
        out['faces'] = len(faces); out['n'] = max(len(hums), len(faces))
        if faces:
            f = max(faces, key=lambda b: b.size.width * b.size.height)
            out['face_area'] = float(f.size.width * f.size.height); out['cx'] = float(f.origin.x + f.size.width / 2)
        if hums:
            hb = max(hums, key=lambda b: b.size.width * b.size.height)
            out['area'] = float(hb.size.width * hb.size.height); out['top'] = float(hb.origin.y + hb.size.height)
            if not faces:
                out['cx'] = float(hb.origin.x + hb.size.width / 2)
        out['cx'] = float(np.clip(out['cx'], 0.16, 0.84))
        H_, W_ = img.shape[:2]; cw = int(H_ * 9 / 16)
        x0 = int(np.clip(out['cx'] * W_ - cw / 2, 0, W_ - cw))
        g = cv2.cvtColor(img[:, x0:x0 + cw], cv2.COLOR_BGR2GRAY)
        out['luma'] = float(g.mean()); out['sharp'] = float(cv2.Laplacian(g, cv2.CV_64F).var())
    except Exception:
        pass
    finally:
        try: os.remove(tmpj)
        except OSError: pass
    return out


# beats where an empty plate (no person in frame) is acceptable if it is sharp
NO_PERSON_OK = {'EXTERIOR', 'NIGHT_EXTERIOR', 'WIDE'}


def shot_score(pr: dict, identity: bool, night: bool, beat: str = '') -> float:
    """Higher is better; -1e9 = reject (black frames, soft frames, empty plates, extra people, body-part close-ups)."""
    if pr['luma'] < (34 if night else 45):
        return -1e9
    if pr['sharp'] < 15:
        return -1e9
    sc = min(pr['sharp'], 300) / 300
    if identity:
        if pr['n'] == 0:
            return 0.2 * sc if beat in NO_PERSON_OK and pr['sharp'] >= 60 else -1e9   # an empty plate must at least be crisp
        if pr['faces']:
            sc += 2.0 + (0.5 if 0.004 <= pr['face_area'] <= 0.12 else 0) - (1.5 if pr['face_area'] > 0.2 else 0)
        else:
            sc += 1.0                                   # back/side views are fine
            if pr['area'] > 0.35 and pr['top'] > 0.97:  # body fills frame, head cut off = body-part close-up
                sc -= 1.5
        if pr['n'] > 2:
            sc -= 0.6
    return sc


EXCLUDE = set(json.load(open(WB + '/exclude.json'))) if os.path.exists(WB + '/exclude.json') else set()


def render_shot(clip: dict, inpt: float, dur: float, night: bool, cap_png: str, out: str, px: float = None):
    master = looksheet.resolve_master(clip['stem'])
    if px is None:
        px = person_x(master, inpt + dur / 2)
    # 9:16 window over the 16:9 master, full height, centred on the person
    vf_crop = f"crop=trunc(ih*9/16/2)*2:ih:max(0\\,min(iw-ih*9/16\\,iw*{px:.4f}-ih*9/32)):0"
    head = housechain.house_head(W, H, cover=True)
    post = '' if (night or not DAY_POST) else (',' + DAY_POST)
    vf = f"{vf_crop},{head},{housechain.HOUSE_TAIL}{post},fps={FPS},format=yuv420p[v];[v][1:v]overlay=0:0,format=yuv420p"
    nfr = int(round(dur * FPS))
    cmd = [FF, '-v', 'error', '-y', '-ss', f'{inpt:.3f}', '-i', master, '-loop', '1', '-i', cap_png,
           '-filter_complex', f"[0:v]{vf}", '-frames:v', str(nfr), '-an',
           '-c:v', 'libx264', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p',
           '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv', out]
    if ffx.run(cmd[1:]).returncode: raise SystemExit('render failed '+out)
    return dict(clip=clip['stem'], key=clip['key'], beat=clip['beat'], inpt=round(inpt, 3), dur=round(dur, 3), px=round(px, 3))


# ---------------------------------------------------------------- casting law (grades.jsonl + identity pool)
FALLBACK = {   # beat -> substitutes when the pool for that beat runs dry
    'INTERIOR': ['NIGHT_INTERIOR', 'KITCHEN', 'WIDE'], 'WORKSPACE': ['INTERIOR'], 'KITCHEN': ['FOOD', 'INTERIOR'],
    'FOOD': ['KITCHEN'], 'STREET': ['TRANSIT', 'EXTERIOR'], 'TRANSIT': ['STREET'], 'ACTIVITY': ['EXTERIOR', 'STREET'],
    'EXTERIOR': ['WIDE', 'STREET'], 'NIGHT_INTERIOR': ['INTERIOR'], 'NIGHT_EXTERIOR': ['EXTERIOR', 'WIDE'],
}
NO_IDENTITY = {'WIDE'}   # beats where no identified person is required
_GR = None
_SETTLED = None
def law():
    global _GR, _SETTLED
    if _GR is None:
        _GR = G.load_grades()
        pool = json.load(open(IDENTITY_POOL))
        _SETTLED = G.effective_settled(pool['settled_pool'], _GR)
    return _GR, _SETTLED

def pick_inpoint(clip, dur, identity, rnd):
    """Return an in-point that the grades allow, or None."""
    gr, settled = law()
    stem, cd = clip['stem'], float(clip['dur'])
    if identity:
        wins = G.hero_windows(stem, gr)
        if not wins:
            if stem not in settled and G.identity_slot_reason(stem, gr, 0.0, dur) is not None:
                return None
            wins = [(0.0, cd)]
    else:
        wins = [(0.0, cd)]
    tries = []
    for a, b in wins:
        a, b = max(a, 0.25), min(b, cd - 0.25)
        if b - a >= dur:
            tries += [rnd.uniform(a, b - dur) for _ in range(4)]
    rnd.shuffle(tries)
    for t0 in tries:
        if G.never_reason(stem, t0, t0 + dur, gr):
            continue
        if identity and G.identity_slot_reason(stem, gr, t0, t0 + dur):
            continue
        return t0
    return None


# ---------------------------------------------------------------- build
def build(spec: dict):
    rnd = random.Random(spec.get('seed', 1))
    pool = load_pool()
    used_global = set(spec.get('avoid_keys', []))
    ban = set(spec.get('ban_keys', []))            # hard rejects for THIS variant (unlike avoid_keys)
    ban_hook = set(spec.get('ban_hook_keys', []))  # hooks already used by sibling variants
    grid, bl = beat_grid(spec['song'], spec.get('song_start', 0.0), spec.get('max_dur', 45), spec.get('beat_len'))
    out_dir = spec['out_dir']; os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='ditl_', dir=WB)
    shots, edl = [], []
    bi = 0  # beat index; shot boundaries sit on beats
    t_prev = 0.0
    used = set()
    for si, s in enumerate(spec['shots']):
        nb = s.get('beats', 1)
        bi += nb
        if bi >= len(grid):
            break
        # snap the cut to an ABSOLUTE frame so per-shot rounding never accumulates
        f_prev = int(round(t_prev * FPS))
        f_end = max(int(round((grid[bi] - CUT_LEAD) * FPS)), f_prev + 6)
        dur = (f_end - f_prev) / FPS
        # choose a clip
        cands = []
        for b in s['beat']:
            cands += [c for c in pool.get(b, []) if c['key'] not in used and c['key'] not in used_global]
        if s.get('night') is not None:
            pref = [c for c in cands if bool(c.get('night')) == bool(s['night'])]
            cands = pref or cands
        cands = [c for c in cands if float(c.get('dur') or 0) >= dur + 0.6]
        if not cands:
            cands = [c for b in s['beat'] for c in pool.get(b, []) if float(c.get('dur') or 0) >= dur + 0.6]
        if not cands:
            raise SystemExit(f'no clip for shot {si} beats {s["beat"]}')
        identity = not (set(s['beat']) <= NO_IDENTITY) and not s.get('no_identity')
        rnd.shuffle(cands)
        night = bool(s.get('night'))

        def choose(clist, k=3):
            opts = []
            for c in clist:
                if c['stem'] in EXCLUDE or c['key'] in used or c['key'] in ban or (si == 0 and c['key'] in ban_hook):
                    continue
                for _ in range(2):
                    t0 = pick_inpoint(c, dur, identity, rnd)
                    if t0 is not None:
                        opts.append((c, t0))
                if len(opts) >= 2 * k:
                    break
            best = None
            m = None
            for c, t0 in opts[:2 * k]:
                m = m or looksheet.resolve_master(c['stem'])
                pr = probe(c['proxy'], t0 + dur / 2)
                sc = shot_score(pr, identity, night, c.get('beat', ''))
                if si == 0 and sc > -1e8 and (pr['n'] == 0 or (not pr['faces'] and (pr['area'] > 0.35 or pr['top'] > 0.95)) or pr['sharp'] < 40):
                    sc = -1e9      # the hook must read in one glance: a whole person or a face, sharp
                if sc > -1e8 and bool(c.get('night')) != night:
                    sc -= 1.5          # keep day shots in the day half and night shots in the night half
                if best is None or sc > best[0]:
                    best = (sc, c, t0, pr)
            return best

        best = choose(cands)
        if best is None or best[0] < -1e8:   # avoidance is a preference, never a reason to fail
            allc = [c for b in s['beat'] for c in pool.get(b, []) if c['key'] not in used and float(c.get('dur') or 0) >= dur + 0.6]
            rnd.shuffle(allc)
            b0 = choose(allc) if allc else None
            if b0 is not None and (best is None or b0[0] > best[0]):
                best = b0
        if best is None or best[0] < -1e8:
            fb = []
            for b in s['beat']:
                fb += [x for x in FALLBACK.get(b, []) if x not in s['beat'] and x not in fb]
            fc = [c for b in fb for c in pool.get(b, []) if float(c.get('dur') or 0) >= dur + 0.6]
            rnd.shuffle(fc)
            b2 = choose(fc) if fc else None
            if b2 is not None and (best is None or b2[0] > best[0]):
                best = b2
        clip = inpt = None; px = None
        if best is not None:
            _, clip, inpt, pr = best
            px = pr['cx'] if pr['n'] else 0.5
        if clip is None:
            raise SystemExit(f'no grade-legal clip for shot {si} beats {s["beat"]}')
        used.add(clip['key'])
        cap = f'{tmp}/cap{si:02d}.png'
        caption_png(cap, s.get('time'), s.get('label'), s.get('hook'))
        seg = f'{tmp}/s{si:02d}.mp4'
        info = render_shot(clip, inpt, dur, bool(s.get('night')), cap, seg, px)
        info.update(identity='person-A' if identity else 'none', start=round(t_prev, 3), end=round(t_prev + dur, 3), time=s.get('time'), label=s.get('label'))
        edl.append(info); shots.append(seg)
        t_prev = f_end / FPS
    total = t_prev
    lst = f'{tmp}/list.txt'
    open(lst, 'w').write(''.join(f"file '{p}'\n" for p in shots))
    silent = f'{tmp}/video.mp4'
    ffx.run(['-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', silent])
    out = os.path.join(out_dir, spec['name'] + '.mp4')
    fade = 0.6
    ffx.run(['-v', 'error', '-y', '-i', silent, '-ss', f"{spec.get('song_start', 0.0):.3f}", '-i', spec['song'],
                    '-map', '0:v', '-map', '1:a', '-t', f'{total:.3f}', '-c:v', 'copy',
                    '-af', f'afade=t=out:st={max(0, total - fade):.3f}:d={fade}', '-c:a', 'aac', '-b:a', '256k',
                    '-movflags', '+faststart', out])
    json.dump(dict(spec=spec, beat_len=round(bl, 4), total=round(total, 3), edl=edl), open(out[:-4] + '.edl.json', 'w'), indent=1)
    shutil.rmtree(tmp, ignore_errors=True)
    return out, edl


if __name__ == '__main__':
    spec = json.load(open(sys.argv[1]))
    out, edl = build(spec)
    print(out, len(edl), 'shots')

"""story.py - DITL variants cast so the day reads in a believable order.

Story rules (the point of this file):
  * Every shot sits on a REAL capture time (master creation_time is UTC; shift it to the shoot's local clock with
    DITL_UTC_OFFSET_HOURS, default 0). Clips with no capture time are never used.
  * Day or night = the real capture hour (05:00-18:44 day, 18:45-04:59 night).
  * Shots run in real chronological order. The daytime block is ONE shoot day; the night block is ONE shoot day. A
    bridge scene (BRIDGE_FAMILY) from the night's day joins the two blocks.
  * Display times are monotonic: day scenes are spread 7:00AM-5:30PM in real order, the bridge sits at ~6PM, night
    scenes keep their real (rounded) time.

Cut grammar: a synthetic, evenly spaced cut list (hook of DITL_HOOK_SEC, then one cut per DITL_BEAT_SEC) laid over
DITL_SONG from DITL_SONG_START. Use only audio you hold a licence for.

    python3 story.py S01 [S02 ...]      (specs in $DITL_WORKDIR/story_specs.json)
"""
from __future__ import annotations
import datetime, json, os, random, shutil, sys, tempfile
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ditl  # noqa: E402
from ditl import ffx, housechain, looksheet  # noqa: E402
from _env import read_text  # noqa: E402

FF = ditl.FF
W, H, FPS = 1080, 1920, 25
WB = ditl.WB
OUT = WB + '/out_story'
SONG = os.environ.get('DITL_SONG') or WB + '/songs/track.wav'
SONG_START = float(os.environ.get('DITL_SONG_START', '0'))
HOOK_SEC = float(os.environ.get('DITL_HOOK_SEC', '2.0'))
BEAT_SEC = float(os.environ.get('DITL_BEAT_SEC', '0.5'))
DAY_SLOTS = [3, 2, 2, 2, 2]   # shots per day scene; first slot = hook shot + 2
BRIDGE = 2
NIGHT_SLOTS = [2, 2]
_N_SHOTS = sum(DAY_SLOTS) + BRIDGE + sum(NIGHT_SLOTS)
CUTS = [0.0] + [round(HOOK_SEC + BEAT_SEC * i, 3) for i in range(_N_SHOTS)]
assert sum(DAY_SLOTS) + BRIDGE + sum(NIGHT_SLOTS) == len(CUTS) - 1
DAY_POST = read_text(WB + '/look/DAY_POST.txt')          # optional extra ffmpeg filters
NIGHT_POST = read_text(WB + '/look/NIGHT_POST.txt')
# Caption font: an open-licensed or licensed VARIABLE font you supply with Semibold/Regular named
# instances (e.g. an SIL OFL variable sans). Placeholder default: $DITL_WORKDIR/fonts/story-variable.ttf.
SF = os.environ.get('DITL_STORY_FONT') or os.path.join(WB, 'fonts', 'story-variable.ttf')

FAMILY = {  # beat -> (family, day label, night label)
    'INTERIOR': ('home', 'morning', 'evening'), 'NIGHT_INTERIOR': ('evening', 'evening', 'evening'),
    'WORKSPACE': ('work', 'work block', 'late session'), 'KITCHEN': ('kitchen', 'coffee', 'dinner prep'),
    'FOOD': ('food', 'lunch', 'dinner'), 'STREET': ('move', 'on the way', 'on the way'),
    'TRANSIT': ('move', 'on the way', 'on the way'), 'ACTIVITY': ('activity', 'break', 'walk'),
    'EXTERIOR': ('outside', 'outside', 'outside'), 'NIGHT_EXTERIOR': ('outside', 'outside', 'outside'),
    'WIDE': ('wide', 'establishing', 'establishing'),
}
OPENING_FAMILY = 'home'     # preferred first scene of the day
BRIDGE_FAMILY = 'move'      # scene that joins the day block to the night block
NO_IDENTITY = {'WIDE'}


UTC_OFFSET_HOURS = float(os.environ.get('DITL_UTC_OFFSET_HOURS', '0'))   # shoot-location clock vs UTC creation_time


def local(ct: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ct[:19]) + datetime.timedelta(hours=UTC_OFFSET_HOURS)


def story_day(t: datetime.datetime) -> datetime.date:
    return (t - datetime.timedelta(hours=5)).date()     # a "day" runs 05:00 -> 04:59 next morning


def is_night(t: datetime.datetime) -> bool:
    m = t.hour * 60 + t.minute
    return m >= 18 * 60 + 45 or m < 5 * 60


def load_clips():
    ct = json.load(open(WB + '/capture_times.json'))
    clips = []
    for beat, cs in ditl.load_pool().items():
        if beat not in FAMILY:
            continue
        for c in cs:
            e = ct.get(c['key']) or {}
            if not e.get('ct') or c['stem'] in ditl.EXCLUDE:
                continue
            t = local(e['ct'])
            clips.append(dict(c, t=t, sday=story_day(t), night_real=is_night(t), fam=FAMILY[beat][0]))
    return sorted(clips, key=lambda c: c['t'])


def scenes_of(clips):
    """Group chronologically into scenes: same family, < 12 min apart, same day/night side."""
    out = []
    for c in clips:
        s = out[-1] if out else None
        if s and s['fam'] == c['fam'] and s['night'] == c['night_real'] and (c['t'] - s['clips'][-1]['t']).seconds < 720:
            s['clips'].append(c)
        else:
            out.append(dict(fam=c['fam'], beat=c['beat'], night=c['night_real'], sday=c['sday'], clips=[c]))
    for s in out:
        s['t'] = s['clips'][0]['t']
        s['cap'] = sum(1 if float(x['dur']) < 3.0 else 2 for x in s['clips'])   # long clips can give two in-points
    return out


def choose_scenes(scenes, n, rnd, template, prefer_first=None, skip_fams=(), need_early=False):
    """Pick n scenes in chronological order, weighted to well-stocked scenes, spread across the block, varied."""
    cand = [s for s in scenes if s['fam'] not in skip_fams and s['cap'] >= 1]
    if len(cand) < n:
        raise SystemExit(f'only {len(cand)} usable scenes for {n} slots')
    first = None
    if prefer_first:
        early = [s for s in cand[:max(3, len(cand) // 3)] if s['fam'] == prefer_first]
        first = early[0] if early else None
    w = np.array([min(s['cap'], 4) ** 0.7 for s in cand], float)
    for _ in range(6000):
        pool = [s for s in cand if s is not first]
        ww = np.array([min(s['cap'], 4) ** 0.7 for s in pool], float)
        k = n - (1 if first else 0)
        idx = rnd.sample(range(len(pool)), k) if len(pool) >= k else None
        if idx is None:
            break
        # weighted rejection: keep the draw with probability ~ mean weight
        if rnd.random() > float(np.mean(ww[idx])) / float(ww.max()):
            continue
        pick = sorted([pool[i] for i in idx] + ([first] if first else []), key=lambda s: s['t'])
        if first and pick[0] is not first:
            continue
        fams = [p['fam'] for p in pick]
        if any(a == b for a, b in zip(fams, fams[1:])):
            continue
        if fams.count(OPENING_FAMILY) > 1 or (first is None and OPENING_FAMILY in fams):
            continue
        if max(fams.count(f) for f in set(fams)) > 2:
            continue
        if need_early and pick[0]['t'].hour < 5 and any(5 <= c['t'].hour < 22 or c['t'].hour >= 18 for c in cand if c['t'].hour >= 18):
            continue
        try:
            cnt = allocate(pick, template)
        except SystemExit:
            continue
        return pick, cnt
    raise SystemExit('no scene selection fits')


def allocate(pick, template):
    """Shot counts per scene: start from the reference template, then move shots to scenes that can supply them."""
    cnt = list(template)
    cap = [min(s['cap'], 2 if s['fam'] == OPENING_FAMILY else 4) for s in pick]
    for _ in range(100):
        over = [i for i in range(len(cnt)) if cnt[i] > cap[i]]
        if not over:
            return cnt
        i = over[0]; cnt[i] -= 1
        spare = [j for j in range(len(cnt)) if cnt[j] < cap[j] and j != i]
        if not spare:
            raise SystemExit('not enough footage in the picked scenes')
        cnt[min(spare, key=lambda j: (cnt[j], abs(j - i)))] += 1
    raise SystemExit('allocation did not converge')


def fmt(t: datetime.datetime) -> str:
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d}{'AM' if t.hour < 12 else 'PM'}"


def round10(t):
    return t.replace(minute=(t.minute // 10) * 10, second=0, microsecond=0)


def display_times(day_scenes, bridge, night_scenes):
    """Monotonic display clock: day spread 7:00AM-5:30PM in real order, bridge ~6PM, night = real time rounded."""
    base = datetime.datetime(2000, 1, 1)
    t0, t1 = day_scenes[0]['t'], day_scenes[-1]['t']
    span = max((t1 - t0).total_seconds(), 1)
    out = []
    for s in day_scenes:
        f = (s['t'] - t0).total_seconds() / span
        out.append(round10(base + datetime.timedelta(minutes=7 * 60 + f * (17.5 - 7) * 60)))
    out.append(base.replace(hour=18))                      # bridge: 6:00PM
    for s in night_scenes:
        r = s['t']; h = r.hour + (24 if r.hour < 5 else 0)
        out.append(round10(base + datetime.timedelta(hours=h, minutes=r.minute)))
    for i in range(1, len(out)):                           # strictly increasing, 10-min steps
        if out[i] <= out[i - 1]:
            out[i] = out[i - 1] + datetime.timedelta(minutes=10)
    return [fmt(t) for t in out]


def caption_png(path, time_txt=None, label=None, hook=None):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)

    def font(size, var):
        f = ImageFont.truetype(SF, size); f.set_variation_by_name(var); return f

    def put(text, f, cap_top, track=0.0, ref='H'):
        top = f.getbbox(ref)[1]
        ws = [f.getlength(c) for c in text]
        x = W / 2 - (sum(ws) + track * (len(text) - 1)) / 2
        for c, w in zip(text, ws):
            d.text((x + 1, cap_top - top + 2), c, font=f, fill=(0, 0, 0, 60))
            d.text((x, cap_top - top), c, font=f, fill=(255, 255, 255, 255))
            x += w + track
    if hook:   # two lines, line pitch 88 px
        f = font(66, 'Semibold')
        for i, line in enumerate(hook):
            put(line, f, 888 + i * 88, track=-1.2)
    else:      # time above, lowercase label below
        if time_txt:
            put(time_txt, font(56, 'Semibold'), 940, track=-0.75)
        if label:
            put(label, font(45, 'Regular'), 1003, track=0.75, ref='l')
    im.save(path)


def render_shot(clip, inpt, dur, night, cap_png, out, px):
    master = looksheet.resolve_master(clip['stem'])
    vf_crop = f"crop=trunc(ih*9/16/2)*2:ih:max(0\\,min(iw-ih*9/16\\,iw*{px:.4f}-ih*9/32)):0"
    post = NIGHT_POST if night else DAY_POST
    post = (post + ',') if post else ''
    vf = (f"{vf_crop},{housechain.house_head(W, H, cover=True)},{housechain.HOUSE_TAIL},{post}fps={FPS},format=yuv420p[v];"
          f"[v][1:v]overlay=0:0,format=yuv420p")
    nfr = int(round(dur * FPS))
    cmd = ['-v', 'error', '-y', '-ss', f'{inpt:.3f}', '-i', master, '-loop', '1', '-i', cap_png, '-filter_complex', f"[0:v]{vf}",
           '-frames:v', str(nfr), '-an', '-c:v', 'libx264', '-preset', 'medium', '-crf', '16', '-pix_fmt', 'yuv420p',
           '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv', out]
    if ffx.run(cmd).returncode:
        raise SystemExit('render failed ' + out)


def cast(scene, k, dur_list, night, rnd, used, avoid, hook_first=False):
    """k shots from one scene, in chronological order, each the best legal probed in-point."""
    picks = []
    pool = list(scene['clips'])
    slots = []
    for c in pool:
        slots.append(c)
        if float(c['dur']) >= 3.0:
            slots.append(c)
    order = sorted(range(len(slots)), key=lambda i: (slots[i]['key'] in avoid, rnd.random()))
    for j in range(k):
        dur = dur_list[j]
        best = None
        for i in order:
            c = slots[i]
            if (c['key'], 0) in used and (c['key'], 1) in used:
                continue
            identity = c['beat'] not in NO_IDENTITY
            for _ in range(3):
                t0 = ditl.pick_inpoint(c, dur, identity, rnd)
                if t0 is None:
                    continue
                if any(p['key'] == c['key'] and abs(p['inpt'] - t0) < dur + 0.3 for p in picks):
                    continue
                pr = ditl.probe(c['proxy'], t0 + dur / 2)
                sc = ditl.shot_score(pr, identity, night, c['beat'])
                if hook_first and j == 0 and sc > -1e8 and (pr['n'] == 0 or pr['sharp'] < 40 or
                                                            (not pr['faces'] and (pr['area'] > 0.35 or pr['top'] > 0.95))):
                    sc = -1e9
                sc -= 0.8 * (c['key'] in avoid)
                if best is None or sc > best[0]:
                    best = (sc, c, t0, pr)
            if best and best[0] > 2.5:
                break
        if best is None or best[0] < -1e8:
            raise SystemExit(f"scene {scene['beat']}@{scene['t']:%H:%M} could not fill shot {j}")
        _, c, t0, pr = best
        n = sum(1 for p in picks if p['key'] == c['key'])
        used.add((c['key'], n))
        picks.append(dict(key=c['key'], clip=c, inpt=t0, px=pr['cx'], t=c['t'] + datetime.timedelta(seconds=t0)))
    picks.sort(key=lambda p: (p['t']))
    return picks


def build(spec, clips, usage):
    rnd = random.Random(spec['seed'])
    scenes = scenes_of(clips)
    dsc = [s for s in scenes if s['sday'] == datetime.date.fromisoformat(spec['day']) and not s['night']]
    nsday = datetime.date.fromisoformat(spec['night'])
    nsc = [s for s in scenes if s['sday'] == nsday and s['night']]
    br = [s for s in scenes if s['sday'] == nsday and not s['night'] and s['fam'] == BRIDGE_FAMILY and s['cap'] >= BRIDGE and s['t'].hour >= 12]
    if spec['day'] == spec['night']:
        br = [b for b in br if b['t'] >= max(x['t'] for x in dsc[:1])] or br
    if not br:
        raise SystemExit('no bridge scene on ' + spec['night'])
    bridge = rnd.choice(br)
    dsc = [s for s in dsc if s['t'] < bridge['t'] or spec['day'] != spec['night']]
    day_pick, dcnt = choose_scenes(dsc, len(DAY_SLOTS), rnd, DAY_SLOTS, prefer_first=OPENING_FAMILY, skip_fams=(BRIDGE_FAMILY,))
    night_pick, ncnt = choose_scenes(nsc, len(NIGHT_SLOTS), rnd, NIGHT_SLOTS, skip_fams=(OPENING_FAMILY, BRIDGE_FAMILY), need_early=True)
    if dcnt[0] < 2:
        raise SystemExit('first scene cannot carry the hook')
    times = display_times(day_pick, bridge, night_pick)
    plan = [(s, k, False) for s, k in zip(day_pick, dcnt)] + [(bridge, BRIDGE, False)] + \
           [(s, k, True) for s, k in zip(night_pick, ncnt)]
    avoid = {k for k, n in usage.items() if n >= 2}
    used = set()
    tmp = tempfile.mkdtemp(prefix='story_', dir=WB)
    shots, edl, si = [], [], 0
    for (s, k, night), tm in zip(plan, times):
        durs = [CUTS[si + j + 1] - CUTS[si + j] for j in range(k)]
        picks = cast(s, k, durs, night, rnd, used, avoid, hook_first=(si == 0))
        label = FAMILY[s['beat']][2 if night else 1]
        for j, p in enumerate(picks):
            a, b = CUTS[si], CUTS[si + 1]
            cap = f'{tmp}/c{si:02d}.png'
            if si == 0:
                caption_png(cap, hook=spec['hook'])
            else:
                caption_png(cap, time_txt=tm, label=label)
            seg = f'{tmp}/s{si:02d}.mp4'
            render_shot(p['clip'], p['inpt'], b - a, night, cap, seg, p['px'])
            shots.append(seg)
            edl.append(dict(i=si, start=a, end=b, key=p['key'], clip=p['clip']['stem'], beat=p['clip']['beat'],
                            shot_at=p['t'].strftime('%Y-%m-%d %H:%M:%S'), night=night, inpt=round(p['inpt'], 3),
                            time=None if si == 0 else tm, label=None if si == 0 else label))
            si += 1
    os.makedirs(OUT, exist_ok=True)
    lst = f'{tmp}/list.txt'
    open(lst, 'w').write(''.join(f"file '{p}'\n" for p in shots))
    out = f"{OUT}/{spec['name']}.mp4"
    total = CUTS[-1]
    r = ffx.run(['-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-ss', f'{SONG_START:.3f}', '-t', f'{total:.3f}',
                 '-i', SONG, '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k',
                 '-af', f'afade=t=out:st={total - 0.15:.3f}:d=0.15', '-movflags', '+faststart', '-shortest', out])
    if r.returncode:
        raise SystemExit('mux failed')
    json.dump(dict(spec=spec, total=total, edl=edl), open(out[:-4] + '.edl.json', 'w'), indent=1)
    shutil.rmtree(tmp, ignore_errors=True)
    return out, edl


def audit(edl):
    """The story rules, checked on the output: real times monotonic per block, no night clip in the day block or vice versa."""
    errs = []
    ts = [(datetime.datetime.strptime(x['shot_at'], '%Y-%m-%d %H:%M:%S'), x) for x in edl]
    for (ta, a), (tb, b) in zip(ts, ts[1:]):
        if a['night'] == b['night'] and tb < ta - datetime.timedelta(minutes=2) and story_day(ta) == story_day(tb):
            errs.append(f"time goes backwards {a['clip']}->{b['clip']}")
    for t, x in ts:
        if is_night(t) != x['night']:
            errs.append(f"{x['clip']} shot {t:%H:%M} but placed in {'night' if x['night'] else 'day'}")
    return errs


if __name__ == '__main__':
    import fcntl
    lk = open(WB + '/.story.lock', 'w')
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit('another story batch is already running')
    specs = json.load(open(WB + '/story_specs.json'))
    want = sys.argv[1:]
    clips = load_clips()
    up = WB + '/story_usage.json'
    usage = json.load(open(up)) if os.path.exists(up) else {}
    for spec in specs:
        if want and spec['name'][:3] not in want and spec['name'] not in want:
            continue
        if os.path.exists(f"{OUT}/{spec['name']}.mp4"):
            print('skip', spec['name']); continue
        try:
            out, edl = build(spec, clips, usage)
            for x in edl:
                usage[x['key']] = usage.get(x['key'], 0) + 1
            json.dump(usage, open(up, 'w'))
            errs = audit(edl)
            print('OK' if not errs else 'LAWFAIL', spec['name'], len(edl), 'shots', errs[:3], flush=True)
        except SystemExit as e:
            print('FAIL', spec['name'], e, flush=True)

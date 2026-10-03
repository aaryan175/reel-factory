"""Write DITL batch specs: one song per variant, song window chosen so the track's biggest energy lift (the drop)
lands on the template's hinge shot (the first night shot, if any).
    python3 make_specs.py            -> $DITL_WORKDIR/specs/V01..Vnn.json + BATCH_PLAN.md
Songs are read from $DITL_WORKDIR/songs; use only audio you hold a licence for.
"""
import json, os, glob, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from templates import TEMPLATES  # noqa: E402
from _env import WB  # noqa: E402

SONGS = WB + '/songs'

# Example hook cards (two lines each). Keep personal details out of hook text.
HOOKS = {
    'STUDIO_DAY': [['A day at', 'the studio'], ['What a normal', 'day looks like'], ['One day,', 'start to finish']],
    'MORNING': [['A simple', 'morning routine'], ['How the day', 'starts']],
    'EVENING': [['An evening', 'routine'], ['After hours', 'at the studio']],
}
PLAN = [('STUDIO_DAY', 3), ('MORNING', 2), ('EVENING', 2)]
LENGTHS = {'STUDIO_DAY': (12, 24)}   # target seconds (lo, hi); other templates use DEFAULT_LENGTH
DEFAULT_LENGTH = (8, 18)


def hinge_beats(shots):
    n = 0
    for s in shots:
        if s.get('night'):
            return n
        n += s['beats']
    return None


def fit_length(shots, bl, lo, hi):
    """Stretch/shrink beats per shot so the reel lands in [lo, hi] seconds without breaking the 1-2 beat grammar."""
    tot = lambda: sum(x['beats'] for x in shots) * bl
    if tot() < lo:                       # short template on a fast song: every shot one beat longer
        for x in shots:
            x['beats'] = x['beats'] * 2 if x['beats'] < 3 else x['beats'] + 2
    guard = 0
    while tot() > hi and guard < 200:
        guard += 1
        longs = [i for i, x in enumerate(shots) if 1 < x['beats'] and i > 0 and x['beats'] < 5]
        if longs:
            shots[longs[-1 - (guard % len(longs))]]['beats'] -= 1
            continue
        # drop a repeated shot (same time+label as its neighbour), latest first
        dup = [i for i in range(len(shots) - 1, 1, -1) if shots[i].get('time') == shots[i - 1].get('time') and not shots[i].get('night') == (not shots[i - 1].get('night'))]
        if not dup:
            break
        shots.pop(dup[0])
    return shots


def song_window(path, need, hinge_t):
    import librosa
    y, sr = librosa.load(path, sr=22050, mono=True)
    dur = len(y) / sr
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    t = librosa.times_like(rms, sr=sr, hop_length=512)
    k = int(sr / 512 * 1.0)
    sm = np.convolve(rms, np.ones(k) / k, mode='same')
    lift = np.r_[np.zeros(2 * k), sm[2 * k:] - sm[:-2 * k]]   # energy now minus 2 s ago
    if hinge_t is None:
        # no hinge: start on the first strong section after 8 s
        cand = np.where((t > 8) & (t < dur - need - 2))[0]
        start = float(t[cand[np.argmax(sm[cand])]]) - 1.0 if len(cand) else 0.0
        return max(0.0, start), round(float(dur), 1), None
    ok = np.where((t > hinge_t + 2) & (t < dur - (need - hinge_t) - 1))[0]
    if not len(ok):
        return 0.0, round(float(dur), 1), None
    drop = float(t[ok[np.argmax(lift[ok])]])
    return max(0.0, drop - hinge_t), round(float(dur), 1), round(drop, 2)


def bpm(path, start, need):
    import librosa
    y, sr = librosa.load(path, sr=22050, offset=start, duration=min(need, 30))
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    return float(np.atleast_1d(tempo)[0])


def main():
    songs = sorted(p for p in glob.glob(SONGS + '/*') if not p.endswith('.txt'))
    os.makedirs(WB + '/specs', exist_ok=True)
    rows, n, si = [], 0, 0
    for tname, count in PLAN:
        for j in range(count):
            n += 1; song = songs[si % len(songs)]; si += 1
            shots = [dict(s) for s in TEMPLATES[tname]]
            shots[0]['hook'] = HOOKS[tname][j % len(HOOKS[tname])]
            b = bpm(song, 30.0, 30)
            bl = 60.0 / b
            # keep one-beat shots >= 9 frames: halve the grid on very fast tracks
            if bl < 0.38:
                bl *= 2
            lo, hi = LENGTHS.get(tname, DEFAULT_LENGTH)
            shots = fit_length(shots, bl, lo, hi)
            total_beats = sum(s['beats'] for s in shots)
            need = total_beats * bl + 1.0
            hb = hinge_beats(shots)
            start, sdur, drop = song_window(song, need, None if hb in (None, 0) else hb * bl)
            name = f"V{n:02d}_{tname}"
            spec = dict(name=name, seed=1000 + n, song=os.path.realpath(song), song_start=round(start, 2),
                        max_dur=round(need + 1, 1), out_dir=WB + '/out', shots=shots, template=tname,
                        bpm=round(b, 1), beat_len=round(bl, 4))
            json.dump(spec, open(f"{WB}/specs/{name}.json", 'w'), indent=1)
            rows.append((name, tname, os.path.basename(song)[:60], round(b, 1), round(start, 1), drop, round(need, 1), ' / '.join(shots[0]['hook'])))
            print(rows[-1], flush=True)
    with open(WB + '/specs/BATCH_PLAN.md', 'w') as f:
        f.write('| id | template | song | bpm | song start s | drop s | length s | hook |\n|---|---|---|---|---|---|---|---|\n')
        for r in rows:
            f.write('| ' + ' | '.join(str(x) for x in r) + ' |\n')


if __name__ == '__main__':
    main()

"""Render every spec in $DITL_WORKDIR/specs/V*.json one after another (one reel at a time; ffmpeg is already
serialised by onetoone.ffx). Clips used by earlier variants are avoided where the pool allows, so variants stay
visually distinct.
    python3 batch.py [V01 V02 ...]
"""
import glob, json, os, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ditl, review  # noqa: E402
from _env import WB  # noqa: E402


def main(only):
    specs = sorted(glob.glob(WB + '/specs/V*.json'))
    if only:
        specs = [p for p in specs if any(os.path.basename(p).startswith(o) for o in only)]
    usage = json.load(open(WB + '/usage.json')) if os.path.exists(WB + '/usage.json') else {}
    for p in specs:
        spec = json.load(open(p))
        out = os.path.join(spec['out_dir'], spec['name'] + '.mp4')
        if os.path.exists(out):
            print('skip (exists)', spec['name']); continue
        # avoid keys used 2+ times already across the batch
        spec['avoid_keys'] = [k for k, n in usage.items() if n >= 2]
        try:
            out, edl = ditl.build(spec)
            for x in edl:
                usage[x['key']] = usage.get(x['key'], 0) + 1
            json.dump(usage, open(WB + '/usage.json', 'w'))
            print('OK', spec['name'], len(edl), 'shots', review.sheet(out), flush=True)
        except SystemExit as e:
            print('FAIL', spec['name'], e, flush=True)
        except Exception:
            print('FAIL', spec['name'], traceback.format_exc()[-400:], flush=True)


if __name__ == '__main__':
    import fcntl
    lk = open(WB + '/.batch.lock', 'w')
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit('another DITL batch is already running')
    main(sys.argv[1:])

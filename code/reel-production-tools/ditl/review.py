"""Contact sheet per rendered DITL variant (one mid-frame per shot, from its .edl.json) for eye audit.
    python3 review.py <out.mp4> [...]      -> <out>_sheet.jpg next to it
"""
import json, os, sys, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from onetoone import ffx  # noqa: E402


def sheet(mp4):
    e = json.load(open(mp4[:-4] + '.edl.json'))
    tmp = tempfile.mkdtemp(prefix='rev_', dir=os.path.dirname(mp4))
    for i, x in enumerate(e['edl']):
        t = (x['start'] + x['end']) / 2
        ffx.run(['-v', 'error', '-y', '-ss', f'{t:.3f}', '-i', mp4, '-frames:v', '1', '-vf', 'scale=180:-1,format=yuvj420p', f'{tmp}/{i:03d}.jpg'])
    n = len(e['edl']); cols = 10; rows = (n + cols - 1) // cols
    out = mp4[:-4] + '_sheet.jpg'
    ffx.run(['-v', 'error', '-y', '-pattern_type', 'glob', '-i', f'{tmp}/*.jpg', '-vf', f'tile={cols}x{rows}', '-frames:v', '1', out])
    shutil.rmtree(tmp, ignore_errors=True)
    return out


if __name__ == '__main__':
    for m in sys.argv[1:]:
        print(sheet(m))

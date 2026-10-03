"""Build $DITL_WORKDIR/out/index.html: every rendered DITL variant with player, song, hook, length and its
shot sheet. Served read-only on a private network (python3 -m http.server); never expose it publicly without auth."""
import glob, html, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import WB  # noqa: E402
OUT = WB + '/out'


def main():
    cards = []
    for edl in sorted(glob.glob(OUT + '/*.edl.json')):
        e = json.load(open(edl)); s = e['spec']; name = s['name']
        mp4 = name + '.mp4'; sh = name + '_sheet.jpg'
        hook = ' / '.join(s['shots'][0].get('hook') or [])
        song = os.path.basename(s['song']).split('__')[0]
        cards.append(f'''<section class="card"><h2>{html.escape(name)}</h2>
<p class="meta">{html.escape(s.get('template', ''))} · {e['total']:.1f}s · {len(e['edl'])} shots · {html.escape(song)} @ {s.get('song_start', 0)}s</p>
<p class="hook">“{html.escape(hook)}”</p>
<video controls preload="none" playsinline poster="preview/{name}.jpg" src="preview/{mp4}"></video>
<p class="meta"><a style="color:#8bd" href="{mp4}" download>full-quality master</a></p>
{f'<img loading="lazy" src="{sh}">' if os.path.exists(os.path.join(OUT, sh)) else ''}</section>''')
    page = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DITL variants</title><style>
:root{{--bg:#0d0d0d;--fg:#eee;--mut:#9a9a9a;--card:#171717}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 -apple-system,Helvetica Neue,sans-serif;padding:16px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:var(--mut);margin:0 0 16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}}
.card{{background:var(--card);border-radius:10px;padding:12px}} .card h2{{font-size:15px;margin:0 0 4px}}
.meta{{color:var(--mut);font-size:12px;margin:0}} .hook{{margin:6px 0}}
video{{width:100%;aspect-ratio:9/16;background:#000;border-radius:6px}} img{{width:100%;margin-top:8px;border-radius:4px}}
</style></head><body><h1>Day-in-the-life variants</h1>
<p class="sub">Local review only.</p>
<div class="grid">{''.join(cards)}</div></body></html>'''
    open(OUT + '/index.html', 'w').write(page)
    print(OUT + '/index.html', len(cards), 'cards')


if __name__ == '__main__':
    main()

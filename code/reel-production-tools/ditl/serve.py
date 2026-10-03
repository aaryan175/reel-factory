"""Read-only review server for $DITL_WORKDIR/out with HTTP Range support.
python3 -m http.server ignores Range, so Safari / iPhone refuse to play the mp4s (they need 206 Partial Content).
    python3 serve.py [port]        (default 8515; binds DITL_SERVE_HOST, default 127.0.0.1 - never expose it publicly without auth)
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import WB  # noqa: E402
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(WB, 'out')
CHUNK = 2 * 1024 * 1024   # open-ended ranges are served in chunks so many players do not saturate the link


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        path = self.translate_path(self.path)
        rng = self.headers.get('Range')
        if not rng or os.path.isdir(path) or not os.path.isfile(path):
            return super().send_head()
        m = re.match(r'bytes=(\d*)-(\d*)', rng)
        size = os.path.getsize(path)
        if not m:
            self.send_error(416); return None
        a, b = m.groups()
        if a == '':                       # suffix range: last N bytes
            start, end = max(0, size - int(b)), size - 1
        else:
            start, end = int(a), (int(b) if b else min(size - 1, int(a) + CHUNK - 1))  # open-ended: send a chunk, the player asks for more
        end = min(end, size - 1)
        if start > end:
            self.send_response(416); self.send_header('Content-Range', f'bytes */{size}'); self.end_headers(); return None
        f = open(path, 'rb'); f.seek(start)
        self.send_response(206)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    def copyfile(self, src, dst):
        n = getattr(self, '_remaining', None)
        if n is None:
            return super().copyfile(src, dst)
        while n > 0:
            buf = src.read(min(256 * 1024, n))
            if not buf:
                break
            try:
                dst.write(buf)
            except (BrokenPipeError, ConnectionResetError):
                break
            n -= len(buf)
        self._remaining = None


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8515
    ThreadingHTTPServer((os.environ.get('DITL_SERVE_HOST', '127.0.0.1'), port), partial(RangeHandler, directory=ROOT)).serve_forever()

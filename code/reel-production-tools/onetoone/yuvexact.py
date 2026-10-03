"""onetoone.yuvexact — player-exact BT.709 limited-range YUV420 <-> RGB for compositing and measuring.

ffmpeg's swscale yuv->rgb (`-pix_fmt rgb24`) reads ~2 levels darker than a real player on tv-range BT.709 video. A lane that decodes with swscale, blends captions and
re-encodes bakes that -2 into the delivered picture. Composite on these planes instead. L0038."""
from __future__ import annotations

import numpy as np

KR, KB = 0.2126, 0.0722


def yuv420_to_rgb(buf: bytes, w: int, h: int) -> np.ndarray:
    Y = np.frombuffer(buf, np.uint8, w * h).reshape(h, w).astype(np.float32)
    U = np.frombuffer(buf, np.uint8, w * h // 4, w * h).reshape(h // 2, w // 2).astype(np.float32)
    V = np.frombuffer(buf, np.uint8, w * h // 4, w * h * 5 // 4).reshape(h // 2, w // 2).astype(np.float32)
    U = np.repeat(np.repeat(U, 2, 0), 2, 1)
    V = np.repeat(np.repeat(V, 2, 0), 2, 1)
    y, cb, cr = (Y - 16) / 219, (U - 128) / 224, (V - 128) / 224
    R = y + 2 * (1 - KR) * cr
    B = y + 2 * (1 - KB) * cb
    G = (y - KR * R - KB * B) / (1 - KR - KB)
    return np.clip(np.rint(np.stack([R, G, B], -1) * 255), 0, 255).astype(np.uint8)


def rgb_to_yuv420(rgb: np.ndarray) -> bytes:
    x = rgb.astype(np.float32) / 255.0
    yp = KR * x[..., 0] + (1 - KR - KB) * x[..., 1] + KB * x[..., 2]
    cb = (x[..., 2] - yp) / (2 * (1 - KB))
    cr = (x[..., 0] - yp) / (2 * (1 - KR))
    Y = np.clip(np.rint(16 + 219 * yp), 16, 235).astype(np.uint8)

    def sub(c):
        c = 0.25 * (c[0::2, 0::2] + c[1::2, 0::2] + c[0::2, 1::2] + c[1::2, 1::2])
        return np.clip(np.rint(128 + 224 * c), 16, 240).astype(np.uint8)

    return Y.tobytes() + sub(cb).tobytes() + sub(cr).tobytes()

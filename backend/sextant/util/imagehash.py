"""Photo fingerprints for profile-photo correlation and local "reverse image search".

* dHash (gradient) and pHash (DCT, more robust to resizing/re-compression), each also computed for
  the mirrored image (profile photos are often flipped).
* A tiny re-encoded JPEG thumbnail (EXIF and all other metadata dropped) so the UI can show photos
  without loading anything from the internet.
* For photos *you* provide: a check for embedded GPS coordinates / camera data.

A matching hash means "these images look alike" — never "same person". Pillow is optional; without it
photo features are reported as unavailable.
"""

from __future__ import annotations

import base64
import io
import math
import warnings

try:  # optional dependency
    from PIL import Image, ImageOps, ImageStat
    Image.MAX_IMAGE_PIXELS = 16_000_000          # decompression-bomb guard
    PIL_AVAILABLE = True
except Exception:  # pragma: no cover
    Image = ImageOps = ImageStat = None
    PIL_AVAILABLE = False

ALLOWED_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MATCH_SIMILARITY = 0.875               # both dHash and pHash ≥ 87.5% similar (≤ 8 of 64 bits differ): possible match

_N = 32
_COS = [[math.cos((2 * x + 1) * u * math.pi / (2 * _N)) for x in range(_N)] for u in range(8)]


def _open(data: bytes, limit: int):
    if not PIL_AVAILABLE or not data or len(data) > limit:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")                              # palette/transparency notices etc.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS)
            img.load()
            return img
    except Exception:
        return None


def _bits_hex(bits: list[bool]) -> str:
    v = 0
    for b in bits:
        v = (v << 1) | (1 if b else 0)
    return f"{v:016x}"


def _dhash(gray) -> str:
    px = list(gray.resize((9, 8), Image.Resampling.LANCZOS).tobytes())   # mode "L": one byte per pixel
    return _bits_hex([px[r * 9 + c] > px[r * 9 + c + 1] for r in range(8) for c in range(8)])


def _phash(gray) -> str:
    px = list(gray.resize((_N, _N), Image.Resampling.LANCZOS).tobytes())
    rows = [px[i * _N:(i + 1) * _N] for i in range(_N)]
    # separable 2-D DCT-II; only the 8x8 low-frequency block is needed
    tmp = [[sum(_COS[v][y] * rows[x][y] for y in range(_N)) for v in range(8)] for x in range(_N)]
    coeffs = [sum(_COS[u][x] * tmp[x][v] for x in range(_N)) for u in range(8) for v in range(8)]
    median = sorted(coeffs[1:])[len(coeffs) // 2]
    return _bits_hex([c > median for c in coeffs])


def _thumb(img, size: int = 96) -> str:
    t = img.convert("RGB")
    t.thumbnail((size, size))
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=72, optimize=True)                  # re-encoded: no EXIF/metadata survives
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def fingerprint(data: bytes, limit: int = MAX_IMAGE_BYTES, thumb_size: int = 96) -> dict | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)                  # palette/transparency notices from Pillow
        return _fingerprint(data, limit, thumb_size)


def _fingerprint(data: bytes, limit: int, thumb_size: int) -> dict | None:
    img = _open(data, limit)
    if img is None:
        return None
    try:
        base = ImageOps.exif_transpose(img)
        gray = base.convert("L")
        mirrored = ImageOps.mirror(gray)
        # near-uniform images (blank/default avatars, solid colours) carry no identity signal at all
        flat = ImageStat.Stat(gray.resize((64, 64))).stddev[0] < 12
        return {
            "flat": flat,
            "dhash": _dhash(gray), "phash": _phash(gray),
            "dhash_flip": _dhash(mirrored), "phash_flip": _phash(mirrored),
            "thumb": _thumb(base, thumb_size), "width": base.width, "height": base.height, "format": img.format,
        }
    except Exception:
        return None


def photo_metadata_warnings(data: bytes) -> list[str]:
    """Privacy check for a photo *you* provide: GPS position or camera details embedded in the file."""
    img = _open(data, MAX_UPLOAD_BYTES)
    if img is None:
        return []
    try:
        exif = img.getexif()
    except Exception:
        return []
    out = []
    if exif.get(34853):                                               # GPSInfo IFD
        out.append("The original file contains GPS coordinates — anyone who gets the file can see where it was taken. "
                   "Platforms usually strip this, but email, messaging and file sharing may not.")
    make, model = exif.get(271), exif.get(272)
    if make or model:
        out.append(f"The original file names the camera/phone ({' '.join(str(x) for x in (make, model) if x)}).")
    if exif.get(306) or exif.get(36867):
        out.append("The original file contains the date and time it was taken.")
    return out


def upload_copy(data: bytes, size: int = 512) -> str | None:
    """Re-encoded, metadata-free copy — used only if you opt in to an online reverse-image search."""
    img = _open(data, MAX_UPLOAD_BYTES)
    if img is None:
        return None
    base = ImageOps.exif_transpose(img).convert("RGB")
    base.thumbnail((size, size))
    buf = io.BytesIO()
    base.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


# --------------------------------------------------------------------------- comparison

def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def similarity(a: str, b: str) -> float:
    return 1.0 - hamming(a, b) / (len(a) * 4)


def informative(h: str | None) -> bool:
    """Flat / near-blank images (default avatars, solid colours) hash to almost all 0s or 1s."""
    if not h:
        return False
    ones = bin(int(h, 16)).count("1")
    return 8 <= ones <= 56


def photo_similarity(a: dict, b: dict) -> float:
    """Similarity of two fingerprints. For each orientation of ``b`` (as-is / mirrored) *both* hash
    families must agree — the lower of the dHash and pHash similarities counts — and the better
    orientation wins. Requiring agreement keeps unrelated photos from matching by chance.
    Flat images (blank/default avatars) never match. Older data with only a dHash uses that alone."""
    if a.get("flat") or b.get("flat"):
        return 0.0
    best = 0.0
    for suffix in ("", "_flip"):
        scores = []
        for kind in ("dhash", "phash"):
            ha, hb = a.get(kind), b.get(kind + suffix)
            if ha and hb:
                if not (informative(ha) and informative(hb)):
                    scores = []
                    break
                scores.append(similarity(ha, hb))
        if scores:
            best = max(best, min(scores))
    return best


def dhash(data: bytes) -> str | None:
    """Backwards-compatible helper: just the dHash."""
    fp = fingerprint(data)
    return fp["dhash"] if fp else None

"""Renders a subscription link as a PNG QR code, in-process.

Deliberately `segno` and not `qrcode`: segno is pure-Python with no
dependencies and writes PNG itself, so this adds nothing to the image that
needs a native build (`qrcode` pulls in Pillow). Nothing here touches disk —
the bytes go straight to Telegram's sendPhoto — so there is no temp file to
clean up and no path for one operator's QR to be served to anyone else.
"""

from __future__ import annotations

import io

import segno

# 'M' (~15% recoverable) is the level every mainstream VPN client's scanner is
# tuned for, and a subscription URL is short enough that a higher level would
# buy resilience nobody needs at the cost of a denser, harder-to-scan image.
ERROR_CORRECTION = "m"
# Module size in pixels. 8 puts a typical subscription URL around 400-500px —
# comfortably above the ~300px where Telegram's own image compression starts
# eating fine QR modules on a phone screen, without sending a needlessly big
# file for every account in a 30-account batch.
DEFAULT_SCALE = 8
# The quiet zone the QR spec requires. Scanners genuinely fail without it, so
# this is not cosmetic padding — don't drop it to make the image smaller.
QUIET_ZONE_MODULES = 4


def subscription_qr_png(data: str, *, scale: int = DEFAULT_SCALE) -> bytes:
    """PNG bytes for `data`, black on an OPAQUE white background.

    The explicit `light="white"` matters: segno's default PNG background is
    transparent, and Telegram composites a transparent image onto the
    viewer's own theme — so a dark-mode client renders a black-on-black QR
    that no scanner can read, while the same file looks perfectly fine to
    anyone testing in light mode. Always pass a real background colour here.
    """
    if not data or not data.strip():
        raise ValueError("Refusing to render a QR code for an empty subscription link")

    qr = segno.make(data, error=ERROR_CORRECTION)
    buffer = io.BytesIO()
    qr.save(
        buffer,
        kind="png",
        scale=scale,
        border=QUIET_ZONE_MODULES,
        dark="black",
        light="white",
    )
    return buffer.getvalue()

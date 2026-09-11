"""Screen capture and model-image scaling.

Coordinates contract (ARCHITECTURE.md): capture the full virtual desktop at
physical pixels, downscale to at most ``max_width`` for the model, and return
scale factors that map model-space coordinates back to physical pixels.
"""

from __future__ import annotations

import base64
import io

from PIL import Image

_MAX_WIDTH = 1280


def capture() -> Image.Image:
    """Grab the full virtual screen (all monitors) as a physical-pixel PIL image."""
    import mss

    with mss.mss() as sct:
        monitor = sct.monitors[0]
        raw = sct.grab(monitor)
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def origin() -> tuple[int, int]:
    """Top-left of the virtual screen in virtual-desktop coordinates.

    Monitors left of the primary have negative X, so physical click targets are
    ``origin + model_coord * scale``.
    """
    import mss

    with mss.mss() as sct:
        mon = sct.monitors[0]
    return int(mon["left"]), int(mon["top"])


def to_model_image(
    img: Image.Image, max_width: int = _MAX_WIDTH
) -> tuple[Image.Image, float, float]:
    """Downscale for the model.

    Returns ``(model_image, scale_x, scale_y)`` where
    ``physical = model_coord * scale``. If the capture is already within
    ``max_width`` the image is returned unscaled (scale factors 1.0).
    """
    width, height = img.size
    if width <= max_width:
        return img.copy(), 1.0, 1.0
    scale_x = width / max_width
    model_height = max(1, round(height / scale_x))
    model = img.resize((max_width, model_height), Image.LANCZOS)
    scale_y = height / model_height
    return model, scale_x, scale_y


def to_base64_png(img: Image.Image) -> str:
    """Encode a PIL image as a base64 PNG string."""
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def to_base64_jpeg(img: Image.Image, quality: int = 90) -> str:
    """Encode a PIL image as a base64 JPEG string, for providers that want JPEG."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality)
    return base64.b64encode(buffer.getvalue()).decode("ascii")

import io
import numpy as np
from PIL import Image


class BackgroundSubtractor:
    """
    Running-average background model.
    bg = (1-alpha)*bg + alpha*frame each tick.
    Foreground = pixels where any channel differs from bg by > threshold.
    """

    def reset(self, background_jpeg: bytes):
        img = Image.open(io.BytesIO(background_jpeg)).convert("RGB")
        self.bg = np.array(img, dtype=np.float32)

    def __init__(self, background_jpeg: bytes, alpha: float = 0.05, threshold: int = 25):
        self.alpha = alpha
        self.threshold = threshold
        img = Image.open(io.BytesIO(background_jpeg)).convert("RGB")
        self.bg = np.array(img, dtype=np.float32)

    def process(self, jpeg_bytes: bytes) -> bytes:
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        frame = np.array(img, dtype=np.float32)

        diff = np.abs(frame - self.bg)
        mask = diff.max(axis=2) > self.threshold

        # update all pixels — static objects get absorbed because they always
        # contribute; the moving circle doesn't because each pixel only sees it
        # for ~1 frame per orbit, keeping its background estimate near black
        self.bg = (1 - self.alpha) * self.bg + self.alpha * frame

        out = np.zeros((frame.shape[0], frame.shape[1], 3), dtype=np.uint8)
        out[mask] = 255

        buf = io.BytesIO()
        Image.fromarray(out).save(buf, format="JPEG", quality=85)
        return buf.getvalue()

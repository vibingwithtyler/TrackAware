import io
import numpy as np
from PIL import Image
from scipy.ndimage import label


class MotionDetector:
    """
    Two-frame differencing detector.
    Responds within a single frame interval — no warm-up needed.
    Returns pixel-space centroids for all moving blobs above min_area.
    Nearby blobs (within merge_dist px) are merged into one area-weighted
    centroid, eliminating the leading/trailing-edge double-detection that
    frame differencing inherently produces.
    """

    def __init__(self, threshold: int = 15, min_area: int = 1, merge_dist: int = 20):
        self.threshold = threshold
        self.min_area = min_area
        self.merge_dist = merge_dist
        self._prev: np.ndarray | None = None

    def process(self, jpeg_bytes: bytes) -> list[dict]:
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
        frame = np.array(img, dtype=np.float32)

        if self._prev is None:
            self._prev = frame
            return []

        diff = np.abs(frame - self._prev)
        self._prev = frame

        mask = diff > self.threshold
        labeled, num = label(mask)

        blobs = []
        for i in range(1, num + 1):
            region = labeled == i
            area = int(region.sum())
            if area < self.min_area:
                continue
            ys, xs = np.where(region)
            blobs.append({"x": float(xs.mean()), "y": float(ys.mean()), "area": area})

        return self._merge(blobs)

    def _merge(self, blobs: list[dict]) -> list[dict]:
        """Greedily merge blobs whose centroids are within merge_dist of each other."""
        merged = []
        used = [False] * len(blobs)

        for i, a in enumerate(blobs):
            if used[i]:
                continue
            group = [a]
            used[i] = True
            for j, b in enumerate(blobs):
                if used[j]:
                    continue
                dx, dy = a["x"] - b["x"], a["y"] - b["y"]
                if (dx * dx + dy * dy) ** 0.5 <= self.merge_dist:
                    group.append(b)
                    used[j] = True

            total_area = sum(g["area"] for g in group)
            merged.append({
                "x": round(sum(g["x"] * g["area"] for g in group) / total_area, 1),
                "y": round(sum(g["y"] * g["area"] for g in group) / total_area, 1),
                "area": total_area,
            })

        return merged

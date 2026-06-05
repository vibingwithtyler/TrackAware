import time
import threading
from generator import make_frame, make_background, FPS, get_scenario, get_ghost_detections, get_adsb_mode, get_marine_mode
from tracker import BackgroundSubtractor
from motion import MotionDetector
from logger import event_log


class FrameProducer:
    """
    Single background thread generating frames at ~FPS.
    Runs background subtraction and motion detection on every frame.
    Stream endpoints and the SSE detection endpoint all read from shared state.
    """

    def __init__(self):
        self._raw: bytes | None = None
        self._bg: bytes | None = None
        self._detections: list[dict] = []
        self._frame_num: int = 0
        self._lock = threading.Lock()
        self._subtractor = BackgroundSubtractor(make_background())
        self._motion = MotionDetector()
        self._t = 0
        self._reset_event = threading.Event()
        self._had_detection = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        frame_duration = 1.0 / FPS
        while True:
            start = time.perf_counter()

            if self._reset_event.is_set():
                self._reset_event.clear()
                bg = make_background()
                self._subtractor = BackgroundSubtractor(bg)
                self._motion = MotionDetector()
                self._had_detection = False
                event_log.log('SYSTEM', 'INFO',
                              f'Background model seeded — scenario: {get_scenario()}')

            raw = make_frame(self._t)

            if get_adsb_mode() or get_marine_mode():
                bg         = self._subtractor.process(raw)
                detections = []
                if self._had_detection:
                    event_log.log('DETECT', 'WARN', 'TGT-001 lost')
                    self._had_detection = False
            else:
                bg              = self._subtractor.process(raw)
                real_detections = self._motion.process(raw)

                for d in real_detections:
                    d['id'] = 'TGT-001'

                if real_detections and not self._had_detection:
                    d = real_detections[0]
                    event_log.log('DETECT', 'INFO',
                                  f'TGT-001 acquired — pixel ({int(d["x"])}, {int(d["y"])})  area={d["area"]}')
                    self._had_detection = True
                elif not real_detections and self._had_detection:
                    event_log.log('DETECT', 'WARN', 'TGT-001 lost')
                    self._had_detection = False

                detections = real_detections + get_ghost_detections()

            with self._lock:
                self._raw = raw
                self._bg = bg
                self._detections = detections
                self._frame_num = self._t
            self._t += 1
            elapsed = time.perf_counter() - start
            sleep = frame_duration - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def stream(self, source: str):
        """MJPEG generator. source: 'raw' | 'bg'"""
        frame_duration = 1.0 / FPS
        while True:
            start = time.perf_counter()
            with self._lock:
                frame = self._raw if source == "raw" else self._bg
            if frame is not None:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
            elapsed = time.perf_counter() - start
            sleep = frame_duration - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def get_detections(self) -> tuple[int, list[dict]]:
        """Return (frame_num, detections) snapshot."""
        with self._lock:
            return self._frame_num, list(self._detections)

    def reset_subtractor(self):
        """Signal _run to reinitialise the background model on its next frame."""
        self._reset_event.set()


# module-level singleton so all Flask threads share one producer
producer = FrameProducer()

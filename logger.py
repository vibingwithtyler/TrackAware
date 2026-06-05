import collections
import threading
from datetime import datetime, timezone


class EventLogger:
    def __init__(self, maxlen: int = 2000):
        self._events: collections.deque = collections.deque(maxlen=maxlen)
        self._seq: int = 0
        self._lock = threading.Lock()
        self._subs: list[collections.deque] = []
        self._sub_lock = threading.Lock()

    def log(self, source: str, level: str, message: str):
        ts = datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3] + 'Z'
        with self._lock:
            self._seq += 1
            event = {'seq': self._seq, 'ts': ts, 'source': source, 'level': level, 'msg': message}
            self._events.append(event)
        with self._sub_lock:
            for q in self._subs:
                q.append(event)

    def subscribe(self) -> collections.deque:
        q: collections.deque = collections.deque()
        with self._sub_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: collections.deque):
        with self._sub_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def get_recent(self, n: int = 500) -> list:
        with self._lock:
            return list(self._events)[-n:]

    def get_since(self, after_seq: int) -> list:
        """Return up to 500 events with seq > after_seq, in arrival order."""
        with self._lock:
            return [e for e in self._events if e['seq'] > after_seq][-500:]


event_log = EventLogger()

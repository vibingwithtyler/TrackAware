import json
import math
import os
import socket as _socket
import ssl
import threading
import time
import websocket
from logger import event_log

AISSTREAM_KEY = os.environ.get('AISSTREAM_API_KEY', 'e4dbeaf1b785aa70b26e4a1b690f44e63c9973f8')
_WS_URL       = 'wss://stream.aisstream.io/v0/stream'
BOX_KM        = 48.28  # 30 miles
VP_MARGIN     = 0.20


def _in_box(v_lat: float, v_lon: float, c_lat: float, c_lon: float) -> bool:
    dlat = abs(v_lat - c_lat) * 111.32
    dlon = abs(v_lon - c_lon) * 111.32 * math.cos(math.radians(c_lat))
    return dlat <= BOX_KM / 2 and dlon <= BOX_KM / 2


def _edge_follow(ac_lat: float, ac_lon: float, c_lat: float, c_lon: float) -> tuple[float, float, bool]:
    """Returns (new_c_lat, new_c_lon, shifted) using 20/20 logic matching synthetic tracker."""
    h_lat = (BOX_KM / 2) / 111.32
    h_lon = (BOX_KM / 2) / (111.32 * math.cos(math.radians(c_lat)))
    px = (ac_lon - c_lon + h_lon) / (h_lon * 2) * 500
    py = (c_lat  + h_lat - ac_lat) / (h_lat * 2) * 500
    m  = VP_MARGIN * 500   # 100 px
    s  = 500
    new_c_lat, new_c_lon, shifted = c_lat, c_lon, False
    if px < m:
        new_c_lon = ac_lon + h_lon * (1 - 2 * (s - m) / s)
        shifted = True
    elif px > s - m:
        new_c_lon = ac_lon + h_lon * (1 - 2 * m / s)
        shifted = True
    if py < m:
        new_c_lat = ac_lat + h_lat * (2 * (s - m) / s - 1)
        shifted = True
    elif py > s - m:
        new_c_lat = ac_lat + h_lat * (2 * m / s - 1)
        shifted = True
    return new_c_lat, new_c_lon, shifted


def _subscription(lat: float, lon: float) -> str:
    h_lat = (BOX_KM / 2) / 111.32
    h_lon = (BOX_KM / 2) / (111.32 * math.cos(math.radians(lat)))
    return json.dumps({
        'Apikey':           AISSTREAM_KEY,
        'BoundingBoxes':    [[[lat - h_lat, lon - h_lon], [lat + h_lat, lon + h_lon]]],
        'FilterMessageTypes': ['PositionReport'],
    })


def _parse(msg: dict) -> dict | None:
    try:
        meta = msg.get('MetaData', {})
        pos  = msg.get('Message', {}).get('PositionReport', {})
        lat  = meta.get('latitude')
        lon  = meta.get('longitude')
        if lat is None or lon is None:
            return None
        hdg = pos.get('TrueHeading')
        if hdg == 511:
            hdg = None
        return {
            'mmsi':      str(meta.get('MMSI', '')),
            'name':      (meta.get('ShipName') or '').strip(),
            'lat':       float(lat),
            'lon':       float(lon),
            'heading':   hdg,
            'cog':       pos.get('Cog'),
            'speed_kt':  pos.get('Sog'),
            'status':    pos.get('NavigationalStatus', 0),
            'last_seen': time.time(),
        }
    except Exception:
        return None


class MarinePoller:
    def __init__(self):
        self._lock        = threading.Lock()
        self._active      = False
        self._lat         = None
        self._lon         = None
        self._vessels     = {}    # mmsi → vessel dict
        self._follow      = None  # MMSI currently locked
        self._follow_data = None
        self._error       = None
        self._sub_key     = None  # (lat, lon) of last sent subscription
        threading.Thread(target=self._run, daemon=True).start()

    # ── Public API ─────────────────────────────────────────────────────────────

    def activate(self, lat: float, lon: float):
        with self._lock:
            self._active      = True
            self._lat         = lat
            self._lon         = lon
            self._vessels     = {}
            self._follow      = None
            self._follow_data = None
            self._error       = None
            self._sub_key     = None
        event_log.log('SYSTEM', 'INFO',
                      f'Marine mode activated — center ({lat:.4f}, {lon:.4f})')

    def deactivate(self):
        with self._lock:
            self._active      = False
            self._vessels     = {}
            self._follow      = None
            self._follow_data = None
        event_log.log('SYSTEM', 'INFO', 'Marine mode deactivated')

    def set_follow(self, mmsi: str):
        with self._lock:
            self._follow = mmsi
        event_log.log('DETECT', 'INFO', f'Marine lock — MMSI {mmsi}')

    def release_follow(self):
        with self._lock:
            prev              = self._follow
            self._follow      = None
            self._follow_data = None
        if prev:
            event_log.log('DETECT', 'WARN', f'Marine lock released — MMSI {prev}')

    def get_state(self) -> dict:
        with self._lock:
            return {
                'active':      self._active,
                'vessels':     list(self._vessels.values()),
                'follow_mmsi': self._follow,
                'follow_data': dict(self._follow_data) if self._follow_data else None,
                'center':      {'lat': self._lat, 'lon': self._lon} if self._lat else None,
                'error':       self._error,
            }

    # ── Background thread ──────────────────────────────────────────────────────

    def _prune_vessels(self):
        cutoff = time.time() - 30
        with self._lock:
            stale = [k for k, v in self._vessels.items() if v['last_seen'] < cutoff]
            for k in stale:
                del self._vessels[k]

    def _run(self):
        while True:
            with self._lock:
                active = self._active
                lat    = self._lat
                lon    = self._lon

            if not active or lat is None:
                time.sleep(1)
                continue

            try:
                ws = websocket.create_connection(
                    _WS_URL, timeout=10,
                    sslopt={'cert_reqs': ssl.CERT_NONE},
                )
                sub_key = (round(lat, 6), round(lon, 6))
                ws.send(_subscription(lat, lon))
                ws.settimeout(2.0)

                last_prune = time.time()

                while True:
                    with self._lock:
                        if not self._active:
                            break
                        c_lat  = self._lat
                        c_lon  = self._lon
                        follow = self._follow

                    new_key = (round(c_lat, 6), round(c_lon, 6))
                    if new_key != sub_key:
                        ws.send(_subscription(c_lat, c_lon))
                        sub_key = new_key

                    now = time.time()
                    if now - last_prune > 10:
                        self._prune_vessels()
                        last_prune = now

                    try:
                        raw = ws.recv()
                    except (websocket.WebSocketTimeoutException, _socket.timeout):
                        continue
                    except Exception:
                        break

                    vessel = _parse(json.loads(raw))
                    if vessel and _in_box(vessel['lat'], vessel['lon'], c_lat, c_lon):
                        with self._lock:
                            self._vessels[vessel['mmsi']] = vessel
                            self._error = None
                            if follow and vessel['mmsi'] == follow:
                                self._follow_data = vessel
                                new_c_lat, new_c_lon, shifted = _edge_follow(
                                    vessel['lat'], vessel['lon'],
                                    self._lat, self._lon,
                                )
                                if shifted:
                                    self._lat = new_c_lat
                                    self._lon = new_c_lon
                                    event_log.log('SYSTEM', 'INFO',
                                                  f'Camera repointed — ({new_c_lat:.4f}, {new_c_lon:.4f})')

                ws.close()

            except Exception as e:
                with self._lock:
                    self._error = str(e)
                time.sleep(5)


marine_poller = MarinePoller()

import json
import math
import ssl
import threading
import time
import urllib.request
from logger import event_log

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE

POLL_INTERVAL = 5.0
RADIUS_NM     = 25.0  # covers corners of 35×35 mi box
BOX_KM        = 56.33  # 35 miles
VP_MARGIN     = 0.20  # 20% of frame — matches synthetic tracker

_LIVE_URL = 'https://api.airplanes.live/v2/point/{lat}/{lon}/{r}'
_LOL_URL  = 'https://api.adsb.lol/v2/point/{lat}/{lon}/{r}'


def _get(url: str) -> list:
    req = urllib.request.Request(url, headers={'User-Agent': 'TrackVision/1.0'})
    with urllib.request.urlopen(req, timeout=6, context=_SSL_CTX) as res:
        return json.loads(res.read()).get('ac', [])


def _fetch(lat: float, lon: float) -> list:
    url = _LIVE_URL.format(lat=round(lat, 6), lon=round(lon, 6), r=RADIUS_NM)
    try:
        return _get(url)
    except Exception:
        pass
    return _get(_LOL_URL.format(lat=round(lat, 6), lon=round(lon, 6), r=RADIUS_NM))


def _in_box(ac_lat: float, ac_lon: float, c_lat: float, c_lon: float) -> bool:
    dlat = abs(ac_lat - c_lat) * 111.32
    dlon = abs(ac_lon - c_lon) * 111.32 * math.cos(math.radians(c_lat))
    return dlat <= BOX_KM / 2 and dlon <= BOX_KM / 2


def _edge_follow(ac_lat: float, ac_lon: float, c_lat: float, c_lon: float) -> tuple[float, float, bool]:
    """Returns (new_c_lat, new_c_lon, shifted) using 20/20 edge logic matching synthetic tracker."""
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


def _parse(ac: dict) -> dict | None:
    lat = ac.get('lat')
    lon = ac.get('lon')
    if lat is None or lon is None:
        return None
    alt = ac.get('alt_baro', 0)
    if alt == 'ground' or alt is None:
        alt = 0
    return {
        'icao24':   ac.get('hex', '').upper(),
        'callsign': (ac.get('flight') or ac.get('r') or '').strip(),
        'lat':      float(lat),
        'lon':      float(lon),
        'alt_ft':   int(alt) if isinstance(alt, (int, float)) else 0,
        'heading':  ac.get('track'),
        'speed_kt': ac.get('gs'),
        'type':     ac.get('t', ''),
    }


class ADSBPoller:
    def __init__(self):
        self._lock        = threading.Lock()
        self._active      = False
        self._lat         = None
        self._lon         = None
        self._flights     = []
        self._follow      = None        # ICAO24 currently locked
        self._follow_data = None
        self._error       = None
        threading.Thread(target=self._run, daemon=True).start()

    # ── Public API ────────────────────────────────────────────────────────────

    def activate(self, lat: float, lon: float):
        with self._lock:
            self._active      = True
            self._lat         = lat
            self._lon         = lon
            self._flights     = []
            self._follow      = None
            self._follow_data = None
            self._error       = None
        event_log.log('SYSTEM', 'INFO',
                      f'ADS-B mode activated — center ({lat:.4f}, {lon:.4f})')

    def deactivate(self):
        with self._lock:
            self._active      = False
            self._flights     = []
            self._follow      = None
            self._follow_data = None
        event_log.log('SYSTEM', 'INFO', 'ADS-B mode deactivated')

    def update_center(self, lat: float, lon: float):
        with self._lock:
            if self._active:
                self._lat = lat
                self._lon = lon

    def set_follow(self, icao24: str):
        icao24 = icao24.upper()
        with self._lock:
            self._follow = icao24
        event_log.log('DETECT', 'INFO', f'ADS-B lock — {icao24}')

    def release_follow(self):
        with self._lock:
            prev              = self._follow
            self._follow      = None
            self._follow_data = None
        if prev:
            event_log.log('DETECT', 'WARN', f'ADS-B lock released — {prev}')

    def get_state(self) -> dict:
        with self._lock:
            return {
                'active':      self._active,
                'flights':     list(self._flights),
                'follow_icao': self._follow,
                'follow_data': dict(self._follow_data) if self._follow_data else None,
                'center':      {'lat': self._lat, 'lon': self._lon} if self._lat else None,
                'error':       self._error,
            }

    # ── Background thread ─────────────────────────────────────────────────────

    def _run(self):
        while True:
            with self._lock:
                active = self._active
                lat    = self._lat
                lon    = self._lon
                follow = self._follow

            if not active or lat is None:
                time.sleep(1)
                continue

            try:
                raw     = _fetch(lat, lon)
                flights = []
                for ac in raw:
                    p = _parse(ac)
                    if p and _in_box(p['lat'], p['lon'], lat, lon):
                        flights.append(p)

                follow_data = None
                if follow:
                    for f in flights:
                        if f['icao24'] == follow:
                            follow_data = f
                            break

                new_lat, new_lon, shifted = lat, lon, False
                if follow_data:
                    new_lat, new_lon, shifted = _edge_follow(
                        follow_data['lat'], follow_data['lon'], lat, lon
                    )

                with self._lock:
                    self._flights     = flights
                    self._follow_data = follow_data
                    self._error       = None
                    if shifted:
                        self._lat = new_lat
                        self._lon = new_lon

                if shifted:
                    event_log.log('SYSTEM', 'INFO',
                                  f'Camera repointed — ({new_lat:.4f}, {new_lon:.4f})')

            except Exception as e:
                with self._lock:
                    self._error = str(e)

            time.sleep(POLL_INTERVAL)


adsb_poller = ADSBPoller()

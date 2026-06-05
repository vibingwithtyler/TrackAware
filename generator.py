import math
import time
import io
import threading
import random
from PIL import Image, ImageDraw
from logger import event_log

# ── Scenario state ────────────────────────────────────────────────────────────
_scenario_lock = threading.Lock()
_scenario = 'circular_orbit'

SCENARIOS = {
    'circular_orbit': 'Circular Orbit',
    'square_pattern': 'Square Pattern',
    'tracking':       'Tracking Scenario',
}

def set_scenario(name: str):
    global _scenario
    if name not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {name}")
    with _scenario_lock:
        _scenario = name

def get_scenario() -> str:
    with _scenario_lock:
        return _scenario


# ── Static objects ────────────────────────────────────────────────────────────
_static_lock = threading.Lock()
_static_objects: list[tuple[int, int]] = []


# ── ADS-B mode flag ──────────────────────────────────────────────────────────
_adsb_lock = threading.Lock()
_adsb_mode = False

def set_adsb_mode(active: bool):
    global _adsb_mode
    with _adsb_lock:
        _adsb_mode = active

def get_adsb_mode() -> bool:
    with _adsb_lock:
        return _adsb_mode


# ── Marine mode flag ──────────────────────────────────────────────────────────
_marine_lock = threading.Lock()
_marine_mode = False

def set_marine_mode(active: bool):
    global _marine_mode
    with _marine_lock:
        _marine_mode = active

def get_marine_mode() -> bool:
    with _marine_lock:
        return _marine_mode


# ── Ghost detects ─────────────────────────────────────────────────────────────
_GHOST_LIFETIME = 5.0    # seconds each ghost detect lives
_GHOST_MIN_INT  = 5.0    # min seconds between spawn attempts
_GHOST_MAX_INT  = 10.0   # max seconds between spawn attempts

_ghost_lock    = threading.Lock()
_ghost_detects: list[dict] = []   # [{px, py, expires_at, id}]
_ghost_next_t  = 0.0              # wall time for next spawn attempt
_ghost_id_seq  = 1                # TGT-001 = real mover; ghosts start at 002


def _tick_ghost_detects():
    global _ghost_next_t, _ghost_id_seq
    with _adsb_lock:
        if _adsb_mode:
            return
    with _marine_lock:
        if _marine_mode:
            return
    now = time.time()
    new_detect = None
    expired_ids = []
    with _ghost_lock:
        if now >= _ghost_next_t:
            if random.random() < 0.5:
                margin = 10
                px = random.randint(margin, WIDTH - margin)
                py = random.randint(margin, HEIGHT - margin)
                _ghost_id_seq += 1
                gid = f'TGT-{_ghost_id_seq:03d}'
                _ghost_detects.append({'px': px, 'py': py, 'expires_at': now + _GHOST_LIFETIME, 'id': gid})
                new_detect = (gid, px, py)
            _ghost_next_t = now + _GHOST_MIN_INT + random.random() * (_GHOST_MAX_INT - _GHOST_MIN_INT)
        alive = [g for g in _ghost_detects if g['expires_at'] > now]
        expired_ids = [g['id'] for g in _ghost_detects if g['expires_at'] <= now]
        _ghost_detects[:] = alive
    if new_detect:
        gid, px, py = new_detect
        event_log.log('DETECT', 'INFO', f'{gid} acquired — pixel ({px}, {py})')
    for gid in expired_ids:
        event_log.log('DETECT', 'WARN', f'{gid} lost')


def clear_ghost_detects():
    expired_ids = []
    with _ghost_lock:
        expired_ids = [g['id'] for g in _ghost_detects]
        _ghost_detects.clear()
    for gid in expired_ids:
        event_log.log('DETECT', 'WARN', f'{gid} lost')


def get_ghost_detections() -> list[dict]:
    with _ghost_lock:
        return [{'x': float(g['px']), 'y': float(g['py']), 'area': 4, 'id': g['id']}
                for g in _ghost_detects]

def add_static_object() -> tuple[int, int]:
    margin = 20
    x = random.randint(margin, WIDTH - margin)
    y = random.randint(margin, HEIGHT - margin)
    with _static_lock:
        _static_objects.append((x, y))
    return x, y

def clear_static_objects() -> int:
    with _static_lock:
        count = len(_static_objects)
        _static_objects.clear()
    return count


# ── Scene constants ───────────────────────────────────────────────────────────
WIDTH  = 500
HEIGHT = 500
RADIUS = 2
FPS    = 30

# Circular orbit
ORBIT_RADIUS  = 180
ANGULAR_SPEED = 2 * math.pi / (FPS * 4)

# Square pattern — 30% margin
_SQ_MARGIN = int(WIDTH * 0.30)
_SQ_X0, _SQ_Y0 = _SQ_MARGIN, _SQ_MARGIN
_SQ_X1, _SQ_Y1 = WIDTH - _SQ_MARGIN, HEIGHT - _SQ_MARGIN
_SQ_SIDE   = _SQ_X1 - _SQ_X0
_SQ_PERIM  = 4 * _SQ_SIDE
_SQ_PERIOD = FPS * 4

# Tracking scenario — large world space
WORLD_W = 5000
WORLD_H = 5000
TRACKING_ORBIT_R = 1800
TRACKING_PERIOD  = FPS * 60                              # 60-second orbit
TRACKING_SPEED   = 2 * math.pi / TRACKING_PERIOD

VP_MARGIN = int(WIDTH * 0.20)                            # 100px — 20% of viewport


# ── Viewport state (tracking scenario) ───────────────────────────────────────
_vp_lock        = threading.Lock()
_vx: float      = 0.0
_vy: float      = 0.0
_vp_initialized = False
_vp_version     = 0     # incremented on every change
_vp_is_init     = True  # True when the version bump is just initialization


def reset_tracking_viewport():
    """Call before starting the tracking scenario so viewport re-centers on frame 1."""
    global _vp_initialized
    with _vp_lock:
        _vp_initialized = False


def get_viewport_state() -> tuple[float, float, int, bool]:
    """Returns (vx, vy, version, is_init)."""
    with _vp_lock:
        return _vx, _vy, _vp_version, _vp_is_init


def _update_viewport(wx: float, wy: float):
    global _vx, _vy, _vp_initialized, _vp_version, _vp_is_init
    shifted = False
    with _vp_lock:
        if not _vp_initialized:
            _vx = wx - WIDTH  / 2
            _vy = wy - HEIGHT / 2
            _vp_initialized = True
            _vp_is_init     = True
            _vp_version    += 1
        else:
            old_vx = _vx
            old_vy = _vy
            rel_x = wx - _vx
            rel_y = wy - _vy
            changed = False

            if rel_x < VP_MARGIN:                  # near left  → place at 20% from right
                _vx = wx - (WIDTH - VP_MARGIN)
                changed = True
            elif rel_x > WIDTH - VP_MARGIN:        # near right → place at 20% from left
                _vx = wx - VP_MARGIN
                changed = True

            if rel_y < VP_MARGIN:                  # near top   → place at 20% from bottom
                _vy = wy - (HEIGHT - VP_MARGIN)
                changed = True
            elif rel_y > HEIGHT - VP_MARGIN:       # near bottom → place at 20% from top
                _vy = wy - VP_MARGIN
                changed = True

            if changed:
                _vp_is_init  = False
                _vp_version += 1
                cx = _vx + WIDTH  / 2
                cy = _vy + HEIGHT / 2
                dx = _vx - old_vx
                dy = _vy - old_vy
                event_log.log('SYSTEM', 'INFO',
                              f'Camera repointed — world center ({cx:.0f}, {cy:.0f})'
                              f'  Δ({dx:+.0f}, {dy:+.0f})')
                shifted = True
    if shifted:
        clear_ghost_detects()


# ── Position functions ────────────────────────────────────────────────────────
def _circular_pos(t: float) -> tuple[float, float]:
    return (
        WIDTH  / 2 + ORBIT_RADIUS * math.cos(t * ANGULAR_SPEED),
        HEIGHT / 2 + ORBIT_RADIUS * math.sin(t * ANGULAR_SPEED),
    )

def _square_pos(t: float) -> tuple[float, float]:
    dist = (t % _SQ_PERIOD) / _SQ_PERIOD * _SQ_PERIM
    s = _SQ_SIDE
    if dist < s:
        return _SQ_X0 + dist, float(_SQ_Y0)
    elif dist < 2 * s:
        return float(_SQ_X1), _SQ_Y0 + (dist - s)
    elif dist < 3 * s:
        return _SQ_X1 - (dist - 2 * s), float(_SQ_Y1)
    else:
        return float(_SQ_X0), _SQ_Y1 - (dist - 3 * s)

def _tracking_pos(t: float) -> tuple[float, float]:
    return (
        WORLD_W / 2 + TRACKING_ORBIT_R * math.cos(t * TRACKING_SPEED),
        WORLD_H / 2 + TRACKING_ORBIT_R * math.sin(t * TRACKING_SPEED),
    )


# ── Drawing helpers ───────────────────────────────────────────────────────────
def _draw_test_pattern(draw: ImageDraw.ImageDraw) -> None:
    DIM    = (60, 60, 60)
    BRIGHT = (120, 120, 120)
    for x in range(0, WIDTH, 50):
        draw.line([(x, 0), (x, HEIGHT)], fill=DIM, width=1)
    for y in range(0, HEIGHT, 50):
        draw.line([(0, y), (WIDTH, y)], fill=DIM, width=1)
    cx, cy = WIDTH // 2, HEIGHT // 2
    draw.line([(cx - 20, cy), (cx + 20, cy)], fill=BRIGHT, width=2)
    draw.line([(cx, cy - 20), (cx, cy + 20)], fill=BRIGHT, width=2)
    draw.ellipse([cx - 40, cy - 40, cx + 40, cy + 40], outline=DIM, width=1)
    L = 18
    corners = [(10, 10), (WIDTH-10, 10), (10, HEIGHT-10), (WIDTH-10, HEIGHT-10)]
    dirs    = [(1, 1), (-1, 1), (1, -1), (-1, -1)]
    for (bx, by), (dx, dy) in zip(corners, dirs):
        draw.line([(bx, by), (bx + dx*L, by)], fill=BRIGHT, width=2)
        draw.line([(bx, by), (bx, by + dy*L)], fill=BRIGHT, width=2)


def make_background() -> bytes:
    """Return the static background for the current scenario."""
    img  = Image.new("RGB", (WIDTH, HEIGHT), color=(0, 0, 0))
    if get_scenario() != 'tracking':
        _draw_test_pattern(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def make_frame(t: float) -> bytes:
    suppress = False
    with _adsb_lock:
        suppress = _adsb_mode
    if not suppress:
        with _marine_lock:
            suppress = _marine_mode
    if suppress:
        img = Image.new("RGB", (WIDTH, HEIGHT), color=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    img  = Image.new("RGB", (WIDTH, HEIGHT), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)

    with _scenario_lock:
        scenario = _scenario

    if scenario == 'circular_orbit':
        _draw_test_pattern(draw)
        cx, cy = _circular_pos(t)

    elif scenario == 'square_pattern':
        _draw_test_pattern(draw)
        cx, cy = _square_pos(t)

    else:  # tracking
        wx, wy = _tracking_pos(t)
        _update_viewport(wx, wy)
        with _vp_lock:
            vx, vy = _vx, _vy
        cx, cy = wx - vx, wy - vy

    draw.ellipse(
        [cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS],
        fill=(255, 255, 255),
    )

    if scenario != 'tracking':
        with _static_lock:
            statics = list(_static_objects)
        for sx, sy in statics:
            draw.ellipse(
                [sx - RADIUS, sy - RADIUS, sx + RADIUS, sy + RADIUS],
                fill=(255, 255, 255),
            )

    _tick_ghost_detects()
    with _ghost_lock:
        ghosts = list(_ghost_detects)
    for g in ghosts:
        gx, gy = g['px'], g['py']
        draw.ellipse(
            [gx - RADIUS, gy - RADIUS, gx + RADIUS, gy + RADIUS],
            fill=(255, 255, 255),
        )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()

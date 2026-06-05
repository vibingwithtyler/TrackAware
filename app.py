import json
import random
import time
import uuid
from flask import Flask, Response, render_template, jsonify, request
from producer import producer
from adsb import adsb_poller
from marine import marine_poller
from generator import (add_static_object, clear_static_objects,
                        set_scenario, get_scenario, SCENARIOS,
                        reset_tracking_viewport, get_viewport_state,
                        set_adsb_mode, set_marine_mode)
from logger import event_log

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

SERVER_ID = uuid.uuid4().hex[:8]

_OP_WORDS = ['WATCHFIRE', 'IRONVEIL', 'SHADOWGATE', 'COLDWATCH', 'DARKSTAR',
             'NIGHTFALL', 'STEELPOINT', 'VANGUARD', 'BLACKWATCH', 'STARFALL']
MISSION_ID      = f"OP-{random.choice(_OP_WORDS)}-{random.randint(1, 99):02d}"
MISSION_STARTED = time.time()

event_log.log('SYSTEM', 'INFO', f'Server started — {MISSION_ID} — port 8080')


@app.route("/")
def index():
    from flask import redirect
    return redirect("/map")


@app.route("/stream/raw")
def stream_raw():
    return Response(
        producer.stream("raw"),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/stream/bg")
def stream_bg():
    return Response(
        producer.stream("bg"),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/add-static", methods=["POST"])
def add_static():
    x, y = add_static_object()
    event_log.log('USER', 'INFO', f'Static object placed ({x}, {y})')
    return jsonify(x=x, y=y)


@app.route("/clear-static", methods=["POST"])
def clear_static():
    count = clear_static_objects()
    event_log.log('USER', 'INFO', f'Cleared {count} static object(s)')
    return jsonify(cleared=count)


@app.route("/events/stream")
def events_stream():
    def generate():
        last_frame = -1
        last_version = None
        last_vx = 0.0
        last_vy = 0.0
        while True:
            frame_num, detections = producer.get_detections()
            if frame_num != last_frame:
                last_frame = frame_num
                payload = json.dumps({"frame": frame_num, "detections": detections})
                yield f"event: detections\ndata: {payload}\n\n"

            vx, vy, version, is_init = get_viewport_state()
            if version != last_version:
                if last_version is not None and not is_init:
                    dx = vx - last_vx
                    dy = vy - last_vy
                    yield f"event: viewport\ndata: {json.dumps({'dx': round(dx, 1), 'dy': round(dy, 1)})}\n\n"
                last_vx = vx
                last_vy = vy
                last_version = version

            time.sleep(1 / 15)
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/map")
def map_view():
    return render_template("map.html", mission_id=MISSION_ID, started_at=int(MISSION_STARTED))


@app.route("/scenario", methods=["GET", "POST"])
def scenario():
    if request.method == "POST":
        name = request.json.get("scenario")
        try:
            set_scenario(name)
            if name == "tracking":
                reset_tracking_viewport()
            producer.reset_subtractor()
            event_log.log('SYSTEM', 'INFO', f'Scenario → {name}')
            return jsonify(ok=True, scenario=name)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
    return jsonify(scenario=get_scenario(), available=SCENARIOS)



@app.route("/vision")
def vision():
    return render_template("vision.html", mission_id=MISSION_ID, started_at=int(MISSION_STARTED))


@app.route("/log")
def log_view():
    return render_template("log.html", mission_id=MISSION_ID, started_at=int(MISSION_STARTED))


@app.route("/log/stream")
def log_stream():
    def generate():
        for evt in event_log.get_recent(500):
            yield f"data: {json.dumps(evt)}\n\n"
        q = event_log.subscribe()
        try:
            while True:
                while q:
                    yield f"data: {json.dumps(q.popleft())}\n\n"
                time.sleep(0.05)
        finally:
            event_log.unsubscribe(q)
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/log/poll")
def log_poll():
    after = request.args.get('after', 0, type=int)
    events = event_log.get_since(after)
    return jsonify(events=events, server_id=SERVER_ID)


@app.route("/stream/status")
def stream_status():
    frame_num, detections = producer.get_detections()
    return jsonify(frame=frame_num)


@app.route("/adsb/flights")
def adsb_flights():
    return jsonify(adsb_poller.get_state())


@app.route("/adsb/mode", methods=["POST"])
def adsb_mode():
    data   = request.get_json()
    active = bool(data.get("active"))
    set_adsb_mode(active)
    if active:
        adsb_poller.activate(float(data["lat"]), float(data["lon"]))
    else:
        adsb_poller.deactivate()
        producer.reset_subtractor()   # reseed background model on return to synthetic
    return jsonify(ok=True)


@app.route("/adsb/follow", methods=["POST"])
def adsb_follow():
    data   = request.get_json()
    icao24 = data.get("icao24")
    if icao24:
        adsb_poller.set_follow(icao24)
    else:
        adsb_poller.release_follow()
    return jsonify(ok=True)


@app.route("/marine/vessels")
def marine_vessels():
    return jsonify(marine_poller.get_state())


@app.route("/marine/mode", methods=["POST"])
def marine_mode():
    data   = request.get_json()
    active = bool(data.get("active"))
    set_marine_mode(active)
    if active:
        marine_poller.activate(float(data["lat"]), float(data["lon"]))
    else:
        marine_poller.deactivate()
        producer.reset_subtractor()
    return jsonify(ok=True)


@app.route("/marine/follow", methods=["POST"])
def marine_follow():
    data = request.get_json()
    mmsi = data.get("mmsi")
    if mmsi:
        marine_poller.set_follow(str(mmsi))
    else:
        marine_poller.release_follow()
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)

import json
import random
import time
import uuid
from flask import Flask, Response, redirect, render_template, jsonify, request
from adsb import adsb_poller
from marine import marine_poller
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
    return redirect("/map")


@app.route("/map")
def map_view():
    return render_template("map.html", mission_id=MISSION_ID, started_at=int(MISSION_STARTED))



@app.route("/adsb/flights")
def adsb_flights():
    return jsonify(adsb_poller.get_state())


@app.route("/adsb/mode", methods=["POST"])
def adsb_mode():
    data   = request.get_json()
    active = bool(data.get("active"))
    if active:
        adsb_poller.activate(float(data["lat"]), float(data["lon"]))
    else:
        adsb_poller.deactivate()
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
    if active:
        marine_poller.activate(float(data["lat"]), float(data["lon"]))
    else:
        marine_poller.deactivate()
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

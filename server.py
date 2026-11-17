"""Flask web app: dashboard + JSON API. Run `python server.py`."""
import hmac
import json
import os

import pandas as pd
from flask import Flask, Response, jsonify, request, send_from_directory, abort

from scanner import Scanner, Store

HERE = os.path.dirname(os.path.abspath(__file__))


def create_app(config_path=None, start_scanner=True):
    config_path = config_path or os.environ.get("CONFIG", os.path.join(HERE, "config.json"))
    cfg = json.load(open(config_path))
    store = Store(os.environ.get("DB_PATH", os.path.join(HERE, "signals.db")))
    scanner = Scanner(cfg, store)
    app = Flask(__name__, static_folder=os.path.join(HERE, "static"))
    app.scanner, app.store = scanner, store

    token = os.environ.get("ACCESS_TOKEN", "")

    @app.before_request
    def guard():
        if request.path.startswith("/api/") and request.path != "/api/health" and token:
            sent = request.headers.get("X-Token") or request.args.get("token", "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                abort(401)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify(ok=True, last_scan=scanner.last_scan)

    @app.get("/api/state")
    def state():
        return jsonify(scanner.snapshot())

    @app.get("/api/signals")
    def signals():
        after = request.args.get("after", 0, type=int)
        limit = min(request.args.get("limit", 50, type=int), 200)
        return jsonify(store.list(after, limit))

    @app.get("/api/ea/poll")
    def ea_poll():
        """Plain-text feed for the MT5 EA.

        Line 1:  OK|<max_signal_id>
        Then one line per new signal:
          id|symbol|side|sl_dist|tp1_dist|tp2_dist|age_seconds|grade|setup
        Distances are price distances from entry, so the EA applies them to ITS
        broker's fill price (broker and data-feed prices differ slightly).
        after=-1 returns only the first line (used to skip old history on first run).
        """
        after = request.args.get("after", -1, type=int)
        mt_mode = request.args.get("mode", "")[:10]
        scanner.ea = {"at": pd.Timestamp.now(tz="UTC").isoformat(),
                      "account": request.args.get("acct", "")[:20], "mode": mt_mode,
                      "dry": request.args.get("dry", "1") == "1"}
        lines = [f"OK|{store.max_id()}"]
        if after >= 0:
            for s in store.after_asc(after):
                sl_d, tp1_d = abs(s["entry"] - s["sl"]), abs(s["tp1"] - s["entry"])
                tp2_d = max(abs(s["tp2"] - s["entry"]), tp1_d)   # never let TP2 sit nearer than TP1
                lines.append("|".join([str(s["id"]), s["symbol"], s["side"], f"{sl_d:.8f}", f"{tp1_d:.8f}",
                                       f"{tp2_d:.8f}", str(s["age"]), s["grade"],
                                       s["setup"].replace("|", "/")]))
        return Response("\n".join(lines) + "\n", mimetype="text/plain")

    if start_scanner:
        scanner.start()
    return app


if __name__ == "__main__":
    if not os.environ.get("ACCESS_TOKEN") and os.environ.get("HOST", "127.0.0.1") != "127.0.0.1":
        print("WARNING: no ACCESS_TOKEN set and the server is public. Set ACCESS_TOKEN.")
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"),
                     port=int(os.environ.get("PORT", 8000)), threaded=True)
else:
    # gunicorn server:app  (use ONE worker so there is one scanner)
    app = create_app() if os.environ.get("RUN_SCANNER", "1") == "1" else None

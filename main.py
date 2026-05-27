"""Cloud Run entry point for the CDS hourly pipeline."""
import logging

from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/trigger", methods=["POST"])
def trigger():
    from app.backend.scheduler import _collect_all
    _collect_all()
    return jsonify({"status": "done"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

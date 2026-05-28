"""Cloud Run entry point for the CDS hourly pipeline."""
import logging
import sys
from pathlib import Path

# Ensure `from backend.xxx` imports resolve correctly.
# scheduler.py does this for itself, but main.py routes need it too.
_APP_DIR = Path(__file__).resolve().parent / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

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


@app.route("/test-bq", methods=["GET"])
def test_bq():
    """Smoke-test: query prod-tech BQ directly and return a few rows from latest_sbar_fact."""
    try:
        from app.backend.services.bq_client import get_bq_client
        rows = get_bq_client().execute_select(
            "SELECT cpmrn, encounters, sbar_id, urgency"
            " FROM `prod-tech-project1-bv479-zo027.patient.latest_sbar_fact`"
            " LIMIT 3"
        )
        return jsonify({"status": "ok", "rows": rows, "count": len(rows)})
    except Exception as exc:
        logging.exception("test-bq failed")
        return jsonify({"status": "error", "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

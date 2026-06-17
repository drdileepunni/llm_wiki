"""Cloud Run entry point for the CDS hourly pipeline."""
import logging
import sys
from pathlib import Path

# Ensure `from backend.xxx` imports resolve correctly.
# scheduler.py does this for itself, but main.py routes need it too.
_APP_DIR = Path(__file__).resolve().parent / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from flask import Flask, jsonify, request

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


@app.route("/alert-feedback", methods=["POST"])
def alert_feedback():
    """
    Receive a Google Chat button-click event relayed by the chat-microservice.

    The alert card's Submit button carries parameters (action_id = alert_id,
    cb_token) and form inputs (rating 1-5, optional feedback_text). Stores the
    rating in cds_study.study_alert_feedback and returns a hostAppDataAction
    that updates the card in place.
    """
    import os

    event = request.get_json(silent=True) or {}
    common = event.get("commonEventObject", {}) or {}
    parameters = common.get("parameters", {}) or {}
    form_inputs = common.get("formInputs", {}) or {}

    expected_token = os.getenv("ALERT_FEEDBACK_TOKEN", "")
    if not expected_token or parameters.get("cb_token") != expected_token:
        logging.warning("alert-feedback: rejected request with bad/missing cb_token")
        return jsonify({"error": "unauthorized"}), 401

    def form_value(name):
        values = form_inputs.get(name, {}).get("stringInputs", {}).get("value", [])
        return values[0].strip() if values else ""

    chat_event = event.get("chat", {}) or {}
    user = chat_event.get("user", {}) or {}
    user_email = user.get("email", "unknown")
    user_display = user.get("displayName", "unknown")
    payload = chat_event.get("buttonClickedPayload", {}) or {}
    message = payload.get("message", {}) or {}
    space_name = (payload.get("space", {}) or {}).get("name", "")

    alert_id = parameters.get("action_id", "")
    rating = form_value("rating")
    note = form_value("feedback_text")

    if not rating:
        return jsonify({
            "hostAppDataAction": {"chatDataAction": {"createMessageAction": {
                "message": {"text": f"⚠️ {user_display}, please select a rating (1–5) before submitting."}
            }}}
        })

    logging.info(
        "alert-feedback: alert_id=%s rated %s/5 by %s (note=%r)",
        alert_id, rating, user_email, note,
    )

    from backend.services.bq_store import get_bq_store
    store = get_bq_store()

    # Validate alert_id against study_alerts.  If it doesn't match a real alert
    # row, the click came from a stale or test card — reject it rather than
    # polluting the feedback table with a fake ID.
    cpmrn, encounter, problem_name = "", None, ""
    try:
        rows = store.find_alerts({"alert_id": alert_id})
        if rows:
            cpmrn = rows[0].get("CPMRN", "")
            encounter = rows[0].get("encounter")
            problem_name = rows[0].get("problem_name", "")
        else:
            logging.warning(
                "alert-feedback: alert_id=%r not found in study_alerts — "
                "rejecting feedback from stale/test card",
                alert_id,
            )
            return jsonify({
                "hostAppDataAction": {"chatDataAction": {"createMessageAction": {
                    "message": {"text": (
                        "⚠️ This alert card is outdated and can no longer accept feedback. "
                        "Please rate from the current alert card."
                    )}
                }}}
            })
    except Exception:
        logging.exception("alert-feedback: study_alerts lookup failed for %s", alert_id)

    try:
        store.insert_alert_feedback({
            "alert_id": alert_id,
            "CPMRN": cpmrn,
            "encounter": encounter,
            "problem_name": problem_name,
            "rating": int(rating),
            "feedback_text": note or None,
            "user_email": user_email,
            "user_display": user_display,
            "space_name": space_name,
            "message_name": message.get("name", ""),
        })
    except Exception:
        logging.exception("alert-feedback: BQ insert failed for %s", alert_id)
        return jsonify({
            "hostAppDataAction": {"chatDataAction": {"createMessageAction": {
                "message": {"text": "⚠️ Could not record your feedback. Please try again later."}
            }}}
        })

    status_text = f"{'⭐' * int(rating)} Rated <b>{rating}/5</b> by {user_display}"
    if note:
        status_text += f"<br>💬 <i>{note}</i>"

    from tools.radar_sync.alert_cards import replace_rating_section_with_status
    updated_cards = replace_rating_section_with_status(message.get("cardsV2", []), status_text)

    return jsonify({
        "hostAppDataAction": {"chatDataAction": {"updateMessageAction": {
            "message": {"cardsV2": updated_cards}
        }}}
    })


@app.route("/order-action", methods=["POST"])
def order_action():
    """
    Receive a Google Chat Submit click from a medication-reconciliation card and
    execute the ticked order changes against the EMR.

    The card's Submit button carries parameters (action=order_recon_submit,
    action_set_id, cb_token) and form inputs (edit_actions / discontinue_actions /
    new_actions — the CHECK_BOX values the clinician left ticked). We load the
    proposed action set from GCS, filter to the selected keys, apply them
    sequentially via order_actions.apply_order_actions, and return a
    hostAppDataAction that replaces the card's checkboxes with a results summary.
    """
    import os

    event = request.get_json(silent=True) or {}
    common = event.get("commonEventObject", {}) or {}
    parameters = common.get("parameters", {}) or {}
    form_inputs = common.get("formInputs", {}) or {}

    expected_token = os.getenv("ALERT_FEEDBACK_TOKEN", "")
    if not expected_token or parameters.get("cb_token") != expected_token:
        logging.warning("order-action: rejected request with bad/missing cb_token")
        return jsonify({"error": "unauthorized"}), 401

    def selected(name):
        return form_inputs.get(name, {}).get("stringInputs", {}).get("value", []) or []

    chat_event = event.get("chat", {}) or {}
    user = chat_event.get("user", {}) or {}
    user_display = user.get("displayName", "unknown")
    payload = chat_event.get("buttonClickedPayload", {}) or {}
    message = payload.get("message", {}) or {}

    action_set_id = parameters.get("action_set_id", "")
    selected_keys = selected("edit_actions") + selected("discontinue_actions") + selected("new_actions")

    from tools.radar_sync.order_action_store import load_action_set, select_actions
    from tools.radar_sync.order_recon_card import replace_actions_with_results

    action_set = load_action_set(action_set_id)
    if not action_set:
        logging.warning("order-action: action_set %r not found", action_set_id)
        return jsonify({
            "hostAppDataAction": {"chatDataAction": {"createMessageAction": {
                "message": {"text": (
                    "⚠️ This reconciliation card is outdated and can no longer be applied. "
                    "Please regenerate it."
                )}
            }}}
        })

    if not selected_keys:
        return jsonify({
            "hostAppDataAction": {"chatDataAction": {"createMessageAction": {
                "message": {"text": f"⚠️ {user_display}, no changes were selected — nothing to apply."}
            }}}
        })

    actions = select_actions(action_set, selected_keys)
    cpmrn = action_set.get("cpmrn", "")
    encounter = action_set.get("encounter", 1)

    logging.info(
        "order-action: applying %d action(s) for %s enc=%d by %s",
        len(actions), cpmrn, encounter, user_display,
    )

    from tools.radar_sync.order_actions import apply_order_actions
    try:
        results = apply_order_actions(cpmrn, encounter, actions)
    except Exception:
        logging.exception("order-action: apply failed for action_set %s", action_set_id)
        return jsonify({
            "hostAppDataAction": {"chatDataAction": {"createMessageAction": {
                "message": {"text": "⚠️ Could not apply the order changes. Please try again."}
            }}}
        })

    updated_cards = replace_actions_with_results(message.get("cardsV2", []), results)
    return jsonify({
        "hostAppDataAction": {"chatDataAction": {"updateMessageAction": {
            "message": {"cardsV2": updated_cards}
        }}}
    })


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

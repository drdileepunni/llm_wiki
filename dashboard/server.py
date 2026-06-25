"""
Flask server for the local CDS pipeline dashboard.
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, render_template, request
import json as _json

from .config_writer import save_app_setting, save_monitoring_protocol
from .metrics import (
    get_summary, get_timeseries, get_by_problem,
    get_by_rater, get_cost, get_comments, get_ratings_per_alert,
    get_alerts_per_run, get_runs, get_run_patient_audit,
)
from .audit import get_active_next_checks, get_documentation_audits, get_patient_detail
from .agreement import compute_agreement
from .config_reader import (
    get_monitoring_protocols, get_lab_alert_rules,
    get_symptom_alert_rules, get_operational_settings,
    get_lab_staleness_overrides,
)

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(_HERE / "templates"),
        static_folder=str(_HERE / "static"),
        static_url_path="/static",
    )

    # ── page routes ──────────────────────────────────────────────────────────

    @app.route("/")
    def dashboard_page():
        return render_template("dashboard.html")

    @app.route("/config")
    def config_page():
        return render_template("config.html")

    @app.route("/docs")
    def docs_page():
        return render_template("docs.html")

    @app.route("/audit")
    def audit_page():
        return render_template("audit.html")

    # ── helpers ───────────────────────────────────────────────────────────────

    def _date_params():
        return request.args.get("from"), request.args.get("to")

    def _ok(data):
        return jsonify({"ok": True, "data": data})

    def _err(msg, code=500):
        return jsonify({"ok": False, "error": str(msg)}), code

    # ── metrics API ──────────────────────────────────────────────────────────

    @app.route("/api/metrics/summary")
    def api_summary():
        try:
            return _ok(get_summary(*_date_params()))
        except Exception as e:
            log.exception("api_summary failed")
            return _err(e)

    @app.route("/api/metrics/timeseries")
    def api_timeseries():
        try:
            bucket = request.args.get("bucket", "day")
            return _ok(get_timeseries(bucket, *_date_params()))
        except Exception as e:
            log.exception("api_timeseries failed")
            return _err(e)

    @app.route("/api/metrics/by-problem")
    def api_by_problem():
        try:
            return _ok(get_by_problem(*_date_params()))
        except Exception as e:
            log.exception("api_by_problem failed")
            return _err(e)

    @app.route("/api/metrics/by-rater")
    def api_by_rater():
        try:
            return _ok(get_by_rater(*_date_params()))
        except Exception as e:
            log.exception("api_by_rater failed")
            return _err(e)

    @app.route("/api/metrics/cost")
    def api_cost():
        try:
            return _ok(get_cost(*_date_params()))
        except Exception as e:
            log.exception("api_cost failed")
            return _err(e)

    @app.route("/api/metrics/alerts-per-run")
    def api_alerts_per_run():
        try:
            return _ok(get_alerts_per_run(*_date_params()))
        except Exception as e:
            log.exception("api_alerts_per_run failed")
            return _err(e)

    @app.route("/api/metrics/runs")
    def api_runs():
        try:
            return _ok(get_runs(*_date_params()))
        except Exception as e:
            log.exception("api_runs failed")
            return _err(e)

    @app.route("/api/metrics/run-audit")
    def api_run_audit():
        run_ts = request.args.get("run")
        if not run_ts:
            return _err("Missing 'run' query parameter (ISO timestamp)", 400)
        try:
            return _ok(get_run_patient_audit(run_ts))
        except Exception as e:
            log.exception("api_run_audit failed")
            return _err(e)

    # ── audit API ─────────────────────────────────────────────────────────────

    @app.route("/api/audit/next-checks")
    def api_audit_next_checks():
        try:
            return _ok(get_active_next_checks())
        except Exception as e:
            log.exception("api_audit_next_checks failed")
            return _err(e)

    @app.route("/api/audit/documentation")
    def api_audit_documentation():
        try:
            hours_back = int(request.args.get("hours_back", 72))
            return _ok(get_documentation_audits(hours_back=hours_back))
        except Exception as e:
            log.exception("api_audit_documentation failed")
            return _err(e)

    @app.route("/api/audit/patient/<cpmrn>/<int:enc>")
    def api_audit_patient(cpmrn: str, enc: int):
        try:
            return _ok(get_patient_detail(cpmrn, enc))
        except Exception as e:
            log.exception("api_audit_patient failed for %s enc=%d", cpmrn, enc)
            return _err(e)

    @app.route("/api/metrics/agreement")
    def api_agreement():
        try:
            ratings = get_ratings_per_alert(*_date_params())
            result  = compute_agreement(ratings)
            return _ok(result)
        except Exception as e:
            log.exception("api_agreement failed")
            return _err(e)

    @app.route("/api/feedback/comments")
    def api_comments():
        try:
            limit = int(request.args.get("limit", 50))
            return _ok(get_comments(limit, *_date_params()))
        except Exception as e:
            log.exception("api_comments failed")
            return _err(e)

    # ── config API ────────────────────────────────────────────────────────────

    @app.route("/api/config/protocols")
    def api_protocols():
        try:
            return _ok(get_monitoring_protocols())
        except Exception as e:
            log.exception("api_protocols failed")
            return _err(e)

    @app.route("/api/config/lab-rules")
    def api_lab_rules():
        try:
            return _ok(get_lab_alert_rules())
        except Exception as e:
            log.exception("api_lab_rules failed")
            return _err(e)

    @app.route("/api/config/symptom-rules")
    def api_symptom_rules():
        try:
            return _ok(get_symptom_alert_rules())
        except Exception as e:
            log.exception("api_symptom_rules failed")
            return _err(e)

    @app.route("/api/config/lab-staleness-overrides")
    def api_lab_staleness_overrides():
        try:
            return _ok(get_lab_staleness_overrides())
        except Exception as e:
            log.exception("api_lab_staleness_overrides failed")
            return _err(e)

    @app.route("/api/config/operational")
    def api_operational():
        try:
            return _ok(get_operational_settings())
        except Exception as e:
            log.exception("api_operational failed")
            return _err(e)

    # ── config write API (PUT) ────────────────────────────────────────────────

    def _parse_body() -> dict:
        """Parse JSON body, raising ValueError on bad input."""
        body = request.get_data(as_text=True)
        try:
            return _json.loads(body)
        except Exception:
            raise ValueError("Request body is not valid JSON")

    @app.route("/api/config/lab-rules", methods=["PUT"])
    def api_save_lab_rules():
        try:
            doc = _parse_body()
            if "rules" not in doc:
                return _err("Missing 'rules' key", 400)
            save_app_setting("lab_alert_rules", doc)
            return _ok({"saved": "lab_alert_rules"})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as e:
            log.exception("api_save_lab_rules failed")
            return _err(e)

    @app.route("/api/config/symptom-rules", methods=["PUT"])
    def api_save_symptom_rules():
        try:
            doc = _parse_body()
            if "rules" not in doc:
                return _err("Missing 'rules' key", 400)
            save_app_setting("symptom_alert_rules", doc)
            return _ok({"saved": "symptom_alert_rules"})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as e:
            log.exception("api_save_symptom_rules failed")
            return _err(e)

    @app.route("/api/config/lab-staleness-overrides", methods=["PUT"])
    def api_save_lab_staleness_overrides():
        try:
            doc = _parse_body()
            if "overrides" not in doc:
                return _err("Missing 'overrides' key", 400)
            if not isinstance(doc["overrides"], dict):
                return _err("'overrides' must be a JSON object mapping lab name to hours", 400)
            # Validate values are positive integers
            for lab, hours in doc["overrides"].items():
                if not isinstance(hours, (int, float)) or hours <= 0:
                    return _err(f"Invalid value for '{lab}': must be a positive number of hours", 400)
            save_app_setting("lab_staleness_overrides", doc)
            return _ok({"saved": "lab_staleness_overrides"})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as e:
            log.exception("api_save_lab_staleness_overrides failed")
            return _err(e)

    @app.route("/api/config/study-pipeline", methods=["PUT"])
    def api_save_study_pipeline():
        try:
            doc = _parse_body()
            enabled = bool(doc.get("enabled", False))
            save_app_setting("study_pipeline_enabled", {"enabled": enabled})
            return _ok({"saved": "study_pipeline_enabled", "enabled": enabled})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as e:
            log.exception("api_save_study_pipeline failed")
            return _err(e)

    @app.route("/api/config/med-recon", methods=["PUT"])
    def api_save_med_recon():
        try:
            doc = _parse_body()
            enabled = bool(doc.get("enabled", True))
            # med_recon_config also carries categories/name_patterns/key_field — read-merge
            # so toggling `enabled` never clobbers the discovery config.
            from .config_reader import _get_db
            existing = {}
            try:
                existing = _get_db()["app_settings"].find_one({"_id": "med_recon_config"}) or {}
            except Exception:
                log.exception("api_save_med_recon: could not read existing med_recon_config")
            existing = dict(existing)
            existing["enabled"] = enabled
            save_app_setting("med_recon_config", existing)
            return _ok({"saved": "med_recon_config", "enabled": enabled})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as e:
            log.exception("api_save_med_recon failed")
            return _err(e)

    @app.route("/api/config/operational/<setting_id>", methods=["PUT"])
    def api_save_operational(setting_id: str):
        allowed = {"alert_recipients", "gchat_webhook", "monitored_workspaces"}
        if setting_id not in allowed:
            return _err(f"Unknown setting '{setting_id}'", 400)
        try:
            doc = _parse_body()
            save_app_setting(setting_id, doc)
            return _ok({"saved": setting_id})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as e:
            log.exception("api_save_operational failed")
            return _err(e)

    @app.route("/api/config/protocols", methods=["PUT"])
    def api_save_protocol():
        try:
            protocol = _parse_body()
            if not protocol.get("protocol_id"):
                return _err("Missing 'protocol_id'", 400)
            save_monitoring_protocol(protocol)
            return _ok({"saved": protocol["protocol_id"]})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as e:
            log.exception("api_save_protocol failed")
            return _err(e)

    # ── docs API ──────────────────────────────────────────────────────────────

    @app.route("/api/docs")
    def api_docs_list():
        docs_dir = _HERE / "docs"
        docs = []
        for md in sorted(docs_dir.glob("*.md")):
            first_line = md.read_text(encoding="utf-8").strip().splitlines()[0]
            title = first_line.lstrip("#").strip()
            docs.append({"slug": md.stem, "title": title})
        return _ok(docs)

    @app.route("/api/docs/<slug>")
    def api_doc(slug: str):
        docs_dir = _HERE / "docs"
        path = docs_dir / f"{slug}.md"
        if not path.exists():
            return _err("doc not found", 404)
        content = path.read_text(encoding="utf-8")
        return _ok({"slug": slug, "markdown": content})

    # ── health ────────────────────────────────────────────────────────────────

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    return app

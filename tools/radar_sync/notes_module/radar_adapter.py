"""Convert Radar/pat_json patient object into normalized DataFrames and note list."""
import re
from datetime import datetime
from typing import Any

import pandas as pd


def _parse_ts(ts: str | None) -> pd.Timestamp | None:
    if not ts:
        return None
    try:
        return pd.to_datetime(ts, utc=True)
    except Exception:
        return None


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def build_labs_df(patient: dict) -> pd.DataFrame:
    """Build labs_df from documents with category 'labs' and attributes."""
    rows = []
    for doc in patient.get("documents") or []:
        if doc.get("category") != "labs":
            continue
        reported_at = doc.get("reportedAt")
        time_ts = _parse_ts(reported_at)
        if time_ts is None:
            continue
        attrs = doc.get("attributes") or {}
        if isinstance(attrs, dict):
            for name, attr in attrs.items():
                if not isinstance(attr, dict):
                    continue
                raw = attr.get("value")
                value = _safe_float(raw)
                if value is None and raw is not None and str(raw).strip():
                    continue  # skip non-numeric for now, or add as string row
                if value is None:
                    continue
                err = attr.get("errorRange") or {}
                val_range = attr.get("validationRange") or {}
                ref_low = _safe_float(err.get("min") or val_range.get("min"))
                ref_high = _safe_float(err.get("max") or val_range.get("max"))
                rows.append({
                    "time": time_ts,
                    "name": name,
                    "value": value,
                    "unit": "",
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                })
    if not rows:
        return pd.DataFrame(columns=["time", "name", "value", "unit", "ref_low", "ref_high", "name_norm"])
    df = pd.DataFrame(rows)
    df["name_norm"] = df["name"].astype(str).str.lower().str.strip()
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def _vital_float(v: Any) -> float | None:
    if v is None:
        return None
    return _safe_float(v)


def build_vitals_df(patient: dict) -> pd.DataFrame:
    """Build vitals_df from vitals[]; map days* fields to standard names."""
    vitals_list = patient.get("vitals") or []
    rows = []
    col_map = {
        "daysHR": "HR",
        "daysRR": "RR",
        "daysBP": "SBP",  # single BP field -> SBP
        "daysMAP": "MAP",
        "daysSpO2": "SpO2",
        "daysTemperature": "TempC",
        "daysFiO2": "FiO2",
        "daysCVP": "CVP",
        "daysVentPEEP": "PEEP",
        "daysVentPip": "Pip",
        "daysVentRRset": "VentRRset",
    }
    for v in vitals_list:
        ts = v.get("timestamp")
        time_ts = _parse_ts(ts)
        if time_ts is None:
            continue
        row = {"time": time_ts}
        for radarkey, col in col_map.items():
            val = _vital_float(v.get(radarkey))
            if val is not None:
                row[col] = val
        if len(row) > 1:
            rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["time"])
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_med_admin_df(patient: dict) -> pd.DataFrame:
    """Build med_admin_df from orders active/inactive/completed/pending/pta medications."""
    rows = []
    orders = patient.get("orders") or {}
    for status in ("active", "inactive", "completed", "pending", "pta"):
        for med in (orders.get(status) or {}).get("medications") or []:
            start = med.get("startTime")
            time_ts = _parse_ts(start)
            if time_ts is None:
                time_ts = _parse_ts(med.get("createdAt"))
            if time_ts is None:
                continue
            name = med.get("name") or ""
            qty = med.get("quantity")
            unit = (med.get("unit") or "").strip()
            rate = _safe_float(qty)
            rows.append({
                "time": time_ts,
                "drug": name,
                "rate": rate,
                "rate_unit": unit,
                "route": med.get("route") or "",
                "form": med.get("form") or "",
                "category": status,
            })
    if not rows:
        return pd.DataFrame(columns=["time", "drug", "rate", "rate_unit", "route", "form", "category", "drug_norm"])
    df = pd.DataFrame(rows)
    df["drug_norm"] = df["drug"].astype(str).str.lower().str.strip()
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_io_df(patient: dict) -> pd.DataFrame:
    """Build io_df from io.days[].hours[].minutes[] intake/output."""
    rows = []
    io = patient.get("io") or {}
    days = io.get("days") or []
    for day in days:
        day_date_str = day.get("dayDate")
        if not day_date_str:
            continue
        try:
            day_dt = datetime.fromisoformat(day_date_str.replace("Z", "+00:00"))
        except Exception:
            continue
        for hour_blk in day.get("hours") or []:
            h = hour_blk.get("hourName", 0)
            for min_blk in hour_blk.get("minutes") or []:
                m = min_blk.get("minuteName", 0)
                try:
                    time_ts = pd.Timestamp(day_dt.replace(hour=int(h) if isinstance(h, int) else 0, minute=int(m) if isinstance(m, int) else 0).replace(tzinfo=day_dt.tzinfo), tz="UTC")
                except Exception:
                    time_ts = pd.to_datetime(day_date_str, utc=True)
                intake = min_blk.get("intake") or {}
                out = min_blk.get("output") or {}
                # Intake: meds.infusion, meds.bolus, feeds
                for inf in (intake.get("meds") or {}).get("infusion") or []:
                    amt = _safe_float(inf.get("amount"))
                    if amt is not None:
                        rows.append({"time": time_ts, "type": "intake_infusion", "volume_ml": amt})
                for bol in (intake.get("meds") or {}).get("bolus") or []:
                    amt = _safe_float(bol.get("amount"))
                    if amt is not None:
                        rows.append({"time": time_ts, "type": "intake_bolus", "volume_ml": amt})
                feeds = intake.get("feeds") or {}
                if isinstance(feeds, dict):
                    for feed_key, feed_val in feeds.items():
                        if isinstance(feed_val, dict) and "amount" in feed_val:
                            amt = _safe_float(feed_val.get("amount"))
                            if amt is not None:
                                rows.append({"time": time_ts, "type": f"intake_feed_{feed_key}", "volume_ml": amt})
                # Output: drain, procedure, dialysis
                for d in out.get("drain") or []:
                    amt = _safe_float(d.get("amount")) if isinstance(d, dict) else _safe_float(d)
                    if amt is not None:
                        rows.append({"time": time_ts, "type": "output_drain", "volume_ml": amt})
                for p in out.get("procedure") or []:
                    amt = _safe_float(p.get("amount")) if isinstance(p, dict) else _safe_float(p)
                    if amt is not None:
                        rows.append({"time": time_ts, "type": "output_procedure", "volume_ml": amt})
                for d in out.get("dialysis") or []:
                    amt = _safe_float(d.get("amount")) if isinstance(d, dict) else _safe_float(d)
                    if amt is not None:
                        rows.append({"time": time_ts, "type": "output_dialysis", "volume_ml": amt})
    if not rows:
        return pd.DataFrame(columns=["time", "type", "volume_ml"])
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_diets_df(patient: dict) -> pd.DataFrame:
    """Build diets_df from orders active/inactive/completed/pending/pta diets."""
    rows = []
    orders = patient.get("orders") or {}
    for status in ("active", "inactive", "completed", "pending", "pta"):
        for diet in (orders.get(status) or {}).get("diets") or []:
            start = diet.get("startTime")
            time_ts = _parse_ts(start)
            if time_ts is None:
                time_ts = _parse_ts(diet.get("createdAt"))
            if time_ts is None:
                continue
            name = diet.get("name") or ""
            rate_obj = diet.get("rate") or {}
            rate_value = _safe_float(rate_obj.get("value")) if isinstance(rate_obj, dict) else None
            rate_unit = (rate_obj.get("unit") or "") if isinstance(rate_obj, dict) else ""
            rows.append({
                "time": time_ts,
                "name": name,
                "instructions": diet.get("instructions") or "",
                "rate_value": rate_value,
                "rate_unit": rate_unit,
                "category": status,
            })
    if not rows:
        return pd.DataFrame(columns=["time", "name", "instructions", "rate_value", "rate_unit", "category", "name_norm"])
    df = pd.DataFrame(rows)
    df["name_norm"] = df["name"].astype(str).str.lower().str.strip()
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_procedures_df(patient: dict) -> pd.DataFrame:
    """Build procedures_df from orders active/inactive/completed/pending/pta procedures."""
    rows = []
    orders = patient.get("orders") or {}
    for status in ("active", "inactive", "completed", "pending", "pta"):
        for proc in (orders.get(status) or {}).get("procedures") or []:
            start = proc.get("startTime")
            time_ts = _parse_ts(start)
            if time_ts is None:
                time_ts = _parse_ts(proc.get("createdAt"))
            if time_ts is None:
                continue
            p_type = proc.get("pType") or proc.get("name") or ""
            rows.append({
                "time": time_ts,
                "pType": p_type,
                "site": proc.get("site") or "",
                "laterality": proc.get("laterality") or "",
                "instructions": proc.get("instructions") or "",
                "category": status,
            })
    if not rows:
        return pd.DataFrame(columns=["time", "pType", "site", "laterality", "instructions", "category", "pType_norm"])
    df = pd.DataFrame(rows)
    df["pType_norm"] = df["pType"].astype(str).str.lower().str.strip()
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_communications_df(patient: dict) -> pd.DataFrame:
    """Build communications_df from orders active/inactive/completed/pending/pta communications."""
    rows = []
    orders = patient.get("orders") or {}
    for status in ("active", "inactive", "completed", "pending", "pta"):
        for comm in (orders.get(status) or {}).get("communications") or []:
            start = comm.get("startTime")
            time_ts = _parse_ts(start)
            if time_ts is None:
                time_ts = _parse_ts(comm.get("createdAt"))
            if time_ts is None:
                continue
            name = comm.get("name") or ""
            message = comm.get("message") or comm.get("instructions") or ""
            rows.append({
                "time": time_ts,
                "name": name,
                "message": message,
                "category": status,
            })
    if not rows:
        return pd.DataFrame(columns=["time", "name", "message", "category"])
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_lab_orders_df(patient: dict) -> pd.DataFrame:
    """Build lab_orders_df from orders active/inactive/completed/pending/pta labs (distinct from lab results in documents)."""
    rows = []
    orders = patient.get("orders") or {}
    for status in ("active", "inactive", "completed", "pending", "pta"):
        for lab_order in (orders.get(status) or {}).get("labs") or []:
            start = lab_order.get("startTime")
            time_ts = _parse_ts(start)
            if time_ts is None:
                time_ts = _parse_ts(lab_order.get("createdAt"))
            if time_ts is None:
                continue
            investigation = lab_order.get("investigation") or ""
            rows.append({
                "time": time_ts,
                "investigation": investigation,
                "discipline": lab_order.get("discipline") or "",
                "specimenType": lab_order.get("specimenType") or "",
                "category": status,
            })
    if not rows:
        return pd.DataFrame(columns=["time", "investigation", "discipline", "specimenType", "category", "investigation_norm"])
    df = pd.DataFrame(rows)
    df["investigation_norm"] = df["investigation"].astype(str).str.lower().str.strip()
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_bloods_df(patient: dict) -> pd.DataFrame:
    """Build bloods_df from orders active/inactive/completed/pending/pta bloods."""
    rows = []
    orders = patient.get("orders") or {}
    for status in ("active", "inactive", "completed", "pending", "pta"):
        for blood in (orders.get(status) or {}).get("bloods") or []:
            start = blood.get("startTime")
            time_ts = _parse_ts(start)
            if time_ts is None:
                time_ts = _parse_ts(blood.get("createdAt"))
            if time_ts is None:
                continue
            name = blood.get("name") or ""
            rows.append({
                "time": time_ts,
                "name": name,
                "category": status,
            })
    if not rows:
        return pd.DataFrame(columns=["time", "name", "category"])
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_vents_df(patient: dict) -> pd.DataFrame:
    """Build vents_df from orders active/inactive/completed/pending/pta vents."""
    rows = []
    orders = patient.get("orders") or {}
    for status in ("active", "inactive", "completed", "pending", "pta"):
        for vent in (orders.get(status) or {}).get("vents") or []:
            start = vent.get("startTime")
            time_ts = _parse_ts(start)
            if time_ts is None:
                time_ts = _parse_ts(vent.get("createdAt"))
            if time_ts is None:
                continue
            name = vent.get("name") or ""
            rows.append({
                "time": time_ts,
                "name": name,
                "category": status,
            })
    if not rows:
        return pd.DataFrame(columns=["time", "name", "category"])
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def build_notes_df(patient: dict) -> pd.DataFrame:
    """Build notes_df from notes.finalNotes and draftNotes; strip HTML from components."""
    rows = []
    notes_obj = patient.get("notes") or {}
    for source in ("finalNotes", "draftNotes"):
        for note in (notes_obj.get(source) or []):
            for content in (note.get("content") or []):
                ts = content.get("timestamp") or note.get("createdTimestamp")
                time_ts = _parse_ts(ts)
                note_type = (content.get("noteType") or "") + " / " + (content.get("noteSubType") or "")
                author = ""
                if isinstance(content.get("author"), dict):
                    author = (content.get("author") or {}).get("name") or ""
                parts = []
                for comp in (content.get("components") or []):
                    if isinstance(comp, dict) and comp.get("value"):
                        parts.append(_strip_html(comp["value"]))
                text = "\n".join(p for p in parts if p.strip())
                if not text and time_ts is None:
                    continue
                rows.append({
                    "time": time_ts if time_ts is not None else pd.NaT,
                    "type": note_type.strip(" / "),
                    "author": author,
                    "text": text,
                })
    if not rows:
        return pd.DataFrame(columns=["time", "type", "author", "text"])
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


def normalize_patient(patient: dict) -> dict:
    """
    Normalize a single Radar patient object into DataFrames and metadata.
    Returns dict with: admission_id, start_time, end_time, labs_df, vitals_df, med_admin_df, io_df, notes_df,
    diets_df, procedures_df, communications_df, lab_orders_df, bloods_df, vents_df.
    """
    admission_id = (patient.get("CPMRN") or patient.get("_id") or "unknown") + "_" + (str(patient.get("visitId") or patient.get("_id") or ""))
    start_ts = _parse_ts(patient.get("ICUAdmitDate"))
    end_ts = _parse_ts(patient.get("ICUDischargeDate") or patient.get("updatedAt") or patient.get("ICUAdmitDate"))
    if start_ts is None:
        start_ts = pd.Timestamp.now(tz="UTC")
    if end_ts is None:
        end_ts = start_ts

    labs_df = build_labs_df(patient)
    vitals_df = build_vitals_df(patient)
    med_admin_df = build_med_admin_df(patient)
    io_df = build_io_df(patient)
    notes_df = build_notes_df(patient)
    diets_df = build_diets_df(patient)
    procedures_df = build_procedures_df(patient)
    communications_df = build_communications_df(patient)
    lab_orders_df = build_lab_orders_df(patient)
    bloods_df = build_bloods_df(patient)
    vents_df = build_vents_df(patient)

    # Coerce numeric columns where applicable
    all_dfs = [
        ("labs_df", labs_df), ("vitals_df", vitals_df), ("med_admin_df", med_admin_df), ("io_df", io_df),
        ("diets_df", diets_df), ("procedures_df", procedures_df), ("communications_df", communications_df),
        ("lab_orders_df", lab_orders_df), ("bloods_df", bloods_df), ("vents_df", vents_df),
    ]
    for df_name, df in all_dfs:
        if df is not None and not df.empty and "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True)
    if not labs_df.empty and "value" in labs_df.columns:
        labs_df["value"] = pd.to_numeric(labs_df["value"], errors="coerce")
    if not io_df.empty and "volume_ml" in io_df.columns:
        io_df["volume_ml"] = pd.to_numeric(io_df["volume_ml"], errors="coerce")
    if not diets_df.empty and "rate_value" in diets_df.columns:
        diets_df["rate_value"] = pd.to_numeric(diets_df["rate_value"], errors="coerce")

    return {
        "admission_id": admission_id,
        "start_time": start_ts,
        "end_time": end_ts,
        "labs_df": labs_df,
        "vitals_df": vitals_df,
        "med_admin_df": med_admin_df,
        "io_df": io_df,
        "notes_df": notes_df,
        "diets_df": diets_df,
        "procedures_df": procedures_df,
        "communications_df": communications_df,
        "lab_orders_df": lab_orders_df,
        "bloods_df": bloods_df,
        "vents_df": vents_df,
    }

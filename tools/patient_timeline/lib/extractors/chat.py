"""
Extract timeline events from chat.json.

One event per chat message. Event type determined by:
  - isDeleted: true  → CHAT_DELETED
  - attachment.name present → CHAT_FILE_UPLOAD  (note: attachment with no name = text msg)
  - otherwise → CHAT_MESSAGE
"""

from ..models import (
    TimelineEvent,
    CHAT_MESSAGE, CHAT_FILE_UPLOAD, CHAT_ACKNOWLEDGED, CHAT_DELETED,
)
from ..date_utils import parse_timestamp


def _get_id(msg: dict) -> str:
    return str(msg.get("_id") or "")


def _classify_event(msg: dict) -> str:
    if msg.get("isDeleted"):
        return CHAT_DELETED
    attachment = msg.get("attachment") or {}
    if attachment.get("name"):
        return CHAT_FILE_UPLOAD
    return CHAT_MESSAGE


def _file_size_kb(attachment: dict) -> str:
    size = attachment.get("size")
    if size is not None:
        try:
            return f"{int(size) // 1024}KB"
        except (TypeError, ValueError):
            pass
    return ""


def extract_chat_events(messages: list[dict]) -> list[TimelineEvent]:
    events = []

    for msg in messages:
        event_type = _classify_event(msg)
        sender = msg.get("sender") or {}
        actor_name = sender.get("name") or sender.get("email") or "Unknown"
        actor_role = sender.get("role") or "Unknown"

        attachment = msg.get("attachment") or {}
        message_text = (msg.get("message") or "").strip()
        added_to_docs = msg.get("addedToDocuments", False)
        hera_status = attachment.get("heraStatus") or ""

        if event_type == CHAT_FILE_UPLOAD:
            fname = attachment.get("name") or "file"
            ftype = attachment.get("fileType") or ""
            size_str = _file_size_kb(attachment)
            size_part = f", {size_str}" if size_str else ""
            hera_part = f" [HERA: {hera_status}]" if hera_status else ""
            docs_part = " → added to documents" if added_to_docs else ""
            summary = f"File uploaded: {fname} ({ftype}{size_part}){hera_part}{docs_part}"
            detail = (f"file={fname}, type={ftype}, size={attachment.get('size','')}, "
                      f"heraStatus={hera_status}, addedToDocuments={added_to_docs}, "
                      f"isDuplicate={attachment.get('isDuplicateDocument', False)}")

        elif event_type == CHAT_DELETED:
            summary = f"Message deleted (sender: {actor_name})"
            detail = f"isDeleted=True, message={message_text[:100]}"

        else:
            text_excerpt = message_text[:80] + ("..." if len(message_text) > 80 else "")
            if message_text:
                summary = f'Message: "{text_excerpt}"'
            else:
                summary = "Message (no text)"
            detail = message_text
            if added_to_docs:
                summary += " [added to documents]"

        utc, ist, shift = parse_timestamp(msg.get("timestamp") or msg.get("createdAt"))
        if utc is None:
            continue

        events.append(TimelineEvent(
            timestamp_utc=utc,
            timestamp_ist=ist,
            shift=shift,
            source_file="chat.json",
            source_id=_get_id(msg),
            event_category="CHAT",
            event_type=event_type,
            actor_name=actor_name,
            actor_role=actor_role,
            actor_type="human",
            summary=summary,
            detail=detail,
        ))

    return events

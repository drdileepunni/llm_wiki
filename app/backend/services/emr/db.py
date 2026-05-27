from backend.services.gcs_store import get_gcs_db


def get_db():
    return get_gcs_db()

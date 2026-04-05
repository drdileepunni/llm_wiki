from fastapi import APIRouter
from sqlalchemy import desc
from ..database import Session, TokenLog
from ..config import PRICING, MODEL

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

@router.get("/stats")
def get_stats():
    with Session() as session:
        rows = session.query(TokenLog).all()

        total_cost = sum(r.cost_usd for r in rows)
        total_input = sum(r.input_tokens for r in rows)
        total_output = sum(r.output_tokens for r in rows)

        ingests = [r for r in rows if r.operation == "ingest"]
        chats = [r for r in rows if r.operation == "chat"]

        avg_ingest_cost = sum(r.cost_usd for r in ingests) / len(ingests) if ingests else 0
        avg_chat_cost = sum(r.cost_usd for r in chats) / len(chats) if chats else 0

        # Projections
        proj_100_sources = avg_ingest_cost * 100
        proj_monthly_chat = avg_chat_cost * 30  # assume 30 queries/month

        return {
            "total_cost_usd": round(total_cost, 4),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_operations": len(rows),
            "ingest_count": len(ingests),
            "chat_count": len(chats),
            "avg_ingest_cost_usd": round(avg_ingest_cost, 4),
            "avg_chat_cost_usd": round(avg_chat_cost, 4),
            "projection_100_sources_usd": round(proj_100_sources, 2),
            "projection_monthly_chat_usd": round(proj_monthly_chat, 2),
        }

@router.get("/log")
def get_log(limit: int = 50):
    with Session() as session:
        rows = session.query(TokenLog).order_by(desc(TokenLog.timestamp)).limit(limit).all()
        return [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat(),
                "operation": r.operation,
                "source_name": r.source_name,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": round(r.cost_usd, 4),
                "model": r.model,
            }
            for r in rows
        ]

@router.get("/timeseries")
def get_timeseries():
    """Cumulative cost over time for chart."""
    with Session() as session:
        rows = session.query(TokenLog).order_by(TokenLog.timestamp).all()
        cumulative = 0
        result = []
        for r in rows:
            cumulative += r.cost_usd
            result.append({
                "date": r.timestamp.strftime("%Y-%m-%d"),
                "cumulative_cost": round(cumulative, 4),
                "operation": r.operation,
            })
        return result

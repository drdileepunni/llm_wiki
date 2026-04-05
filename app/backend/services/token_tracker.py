from ..database import Session, TokenLog
from ..config import PRICING, MODEL
from datetime import datetime

def calculate_cost(input_tokens: int, output_tokens: int, model: str = MODEL) -> float:
    pricing = PRICING.get(model, PRICING["claude-sonnet-4-5"])
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

def log_call(operation: str, source_name: str, input_tokens: int, output_tokens: int, model: str = MODEL):
    cost = calculate_cost(input_tokens, output_tokens, model)
    with Session() as session:
        entry = TokenLog(
            timestamp=datetime.utcnow(),
            operation=operation,
            source_name=source_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            model=model,
        )
        session.add(entry)
        session.commit()
    return cost

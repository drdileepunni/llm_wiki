from ..database import Session, TokenLog
from ..config import get_pricing, MODEL
from datetime import datetime


def calculate_cost(input_tokens: int, output_tokens: int, model: str = MODEL) -> float:
    """
    Calculate cost in USD.  Passes input_tokens to get_pricing so that
    context-length-tiered models (e.g. gemini-2.5-pro) use the correct tier.
    """
    pricing = get_pricing(model, input_tokens=input_tokens)
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def log_call(
    operation: str,
    source_name: str,
    input_tokens: int,
    output_tokens: int,
    model: str = MODEL,
) -> float:
    """
    Compute cost and persist a TokenLog row.  Returns cost in USD.

    Always pass `model` explicitly — the default is only a last-resort
    fallback for callers that genuinely don't know which model was used.
    """
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

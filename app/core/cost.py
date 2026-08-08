from app.core.config import settings


def compute_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute request cost from the price table using provider-reported token usage.

    Pricing is expressed as $ per 1K tokens, separately for input/output,
    which mirrors how OpenAI/Anthropic publish their price tables.
    """
    pricing = settings.model_pricing.get(model)
    if pricing is None:
        raise ValueError(f"No pricing entry for model '{model}'")
    cost = (input_tokens / 1000.0) * pricing["input_per_1k"] + (output_tokens / 1000.0) * pricing["output_per_1k"]
    return round(cost, 8)


def provider_for(model: str) -> str:
    pricing = settings.model_pricing.get(model)
    if pricing is None:
        raise ValueError(f"No pricing entry for model '{model}'")
    return pricing["provider"]

import time
import json

from flask import Blueprint, request, jsonify, Response, stream_with_context, g

from app.core.auth import authenticate, check_model_allowed, admit_rate_limit, admit_budget, \
    record_actual_spend, GatewayRejection
from app.core.router import resolve_candidates, resolve_auto, run_with_failover, run_stream_with_failover
from app.core.cost import compute_cost_usd
from app.core.cache import lookup as cache_lookup, store as cache_store
from app.core.logs import log_request
from app.core.observability import new_request_id, log_event
from app.providers.base import ProviderError

bp = Blueprint("chat", __name__)


def _extract_prompt_text(messages):
    return " ".join(m.get("content", "") for m in messages if m.get("role") == "user")


def _rough_input_tokens(messages) -> int:
    text = " ".join(m.get("content", "") for m in messages)
    return max(1, int(len(text.split()) * 1.3))


def _openai_shape(text: str, model: str) -> dict:
    return {
        "id": "chatcmpl-prism-mock",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }


def _error(status, err_type, message, request_id=None):
    payload = {"error": {"type": err_type, "message": message}}
    if request_id:
        payload["request_id"] = request_id
    return jsonify(payload), status


@bp.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    request_id = new_request_id()
    start_all = time.time()
    body = request.get_json(force=True, silent=True) or {}
    requested_model = body.get("model")
    messages = body.get("messages", [])
    stream = bool(body.get("stream", False))

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        log_event("warning", "missing_or_malformed_auth_header", request_id=request_id)
        return _error(401, "invalid_key", "Missing or malformed Authorization header. Use 'Bearer <key>'.", request_id)
    plaintext_key = auth_header[len("Bearer "):].strip()

    if not requested_model or not messages:
        return _error(400, "bad_request", "'model' and 'messages' are required.", request_id)

    rec = None
    rl_remaining, rl_reset = None, None
    try:
        rec = authenticate(plaintext_key)
        check_model_allowed(rec, requested_model)
        rl_remaining, rl_reset = admit_rate_limit(rec.key_hash, rec.rpm_limit)
        admit_budget(rec)
    except GatewayRejection as rej:
        log_request(rec.key_prefix if rec else "unknown", rec.tenant if rec else "unknown",
                     requested_model, None, None, "rejected", request_id=request_id, reject_reason=rej.reason)
        log_event("warning", "request_rejected", request_id=request_id, reason=rej.reason)
        err_resp, status = _error(rej.http_status, rej.reason, rej.message, request_id)
        if rej.reason == "rate_limited":
            err_resp.headers["Retry-After"] = "60"
            err_resp.headers["X-RateLimit-Limit"] = str(rec.rpm_limit) if rec else ""
            err_resp.headers["X-RateLimit-Remaining"] = "0"
        return err_resp, status

    log_event("info", "request_admitted", request_id=request_id, tenant=rec.tenant,
               requested_model=requested_model, stream=stream)

    prompt_text = _extract_prompt_text(messages)

    auto_classified_as = None
    if requested_model == "auto":
        plan = resolve_auto(prompt_text)
        auto_classified_as = plan.auto_classified_as
    else:
        try:
            plan = resolve_candidates(requested_model)
        except ValueError as e:
            return _error(400, "unknown_model", str(e), request_id)

    cached = cache_lookup(rec.tenant, messages)
    if cached and not stream:
        log_request(rec.key_prefix, rec.tenant, requested_model, cached["resolved_model"], cached["provider"],
                     "ok", request_id=request_id, input_tokens=cached["input_tokens"],
                     output_tokens=cached["output_tokens"], cost_usd=0.0, cache_hit=True,
                     fallback_used=False, latency_ms=(time.time() - start_all) * 1000, stream=False)
        log_event("info", "cache_hit", request_id=request_id, similarity=round(cached.get("similarity", 0), 4))
        resp = _openai_shape(cached["response_text"], cached["resolved_model"])
        r = jsonify(resp)
        r.headers["x-prism-provider"] = cached["provider"]
        r.headers["x-prism-cache"] = "hit"
        r.headers["x-prism-fallback"] = "false"
        r.headers["x-prism-cost-usd"] = "0.0"
        r.headers["x-prism-request-id"] = request_id
        if rl_remaining is not None:
            r.headers["X-RateLimit-Limit"] = str(rec.rpm_limit)
            r.headers["X-RateLimit-Remaining"] = str(max(0, rl_remaining))
            r.headers["X-RateLimit-Reset"] = str(rl_reset)
        return r

    if stream:
        return _handle_stream(request_id, rec, requested_model, plan, messages, cached)

    start = time.time()
    try:
        outcome = run_with_failover(plan.candidates, messages)
    except ProviderError as e:
        log_request(rec.key_prefix, rec.tenant, requested_model, None, None, "error",
                     request_id=request_id, reject_reason=str(e))
        log_event("error", "upstream_exhausted", request_id=request_id, error=str(e))
        return _error(502, "upstream_error", str(e), request_id)
    latency_ms = (time.time() - start) * 1000

    cost = compute_cost_usd(outcome.resolved_model, outcome.result.input_tokens, outcome.result.output_tokens)
    record_actual_spend(rec.key_hash, cost)
    cache_store(rec.tenant, messages, outcome.result.text, outcome.resolved_model, outcome.provider,
                outcome.result.input_tokens, outcome.result.output_tokens)
    log_request(rec.key_prefix, rec.tenant, requested_model, outcome.resolved_model, outcome.provider, "ok",
                request_id=request_id, input_tokens=outcome.result.input_tokens,
                output_tokens=outcome.result.output_tokens, cost_usd=cost, cache_hit=False,
                fallback_used=outcome.fallback_used, latency_ms=latency_ms, stream=False)
    log_event("info", "request_completed", request_id=request_id, resolved_model=outcome.resolved_model,
               provider=outcome.provider, cost_usd=cost, latency_ms=round(latency_ms, 1),
               fallback_used=outcome.fallback_used)

    resp = _openai_shape(outcome.result.text, outcome.resolved_model)
    if auto_classified_as:
        resp["prism_auto_routed_to"] = auto_classified_as
    r = jsonify(resp)
    r.headers["x-prism-provider"] = outcome.provider
    r.headers["x-prism-cache"] = "miss"
    r.headers["x-prism-fallback"] = "true" if outcome.fallback_used else "false"
    r.headers["x-prism-cost-usd"] = f"{cost:.8f}"
    r.headers["x-prism-request-id"] = request_id
    if rl_remaining is not None:
        r.headers["X-RateLimit-Limit"] = str(rec.rpm_limit)
        r.headers["X-RateLimit-Remaining"] = str(max(0, rl_remaining))
        r.headers["X-RateLimit-Reset"] = str(rl_reset)
    return r


def _sse_chunk(text_piece: str, model: str) -> dict:
    return {
        "id": "chatcmpl-prism-mock",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text_piece}, "finish_reason": None}],
    }


def _handle_stream(request_id, rec, requested_model, plan, messages, cached):
    def event_gen():
        start = time.time()
        if cached:
            for w in cached["response_text"].split(" "):
                chunk = _sse_chunk(w + " ", cached["resolved_model"])
                yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
            log_request(rec.key_prefix, rec.tenant, requested_model, cached["resolved_model"], cached["provider"],
                        "ok", request_id=request_id, input_tokens=cached["input_tokens"],
                        output_tokens=cached["output_tokens"], cost_usd=0.0, cache_hit=True, fallback_used=False,
                        latency_ms=(time.time() - start) * 1000, stream=True)
            return

        full_text_parts = []
        resolved_model = None
        provider = None
        fallback_used = False
        errored = False

        for event in run_stream_with_failover(plan.candidates, messages):
            if event["type"] == "chunk":
                resolved_model = event["model"]
                provider = event["provider"]
                fallback_used = event["fallback_used"]
                full_text_parts.append(event["text"])
                yield f"data: {json.dumps(_sse_chunk(event['text'], resolved_model))}\n\n"
            elif event["type"] == "done":
                resolved_model = event["model"]
                provider = event["provider"]
                fallback_used = event["fallback_used"]
                yield "data: [DONE]\n\n"
            elif event["type"] == "error":
                errored = True
                yield f"data: {json.dumps({'error': {'type': 'upstream_error', 'message': event['message']}})}\n\n"
                yield "data: [DONE]\n\n"

        latency_ms = (time.time() - start) * 1000
        full_text = "".join(full_text_parts)

        if errored or not resolved_model:
            log_request(rec.key_prefix, rec.tenant, requested_model, resolved_model, provider, "error",
                        request_id=request_id, latency_ms=latency_ms, stream=True)
            log_event("error", "stream_failed", request_id=request_id)
            return

        input_tokens = _rough_input_tokens(messages)
        output_tokens = max(1, int(len(full_text.split()) * 1.3))
        cost = compute_cost_usd(resolved_model, input_tokens, output_tokens)
        record_actual_spend(rec.key_hash, cost)
        cache_store(rec.tenant, messages, full_text, resolved_model, provider, input_tokens, output_tokens)
        log_request(rec.key_prefix, rec.tenant, requested_model, resolved_model, provider, "ok",
                    request_id=request_id, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
                    cache_hit=False, fallback_used=fallback_used, latency_ms=latency_ms, stream=True)
        log_event("info", "stream_completed", request_id=request_id, resolved_model=resolved_model,
                   provider=provider, cost_usd=cost, latency_ms=round(latency_ms, 1))

    headers = {
        "Cache-Control": "no-cache",
        "x-prism-cache": "hit" if cached else "miss",
        "x-prism-request-id": request_id,
    }
    return Response(stream_with_context(event_gen()), mimetype="text/event-stream", headers=headers)

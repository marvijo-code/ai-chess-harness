import argparse
import json
import os
import sys
import urllib.request


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def fetch_models(timeout: int) -> list[dict]:
    headers = {"Accept": "application/json"}
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(OPENROUTER_MODELS_URL, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("OpenRouter models response did not include a data list")
    return [model for model in data if isinstance(model, dict)]


def haystack(model: dict) -> str:
    fields = [
        model.get("id", ""),
        model.get("name", ""),
        model.get("description", ""),
        json.dumps(model.get("architecture", {}), separators=(",", ":")),
        json.dumps(model.get("top_provider", {}), separators=(",", ":")),
    ]
    return " ".join(str(field) for field in fields).lower()


def score_model(model: dict, terms: list[str], raw_query: str) -> tuple[int, str]:
    model_id = str(model.get("id", "")).lower()
    name = str(model.get("name", "")).lower()
    text = haystack(model)
    score = 0
    if raw_query and model_id == raw_query:
        score += 1000
    if raw_query and raw_query in model_id:
        score += 300
    if raw_query and raw_query in name:
        score += 250
    for term in terms:
        if term in model_id:
            score += 60
        elif term in name:
            score += 45
        elif term in text:
            score += 15
    return -score, str(model.get("id", ""))


def filter_models(models: list[dict], query: str) -> list[dict]:
    raw_query = query.strip().lower()
    terms = [term for term in raw_query.replace("/", " ").replace("-", " ").split() if term]
    if not terms:
        return sorted(models, key=lambda model: str(model.get("id", "")))
    matches = [model for model in models if all(term in haystack(model) for term in terms)]
    return sorted(matches, key=lambda model: score_model(model, terms, raw_query))


def price_text(model: dict) -> str:
    pricing = model.get("pricing") if isinstance(model.get("pricing"), dict) else {}
    prompt = pricing.get("prompt")
    completion = pricing.get("completion")
    if prompt is None and completion is None:
        return ""
    return f"in={prompt or '?'} out={completion or '?'}"


def render_table(models: list[dict]) -> str:
    rows = []
    for model in models:
        model_id = str(model.get("id", ""))
        name = str(model.get("name", ""))
        context = str(model.get("context_length") or "")
        price = price_text(model)
        rows.append((model_id, name, context, price))
    widths = [
        min(42, max([len("id"), *(len(row[0]) for row in rows)], default=2)),
        min(36, max([len("name"), *(len(row[1]) for row in rows)], default=4)),
        max([len("context"), *(len(row[2]) for row in rows)], default=7),
    ]
    lines = [
        f"{'id':<{widths[0]}}  {'name':<{widths[1]}}  {'context':>{widths[2]}}  price",
        f"{'-' * widths[0]}  {'-' * widths[1]}  {'-' * widths[2]}  -----",
    ]
    for model_id, name, context, price in rows:
        lines.append(
            f"{model_id[:widths[0]]:<{widths[0]}}  {name[:widths[1]]:<{widths[1]}}  {context:>{widths[2]}}  {price}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search OpenRouter models by id, name, provider, or description.")
    parser.add_argument("query", nargs="*", help="Search terms, for example: grok 4.3")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="Print full matching model metadata as JSON.")
    parser.add_argument("--first-id", action="store_true", help="Print only the first matching model id.")
    args = parser.parse_args()

    query = " ".join(args.query)
    models = filter_models(fetch_models(args.timeout), query)
    limited = models[: max(1, args.limit)]
    if args.first_id:
        if not limited:
            raise SystemExit(f"No OpenRouter models matched: {query!r}")
        print(limited[0].get("id", ""))
        return
    if args.json:
        print(json.dumps(limited, indent=2))
        return
    if not limited:
        print(f"No OpenRouter models matched: {query!r}", file=sys.stderr)
        raise SystemExit(1)
    print(render_table(limited))


if __name__ == "__main__":
    main()

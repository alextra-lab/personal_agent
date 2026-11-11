"""The retired SearXNG search form that the recorded FRE-1641 runs hold (FRE-1767).

``web_search`` called a SearXNG proxy when these runs were recorded; FRE-1766 replaced it with
direct Exa and Brave calls and FRE-1763 removed SearXNG. The harness can no longer record new
searches, so this module keeps only what re-scoring a recorded run needs.
"""

from __future__ import annotations

from typing import Any

#: The Exa highlight lengths the recorded runs hold, one ``search_<L>.jsonl.gz`` each (D2.1).
RECORDED_LENGTHS = (500, 800, 1200, 1600, 2000)
#: The production Exa length when the runs were recorded: the full form.
FULL_FORM_LENGTH = 2000


def shape_search_response(
    data: dict[str, Any],
    *,
    query: str,
    categories: str,
    engines: str | None,
    max_results: int,
) -> dict[str, Any]:
    """Shape a raw SearXNG JSON response into the ``web_search`` result.

    Moved here from ``tools/web.py`` by FRE-1767: ``web_search`` no longer has this form, and
    only the scoring of recorded runs reads it.

    Args:
        data: The parsed SearXNG JSON response.
        query: The query sent.
        categories: The categories sent.
        engines: The engines sent, or ``None``.
        max_results: The number of results to keep (already clamped to 1-50).

    Returns:
        The ``web_search`` result dict.
    """
    # SearXNG's own `number_of_results` total has been observed to read 0 on a
    # response whose `results` array held 20 real entries — it is independently
    # untrustworthy and must never be used as the count. `result_count` below is
    # always derived from `len(results)`.
    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "engine": item.get("engine", ""),
            "score": item.get("score"),
        }
        for item in (data.get("results") or [])[:max_results]
    ]

    # FRE-1339: SearXNG reports engines it suspended or that errored in the same
    # response (`[engine_name, reason]` pairs) and we were discarding it — a
    # collapsed engine pool then looked identical to a healthy one everywhere we
    # look (result_count, HTTP status), while the surviving low-quality engines'
    # junk filled the model's context. Surfacing it here, keyed on engine health
    # rather than result_count, keeps a legitimately empty search for an obscure
    # query (healthy engines, zero hits) from tripping this signal.
    unresponsive_engines = [
        {"engine": str(entry[0]), "reason": str(entry[1])}
        for entry in (data.get("unresponsive_engines") or [])
        if isinstance(entry, list | tuple) and len(entry) >= 2
    ]
    engines_contributed = sorted({r["engine"] for r in results if r["engine"]})
    degraded_retrieval = bool(unresponsive_engines)

    # Plugin answers (timezone, unit converter, calculator, openmeteo weather).
    # These are returned in the top-level "answers" array rather than "results".
    answers: list[dict[str, Any]] = []
    for item in data.get("answers") or []:
        engine = item.get("engine", "")
        if engine == "openmeteo" or item.get("service") == "Open-meteo":
            # Weather answer: surface the current conditions summary + location.
            current = item.get("current", {})
            location = current.get("location", {})
            answers.append(
                {
                    "engine": engine,
                    "type": "weather",
                    "location": location.get("name", ""),
                    "country": location.get("country_code", ""),
                    "summary": current.get("summary", ""),
                    "temperature": current.get("temperature", {}),
                    "condition": current.get("condition", ""),
                    "feels_like": current.get("feels_like", {}),
                    "humidity": current.get("humidity", {}),
                    "wind_speed": current.get("wind_speed", {}),
                }
            )
        else:
            text = item.get("answer")
            if text:
                answers.append({"engine": engine, "answer": text})

    output: dict[str, Any] = {
        "answers": answers,
        "results": results,
        "result_count": len(results),
        "suggestions": data.get("suggestions", []),
        "infoboxes": [
            {
                "title": ib.get("infobox", ""),
                "content": ib.get("content", "")[:500],
                "urls": [u.get("url") for u in ib.get("urls", [])[:3]],
            }
            for ib in (data.get("infoboxes") or [])[:2]
        ],
        "query": query,
        "categories_used": categories,
        "engines_used": engines,
        "unresponsive_engines": unresponsive_engines,
        "engines_contributed": engines_contributed,
        "degraded_retrieval": degraded_retrieval,
    }
    if degraded_retrieval:
        failed_names = ", ".join(e["engine"] for e in unresponsive_engines)
        output["retrieval_warning"] = (
            f"{len(unresponsive_engines)} engine(s) failed this search ({failed_names}); "
            f"results came only from: {', '.join(engines_contributed) or 'no engine'}. "
            "Treat this result set with caution — it may not be representative of what "
            "a healthy search would return."
        )
    return output

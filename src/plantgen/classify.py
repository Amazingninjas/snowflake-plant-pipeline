"""Classify downtime notes with a local model, the stand-in for Cortex on the emulator.

Same prompt as sql/04_ai_reasons.sql (prompts/downtime_reason.txt, read from bronze.prompts),
and the same constraint: the answer is JSON that can only name reason codes. Works with any
OpenAI-compatible server that takes a JSON schema in response_format (llama-server does).

Cortex gets one note per call. Here notes go in batches, because a hybrid model like Flash-Next
can't reuse the cached instructions between requests on llama-server, so every request rereads
them; with a batch it rereads them once per batch instead of once per note.
"""

import hashlib
import json
import time
import urllib.request
from collections.abc import Callable

from plantgen.emulate import query


def fill(template: str, machine: str, note: str) -> str:
    # Plain replace, like REPLACE() in SQL, so the two runs build the same text.
    return template.replace("{machine}", machine).replace("{note}", note)


def batch_prompt(template: str, stops: list[tuple[str, str]]) -> str:
    """The instructions once, then the stops numbered (machine, note)."""
    if len(stops) == 1:
        return fill(template, *stops[0])
    head = template[: template.index("Machine: {machine}")]
    lines = [f"{i}. Machine: {m} | Note: {n}" for i, (m, n) in enumerate(stops, 1)]
    return (
        head
        + f"There are {len(stops)} stops below. Give one reason for each, in the same order.\n\n"
        + "\n".join(lines)
        + "\n"
    )


def ask(url: str, prompt: str, reasons: list[str], n: int, timeout: float = 600) -> list[str]:
    """n reasons, in order (the schema makes the server return exactly n valid codes)."""
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 12 * n + 20,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "downtime_reasons",
                "schema": {
                    "type": "object",
                    "properties": {
                        "reasons": {
                            "type": "array",
                            "items": {"type": "string", "enum": reasons},
                            "minItems": n,
                            "maxItems": n,
                        }
                    },
                    "required": ["reasons"],
                },
            },
        },
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = json.load(resp)["choices"][0]["message"]["content"]
    try:
        out = json.loads(text)["reasons"]
    except (ValueError, KeyError, TypeError):
        return []
    return out if len(out) == n and all(r in reasons for r in out) else []


def _sample_key(event_id: str) -> str:
    return hashlib.sha1(event_id.encode()).hexdigest()


def classifier(url: str, model: str, sample: int | None = None, batch: int = 20) -> Callable:
    """A step for emulate.run(): classify stops that have no AI reason yet."""

    def classify(cur) -> None:
        (prompt,) = query(
            cur, "SELECT text FROM plant.bronze.prompts WHERE name = %s", ["downtime_reason"]
        )
        template = prompt["text"]
        reasons = sorted(r["reason"] for r in query(cur, "SELECT reason FROM plant.silver.reasons"))
        todo = query(
            cur,
            """SELECT d.event_id, e.kind, d.note
               FROM plant.silver.downtime d
               JOIN plant.bronze.ref_equipment e ON e.code = d.equipment
               LEFT JOIN plant.silver.downtime_ai a ON a.event_id = d.event_id
               WHERE a.event_id IS NULL""",
        )
        todo.sort(key=lambda r: _sample_key(r["event_id"]))  # a stable, spread-out sample
        if sample:
            todo = todo[:sample]
        t0 = time.monotonic()
        for i in range(0, len(todo), batch):
            rows = todo[i : i + batch]
            prompt = batch_prompt(template, [(r["kind"], r["note"]) for r in rows])
            answers = ask(url, prompt, reasons, len(rows))
            if answers:  # a failed batch stays unclassified and is retried on the next run
                cur.execute(
                    "INSERT INTO plant.silver.downtime_ai (event_id, ai_reason, model) VALUES "
                    + ", ".join(["(%s, %s, %s)"] * len(rows)),
                    [
                        v
                        for r, a in zip(rows, answers, strict=True)
                        for v in (r["event_id"], a, model)
                    ],
                )
            done = i + len(rows)
            rate = done / (time.monotonic() - t0)
            print(f"  classified {done}/{len(todo)} ({rate:.2f} stops/s)", flush=True)

    return classify

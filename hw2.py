#!/usr/bin/env python3
"""FTEC5660 HW2 student starter: build an agent that verifies CVs via MCP."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import re
from pathlib import Path
from typing import Any


MCP_URL = "https://ftec5660.ngrok.app/mcp"
MODEL_NAME = "deepseek-v4-flash"
THRESHOLD = 0.5


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def cv_files(folder: Path) -> list[Path]:
    """Return PDFs directly inside *folder*, sorted numerically (CV_2 before CV_10)."""

    def key(path: Path) -> tuple[int, str]:
        digits = "".join(ch for ch in path.stem if ch.isdigit())
        return (int(digits) if digits else math.inf, path.name)

    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
        key=key,
    )


def cv_text(path: Path) -> str:
    """Convert one CV PDF to markdown text."""
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False).convert(str(path)).text_content


async def load_mcp_tools() -> list[Any]:
    """Connect to the course MCP server and return its tools as LangChain tools."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
        {
            "social_graph": {
                "transport": "http",
                "url": MCP_URL,
                "headers": {"ngrok-skip-browser-warning": "true"},
            }
        }
    )
    return await client.get_tools()


SYSTEM_PROMPT = """You are a meticulous KYC verification agent. You verify one CV (résume) \
against the candidate's real LinkedIn and Facebook profiles, which you can \
only access through the provided tools. Never trust the CV itself and never \
use outside knowledge; every fact must come from a tool call.

Follow this workflow:

1. EXTRACT from the CV: full name, current city, every job (company, title, \
seniority, start year, end year), every education entry (degree, school, \
field, graduation year), and every listed skill.

2. IDENTIFY the candidate on LinkedIn with search_linkedin_people. Names are \
shared by hundreds of people and search returns at most 20 matches in no \
particular order, so narrow the search with the location parameter (city or \
country from the CV) and, on LinkedIn, the industry parameter when the CV \
gives one. If several people still match, disambiguate using the CV's city, \
employers or schools, and if needed use the fuzzy search option or search by \
company/school. Call get_linkedin_profile on the best-matching person to get \
the full profile. If you are unsure the profile belongs to the same person, \
cross-check with Facebook (search_facebook_users, then get_facebook_profile); \
note that a Facebook display_name may be a nickname that differs from the \
legal name, so use the Facebook original_name (not display_name) when \
comparing names. The real candidate's Facebook and LinkedIn always agree. \
If no plausible profile exists after several search attempts, treat the CV \
as fabricated.

3. COMPARE, field by field, ONLY these fields: name, city, jobs (company, \
title, seniority, start year, end year), education (degree, school, field, \
graduation year), and skills.

These are NOT discrepancies (do not penalize them):
- Wording or formatting variants of the same fact, e.g. "Bachelor of \
Science" vs BSc, "UI/UX Design" vs UI/UX, "The Chinese University of Hong \
Kong" vs CUHK.
- Seniority expressed in the title instead of a field, e.g. the CV says \
"Senior Engineer" while the profile says title Engineer with seniority \
senior — that MATCHES. But "Senior Analyst" vs title Analyst with \
seniority junior is a discrepancy.
- The CV listing FEWER jobs, degrees, or skills than the profile.
- Job descriptions, the profile headline, and the hometown — never compare \
these, they are never the source of a discrepancy.

These ARE discrepancies (any single one makes the CV unreliable):
- Inflated job title or seniority.
- Shifted employment or graduation years, even by one year (e.g. CV says a \
job started in 2015 but LinkedIn says 2014).
- An upgraded degree (e.g. CV says MSc, profile says BSc), a wrong field of \
study, a fake/wrong school or employer, a wrong city.
- A skill listed on the CV that does not appear in the profile's skills.

4. SCORE: if every comparable claim matches the profile, the CV is valid. \
If at least one real discrepancy exists, it is unreliable.

Reply with 1-3 short sentences naming the evidence you checked and any \
discrepancy found, then on the final line output exactly:
SCORE: <number>
where <number> is one value in [0, 1]: use 0.9 when the CV is fully \
consistent, and 0.1 when you found at least one discrepancy or could not \
find the candidate at all. The final line must contain no other text."""


def build_agent(tools: list[Any]) -> Any:
    """Create and return your agent once.

    ``tools`` are the six SocialGraph MCP tools (Facebook + LinkedIn search and
    profile lookup), already wrapped as LangChain tools. You may add your own
    local tools as well.

    Suggested imports:
        from langchain_deepseek import ChatDeepSeek
        from langchain.agents import create_agent

    Use the DeepSeek model named by ``MODEL_NAME``. The API key is loaded
    from .env.
    """
    from langchain.agents import create_agent
    from langchain_deepseek import ChatDeepSeek

    model = ChatDeepSeek(model=MODEL_NAME, temperature=0, max_tokens=4096)
    return create_agent(model, tools=tools, system_prompt=SYSTEM_PROMPT)


_USER_TEMPLATE = """Verify the following CV against the candidate's LinkedIn and Facebook \
profiles using the tools. Check every job, education entry, skill, the name \
and the city, then give your final verdict.

----- CV TEXT -----
{cv_text}
----- END CV -----"""


_SCORE_TAG_RE = re.compile(r"SCORE\s*[:=]?\s*(0(?:\.\d+)?|1(?:\.0+)?)", re.IGNORECASE)
_ANY_NUM_RE = re.compile(r"(?<![\d.])(0(?:\.\d+)?|1(?:\.0+)?)(?![\d.])")


def _extract_score(content: Any) -> float | None:
    """Pull the agent's final score out of its reply text."""
    if isinstance(content, list):
        text = " ".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    else:
        text = str(content)
    for pattern in (_SCORE_TAG_RE, _ANY_NUM_RE):
        matches = pattern.findall(text)
        if matches:
            score = float(matches[-1])
            if 0.0 <= score <= 1.0:
                return score
    return None


async def score_cvs(agent: Any, cvs: dict[str, str]) -> dict[str, float | None]:
    """Run your agent and return one reliability score per CV.

    ``cvs`` maps each file name to its text, e.g. ``{"CV_1.pdf": "...", ...}``.
    Return a float in [0, 1] for every file name: higher means the CV is more
    likely consistent with the candidate's LinkedIn/Facebook data. A score
    above 0.5 counts as "valid", 0.5 or below counts as "has discrepancy".

        {"CV_1.pdf": 0.9, "CV_4.pdf": 0.1, ...}

    Catch errors per CV (e.g. a failed API call) and still return a score for
    it: an exception here means no results.csv, which scores zero.

    MCP tools are async, so call your agent with ``await agent.ainvoke(...)``.
    You may verify CVs in parallel (e.g. ``asyncio.gather``), but keep at most
    about 3 CVs in flight (e.g. with ``asyncio.Semaphore(3)``): the MCP server is
    shared by the whole class.
    """
    semaphore = asyncio.Semaphore(3)
    fallback_score = 0.5

    async def verify_one(name: str, text: str) -> tuple[str, float]:
        async with semaphore:
            for attempt in range(6):
                try:
                    result = await asyncio.wait_for(
                        agent.ainvoke(
                            {"messages": [("user", _USER_TEMPLATE.format(cv_text=text))]},
                            config={"recursion_limit": 50},
                        ),
                        timeout=600,
                    )
                    score = _extract_score(result["messages"][-1].content)
                    if score is not None:
                        return name, score
                    print(f"[{name}] attempt {attempt + 1}: no score in reply")
                except Exception as exc:  # noqa: BLE001 - never crash the whole run
                    detail = exc
                    while getattr(detail, "exceptions", None):
                        detail = detail.exceptions[0]
                    cause = getattr(detail, "__cause__", None) or detail
                    print(f"[{name}] attempt {attempt + 1} failed: {type(cause).__name__}: {cause}")
                await asyncio.sleep(min(2 ** attempt, 30))
            print(f"[{name}] all attempts failed, returning fallback score")
            return name, fallback_score

    pairs = await asyncio.gather(*(verify_one(name, text) for name, text in cvs.items()))
    return dict(pairs)


# Everything below is provided runner/scoring code. No edits are needed.

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_score(value: Any) -> float | None:
    """Accept a float/int, or text containing exactly one number, in [0, 1]."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        score = float(value)
    else:
        text = str(getattr(value, "content", value))
        matches = _NUMBER_RE.findall(text)
        if len(matches) != 1:
            return None
        score = float(matches[0])
    if math.isnan(score) or not 0.0 <= score <= 1.0:
        return None
    return score


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read labels (1 = valid CV, 0 = has discrepancy) and reasons from the test folder."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return {
        name: entry if isinstance(entry, dict) else {"label": entry}
        for name, entry in json.loads(path.read_text(encoding="utf-8")).items()
    }


def correctness_text(score: float | None, expected: dict[str, Any] | None) -> str:
    """Return `correct`, or an expected/predicted mismatch explanation."""
    if score is None:
        return "incorrect: score is missing or not a number in [0, 1]"
    if expected is None:
        return "not graded: no ground truth for this CV"
    label = int(expected["label"])
    predicted = 1 if score > THRESHOLD else 0
    if predicted == label:
        return "correct"
    reason = f" ({expected['reason']})" if expected.get("reason") else ""
    return f"incorrect: expected {label}{reason}, predicted {predicted}"


def write_results(names: list[str], scores: dict[str, Any], truth: dict[str, dict[str, Any]]) -> tuple[Path, int]:
    """Write the required results.csv file and return how many CVs were correct."""
    output = Path("results.csv")
    correct = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["cv", "score", "correctness"])
        for name in names:
            score = parse_score(scores.get(name))
            verdict = correctness_text(score, truth.get(name))
            correct += verdict == "correct"
            writer.writerow([name, "" if score is None else f"{score:.4f}", verdict])
    return output, correct


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW2 on CV PDFs")
    parser.add_argument(
        "--cv-folder",
        required=True,
        type=Path,
        help="folder containing CV PDF files",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    paths = cv_files(folder)
    if not paths:
        raise SystemExit(f"no PDF files found in {folder}")

    load_env_file()
    cvs = {path.name: cv_text(path) for path in paths}
    tools = await load_mcp_tools()
    agent = build_agent(tools)
    scores = await score_cvs(agent, cvs)
    if not isinstance(scores, dict):
        raise TypeError("score_cvs() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct = write_results(list(cvs), scores, truth)
    summary = f" Accuracy: {correct}/{len(cvs)}." if truth else ""
    print(f"Processed {len(cvs)} CV(s). Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.cv_folder.is_dir():
        raise SystemExit(f"not a folder: {args.cv_folder}")
    return asyncio.run(run(args.cv_folder))


if __name__ == "__main__":
    raise SystemExit(main())

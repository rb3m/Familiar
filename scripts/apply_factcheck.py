"""Apply a factcheck_landmarks.py report to a landmark pack.

Supported facts are kept; "claim"/"unsupported" facts are replaced by the checker's corrected
wording, or dropped when it had none. Landmarks left without facts, or whose check errored, are
left out and listed so they can be rechecked.

Usage: uv run python scripts/apply_factcheck.py PACK.yaml REPORT.json OUT.yaml
"""

import json
import re
import sys
from pathlib import Path

import yaml

# Checker phrasing that should not end up in the companion's facts.
_META = re.compile(
    r"^(the article (states|says|notes|mentions|reports|describes)( that)?"
    r"|according to the article,?)\s+",
    re.I,
)


# "..., but does not mention X": the part before is the supported fact.
_UNSUPPORTED_TAIL = re.compile(
    r",?\s+(but|though|although)\s+(it\s+|the article\s+)?(does not|doesn't)\b.*$", re.I
)


def clean(fix: str) -> str:
    """Strip checker commentary; drop the fix when commentary remains."""
    text = _UNSUPPORTED_TAIL.sub("", _META.sub("", fix.strip())).rstrip(" ,;")
    if not text or re.search(r"\barticle\b", text, re.I):
        return ""
    text = text if text.endswith((".", "!", "?", '"')) else text + "."
    return text[:1].upper() + text[1:]


def corrected_facts(facts: list[str], verdicts: list[dict]) -> list[str] | None:
    """None when the check errored; otherwise the surviving, corrected facts."""
    by_number = {v.get("n"): v for v in verdicts}
    if any(v.get("verdict") == "error" for v in verdicts):
        return None
    result = []
    for number, fact in enumerate(facts, 1):
        verdict = by_number.get(number, {"verdict": "unsupported", "fix": ""})
        replacement = (
            fact if verdict.get("verdict") == "supported" else clean(verdict.get("fix", ""))
        )
        if replacement:
            result.append(replacement)
    return result


def main(pack: str, report: str, out: str) -> int:
    entries = yaml.safe_load(Path(pack).read_text())
    checks = {item["id"]: item for item in json.loads(Path(report).read_text())}
    kept, left_out = [], []
    for entry in entries:
        check = checks.get(entry["id"])
        facts = corrected_facts(entry["facts"], check["verdicts"]) if check else None
        if not facts:
            left_out.append(entry["id"])
            continue
        kept.append({**entry, "facts": facts})
    Path(out).write_text(yaml.safe_dump(kept, sort_keys=False, allow_unicode=True, width=100))
    print(f"kept {len(kept)}, left out {len(left_out)}: {', '.join(left_out) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))

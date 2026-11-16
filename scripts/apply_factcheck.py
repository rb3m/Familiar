"""Compatibility wrapper: apply_factcheck.py PACK.yaml REPORT.json OUT.yaml."""

import json
import sys
from pathlib import Path

from familiar_scs.curation import apply_report, read_pack, write_pack


def main(pack: str, report: str, out: str) -> int:
    kept, left_out = apply_report(read_pack(Path(pack)), json.loads(Path(report).read_text()))
    write_pack(Path(out), kept)
    print(f"kept {len(kept)}, left out {len(left_out)}: {', '.join(left_out) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:4]))

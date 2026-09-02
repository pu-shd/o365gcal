"""Regenerate the environment variable definition XML from the catalogue.

    .venv/bin/python tools/gen_envvars.py && ./scripts/build.sh
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from o365gcal.envvars import CATALOGUE, definition_xml  # noqa: E402

OUT = ROOT / "solution" / "src" / "environmentvariabledefinitions"


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    for var in CATALOGUE:
        d = OUT / var.schema_name
        d.mkdir(parents=True)
        (d / "environmentvariabledefinition.xml").write_text(definition_xml(var))
    print(f"wrote {len(CATALOGUE)} definitions to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

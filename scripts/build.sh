#!/bin/zsh
# Pack solution/src into managed and unmanaged zips in dist/.
#
# solution/src is the source of truth. Portal edits are pulled back with export.sh;
# never hand-edit the zips.
source "${0:A:h}/common.sh"
require_pac

mkdir -p "$OUT_DIR" "$DIST_DIR"

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  info "Running the test suite (SKIP_TESTS=1 to bypass)"
  PY_BIN="python3"
  [[ -x "$REPO_ROOT/.venv/bin/python" ]] && PY_BIN="$REPO_ROOT/.venv/bin/python"
  if ! (cd "$REPO_ROOT" && "$PY_BIN" -m pytest -q >/dev/null 2>&1); then
    (cd "$REPO_ROOT" && "$PY_BIN" -m pytest -q 2>&1 | tail -20)
    die "Tests failed. Packing anyway would ship a solution the tests already reject."
  fi
  ok "tests pass"
fi

info "Validating flow definitions before packing"
if command -v python3 >/dev/null 2>&1; then
  python3 - "$SRC_DIR" <<'PY'
import json, sys, pathlib
src = pathlib.Path(sys.argv[1])
bad = 0
for f in sorted((src / "Workflows").glob("*.json")):
    try:
        json.loads(f.read_text())
    except Exception as exc:
        print(f"  INVALID JSON: {f.name}: {exc}")
        bad += 1
if bad:
    sys.exit(f"{bad} workflow file(s) are not valid JSON")
PY
  ok "flow JSON parses"
fi

for kind in Unmanaged Managed; do
  info "Packing $kind"
  pac solution pack \
    --zipfile "$OUT_DIR/${SOLUTION_NAME}_${kind:l}.zip" \
    --folder "$SRC_DIR" \
    --packagetype "$kind" \
    --errorlevel Info
  cp "$OUT_DIR/${SOLUTION_NAME}_${kind:l}.zip" "$DIST_DIR/"
  ok "dist/${SOLUTION_NAME}_${kind:l}.zip"
done

info "Artefacts ready in $DIST_DIR"
ls -lh "$DIST_DIR"/*.zip

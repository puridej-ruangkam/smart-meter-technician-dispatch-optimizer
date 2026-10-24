#!/bin/bash
# ---------------------------------------------------------------
# One-time setup on macOS (Apple Silicon)
# Run from the project folder:   bash setup_macos.sh
# ---------------------------------------------------------------
cd "$(dirname "$0")"
FAIL=0

echo "=== 1. Xcode Command Line Tools ==="
# xlwings needs these: its appscript dependency ships as source only and is
# compiled at install time. Without them, pip install xlwings fails.
if xcode-select -p >/dev/null 2>&1; then
  echo "OK  $(xcode-select -p)"
else
  echo "!!  MISSING. Run this, finish the dialog, then re-run this script:"
  echo "        xcode-select --install"
  exit 1
fi

echo
echo "=== 2. Microsoft Excel ==="
if [ -d "/Applications/Microsoft Excel.app" ]; then
  echo "OK  Excel for Mac found"
else
  echo "!!  Excel for Mac not found. The writer needs Excel itself to save the"
  echo "    workbook; without it the run will refuse rather than damage the file."
  FAIL=1
fi

echo
echo "=== 3. Python ==="
PY=""
for c in python3.14 python3.13 python3.12 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    V=$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null) || continue
    MAJ=${V%.*}; MIN=${V#*.}
    if [ "$MAJ" -eq 3 ] && [ "$MIN" -ge 11 ]; then PY="$c"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "!!  No Python 3.11+ found. Install from https://www.python.org/downloads/macos/"
  exit 1
fi
echo "OK  $PY ($($PY -V))"

echo
echo "=== 4. Virtual environment (.venv) ==="
[ -d .venv ] || "$PY" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
echo "OK  $(python -V) in $(pwd)/.venv"

echo
echo "=== 5. Dependencies ==="
# pywin32 is skipped by its sys_platform marker; xlwings is installed by its own.
pip install -r requirements.txt || { echo "!!  pip install failed"; exit 1; }

echo
echo "=== 6. Verifying imports ==="
python - <<'PYCHECK'
import importlib, sys
ok = True
for mod in ["pandas", "numpy", "openpyxl", "ortools", "scipy", "xlwings"]:
    try:
        importlib.import_module(mod)
        print(f"OK  {mod}")
    except Exception as exc:
        print(f"!!  {mod}: {exc}")
        ok = False
sys.exit(0 if ok else 1)
PYCHECK
[ $? -ne 0 ] && FAIL=1

echo
echo "=== 7. Road graph ==="
if [ -f mapdata/bangkok_road_graph.npz ]; then
  echo "OK  mapdata/bangkok_road_graph.npz - no rebuild needed"
else
  echo "!!  Missing. Rebuild (~20 min):"
  echo "      curl -L -o mapdata/thailand-latest.osm.pbf https://download.geofabrik.de/asia/thailand-latest.osm.pbf"
  echo "      python tools/build_bangkok_graph.py"
  FAIL=1
fi

# Double-click buttons need the executable bit; a copied or re-synced file
# usually loses it, and Finder then refuses to open it.
chmod +x planner.command map.command 2>/dev/null

echo
echo "=== 8. Test suite ==="
python -m pytest tests/ -q --ignore=tests/test_balance.py || FAIL=1

echo
if [ "$FAIL" -ne 0 ]; then
  echo "=== Setup finished WITH PROBLEMS - see the !! lines above ==="
  exit 1
fi
cat <<'DONE'
=== Setup complete ===

Every new Terminal session starts with:
    source .venv/bin/activate

Next step - test the Excel writer against a COPY, never the live workbook:
    cp workforce.xlsx workforce_TEST.xlsx
    python main.py --workbook workforce_TEST.xlsx --no-map

macOS will ask for permission to control Excel the first time. Approve it.
Then open the copy in Excel and check the Daily Route sheet before running
against the real workbook.
DONE

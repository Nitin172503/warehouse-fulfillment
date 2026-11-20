#!/bin/sh
# Build the static, serverless version of the web app (for GitHub Pages).
# The real Python package runs in the visitor's browser via Pyodide (engine-worker.js).
set -eu
OUT="${1:-_site}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/src/warehouse"
SERVER_ONLY="api.py cli.py"   # need FastAPI/argparse; not used in the browser

rm -rf "$OUT" && mkdir -p "$OUT/py/warehouse"
FILES=""
for f in "$PKG"/*.py; do
  name="$(basename "$f")"
  case " $SERVER_ONLY " in *" $name "*) continue ;; esac
  cp "$f" "$OUT/py/warehouse/$name"
  FILES="$FILES\"$name\","
done
# Version = hash of the Python sources: saved browser progress is discarded when the code changes.
VERSION="$(cat "$OUT"/py/warehouse/*.py | shasum -a 256 | cut -c1-12)"
printf '{"version":"%s","files":[%s]}\n' "$VERSION" "${FILES%,}" > "$OUT/py/manifest.json"

cp "$PKG/static/engine-worker.js" "$PKG/static/engine-client.js" "$OUT/"
sed 's#<!--ENGINE-->#<script src="engine-client.js"></script>#' "$PKG/static/dashboard.html" > "$OUT/index.html"
grep -q 'engine-client.js' "$OUT/index.html" || { echo "engine marker missing in dashboard.html" >&2; exit 1; }
touch "$OUT/.nojekyll"
echo "Built $OUT/ (version $VERSION)"

#!/bin/sh
# Build the static, serverless version of the web app (for GitHub Pages).
# The simulation runs in the visitor's browser via engine.js.
set -eu
OUT="${1:-_site}"
SRC="$(dirname "$0")/../src/warehouse/static"
rm -rf "$OUT" && mkdir -p "$OUT"
cp "$SRC/engine.js" "$OUT/engine.js"
sed 's#<!--ENGINE-->#<script src="engine.js"></script>#' "$SRC/dashboard.html" > "$OUT/index.html"
grep -q 'src="engine.js"' "$OUT/index.html" || { echo "engine marker missing in dashboard.html" >&2; exit 1; }
touch "$OUT/.nojekyll"
echo "Built $OUT/ (index.html + engine.js)"

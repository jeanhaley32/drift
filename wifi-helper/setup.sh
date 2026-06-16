#!/usr/bin/env bash
# One-time setup for drift's Wi-Fi helper: build it, then trigger the macOS
# Location prompt. Click "Allow" when DriftWiFi asks — after that, drift shows
# real Wi-Fi network names. Re-run this if you ever rebuild the helper.
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

echo
echo "Launching DriftWiFi to request Location access…"
echo "→ Click ALLOW on the prompt that appears."
OUT="$(mktemp /tmp/driftwifi.XXXXXX.json)"
open -n ./DriftWiFi.app --args --auth --scan --out "$OUT"

# wait for the helper to write its result (it reads Wi-Fi after you answer)
for _ in $(seq 1 30); do
    [ -s "$OUT" ] && break
    sleep 1
done

echo
if [ -s "$OUT" ]; then
    echo "Helper response: $(cat "$OUT")"
    if grep -q '"auth":1' "$OUT"; then
        echo "✅ Authorized — drift will now show real Wi-Fi names."
    else
        echo "⚠️  Not authorized yet. Re-run this script and click Allow, or enable"
        echo "   DriftWiFi under System Settings → Privacy & Security → Location Services."
    fi
else
    echo "⚠️  No response — the prompt may have been dismissed. Re-run to try again."
fi
rm -f "$OUT"

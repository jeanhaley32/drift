#!/usr/bin/env bash
# Build DriftWiFi.app — drift's location-aware Wi-Fi helper.
# Requires Xcode command-line tools (swiftc). Pure Swift, no Xcode project.
set -euo pipefail
cd "$(dirname "$0")"

APP="DriftWiFi.app"
ID="com.jeanhaley32.driftwifi"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"

swiftc -O \
    -framework CoreLocation -framework CoreWLAN -framework Foundation \
    main.swift -o "$APP/Contents/MacOS/DriftWiFi"

# Ad-hoc code-sign so TCC can track a stable identity for the bundle.
# (A re-build changes the binary hash and may re-prompt for permission — fine.)
codesign --force --sign - --identifier "$ID" "$APP"

echo "built $APP"
echo "next: ./DriftWiFi.app/Contents/MacOS/DriftWiFi --auth   (click Allow on the prompt)"

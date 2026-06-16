// DriftWiFi — a tiny location-aware Wi-Fi helper for drift.
//
// macOS 14+ redacts Wi-Fi network names unless the *app reading them* holds
// Location Services permission. A terminal (iTerm/Terminal) never requests
// location, so it can't be granted — and drift, running inside it, sees only
// "<redacted>". system_profiler redacts unconditionally; CoreWLAN's ssid()
// returns the real name only when the calling app is Location-authorized.
//
// This helper is its own ad-hoc-signed .app bundle with an
// NSLocationWhenInUseUsageDescription, so macOS can grant Location to
// *DriftWiFi itself*. Launched via LaunchServices (`open`), it is its own
// responsible process, so CoreWLAN returns un-redacted SSIDs regardless of
// which terminal launched drift. No Developer account / entitlement needed.
//
// Usage:
//   DriftWiFi --auth [--out FILE]   request Location auth (shows prompt), then read
//   DriftWiFi [--out FILE] [--scan]  read current Wi-Fi (+ nearby with --scan)
//
// Output: one line of JSON, e.g.
//   {"ok":true,"auth":1,"ssid":"MyNet","rssi":-52,"neighbors":[["Other",-70]]}

import Foundation
import CoreLocation
import CoreWLAN

let args = CommandLine.arguments
func argVal(_ k: String) -> String? {
    if let i = args.firstIndex(of: k), i + 1 < args.count { return args[i + 1] }
    return nil
}
let outPath = argVal("--out")
let doScan = args.contains("--scan")
let wantAuth = args.contains("--auth")

func emit(_ obj: [String: Any]) {
    let data = (try? JSONSerialization.data(withJSONObject: obj, options: [])) ?? Data()
    if let p = outPath {
        try? data.write(to: URL(fileURLWithPath: p))   // launched via `open`: no stdout
    } else {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0a]))
    }
}

func readWiFi(authorized: Bool) -> [String: Any] {
    var o: [String: Any] = ["ok": true, "auth": authorized ? 1 : 0]
    guard let iface = CWWiFiClient.shared().interface() else {
        o["ok"] = false; o["error"] = "no Wi-Fi interface"; return o
    }
    o["ssid"] = iface.ssid() ?? NSNull()
    o["rssi"] = iface.rssiValue()
    o["interface"] = iface.interfaceName ?? NSNull()
    // nearby networks: strongest RSSI per SSID, sorted, capped
    var best: [String: Int] = [:]
    if doScan, let nets = try? iface.scanForNetworks(withSSID: nil) {
        for n in nets {
            guard let name = n.ssid, !name.isEmpty else { continue }
            let r = n.rssiValue
            if best[name] == nil || r > best[name]! { best[name] = r }
        }
    }
    o["neighbors"] = best.sorted { $0.value > $1.value }.prefix(16).map { [$0.key, $0.value] }
    return o
}

final class Mgr: NSObject, CLLocationManagerDelegate {
    let m = CLLocationManager()
    var fired = false
    override init() { super.init(); m.delegate = self }
    func start() { m.requestWhenInUseAuthorization(); finish(m.authorizationStatus) }
    func locationManagerDidChangeAuthorization(_ mm: CLLocationManager) { finish(mm.authorizationStatus) }
    func finish(_ s: CLAuthorizationStatus) {
        if s == .notDetermined || fired { return }
        fired = true
        let ok = (s == .authorizedAlways || s == .authorized)
        emit(readWiFi(authorized: ok))
        exit(ok ? 0 : 1)
    }
}

if wantAuth {
    let mgr = Mgr()
    mgr.start()
    DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
        emit(["ok": false, "error": "authorization timed out"]); exit(2)
    }
    RunLoop.main.run()
} else {
    let s = CLLocationManager().authorizationStatus
    emit(readWiFi(authorized: s == .authorizedAlways || s == .authorized))
}

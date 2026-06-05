# drift

**An ambient, themed "living terminal" — part screensaver, part generative art, part glanceable system monitor.**

`drift` turns your machine's real telemetry into animated, strongly-themed scenes that cycle on random intervals. CPU load, memory pressure, battery, and a *lot* of network detail aren't just numbers in a corner — they're the material the scenes are made of. Meteors fall at your download rate, steam gauges climb with memory, Docker containers sail in as ships, and your GitHub streak keeps a little ASCII pet happy.

It's the kind of thing you leave running in a spare terminal and glance at.

```
  ./drift
```

That's it. macOS, pure standard-library Python 3, plus a handful of cheap shell tools you already have. No installs, no dependencies.

---

## Themes

`drift` rotates through eight scenes (and you can lock to any one):

| Theme | What it shows | Telemetry behind it |
|-------|---------------|---------------------|
| **COSMOS** | Retro space — stars, a planet, meteors, a UFO, a rocket | meteors = download rate · rocket climb = CPU · pulse rings = latency · star cluster = nearby Wi-Fi |
| **BOILER** | Steampunk machine room — gauges, gears, pistons, steam | gauges = CPU/MEM/NET · steam = memory · valve flash = CPU spike |
| **SIGNAL** | Live network node-graph | nodes = TCP connections · packets = throughput · pulse = latency |
| **SOCKETS** | A patch-bay of individual connections, one row each | each row's flow = *that* socket's real bytes/sec · color = remote port |
| **NET GRID** | Cyberpunk data-tower skyline | tower height = socket activity over a rolling window · ▲▼ pulses = live up/down |
| **HARBOR** | Your Docker fleet as ships in a harbor | cargo = memory · funnel smoke = CPU · wake = net I/O · crew = PIDs |
| **GITHUB** | Live contribution heatmap + account stats | your past-year graph, streaks, stars, followers, open PRs, notifications |
| **OCTO-PET** | A Tamagotchi whose mood reflects your GitHub activity | long streak → ecstatic · idleness → sleepy/hungry · orbited by stars/followers/notifs |

HARBOR appears only when a Docker daemon is reachable; GITHUB and OCTO-PET only when the [`gh`](https://cli.github.com/) CLI is authenticated.

---

## Controls

| Key | Action |
|-----|--------|
| `q` | quit |
| `space` | skip to next scene now |
| `n` / `p` | next / previous theme |
| `l` | lock the current theme (stop auto-rotation) |
| `h` | toggle the telemetry **decoder HUD** |
| `f` | toggle the fps / frame / typing meter |
| `+` / `-` | speed up / slow down the animation |

The **decoder HUD** (`h`, or start with `--hud`) is the legend: it spells out exactly which stat is driving each element on screen, plus a live readout of clock, uptime, load, memory, disk, battery, and process count.

---

## Privacy

Telemetry is **display-only. Nothing is ever sent anywhere.** The two features that *could* reach outside your machine are **off by default** and opt-in:

- `--public-ip` — makes a single DNS lookup of your public IP.
- `--lsof` — inspects per-process remote hosts locally.

The optional global typing meter (`--global-keys`) installs a **listen-only** macOS event tap and counts *only the timing* of key-down events — it never reads, stores, logs, or transmits *which* key was pressed. There is no content to leak. It requires macOS **Input Monitoring** permission and silently degrades to off without it.

GitHub stats come from your locally-authenticated `gh` CLI (keyring); the network is only touched to refresh them, slowly.

---

## Usage

```
./drift [options]
```

| Flag | Description |
|------|-------------|
| `--theme NAME` | lock to one theme (`cosmos`, `boiler`, `signal`, `sockets`, `grid`, `harbor`, `github`, `octopet`) |
| `--hud` | start with the telemetry decoder HUD visible |
| `--lock` | don't auto-rotate themes |
| `--minutes N` | exit after N minutes |
| `--min-secs` / `--max-secs` | bounds on how long each scene stays up (default 45–110s) |
| `--fps N` | target frame rate (default 60; lower to save CPU) |
| `--no-fps-meter` | hide the fps / frame counter |
| `--gh-user LOGIN` | show GitHub stats for another account (public data only) |
| `--gh-repo OWNER/REPO` | merge *your* commits from a repo into the contribution graph (repeatable — great for private org repos GitHub doesn't attribute to your account) |
| `--global-keys` | drive the typing meter from all system-wide typing (timing only; see Privacy) |
| `--public-ip` | look up your public IP (one outbound request) |
| `--lsof` | inspect per-process remote hosts (slower) |

---

## How it works

- **Pure-stdlib Python 3 + curses.** No pip, no venv. Telemetry is gathered by shelling out to standard macOS tools (`netstat`, `vm_stat`, `pmset`, `nettop`, `arp`, `ping`, `system_profiler`, `docker`, `gh`).
- **A background sampler thread** polls those tools on tiered cadences (fast stats every ~1s, network sockets every 2s, Docker every 5s, Wi-Fi/VPN every 30s, GitHub every 5 min) so the render loop never blocks on a slow shell call.
- **Frame-rate independent animation.** Motion is scaled by real elapsed time against a reference rate, so scenes look identical at any fps — just smoother.
- **Crash-safe drawing.** Every shell call is timeout-guarded and never raises; every parser tolerates malformed input; all curses writes clip to the screen bounds.

Requires **macOS** and **Python 3** (both ship with the system). Optional: [`gh`](https://cli.github.com/) for the GitHub scenes, Docker for HARBOR, and `pyobjc` for `--global-keys`.

---

*drift — see you out there. ✨*

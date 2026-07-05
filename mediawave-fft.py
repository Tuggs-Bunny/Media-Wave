#!/usr/bin/env python3
import json, time, signal, sys, threading, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import numpy as np
except ImportError:
    sys.exit("pip install numpy --user")

try:
    import dbus
except ImportError:
    sys.exit("sudo dnf install python3-dbus")

PORT  = 19876
RATE  = 48000
BANDS = 9
CHUNK = 256

state = {
    "bands":  [0.0] * BANDS,
    "player": {"title": "", "artist": "", "position": 0,
               "length": 0, "playing": False, "art": ""}
}
lock        = threading.Lock()
mpris_iface = None
running     = True

signal.signal(signal.SIGTERM, lambda *_: globals().update(running=False))
signal.signal(signal.SIGINT,  lambda *_: globals().update(running=False))

def find_monitor_source():
    """Return a running monitor source name, falling back to default sink monitor."""
    try:
        r = subprocess.run(["pactl", "list", "sources", "short"],
                           capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and ".monitor" in parts[1] and parts[4] == "RUNNING":
                return parts[1]
        r2 = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=3)
        for line in r2.stdout.splitlines():
            if line.startswith("Default Sink:"):
                return line.split(":", 1)[1].strip() + ".monitor"
    except Exception as e:
        print(f"[audio] pactl error: {e}", flush=True)
    return None

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path.strip("/")
        if path == "playpause":
            try: mpris_iface and mpris_iface.PlayPause()
            except: pass
        elif path == "next":
            try: mpris_iface and mpris_iface.Next()
            except: pass
        elif path == "previous":
            try: mpris_iface and mpris_iface.Previous()
            except: pass
        elif path.startswith("seek"):
            try:
                pos = int(parse_qs(urlparse(self.path).query).get("pos", [0])[0])
                if mpris_iface:
                    mpris_iface.SetPosition(
                        dbus.ObjectPath("/org/mpris/MediaPlayer2"), dbus.Int64(pos))
            except: pass
        with lock:
            body = json.dumps(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_): pass

def run_http():
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    srv.socket.setsockopt(1, 2, 1)
    print(f"[http] 127.0.0.1:{PORT}", flush=True)
    while running:
        srv.handle_request()

IGNORED_MPRIS = {"discord", "kdeconnect", "plasma-browser-integration"}

def run_mpris():
    global mpris_iface
    bus     = dbus.SessionBus()
    skipped = False
    while running:
        try:
            proxy = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            names = [str(n) for n in
                     dbus.Interface(proxy, "org.freedesktop.DBus").ListNames()
                     if str(n).startswith("org.mpris.MediaPlayer2.")
                     and not any(ign in str(n).lower() for ign in IGNORED_MPRIS)]
            if not names:
                mpris_iface = None
                time.sleep(1); continue

            obj   = bus.get_object(names[0], "/org/mpris/MediaPlayer2")
            props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            mpris_iface = dbus.Interface(obj, "org.mpris.MediaPlayer2.Player")

            playing = str(props.Get("org.mpris.MediaPlayer2.Player",
                                    "PlaybackStatus")) == "Playing"
            pos     = int(props.Get("org.mpris.MediaPlayer2.Player", "Position"))
            m       = props.Get("org.mpris.MediaPlayer2.Player", "Metadata")
            length  = int(m.get("mpris:length", 0))
            artists = m.get("xesam:artist", [])

            if playing and length > 10_000_000 and (length - pos) < 2_000_000:
                if not skipped:
                    try:
                        mpris_iface.Next()
                        skipped = True
                        time.sleep(0.5)
                        pos    = int(props.Get("org.mpris.MediaPlayer2.Player", "Position"))
                        m      = props.Get("org.mpris.MediaPlayer2.Player", "Metadata")
                        length = int(m.get("mpris:length", 0))
                        artists = m.get("xesam:artist", [])
                    except Exception as e:
                        print(f"[mpris] Next() error: {e}", flush=True)
            else:
                skipped = False

            with lock:
                state["player"] = {
                    "title":    str(m.get("xesam:title", "")),
                    "artist":   str(artists[0]) if artists else "",
                    "position": pos,
                    "length":   length,
                    "playing":  playing,
                    "art":      str(m.get("mpris:artUrl", "")),
                }
        except Exception as e:
            print(f"[mpris] {e}", flush=True)
        time.sleep(0.2)

def run_audio():
    low, high = 40.0, 16_000.0
    edges    = [(low*(high/low)**(i/BANDS), low*(high/low)**((i+1)/BANDS))
                for i in range(BANDS)]
    frame    = CHUNK * 2 * 2
    smoothed = [0.0] * BANDS

    while running:
        # Keep trying until we find elisa - NEVER use anything else
        target = None
        while running and target is None:
            target = find_monitor_source()
            if target is None:
                print("[audio] waiting for monitor source...", flush=True)
                time.sleep(2)

        if not running:
            break

        print(f"[audio] recording from: {target}", flush=True)
        proc = subprocess.Popen(
            ["parec", "--device", target,
             "--rate", str(RATE), "--channels", "2", "--format", "s16le",
             "--latency-msec=20"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        try:
            check_counter = 0
            while running:
                raw = proc.stdout.read(frame)
                if len(raw) < frame:
                    break

                # pw-record silently switches to mic when its target node vanishes,
                # so we verify Elisa is still in PipeWire every ~1 second.
                check_counter += 1
                if check_counter >= 200:
                    check_counter = 0
                    if find_monitor_source() is None:
                        print("[audio] monitor source gone, stopping capture", flush=True)
                        break

                s    = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                mono = (s[0::2] + s[1::2]) / 65536.0
                fft  = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
                freq = np.fft.rfftfreq(len(mono), 1.0/RATE)
                raw_b = []
                for fl, fh in edges:
                    mask = (freq >= fl) & (freq < fh)
                    raw_b.append(float(np.mean(fft[mask])) if mask.any() else 0.0)
                mx = max(raw_b) or 1.0
                raw_b = [min(1.0, v/mx) for v in raw_b]
                for i in range(BANDS):
                    smoothed[i] = smoothed[i]*0.15 + raw_b[i]*0.85
                with lock:
                    state["bands"] = [round(v, 3) for v in smoothed]
        except Exception as e:
            print(f"[audio] stream error: {e}", flush=True)
        finally:
            try: proc.terminate()
            except: pass
            smoothed = [0.0] * BANDS
            with lock:
                state["bands"] = [0.0] * BANDS

        if running:
            # Wait before retrying - gives elisa time to reappear in PipeWire
            time.sleep(2)

if __name__ == "__main__":
    for fn in (run_http, run_mpris, run_audio):
        threading.Thread(target=fn, daemon=True).start()
    while running:
        time.sleep(0.5)

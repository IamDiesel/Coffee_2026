#!/usr/bin/env python3
"""
Baseline-Messung fuer Coffee26: Schneidet den Bus mit Zeitstempeln mit.

Der Pi schaltet auf MITM, reicht alle Bytes sofort unveraendert durch und
protokolliert jeden Frame mit Millisekunden-Zeitstempel. Damit laesst sich
messen, was das ECHTE Display beim Einschalten per Hardware-Taste sendet
(Anzahl/Abstand der Beep- und Power-Frames), wann das Mainboard antwortet
und ab wann der Bus stabil laeuft.

Vorher den Dienst stoppen:
    sudo systemctl stop coffee26
Messen (Dauer in Sekunden, Standard 90), optional mit Namen fuer die Messung:
    sudo /home/fuchsi/.coffee/bin/python3 baseline_capture.py 90 einschalten
Nur auswerten (ohne Hardware):
    python3 baseline_capture.py --analyze baseline_....csv
Am Ende gehen die Relais IMMER zurueck in den Bypass.
"""
import sys
import time

PORT_DISP = "/dev/ttyAMA2"   # RX: Frames vom Display (12 Byte)
PORT_MAIN = "/dev/ttyAMA3"   # RX: Frames vom Mainboard (19 Byte)
PIN_PWR_DISP = 17
PIN_MITM = 27
FLEN = {"D": 12, "M": 19}


def hx(data):
    return " ".join(f"{b:02x}" for b in data)


def label_display(frame):
    """Kurzbeschreibung eines Display-Frames (Byte 2 = System, 7 = Getraenk, 8 = Einstellung, 9 = Aktion)."""
    b = bytes.fromhex(frame.replace(" ", ""))
    if len(b) < 12:
        return "?"
    parts = []
    sysb = {0x00: None, 0x0a: "BEEP/WAKE", 0x01: "POWER(01)", 0x02: "POWER(02)"}.get(b[2], f"sys={b[2]:02x}")
    if sysb:
        parts.append(sysb)
    drinks = {0x01: "PowerOff", 0x02: "Espresso", 0x04: "HotWater", 0x08: "Coffee", 0x10: "Steam"}
    if b[7]:
        parts.append(drinks.get(b[7], f"btn={b[7]:02x}"))
    if b[8]:
        parts.append({0x02: "Bohnen", 0x04: "Tasse"}.get(b[8], f"set={b[8]:02x}"))
    if b[9]:
        parts.append("Play" if b[9] == 0x01 else f"act={b[9]:02x}")
    return "+".join(parts) if parts else "idle"


class FrameParser:
    def __init__(self, direction):
        self.dir = direction
        self.buf = bytearray()
        self.junk = 0

    def feed(self, data, t_ms, out):
        self.buf += data
        n = FLEN[self.dir]
        while True:
            i = self.buf.find(b"\xd5\x55")
            if i < 0:
                self.junk += max(0, len(self.buf) - 1)
                del self.buf[:-1]
                return
            if i > 0:
                self.junk += i
                del self.buf[:i]
            if len(self.buf) < n:
                return
            out.append((t_ms, self.dir, hx(self.buf[:n])))
            del self.buf[:n]


# ----------------------------------------------------------------- Auswertung
def analyze(rows, junk=None, out=sys.stdout):
    """rows: Liste (t_ms, 'D'|'M', hex)."""
    p = lambda *a: print(*a, file=out)
    if not rows:
        p("Keine Frames aufgezeichnet.")
        return
    d = [r for r in rows if r[1] == "D"]
    m = [r for r in rows if r[1] == "M"]
    p("=" * 78)
    p(f"Frames gesamt: Display {len(d)}, Mainboard {len(m)}" + (f" | Muell-Bytes: {junk}" if junk else ""))
    if d:
        p(f"Erster Display-Frame:   t = {d[0][0] / 1000:8.3f} s   [{label_display(d[0][2])}]")
    if m:
        p(f"Erster Mainboard-Frame: t = {m[0][0] / 1000:8.3f} s")
    if d and m:
        p(f"-> Mainboard antwortet {(m[0][0] - d[0][0]):.0f} ms nach dem ersten Display-Frame, "
          f"nach {sum(1 for r in d if r[0] <= m[0][0])} Display-Frames")

    # --- Lauflaengen der Display-Frames: zeigt die echte Einschaltsequenz
    p("\n--- Display-Sequenz (gleiche Frames zusammengefasst) " + "-" * 25)
    p(f"{'Start [s]':>10} {'Dauer':>8} {'Anzahl':>7} {'Abstand':>9}  Inhalt")
    segs = []
    for t, _, h in d:
        if segs and segs[-1]["hex"] == h and t - segs[-1]["t1"] < 500:
            segs[-1]["t1"] = t
            segs[-1]["n"] += 1
        else:
            segs.append({"t0": t, "t1": t, "n": 1, "hex": h})
    for s in segs[:60]:
        dur = s["t1"] - s["t0"]
        gap = f"{dur / (s['n'] - 1):6.1f} ms" if s["n"] > 1 else "        -"
        p(f"{s['t0'] / 1000:10.3f} {dur:6.0f}ms {s['n']:7d} {gap}  {label_display(s['hex']):<12} {s['hex']}")
    if len(segs) > 60:
        p(f"... {len(segs) - 60} weitere Abschnitte (siehe CSV)")

    # --- Pausen im Display-Verkehr (Display dunkel / bootet)
    gaps = [(d[i][0], d[i + 1][0] - d[i][0]) for i in range(len(d) - 1) if d[i + 1][0] - d[i][0] > 200]
    if gaps:
        p("\n--- Pausen im Display-Verkehr > 200 ms " + "-" * 39)
        for t, g in gaps[:20]:
            p(f"  ab t = {t / 1000:8.3f} s: {g:7.0f} ms Pause")

    # --- Rate pro Sekunde: ab wann ist der Bus stabil?
    p("\n--- Frames pro Sekunde " + "-" * 55)
    t_end = int(rows[-1][0] // 1000) + 1
    per = [[0, 0] for _ in range(t_end + 1)]
    for t, direction, _ in rows:
        per[int(t // 1000)][0 if direction == "D" else 1] += 1
    first = next((i for i, (a, b) in enumerate(per) if a or b), 0)
    stable = None
    for i in range(first, t_end):
        if per[i][0] >= 70 and per[i][1] >= 70 and stable is None:
            stable = i
    for i in range(max(0, first - 1), min(t_end, first + 25)):
        p(f"  t = {i:3d}..{i + 1:<3d} s: Display {per[i][0]:4d}   Mainboard {per[i][1]:4d}")
    if stable is not None and d:
        p(f"-> Bus stabil (beide >= 70 Frames/s) ab t = {stable} s, "
          f"also ca. {stable - d[0][0] / 1000:.1f} s nach dem ersten Display-Frame")
    else:
        p("-> Bus wurde in der Messzeit NICHT stabil (nie beide >= 70 Frames/s)")

    # --- haeufigste Mainboard-Frames
    cnt = {}
    for _, _, h in m:
        cnt[h] = cnt.get(h, 0) + 1
    if cnt:
        p("\n--- Haeufigste Mainboard-Frames " + "-" * 46)
        for h, c in sorted(cnt.items(), key=lambda x: -x[1])[:6]:
            p(f"  {c:6d}x  {h}")
    p("=" * 78)


def load_csv(path):
    rows = []
    with open(path) as f:
        for line in f:
            parts = line.rstrip("\n").split(";")
            if len(parts) == 3 and parts[1] in ("D", "M"):
                rows.append((float(parts[0]), parts[1], parts[2]))
    return rows


# -------------------------------------------------------------------- Messung
def capture(duration, name):
    import RPi.GPIO as GPIO
    import serial

    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = f"baseline_{stamp}_{name}" if name else f"baseline_{stamp}"
    rows = []
    parsers = {"D": FrameParser("D"), "M": FrameParser("M")}

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PIN_PWR_DISP, GPIO.OUT)
    GPIO.setup(PIN_MITM, GPIO.OUT)
    ports = {}
    try:
        ports["D"] = serial.Serial(PORT_DISP, 115200, timeout=0)
        ports["M"] = serial.Serial(PORT_MAIN, 115200, timeout=0)
        GPIO.output(PIN_PWR_DISP, GPIO.HIGH)
        GPIO.output(PIN_MITM, GPIO.LOW)
        time.sleep(0.1)
        for s in ports.values():
            s.reset_input_buffer()

        t0 = time.monotonic()
        print(f"[BASELINE] MITM aktiv, Messung laeuft {duration:.0f} s  ->  JETZT am Display bedienen")
        next_report = 1.0
        last = [0, 0]
        while True:
            now = time.monotonic() - t0
            if now >= duration:
                break
            for direction, ser in ports.items():
                n = ser.in_waiting
                if n:
                    data = ser.read(n)
                    ser.write(data)  # sofort unveraendert durchreichen (wie controller.py)
                    parsers[direction].feed(data, now * 1000.0, rows)
            if now >= next_report:
                nd = sum(1 for r in rows if r[1] == "D")
                nm = len(rows) - nd
                print(f"  t = {next_report:4.0f} s | Display +{nd - last[0]:3d}  Mainboard +{nm - last[1]:3d}")
                last = [nd, nm]
                next_report += 1.0
            time.sleep(0.0005)
    except KeyboardInterrupt:
        print("\n[BASELINE] Abgebrochen, werte bisherige Daten aus.")
    finally:
        GPIO.output(PIN_PWR_DISP, GPIO.HIGH)
        GPIO.output(PIN_MITM, GPIO.HIGH)
        for s in ports.values():
            s.close()
        print("[BASELINE] Relais zurueck im BYPASS.")

    rows.sort(key=lambda r: r[0])
    with open(base + ".csv", "w") as f:
        for t, direction, h in rows:
            f.write(f"{t:.1f};{direction};{h}\n")
    junk = {k: v.junk for k, v in parsers.items()}
    with open(base + "_summary.txt", "w") as f:
        analyze(rows, junk, out=f)
    analyze(rows, junk)
    print(f"Rohdaten: {base}.csv\nAuswertung: {base}_summary.txt")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--analyze":
        analyze(load_csv(sys.argv[2]))
    else:
        dur = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
        capture(dur, sys.argv[2] if len(sys.argv) > 2 else "")

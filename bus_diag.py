"""
Bus-Statistik und Diagnose-Schnappschuesse fuer die Fehlersuche am UART-Bus.

Hintergrund: Mehrfach kamen vom Mainboard (ttyAMA3) keine Daten mehr an,
obwohl das Display (ttyAMA2) weiter sendete. Nur ein kompletter Stromreset
half. Dieses Modul schreibt jede Minute eine Statistikzeile ins Log und
haelt beim Uebergang "Display sendet, Mainboard schweigt" automatisch den
Zustand von Treiber, Pins und Stromversorgung fest.
"""
import re
import subprocess
import time

PROC_TTY = "/proc/tty/driver/ttyAMA"  # Treiberzaehler der PL011-UARTs (nur root)
PORTS = {"disp": 2, "main": 3}         # ttyAMA2 = Display-Seite, ttyAMA3 = Mainboard-Seite
RX_PINS = {"RXD2": 5, "RXD3": 9}      # Eingaenge: Display-TX bzw. Mainboard-TX (Ruhepegel = hi)
SNAPSHOT_CMDS = (
    "cat /proc/tty/driver/ttyAMA",
    "pinctrl get 4,5,8,9",
    "vcgencmd get_throttled",
    "dmesg | tail -n 15",
)


def read_uart_counters():
    """Liefert {'disp': {'rx':..,'fe':..,'brk':..,'oe':..}, 'main': {...}}.

    Der Kernel listet fe/pe/brk/oe nur, wenn sie > 0 sind; fehlende Werte = 0.
    """
    result = {}
    try:
        with open(PROC_TTY) as f:
            for line in f:
                m = re.match(r"\s*(\d+):", line)
                if not m:
                    continue
                idx = int(m.group(1))
                for side, port in PORTS.items():
                    if idx == port:
                        result[side] = {k: int(v) for k, v in
                                        re.findall(r"\b(rx|tx|fe|pe|brk|oe):(\d+)", line)}
    except OSError as e:
        result["error"] = str(e)
    return result


def read_rx_levels():
    """Liest den aktuellen Pegel der RX-Pins per pinctrl: {'RXD2': 'hi', 'RXD3': 'lo'}.

    Eine ruhende UART-Leitung ist hi. Dauerhaft lo heisst: die Gegenseite
    haelt ihre Sendeleitung auf Masse (z. B. haengendes Mainboard).
    """
    pins = ",".join(str(p) for p in RX_PINS.values())
    out = _run(f"pinctrl get {pins}")
    levels = {}
    for name, pin in RX_PINS.items():
        m = re.search(rf"^\s*{pin}:.*\|\s*(hi|lo)\b", out, re.MULTILINE)
        levels[name] = m.group(1) if m else "?"
    return levels


def _run(cmd):
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return (out.stdout + out.stderr).strip()
    except Exception as e:  # Diagnose darf den Controller nie stoppen
        return f"<{e}>"


def snapshot(reason):
    print(f"[BUSDIAG] ===== Schnappschuss: {reason} =====")
    for cmd in SNAPSHOT_CMDS:
        print(f"[BUSDIAG] $ {cmd}")
        for line in _run(cmd).splitlines():
            print(f"[BUSDIAG]   {line}")


class BusStats:
    """Zaehlt Frames je Richtung und meldet sie einmal pro Intervall."""

    INTERVAL = 60  # Sekunden

    def __init__(self):
        self.main_frames = 0
        self.disp_frames = 0
        self.main_junk = 0   # Bytes ohne Frame-Start (Rauschen / Versatz)
        self.disp_junk = 0
        self._next = time.time() + self.INTERVAL
        self._prev = read_uart_counters()
        self._suspicious = False
        self._levels = read_rx_levels()
        print(f"[BUS] RX-Pegel beim Start: {self._levels}")

    @staticmethod
    def _delta(now, prev, side, key):
        return now.get(side, {}).get(key, 0) - prev.get(side, {}).get(key, 0)

    def _driver_text(self, now, side):
        if "error" in now:
            return "n/a"
        d = lambda k: self._delta(now, self._prev, side, k)
        return f"rx+{d('rx')} fe+{d('fe')} brk+{d('brk')} oe+{d('oe')}"

    def tick(self, mqtt=None):
        now_t = time.time()
        if now_t < self._next:
            return
        self._next = now_t + self.INTERVAL

        counters = read_uart_counters()
        levels = read_rx_levels()
        # Pegel ist nur bei ruhendem Bus aussagekraeftig; bei laufendem Verkehr
        # trifft die Stichprobe zufaellig ein Bit und erzeugt Schein-Wechsel.
        bus_idle = self.disp_frames == 0 and self.main_frames == 0
        if bus_idle:
            for name, lvl in levels.items():
                if lvl != self._levels.get(name):
                    print(f"[BUS] PEGELWECHSEL {name}: {self._levels.get(name)} -> {lvl}")
            self._levels = levels
        print(f"[BUS] {self.INTERVAL}s: "
              f"Display {self.disp_frames} Frames/{self.disp_junk} Muell "
              f"[AMA2 {self._driver_text(counters, 'disp')}] | "
              f"Mainboard {self.main_frames} Frames/{self.main_junk} Muell "
              f"[AMA3 {self._driver_text(counters, 'main')}] | "
              f"Pegel RXD2={levels['RXD2']} RXD3={levels['RXD3']}")

        suspicious = self.disp_frames > 0 and self.main_frames == 0
        if suspicious and not self._suspicious:
            snapshot("Display sendet, Mainboard schweigt")
        elif self._suspicious and not suspicious:
            print("[BUS] Mainboard sendet wieder.")
        self._suspicious = suspicious

        if mqtt is not None:
            try:
                mqtt.update_states({"bus_main_fpm": self.main_frames,
                                    "bus_disp_fpm": self.disp_frames})
            except Exception as e:
                print(f"[BUS] MQTT-Update fehlgeschlagen: {e}")

        self.main_frames = self.disp_frames = 0
        self.main_junk = self.disp_junk = 0
        self._prev = counters

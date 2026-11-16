#!/usr/bin/env python3
"""
Bus-Diagnose fuer Coffee26 (Philips 2200 MITM).

Schaltet die Relais in den MITM-Modus, reicht den UART-Verkehr genau wie
controller.py durch (jeder Port schreibt auf sich selbst zurueck) und zaehlt,
was von Display und Mainboard ankommt. Am Ende gehen die Relais IMMER zurueck
in den Bypass.

Vorher den Dienst stoppen, sonst streiten sich zwei Prozesse um die Ports:
    sudo systemctl stop coffee26.service
Aufruf (Dauer in Sekunden, Standard 20):
    sudo /home/fuchsi/.coffee/bin/python3 diag_bus.py 20
"""
import collections
import sys
import time

import RPi.GPIO as GPIO
import serial

PORT_DISP = "/dev/ttyAMA2"  # RX: Frames vom Display (12 Byte)
PORT_MAIN = "/dev/ttyAMA3"  # RX: Frames vom Mainboard (19 Byte)
PIN_PWR_DISP = 17           # active-low: LOW = Display stromlos
PIN_MITM = 27               # active-low: LOW = MITM, HIGH = Bypass
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0


def hx(data):
    return " ".join(f"{b:02x}" for b in data)


def extract_frames(buf, length):
    """Schneidet vollstaendige Frames (Start d5 55) aus dem Puffer."""
    found = []
    while True:
        i = buf.find(b"\xd5\x55")
        if i < 0:
            del buf[:-1]  # letztes Byte koennte ein d5 sein
            return found
        if len(buf) - i < length:
            del buf[:i]
            return found
        found.append(bytes(buf[i:i + length]))
        del buf[:i + length]


def main():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PIN_PWR_DISP, GPIO.OUT)
    GPIO.setup(PIN_MITM, GPIO.OUT)

    sides = {
        "Display ": {"port": PORT_DISP, "flen": 12},
        "Mainbrd ": {"port": PORT_MAIN, "flen": 19},
    }
    try:
        for name, s in sides.items():
            try:
                s["ser"] = serial.Serial(s["port"], 115200, timeout=0)
            except serial.SerialException as e:
                print(f"[FEHLER] {s['port']} laesst sich nicht oeffnen: {e}")
                return
            s.update(buf=bytearray(), raw=0, first=b"", frames=collections.Counter())

        GPIO.output(PIN_PWR_DISP, GPIO.HIGH)
        GPIO.output(PIN_MITM, GPIO.LOW)
        print(f"[DIAG] MITM aktiv, lausche {DURATION:.0f}s ...")
        for s in sides.values():
            s["ser"].reset_input_buffer()

        end = time.time() + DURATION
        next_report = time.time() + 1
        while time.time() < end:
            for s in sides.values():
                n = s["ser"].in_waiting
                if n:
                    data = s["ser"].read(n)
                    s["ser"].write(data)  # durchreichen wie controller.py
                    s["raw"] += len(data)
                    if len(s["first"]) < 64:
                        s["first"] += data[:64 - len(s["first"])]
                    s["buf"] += data
                    for f in extract_frames(s["buf"], s["flen"]):
                        s["frames"][f] += 1
            if time.time() >= next_report:
                print(" | ".join(
                    f"{name}->Pi: {s['raw']:7d} B, {sum(s['frames'].values()):5d} Frames"
                    for name, s in sides.items()))
                next_report += 1
            time.sleep(0.001)

        print("\n========== ERGEBNIS ==========")
        for name, s in sides.items():
            print(f"\n{name.strip()} ({s['port']}): {s['raw']} Bytes, "
                  f"{sum(s['frames'].values())} gueltige Frames")
            print(f"  erste Rohbytes: {hx(s['first']) or '(nichts empfangen)'}")
            for f, c in s["frames"].most_common(5):
                print(f"  {c:6d}x  {hx(f)}")
    finally:
        GPIO.output(PIN_PWR_DISP, GPIO.HIGH)
        GPIO.output(PIN_MITM, GPIO.HIGH)
        for s in sides.values():
            if s.get("ser"):
                s["ser"].close()
        print("\n[DIAG] Relais zurueck im BYPASS. Maschine laeuft autonom.")


if __name__ == "__main__":
    main()

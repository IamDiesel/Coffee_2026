import json
import time
from threading import Thread

from bus_diag import BusStats


class CoffeeController(Thread):
    def __init__(self, hardware, protocol, mqtt, auto_bypass=True, power_cmd_timeout=None):
        super().__init__()
        self.hw = hardware
        self.protocol = protocol
        self.mqtt = mqtt
        self._running = False
        self.stats = BusStats()
        # True = Pi steuert (MITM), False = Handbetrieb (Bypass)
        self.mitm = True
        # Nach gescheitertem Power-On automatisch in den Handbetrieb wechseln
        self.auto_bypass = auto_bypass
        # Wartezeit pro Weckversuch; per HA-Auswahl "Power-On Wartezeit" umschaltbar
        self.power_cmd_timeout = self.POWER_TIMEOUT_DEFAULT
        if power_cmd_timeout in self.POWER_TIMEOUT_CHOICES:
            self.power_cmd_timeout = float(power_cmd_timeout)

    def _bypass_requested(self):
        ev = getattr(self.mqtt, "bypass_requested", None)
        return ev is not None and ev.is_set()

    def _set_mitm(self, active, reason):
        if active != self.mitm:
            self.hw.set_mitm(active)
            self.mitm = active
            mode = "PI-STEUERUNG (MITM)" if active else "HANDBETRIEB (Bypass)"
            print(f"[CONTROLLER] Modus: {mode} | Grund: {reason}")
        if active:
            attr = {"aktiv": "Pi-Steuerung",
                    "hinweis": "Pi-Steuerung aktiv · Klick: Handbetrieb"}
        else:
            attr = {"aktiv": "Handbetrieb",
                    "hinweis": "Handbetrieb aktiv · Klick: Pi-Steuerung"}
        self.mqtt.update_states({"mitm": "ON" if active else "OFF",
                                 "mitm_attr": json.dumps(attr, ensure_ascii=False)})

    def stop(self):
        self._running = False

    def _process_mainboard_stream(self):
        stream = self.hw.get_mainboard_stream()
        while stream.inWaiting() >= 19:
            if stream.read(1) == b'\xd5':
                input_main = b'\xd5' + stream.read(18)
                stream.write(input_main)
                self.stats.main_frames += 1
                status_dict = self.protocol.decode_mainboard_message(input_main)
                self.mqtt.update_states(status_dict)
                self.hw.update_gui(status_dict)
            else:
                self.stats.main_junk += 1

    def _process_display_stream(self):
        stream = self.hw.get_display_stream()
        while stream.inWaiting() >= 12:
            if stream.read(1) == b'\xd5':
                input_disp = b'\xd5' + stream.read(11)
                stream.write(input_disp)
                self.stats.disp_frames += 1
            else:
                self.stats.disp_junk += 1

    def execute_command(self, cmd_topic):
        # --- Umschalten Pi-Steuerung / Handbetrieb ---
        if cmd_topic == "mitm_off":
            self._set_mitm(False, "Schalter in Home Assistant")
            ev = getattr(self.mqtt, "bypass_requested", None)
            if ev is not None:
                ev.clear()
            return
        if cmd_topic == "mitm_on":
            self._set_mitm(True, "Schalter in Home Assistant")
            return

        # --- Einstellung: Wartezeit pro Weckversuch (kein Maschinenbefehl) ---
        if cmd_topic.startswith("power_timeout:"):
            self.set_power_timeout(cmd_topic.split(":", 1)[1], "Auswahl in Home Assistant")
            return

        # Jeder Maschinenbefehl braucht den Pi auf dem Bus -> ggf. zurueckschalten
        if not self.mitm:
            self._set_mitm(True, f"Befehl '{cmd_topic}' aus Home Assistant")

        cmd_bytes = self.protocol.get_cmd(cmd_topic)
        cmd_duration = 0.3

        durations = {
            "espresso": 0.3,
            "coffee": 0.2,
            "steam": 0.6,
            "play": 0.8,
            "power_off": 0.5
        }

        if cmd_topic in durations:
            cmd_duration = durations[cmd_topic]

        if cmd_topic in ["power_on", "power_nc"]:
            result = self.execute_power_on(clean=(cmd_topic == "power_on"))
            # HARTE ANFORDERUNG: Die Maschine muss immer von Hand bedienbar sein.
            # Nur "ok" (Mainboard an UND Display sendet) bleibt unter Pi-Steuerung.
            # Bei dunklem Display oder stummem Mainboard -> Handbetrieb: Display und
            # Mainboard sind dann direkt verbunden, die Display-Tasten funktionieren.
            if result in ("display_dark", "mb_dead") and not self._bypass_requested() and self.auto_bypass:
                reason = ("Display bleibt dunkel" if result == "display_dark"
                          else "Mainboard antwortet nicht")
                print(f"[CONTROLLER] {reason} -> Handbetrieb, damit die Maschine "
                      f"am Display bedient werden kann.")
                self._set_mitm(False, f"Power ON: {reason} (automatischer Rueckfall)")
            return

        if cmd_bytes:
            print(f"[CONTROLLER] Führe '{cmd_topic}' aus (Sende Signalblock für {cmd_duration}s)")
            start_time = time.time()
            while (time.time() - start_time) < cmd_duration:
                self.hw.get_display_stream().write(cmd_bytes)
                self._process_mainboard_stream()
                time.sleep(0.01)  # WICHTIG: Puffer Overflow verhindern

    # --- Power-On-Parameter (Baseline-Messung vom 20.11.2026) ----------------
    # Echtes Display beim Einschalten: 4-16 Weck-Frames (0a) im 9-ms-Takt, dann
    # 16-28 Power-Frames (01/02) im 9-ms-Takt, danach Ruhe-Frames alle 12 ms.
    # Das Mainboard antwortete in beiden Messungen 157 ms nach dem ersten Frame,
    # der Bus lief nach ~0.3 s stabil mit je 83 Frames/s.
    POWER_ATTEMPTS = 11            # Weckversuche fuer das Mainboard
    POWER_TIMEOUT_DEFAULT = 2.0    # s pro Versuch (Standard); 4 und 6 als Test per HA waehlbar
    POWER_TIMEOUT_CHOICES = (2.0, 4.0, 6.0)
    WAKE_FRAMES = 16               # Anzahl Weck-Frames (0a) je Versuch
    WAKE_FRAME_INTERVAL = 0.009    # s Abstand der Weck- und Power-Frames
    BUS_CHECK_SECONDS = 1.5        # s Busbeobachtung je Runde (Original: 3 x 0.5 s)
    BUS_MIN_FRAMES = 60            # je Richtung in einer Runde; Soll 83/s, also ~0.7 s Verkehr
    # Display-Resets, wenn es dunkel bleibt. Baseline aus Philips_2200.py (Jan. 2025):
    # 0.4 s stromlos, max. 10 Wiederholungen. Damals getestet: 0.28 / 0.47 / 0.48 /
    # 0.50 / 0.51 / 0.52 s (je 1-3 Toggles noetig). Variation deshalb nur in diesem Fenster.
    DISPLAY_TRIP_DURATIONS = (0.40, 0.40, 0.45, 0.35, 0.48, 0.30, 0.50, 0.28, 0.52, 0.40)

    def set_power_timeout(self, seconds, reason):
        """Wartezeit pro Weckversuch setzen (nur 2, 4 oder 6 s) und an HA melden."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = None
        if seconds not in self.POWER_TIMEOUT_CHOICES:
            print(f"[CONTROLLER] Ungueltige Power-On-Wartezeit {seconds!r} "
                  f"(erlaubt: {self.POWER_TIMEOUT_CHOICES}) - bleibe bei {self.power_cmd_timeout:.0f}s")
        elif seconds != self.power_cmd_timeout:
            self.power_cmd_timeout = seconds
            print(f"[CONTROLLER] Power-On-Wartezeit: {seconds:.0f}s pro Versuch | Grund: {reason}")
        self.mqtt.update_states({"power_timeout": f"{self.power_cmd_timeout:.0f}"})

    def _publish_power_result(self, text):
        self.mqtt.update_states({"power_on_result": f"{text} ({time.strftime('%H:%M')})"})

    def _sync_bus(self, duration):
        """Reicht den Bus `duration` Sekunden lang normal durch.

        Gibt (Display-Frames, Mainboard-Frames) in diesem Zeitraum zurueck.
        Das Mainboard antwortet nur auf Display-Frames (gemessen: 1:1, ~83/s),
        deshalb schweigt der Bus, solange das Display dunkel ist.
        """
        d0, m0 = self.stats.disp_frames, self.stats.main_frames
        t_end = time.time() + duration
        while time.time() < t_end and not self._bypass_requested():
            self._process_display_stream()
            self._process_mainboard_stream()
            if (self.stats.disp_frames - d0 >= self.BUS_MIN_FRAMES
                    and self.stats.main_frames - m0 >= self.BUS_MIN_FRAMES):
                break
            time.sleep(0.001)
        return self.stats.disp_frames - d0, self.stats.main_frames - m0

    def execute_power_on(self, clean=True):
        """Schaltet die Maschine ein.

        Rueckgabe: "ok" (Mainboard an, Display sendet), "display_dark"
        (Mainboard an, Display dunkel), "mb_dead" (Mainboard antwortet nicht)
        oder "aborted" (Handbetrieb per Schalter angefordert).

        Phase 1: Mainboard wecken. Je Versuch: Weck-Frames, dann Power-Frames
                 im Takt des echten Displays, bis ein gueltiger Mainboard-Frame
                 kommt oder die Wartezeit (2/4/6 s) abgelaufen ist.
        Phase 2: Pruefen, ob der Bus in beide Richtungen laeuft; wenn das
                 Display dunkel bleibt, Display-Reset um die 0.4-s-Baseline.
        """
        wait = self.power_cmd_timeout
        print(f"[CONTROLLER] Starte Power ON (Clean={clean}, Wartezeit {wait:.0f}s pro Versuch)")
        t_start = time.time()

        cmd_pwr = self.protocol.get_cmd('power_on_clean') if clean else self.protocol.get_cmd('power_on_no_clean')
        cmd_beep = self.protocol.get_cmd('beep')
        stream_disp = self.hw.get_display_stream()
        stream_mb = self.hw.get_mainboard_stream()

        self.hw.toggle_display_power(0.4)
        stream_mb.read(stream_mb.inWaiting())

        # ---------- Phase 1: Mainboard wecken ----------
        mb_awake = False
        attempt = 0
        while attempt < self.POWER_ATTEMPTS and not mb_awake:
            attempt += 1
            print(f"[CONTROLLER] Sende Power-Befehl (Versuch {attempt}/{self.POWER_ATTEMPTS})...")
            t_attempt = time.time()
            m0 = self.stats.main_frames

            for _ in range(self.WAKE_FRAMES):
                stream_disp.write(cmd_beep)
                time.sleep(self.WAKE_FRAME_INTERVAL)

            timeout_power = time.time() + wait
            while time.time() < timeout_power and not self._bypass_requested():
                stream_disp.write(cmd_pwr)
                if hasattr(stream_disp, 'flush'):
                    stream_disp.flush()
                time.sleep(self.WAKE_FRAME_INTERVAL)
                # Nur ein vollstaendiger Frame (Start d5, 19 Byte) zaehlt als Antwort;
                # einzelne Stoer-Bytes werden verworfen und als Muell gezaehlt.
                self._process_mainboard_stream()
                if self.stats.main_frames > m0:
                    break

            if self._bypass_requested():
                print(f"[POWERON] ABGEBROCHEN (Handbetrieb angefordert) nach "
                      f"{attempt} Versuch(en), {time.time() - t_start:.1f}s")
                self._publish_power_result("Abgebrochen")
                return "aborted"

            if self.stats.main_frames > m0:
                mb_awake = True
                print(f"[CONTROLLER] Mainboard antwortet (Versuch {attempt}, "
                      f"{(time.time() - t_attempt) * 1000:.0f} ms nach Beginn des Versuchs).")
            else:
                print("[CONTROLLER] Mainboard antwortet nicht. Display-Reset und neuer Versuch...")
                self.hw.toggle_display_power(0.4)

        if not mb_awake:
            print(f"[POWERON] FEHLSCHLAG: Mainboard antwortet nicht nach {attempt} Versuchen "
                  f"(Wartezeit {wait:.0f}s), {time.time() - t_start:.1f}s")
            self._publish_power_result(f"Mainboard antwortet nicht -> Handbetrieb [{wait:.0f}s]")
            return "mb_dead"

        # ---------- Phase 2: Bus pruefen ----------
        trips = (None,) + tuple(self.DISPLAY_TRIP_DURATIONS)
        for i, trip in enumerate(trips):
            if trip is not None:
                print(f"[CONTROLLER] Display dunkel -> Display-Reset {i}/{len(trips) - 1} "
                      f"({trip * 1000:.0f} ms stromlos)")
                self.hw.toggle_display_power(trip)
            disp, main = self._sync_bus(self.BUS_CHECK_SECONDS)
            print(f"[CONTROLLER] Bus-Check: Display {disp} / Mainboard {main} Frames "
                  f"(noetig: je {self.BUS_MIN_FRAMES})")
            if self._bypass_requested():
                print(f"[POWERON] ABGEBROCHEN (Handbetrieb angefordert), Mainboard laeuft bereits")
                self._publish_power_result("Abgebrochen (Mainboard an)")
                return "aborted"
            if disp >= self.BUS_MIN_FRAMES and main >= self.BUS_MIN_FRAMES:
                last = f", letzter Reset {trip * 1000:.0f} ms" if trip is not None else ""
                print(f"[POWERON] ERFOLG nach {attempt} Versuch(en) (Wartezeit {wait:.0f}s), "
                      f"{i} Display-Reset(s){last}, {time.time() - t_start:.1f}s")
                self._publish_power_result(
                    (f"OK, Versuch {attempt}" if i == 0 else f"OK, Versuch {attempt}, {i} Display-Reset(s)")
                    + f" [{wait:.0f}s]")
                return "ok"

        print(f"[POWERON] DISPLAY DUNKEL: Mainboard an (Versuch {attempt}, Wartezeit {wait:.0f}s), "
              f"Display sendet nach {len(trips) - 1} Display-Resets nicht "
              f"({time.time() - t_start:.1f}s). Relais Display-Strom (GPIO17 / Relais 2.1) pruefen!")
        self._publish_power_result(f"Display dunkel -> Handbetrieb [{wait:.0f}s]")
        return "display_dark"

    def run(self):
        self._running = True
        print("[CONTROLLER] Man-in-the-Middle Schleife läuft...")
        self._set_mitm(True, "Start")  # Zustand an HA melden
        self.set_power_timeout(self.power_cmd_timeout, "Start")
        while self._running:
            try:
                if self.mitm:
                    self._process_display_stream()
                    self._process_mainboard_stream()
                # Im Handbetrieb ist der Pi per Relais vom Bus getrennt -> nichts zu tun

                pending_cmd = self.mqtt.get_pending_command()
                if pending_cmd:
                    self.execute_command(pending_cmd)

                self.stats.tick(self.mqtt)

                time.sleep(0.001)
            except Exception as e:
                print(f"\n[CONTROLLER] KRITISCHER FEHLER in Hauptschleife: {e}")
                self._running = False
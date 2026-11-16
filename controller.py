import json
import time
from threading import Thread

from bus_diag import BusStats


class CoffeeController(Thread):
    def __init__(self, hardware, protocol, mqtt, auto_bypass=True):
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
            success = self.execute_power_on(clean=(cmd_topic == "power_on"))
            if not success and not self._bypass_requested() and self.auto_bypass:
                print("[CONTROLLER] Power ON gescheitert -> Handbetrieb, damit die Maschine "
                      "am Display eingeschaltet werden kann.")
                self._set_mitm(False, "Power ON fehlgeschlagen (automatischer Rueckfall)")
            return

        if cmd_bytes:
            print(f"[CONTROLLER] Führe '{cmd_topic}' aus (Sende Signalblock für {cmd_duration}s)")
            start_time = time.time()
            while (time.time() - start_time) < cmd_duration:
                self.hw.get_display_stream().write(cmd_bytes)
                self._process_mainboard_stream()
                time.sleep(0.01)  # WICHTIG: Puffer Overflow verhindern

    def execute_power_on(self, clean=True):
        print(f"[CONTROLLER] Starte Power ON (Clean={clean})")
        t_start = time.time()

        cmd_pwr = self.protocol.get_cmd('power_on_clean') if clean else self.protocol.get_cmd('power_on_no_clean')
        cmd_beep = self.protocol.get_cmd('beep')

        self.hw.toggle_display_power(0.4)

        if cmd_beep:
            for _ in range(23):
                self.hw.get_display_stream().write(cmd_beep)
                time.sleep(0.01)  # WICHTIG: OS-Puffer nicht fluten!

        stream_mb = self.hw.get_mainboard_stream()
        stream_mb.read(stream_mb.inWaiting())

        count_toggle = 0
        success = False

        while count_toggle <= 10 and not success:
            print(f"[CONTROLLER] Sende Power-Befehl (Versuch {count_toggle + 1}/11)...")

            # Zeit-basiertes Senden anstatt Endlos-Schleife ohne Limit
            timeout_power = time.time() + 8.0

            while stream_mb.inWaiting() <= 0:
                self.hw.get_display_stream().write(cmd_pwr)
                if hasattr(self.hw.get_display_stream(), 'flush'):
                    self.hw.get_display_stream().flush()
                time.sleep(0.01)  # VERHINDERT ABSTURZ BEIM BOOTEN
                if time.time() > timeout_power or self._bypass_requested():
                    break

            if self._bypass_requested():
                print(f"[POWERON] ABGEBROCHEN (Handbetrieb angefordert) nach "
                      f"{count_toggle + 1} Versuch(en), {time.time() - t_start:.1f}s")
                return False

            if stream_mb.inWaiting() > 0:
                print("[CONTROLLER] Mainboard reagiert. Synchronisiere Bus...")
                count_disp_msg = 0
                count_main_msg = 0
                timeout = time.time() + 1.5

                while time.time() < timeout:
                    if stream_mb.inWaiting() >= 19:
                        if stream_mb.read(1) == b'\xd5':
                            input_main = b'\xd5' + stream_mb.read(18)
                            # Gleiche Richtung wie _process_mainboard_stream: zurueck auf denselben Port
                            stream_mb.write(input_main)
                            count_main_msg += 1
                            self.stats.main_frames += 1

                    stream_disp = self.hw.get_display_stream()
                    if stream_disp.inWaiting() >= 12:
                        if stream_disp.read(1) == b'\xd5':
                            input_disp = b'\xd5' + stream_disp.read(11)
                            # Gleiche Richtung wie _process_display_stream: zurueck auf denselben Port
                            stream_disp.write(input_disp)
                            count_disp_msg += 1
                            self.stats.disp_frames += 1

                    if (count_disp_msg + count_main_msg) >= 200:
                        success = True
                        break

                if success:
                    print(
                        f"[CONTROLLER] Bus stabil (Nachrichten: {count_disp_msg + count_main_msg}). Power ON erfolgreich.")
                    print(f"[POWERON] ERFOLG nach {count_toggle + 1} Versuch(en), {time.time() - t_start:.1f}s")
                    return True

            if not success:
                print("[CONTROLLER] Bus nicht stabil. Führe Hardware-Reset (Toggle) durch...")
                self.hw.toggle_display_power(0.4)
                count_toggle += 1

        if not success:
            print("[CONTROLLER] KRITISCHER FEHLER: Power ON nach 10 Hardware-Resets fehlgeschlagen!")
            print(f"[POWERON] FEHLSCHLAG nach {count_toggle} Versuchen, {time.time() - t_start:.1f}s")
        return success

    def run(self):
        self._running = True
        print("[CONTROLLER] Man-in-the-Middle Schleife läuft...")
        self._set_mitm(True, "Start")  # Zustand an HA melden
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
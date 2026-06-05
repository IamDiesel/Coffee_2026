import time
from threading import Thread


class CoffeeController(Thread):
    def __init__(self, hardware, protocol, mqtt):
        super().__init__()
        self.hw = hardware
        self.protocol = protocol
        self.mqtt = mqtt
        self._running = False

    def stop(self):
        self._running = False

    def _process_mainboard_stream(self):
        stream = self.hw.get_mainboard_stream()
        while stream.inWaiting() >= 19:
            if stream.read(1) == b'\xd5':
                input_main = b'\xd5' + stream.read(18)
                stream.write(input_main)
                status_dict = self.protocol.decode_mainboard_message(input_main)
                self.mqtt.update_states(status_dict)
                self.hw.update_gui(status_dict)

    def _process_display_stream(self):
        stream = self.hw.get_display_stream()
        while stream.inWaiting() >= 12:
            if stream.read(1) == b'\xd5':
                input_disp = b'\xd5' + stream.read(11)
                stream.write(input_disp)

    def execute_command(self, cmd_topic):
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
            self.execute_power_on(clean=(cmd_topic == "power_on"))
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
                if time.time() > timeout_power:
                    break

            if stream_mb.inWaiting() > 0:
                print("[CONTROLLER] Mainboard reagiert. Synchronisiere Bus...")
                count_disp_msg = 0
                count_main_msg = 0
                timeout = time.time() + 1.5

                while time.time() < timeout:
                    if stream_mb.inWaiting() >= 19:
                        if stream_mb.read(1) == b'\xd5':
                            input_main = b'\xd5' + stream_mb.read(18)
                            self.hw.get_display_stream().write(input_main)
                            count_main_msg += 1

                    stream_disp = self.hw.get_display_stream()
                    if stream_disp.inWaiting() >= 12:
                        if stream_disp.read(1) == b'\xd5':
                            input_disp = b'\xd5' + stream_disp.read(11)
                            stream_mb.write(input_disp)
                            count_disp_msg += 1

                    if (count_disp_msg + count_main_msg) >= 200:
                        success = True
                        break

                if success:
                    print(
                        f"[CONTROLLER] Bus stabil (Nachrichten: {count_disp_msg + count_main_msg}). Power ON erfolgreich.")
                    break

            if not success:
                print("[CONTROLLER] Bus nicht stabil. Führe Hardware-Reset (Toggle) durch...")
                self.hw.toggle_display_power(0.4)
                count_toggle += 1

        if not success:
            print("[CONTROLLER] KRITISCHER FEHLER: Power ON nach 10 Hardware-Resets fehlgeschlagen!")

    def run(self):
        self._running = True
        print("[CONTROLLER] Man-in-the-Middle Schleife läuft...")
        while self._running:
            try:
                self._process_display_stream()
                self._process_mainboard_stream()

                pending_cmd = self.mqtt.get_pending_command()
                if pending_cmd:
                    self.execute_command(pending_cmd)

                time.sleep(0.001)
            except Exception as e:
                print(f"\n[CONTROLLER] KRITISCHER FEHLER in Hauptschleife: {e}")
                self._running = False
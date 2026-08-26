import tkinter as tk
from tkinter import scrolledtext
from threading import Thread
import time
import datetime
import requests
import io
import sys
import unittest
import queue

from hardware_interface import CoffeeMachineHardware
from my_config import HA_URL, HA_TOKEN
from test_full_coverage import PhilipsFullCoverageTest
import mqtt_manager
import protocol


class MockStream:
    def __init__(self, name="", log_callback=None):
        self.name = name
        self.buffer = bytearray()
        self.log_callback = log_callback

    def inWaiting(self):
        return len(self.buffer)

    def read(self, n=1):
        res = self.buffer[:n]
        self.buffer = self.buffer[n:]
        return bytes(res)

    def write(self, data):
        # 1. HARDWARE SIMULATION: Baudrate 115.200
        # 12 Bytes dauern ca. 1.04 ms. Wir nutzen einen Busy-Wait, um Windows/Linux OS-Sleep Latenzen zu umgehen.
        # So blockiert die Simulation exakt wie der echte UART-Puffer und 7500 Iterationen dauern 7,8s!
        target_time = time.perf_counter() + (len(data) / 11520.0)
        while time.perf_counter() < target_time:
            pass

        if self.log_callback:
            self.log_callback(self.name, data)


class RedirectText(io.StringIO):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def write(self, string):
        self.text_widget.insert(tk.END, string)
        self.text_widget.see(tk.END)


class DigitalTwinHardware(CoffeeMachineHardware):
    def __init__(self):
        self._memory_bean = 0x38
        self._memory_cup = 0x38

        self._power_state = "STANDBY"  # OFF, BOOTING, STANDBY
        self.state = bytearray(b'\xd5\x55' + b'\x00' * 17)
        self.log_queue = queue.Queue()

        self._last_cmd = None
        self._last_cmd_time = 0

        # Single Source of Truth: Mapping wird dynamisch aus Protocol.py geladen!
        proto = protocol.PhilipsProtocol()
        self.cmd_map = {}

        # Sicheres Mapping, falls ein Befehl mal 'None' zurückgibt
        cmd_pairs = [
            (proto.get_cmd('espresso'), "espresso_led"),
            (proto.get_cmd('coffee'), "coffee_led"),
            (proto.get_cmd('hot_water'), "hot_water_led"),
            (proto.get_cmd('steam'), "steam_led"),
            (proto.get_cmd('bean'), "bean_led"),
            (proto.get_cmd('cup'), "cup_size_led"),
            (proto.get_cmd('power_off'), "power"),
            (proto.get_cmd('play'), "play"),
            (proto.get_cmd('power_on_clean'), "power"),
            (proto.get_cmd('power_on_no_clean'), "power")
        ]

        for byte_cmd, str_cmd in cmd_pairs:
            if byte_cmd:
                self.cmd_map[bytes(byte_cmd)] = str_cmd

        self.mock_main = MockStream("MAINBOARD", self._on_uart_write)
        self.mock_disp = MockStream("DISPLAY", self._on_uart_write)

        self.dt_states = {}
        self.ha_states = {}

        self.ha_map = {
            'espresso_led': 'binary_sensor.philips_2200_series_espresso_led',
            'double_espresso': 'binary_sensor.philips_2200_series_double_espresso_led',
            'coffee_led': 'binary_sensor.philips_2200_series_coffee_led',
            'double_coffee': 'binary_sensor.philips_2200_series_double_coffee_led',
            'hot_water_led': 'binary_sensor.philips_2200_series_hot_water_led',
            'steam_led': 'binary_sensor.philips_2200_series_steam_led',
            'water_empty': 'binary_sensor.philips_2200_series_wassertank_leer',
            'waste_full': 'binary_sensor.philips_2200_series_mullbehalter_voll',
            'error_active': 'binary_sensor.philips_2200_series_allgemeiner_fehler',
            'bean_led': 'sensor.philips_2200_series_bohnen',
            'cup_size_led': 'sensor.philips_2200_series_tasse',
            'calc_clean_led': 'binary_sensor.philips_2200_series_entkalken_calc_clean',
            'play_led': 'binary_sensor.philips_2200_series_play_pause_led'
        }

        self.gui_thread = Thread(target=self._init_gui)
        self.gui_thread.daemon = True
        self.gui_thread.start()

        Thread(target=self._delayed_startup, daemon=True).start()

        self.polling_thread = Thread(target=self._poll_ha_continuously)
        self.polling_thread.daemon = True
        self.polling_thread.start()

    def _delayed_startup(self):
        time.sleep(1.0)
        self._reset_to_standby()

    def get_mainboard_stream(self):
        return self.mock_main

    def get_display_stream(self):
        return self.mock_disp

    def toggle_display_power(self, duration: float):
        # 2. HARDWARE SIMULATION: Relais kappt den Strom
        self._sync_log(0, f"[HARDWARE] Relais: Display Strom getrennt für {duration}s")
        self._power_state = "OFF"
        self.state = bytearray(b'\xd5\x55' + b'\x00' * 17)
        self._push_state_to_bus()

    def relais_off(self):
        pass

    def update_gui(self, status_dict: dict):
        self.dt_states = status_dict

    def attach_mqtt(self, main_mqtt_manager):
        self.main_mqtt = main_mqtt_manager
        orig_publish = main_mqtt_manager.client.publish
        orig_on_msg = main_mqtt_manager.on_message

        def patched_publish(topic, payload=None, qos=0, retain=False):
            if "config" not in topic and "availability" not in topic:
                sensor = topic.split('/')[-1]
                self._sync_log(2, f"[PUB] {sensor} = {payload}")
            return orig_publish(topic, payload, qos, retain)

        def patched_on_msg(client, userdata, msg):
            try:
                payload = msg.payload.decode('utf-8')
            except:
                payload = str(msg.payload)
            cmd = msg.topic.split('/')[-1]
            self._sync_log(3, f"[SUB] {cmd} = {payload}")
            return orig_on_msg(client, userdata, msg)

        main_mqtt_manager.client.publish = patched_publish
        main_mqtt_manager.client.on_message = patched_on_msg

    def _on_uart_write(self, target, data):
        data_bytes = bytes(data)
        hex_str = " ".join([f"{b:02X}" for b in data])

        if target == "DISPLAY":
            if data_bytes in self.cmd_map:
                now = time.time()
                if data_bytes != self._last_cmd or (now - self._last_cmd_time) > 1.0:
                    self._last_cmd = data_bytes
                    self._last_cmd_time = now
                    key = self.cmd_map[data_bytes]

                    if key == "power":
                        if self._power_state == "OFF":
                            self._sync_log(1, f"[PASSTHRU] PI CMD: POWER | Simuliere Bootvorgang...")
                            self._power_state = "BOOTING"
                            Thread(target=self._simulate_boot_sequence, daemon=True).start()
                        elif self._power_state == "STANDBY":
                            self._sync_log(1, f"[PASSTHRU] PI CMD: POWER | Schalte ab...")
                            self._power_state = "OFF"
                            self.state = bytearray(b'\xd5\x55' + b'\x00' * 17)
                            self._push_state_to_bus()
                    else:
                        # Ignoriere alle anderen Buttons, wenn die Maschine nicht an ist
                        if self._power_state == "STANDBY":
                            self._sync_log(1, f"[PASSTHRU] PI CMD: {key.upper().ljust(12)} | {hex_str}")
                            self._simulate_button_press(key)
            else:
                self._sync_log(1, f"[PASSTHRU] RAW CMD: {hex_str}")

        elif target == "MAINBOARD":
            self._sync_log(0, f"[PASSTHRU] STAT: {hex_str}")

    def _simulate_boot_sequence(self):
        # 3. HARDWARE SIMULATION: Zeit bis der uC des Mainboards aufwacht
        time.sleep(1.5)

        self._power_state = "STANDBY"
        self.state = bytearray(b'\xd5\x55' + b'\x00' * 17)
        self.state[3] = 0x07
        self.state[4] = 0x07
        self.state[5] = 0x07
        self.state[6] = 0x07
        self.state[8] = self._memory_bean
        self.state[9] = 0x07
        self.state[10] = self._memory_cup
        self.state[11] = 0x07

        self._sync_log(0, "[TWIN] Mainboard wach! Flute Bus für Synchronisation...")

        # 4. HARDWARE SIMULATION: 200-Pakete Burst für den Controller-Sync-Test!
        for _ in range(120):
            if self._power_state != "STANDBY": break
            self.mock_main.buffer.extend(self.state)  # 120 Mainboard Pakete
            self.mock_disp.buffer.extend(b'\xd5\x55\x00\x00\x00\x03\x02\x00\x00\x00\x2d\x01')  # 120 Display Pakete
            time.sleep(0.01)

        self._push_state_to_bus()

    def _sync_log(self, active_col_idx, msg):
        self.log_queue.put((active_col_idx, msg))

    def _clear_consoles(self):
        self.log_queue.put((-1, "CLEAR"))

    def _reset_to_standby(self):
        self._power_state = "STANDBY"
        self.state = bytearray(b'\xd5\x55' + b'\x00' * 17)
        self.state[3] = 0x07
        self.state[4] = 0x07
        self.state[5] = 0x07
        self.state[6] = 0x07
        self.state[8] = self._memory_bean
        self.state[9] = 0x07
        self.state[10] = self._memory_cup
        self.state[11] = 0x07
        self._push_state_to_bus()

    def _clear_drinks(self):
        self.state[3] = 0x00
        self.state[4] = 0x00
        self.state[5] = 0x00
        self.state[6] = 0x00

    def _is_standby(self):
        return (self.state[3] == 0x07 and self.state[4] == 0x07 and
                self.state[5] == 0x07 and self.state[6] == 0x07)

    def _push_state_to_bus(self):
        self.mock_main.buffer.extend(self.state)
        hex_str = " ".join([f"{b:02X}" for b in self.state])
        self._sync_log(0, f"[INJECT]   STAT: {hex_str}")

    def _handle_drink_button(self, byte_idx):
        if self._is_standby() or self.state[byte_idx] == 0x00:
            self._clear_drinks()
            self.state[byte_idx] = 0x07
        else:
            if byte_idx in [3, 5]:
                if self.state[byte_idx] == 0x07:
                    self.state[byte_idx] = 0x38
                else:
                    self._reset_to_standby()
            else:
                self._reset_to_standby()

    def _simulate_button_press(self, key):
        if hasattr(self, '_manual_trigger') and self._manual_trigger:
            hex_bytes = next((k for k, v in self.cmd_map.items() if v == key), None)
            if hex_bytes:
                hex_str = " ".join([f"{b:02X}" for b in hex_bytes])
                self._sync_log(1, f"[INJECT]   GUI BTN: {key.upper().ljust(12)} | {hex_str}")
            else:
                self._sync_log(1, f"[INJECT]   GUI BTN: {key.upper().ljust(12)}")

        if key == "play":
            self.state[16] = 0x00 if self.state[16] == 0x07 else 0x07
        elif key == "espresso_led":
            self._handle_drink_button(3)
        elif key == "coffee_led":
            self._handle_drink_button(5)
        elif key == "hot_water_led":
            self._handle_drink_button(4)
        elif key == "steam_led":
            self._handle_drink_button(6)

        elif key == "water_empty":
            self.state[14] = 0x00 if self.state[14] == 0x38 else 0x38
        elif key == "waste_full":
            self.state[15] = 0x00 if self.state[15] == 0x07 else 0x07
        elif key == "error_active":
            self.state[15] = 0x00 if self.state[15] == 0x38 else 0x38

        elif key == "calc_clean_led":
            self.state[12] = 0x00 if self.state[12] == 0x38 else 0x38

        elif key == "bean_led":
            self.state[9] = 0x07
            if self.state[8] == 0x00:
                self.state[8] = 0x38
            elif self.state[8] == 0x38:
                self.state[8] = 0x3F
            else:
                self.state[8] = 0x00
            self._memory_bean = self.state[8]

        elif key == "cup_size_led":
            self.state[11] = 0x07
            if self.state[10] == 0x00:
                self.state[10] = 0x38
            elif self.state[10] == 0x38:
                self.state[10] = 0x3F
            else:
                self.state[10] = 0x00
            self._memory_cup = self.state[10]

        self._push_state_to_bus()

    def _manual_press(self, key):
        self._manual_trigger = True

        if key == "power":
            if self._power_state == "STANDBY":
                self._sync_log(1, "[INJECT]   GUI BTN: POWER | Schalte ab...")
                self._power_state = "OFF"
                self.state = bytearray(b'\xd5\x55' + b'\x00' * 17)
                self._push_state_to_bus()
            elif self._power_state == "OFF":
                self._sync_log(1, "[INJECT]   GUI BTN: POWER | Simuliere Bootvorgang...")
                self._power_state = "BOOTING"
                Thread(target=self._simulate_boot_sequence, daemon=True).start()
        else:
            if getattr(self, '_power_state', "STANDBY") == "STANDBY":
                self._simulate_button_press(key)

        self._manual_trigger = False

    def _poll_ha_continuously(self):
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "content-type": "application/json"}
        while True:
            for internal_key, ha_id in self.ha_map.items():
                try:
                    res = requests.get(f"{HA_URL}/states/{ha_id}", headers=headers, timeout=1)
                    if res.status_code == 200:
                        self.ha_states[internal_key] = str(res.json().get('state', 'OFF')).upper()
                    else:
                        self.ha_states[internal_key] = "ERR 404"
                except:
                    self.ha_states[internal_key] = "OFFLINE"
            time.sleep(2)

    def _run_tests(self):
        def task():
            self.test_console.insert(tk.END, "\n" + "=" * 50 + "\nSTARTE E2E TESTLAUF...\n" + "=" * 50 + "\n")
            old_stdout = sys.stdout
            sys.stdout = RedirectText(self.test_console)

            orig_stop = mqtt_manager.MqttManager.stop

            def patched_stop(mqtt_self):
                if getattr(mqtt_self, 'client', None) and 'Test' in str(getattr(mqtt_self.client, '_client_id', '')):
                    mqtt_self.client.loop_stop()
                    mqtt_self.client.disconnect()
                else:
                    orig_stop(mqtt_self)

            mqtt_manager.MqttManager.stop = patched_stop

            orig_update = mqtt_manager.MqttManager.update_states

            def patched_update(mqtt_self, status_dict):
                orig_update(mqtt_self, status_dict)
                if getattr(mqtt_self, 'client', None) and 'Test' in str(getattr(mqtt_self.client, '_client_id', '')):
                    self.update_gui(status_dict)

            mqtt_manager.MqttManager.update_states = patched_update

            try:
                runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2)
                suite = unittest.TestLoader().loadTestsFromTestCase(PhilipsFullCoverageTest)
                runner.run(suite)
            finally:
                sys.stdout = old_stdout
                self.test_console.insert(tk.END, "\n=== TESTLAUF BEENDET ===\n")
                self.test_console.see(tk.END)

                mqtt_manager.MqttManager.stop = orig_stop
                mqtt_manager.MqttManager.update_states = orig_update

                if hasattr(self, 'main_mqtt'):
                    self.main_mqtt.last_states.clear()

                self._push_state_to_bus()

                proto = protocol.PhilipsProtocol()
                real_status = proto.decode_mainboard_message(self.state)
                self.update_gui(real_status)

        Thread(target=task, daemon=True).start()

    def _init_gui(self):
        self.root = tk.Tk()
        self.root.title("Philips 2200 - Digital Twin Lab")
        self.root.configure(bg='#1e1e1e')
        self.root.geometry("1600x1000")

        tk.Label(self.root, text="PHILIPS 2200 INTERACTIVE TWIN", fg="#00aaff", bg="#1e1e1e",
                 font=("Arial", 20, "bold")).pack(pady=5)

        panel_frame = tk.Frame(self.root, bg="#2d2d2d", bd=2, relief="sunken")
        panel_frame.pack(fill="x", padx=20, pady=5)

        tk.Label(panel_frame, text="Manuelle Kontrolle", fg="white", bg="#2d2d2d", font=("Arial", 10, "bold")).grid(
            row=0, column=0, padx=10, pady=5)
        tk.Label(panel_frame, text="Controller State (Pi)", fg="white", bg="#2d2d2d", font=("Arial", 10, "bold")).grid(
            row=0, column=1, padx=10, pady=5)
        tk.Label(panel_frame, text="Home Assistant (WLAN)", fg="white", bg="#2d2d2d", font=("Arial", 10, "bold")).grid(
            row=0, column=2, padx=10, pady=5)

        self.gui_elements = {}
        layout = [
            ("power", "⏻ Power / Reset", None),
            ("play_led", "▶ Play / Pause (Start)", "play"),
            ("---", "---", None),
            ("espresso_led", "☕ Espresso (Klick = x1/x2/Off)", "espresso_led"),
            ("double_espresso", "↳ Double Espresso", None),
            ("coffee_led", "☕ Coffee (Klick = x1/x2/Off)", "coffee_led"), ("double_coffee", "↳ Double Coffee", None),
            ("hot_water_led", "💧 Hot Water", "hot_water_led"), ("steam_led", "💨 Cappuccino", "steam_led"),
            ("---", "---", None),
            ("bean_led", "🫘 Bohnen (Klick = +)", "bean_led"), ("cup_size_led", "🥤 Tasse (Klick = +)", "cup_size_led"),
            ("---", "---", None),
            ("water_empty", "⚠️ Wasser Leer", "water_empty"), ("waste_full", "⚠️ Müll Voll", "waste_full"),
            ("error_active", "❌ Allgemeiner Fehler", "error_active"),
            ("---", "---", None),
            ("calc_clean_led", "✨ Entkalken (Klick = Toggle)", "calc_clean_led")
        ]

        row_idx = 1
        for gui_key, name, cmd_key in layout:
            if gui_key == "---":
                tk.Frame(panel_frame, height=2, bg="#555").grid(row=row_idx, column=0, columnspan=3, sticky="ew",
                                                                pady=2)
                row_idx += 1
                continue

            action_key = cmd_key if cmd_key else gui_key
            btn = tk.Button(panel_frame, text=name, width=32, bg="#444", fg="white", font=("Arial", 11),
                            cursor="hand2", command=lambda k=action_key: self._manual_press(k))
            btn.grid(row=row_idx, column=0, sticky="w", padx=10, pady=1)

            lbl_dt = tk.Label(panel_frame, text="-", fg="grey", bg="#2d2d2d", font=("Courier", 12, "bold"), width=15)
            lbl_dt.grid(row=row_idx, column=1)
            lbl_ha = tk.Label(panel_frame, text="-", fg="grey", bg="#2d2d2d", font=("Courier", 12, "bold"), width=15)
            lbl_ha.grid(row=row_idx, column=2)

            self.gui_elements[gui_key] = {"dt": lbl_dt, "ha": lbl_ha}
            row_idx += 1

        test_frame = tk.Frame(self.root, bg="#1e1e1e")
        test_frame.pack(side=tk.BOTTOM, fill="x", padx=20, pady=5)

        btn_container = tk.Frame(test_frame, bg="#1e1e1e")
        btn_container.pack(fill="x")

        tk.Button(btn_container, text="▶ Starte automatisierte Test-Suite", bg="#006600", fg="white",
                  font=("Arial", 12, "bold"), command=self._run_tests).pack(side=tk.LEFT, pady=5)
        tk.Button(btn_container, text="🗑 Consolen leeren", bg="#880000", fg="white", font=("Arial", 10, "bold"),
                  command=self._clear_consoles).pack(side=tk.RIGHT, pady=5)

        self.test_console = scrolledtext.ScrolledText(test_frame, height=8, bg="black", fg="#00ff00",
                                                      font=("Consolas", 9))
        self.test_console.pack(fill="x", pady=5)
        self.test_console.insert(tk.END, "Warte auf manuellen Test-Start...\n")

        log_frame = tk.Frame(self.root, bg="#1e1e1e")
        log_frame.pack(fill="both", expand=True, padx=20, pady=10)

        v_scrollbar = tk.Scrollbar(log_frame, orient="vertical")
        v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        pane = tk.PanedWindow(log_frame, orient=tk.HORIZONTAL, sashwidth=6, bg="#555")
        pane.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        titles = ["1. MB -> DISP (State/Status)", "2. DISP -> MB (Cmd/Buttons)", "3. PI -> HA (MQTT Out)",
                  "4. HA -> PI (MQTT In)"]
        colors = ["#00aaff", "#00cc44", "#ffaa00", "#ff3333"]
        self.consoles = []

        for i in range(4):
            col_frame = tk.Frame(pane, bg="#1e1e1e")
            pane.add(col_frame, minsize=200)

            tk.Label(col_frame, text=titles[i], bg="#1e1e1e", fg=colors[i], font=("Arial", 10, "bold")).pack(anchor="w")

            h_scroll = tk.Scrollbar(col_frame, orient="horizontal")
            h_scroll.pack(side=tk.BOTTOM, fill=tk.X)

            txt = tk.Text(col_frame, bg="#0d0d0d", fg=colors[i], font=("Consolas", 8), wrap="none")
            txt.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=2, pady=2)

            txt.config(yscrollcommand=v_scrollbar.set, xscrollcommand=h_scroll.set)
            h_scroll.config(command=txt.xview)

            self.consoles.append(txt)

        def sync_v_scroll(*args):
            for t in self.consoles: t.yview(*args)

        v_scrollbar.config(command=sync_v_scroll)

        def _on_mousewheel(event):
            for t in self.consoles: t.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        for txt in self.consoles: txt.bind("<MouseWheel>", _on_mousewheel)

        self._gui_update_loop()
        self.root.mainloop()

    def _gui_update_loop(self):
        while not self.log_queue.empty():
            try:
                col, msg = self.log_queue.get_nowait()

                if col == -1 and msg == "CLEAR":
                    if hasattr(self, 'consoles'):
                        for txt in self.consoles:
                            txt.delete(1.0, tk.END)
                    continue

                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

                if hasattr(self, 'consoles') and self.consoles:
                    for i, txt in enumerate(self.consoles):
                        if i == col:
                            txt.insert(tk.END, f"[{ts}] {msg}\n")
                        else:
                            txt.insert(tk.END, f"[{ts}] {'-' * 15}\n")
                        txt.see(tk.END)
            except queue.Empty:
                break

        for key, labels in self.gui_elements.items():
            if key in ["power"]: continue

            dt_val = self.dt_states.get(key, "OFF")
            ha_val = self.ha_states.get(key, "WAIT...")

            def _get_color(val, is_warning):
                if val in ["OFF", "Off", "WAIT..."]: return "grey"
                if "ERR" in val or val == "OFFLINE": return "red"
                if val == "ON" and is_warning: return "red"
                if val == "ON" or val in ["1", "2", "3", "Powder", "TopLED"]: return "#00ff00"
                return "grey"

            is_warn = key in ["water_empty", "waste_full", "error_active", "calc_clean_led"]
            labels["dt"].config(text=dt_val, fg=_get_color(dt_val, is_warn))
            labels["ha"].config(text=ha_val, fg=_get_color(ha_val, is_warn))

        self.root.after(200, self._gui_update_loop)

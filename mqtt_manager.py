import paho.mqtt.client as mqtt
import json
import queue
import threading


class MqttManager:
    def __init__(self, broker_ip="192.168.2.44", port=1883, username=None, password=None, client_id="Philips2200_Pi"):
        print(f"[MQTT] Initialisiere Verbindung zu {broker_ip}:{port} als '{client_id}'...")

        self.broker_ip = broker_ip
        self.port = port
        # Thread-sichere Befehlswarteschlange (vorher: ein einzelner Slot, der
        # schnell hintereinander gedrueckte Befehle ueberschrieben hat)
        self.commands = queue.Queue()
        # Wird sofort gesetzt, wenn HA den Handbetrieb anfordert, damit ein
        # laufender Power-On-Versuch abbricht und nicht erst zu Ende laeuft.
        self.bypass_requested = threading.Event()
        self.last_states = {}

        self.client = mqtt.Client(client_id=client_id, clean_session=False)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        if username and password:
            self.client.username_pw_set(username, password)

        self.client.will_set("philips2200/stat/availability", payload="offline", retain=True)

    def start(self):
        try:
            self.client.connect(self.broker_ip, self.port, 60)
            self.client.loop_start()
        except Exception as e:
            print(f"[MQTT] Fehler bei der Verbindung: {e}")

    def stop(self):
        self.client.publish("philips2200/stat/availability", payload="offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()

    def get_pending_command(self):
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("[MQTT] Erfolgreich mit dem Broker verbunden!")
            self.client.publish("philips2200/stat/availability", payload="online", retain=True)
            self.client.subscribe("philips2200/cmnd/#", qos=1)
            self.publish_discovery()
            # Nach (Re-)Connect alle bekannten Zustaende erneut senden, damit
            # HA nach einem Broker-/WLAN-Ausfall nicht auf alten Werten sitzt.
            for key, value in self.last_states.items():
                self.client.publish(f"philips2200/stat/{key}", payload=value, retain=True)
        else:
            print(f"[MQTT] Verbindung fehlgeschlagen mit Code {rc}")

    def on_disconnect(self, client, userdata, rc):
        print("[MQTT] Verbindung getrennt. Versuche automatischen Reconnect...")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        command_str = topic.split("/")[-1]
        payload = msg.payload.decode("utf-8", errors="ignore").strip().upper()
        print(f"[MQTT] Befehl empfangen: {topic} {payload}")

        if command_str == "mitm":
            # Schalter "Pi-Steuerung": ON = Pi steuert (MITM), OFF = Handbetrieb (Bypass)
            if payload == "OFF":
                self.bypass_requested.set()
                self.commands.put("mitm_off")
            elif payload == "ON":
                self.commands.put("mitm_on")
            else:
                print(f"[MQTT] Unbekannter Payload fuer mitm: {payload!r} (erwartet ON/OFF)")
            return

        self.commands.put(command_str)

    def update_states(self, status_dict):
        for key, value in status_dict.items():
            if self.last_states.get(key) != value:
                topic = f"philips2200/stat/{key}"
                self.client.publish(topic, payload=value, retain=True)
                self.last_states[key] = value

    def publish_discovery(self):
        print("[MQTT] Sende Auto-Discovery Konfigurationen an Home Assistant...")

        device_info = {
            "identifiers": ["philips_2200_pi"],
            "name": "Philips 2200 Series",
            "model": "2200 Series",
            "manufacturer": "Philips"
        }

        # --- 1. KOMMANDOS (BUTTONS) ---
        buttons = [
            ("power_on", "Power On", "mdi:power", None),
            ("power_off", "Power Off", "mdi:power-off", None),
            ("espresso", "Espresso", "mdi:cup", None),
            ("coffee", "Coffee", "mdi:coffee", None),
            ("hot_water", "Hot Water", "mdi:tea-outline", None),
            ("steam", "Cappuccino / Steam", "mdi:weather-windy", None),
            # --- DIE BEIDEN FEHLENDEN TASTEN SIND JETZT HIER ---
            ("bean", "Bohnen (Taste)", "mdi:coffee-outline", None),
            ("cup", "Tasse (Taste)", "mdi:cup", None),
            # ---------------------------------------------------
            ("play", "Play / Pause", "mdi:play-pause", None),
            ("power_nc", "Power On (No Clean)", "mdi:liquid-spot", "config")
        ]

        for cmd_id, name, icon, category in buttons:
            topic = f"homeassistant/button/philips2200/{cmd_id}/config"
            payload = {
                "name": name,
                "unique_id": f"p2200_cmd_{cmd_id}",
                "command_topic": f"philips2200/cmnd/{cmd_id}",
                "icon": icon,
                "availability_topic": "philips2200/stat/availability",
                "device": device_info
            }
            if category:
                payload["entity_category"] = category
            self.client.publish(topic, json.dumps(payload), retain=True)

        # --- 2. STATUS (BINARY SENSORS) ---
        binary_sensors = [
            ("espresso_led", "Espresso LED", None, "mdi:led-on", "diagnostic"),
            ("double_espresso", "Double Espresso LED", None, "mdi:dice-2", "diagnostic"),
            ("coffee_led", "Coffee LED", None, "mdi:led-on", "diagnostic"),
            ("double_coffee", "Double Coffee LED", None, "mdi:dice-2", "diagnostic"),
            ("hot_water_led", "Hot Water LED", None, "mdi:led-on", "diagnostic"),
            ("steam_led", "Cappuccino LED", None, "mdi:led-on", "diagnostic"),
            ("water_empty", "Wassertank leer", "problem", "mdi:water-off", "diagnostic"),
            ("waste_full", "Müllbehälter voll", "problem", "mdi:delete-variant", "diagnostic"),
            ("error_active", "Allgemeiner Fehler", "problem", "mdi:alert-circle", "diagnostic"),
            ("calc_clean_led", "Entkalken (Calc/Clean)", None, "mdi:shimmer", "diagnostic"),
            ("play_led", "Play / Pause LED", None, "mdi:play-circle-outline", "diagnostic")
        ]

        for sens_id, name, dev_class, icon, category in binary_sensors:
            topic = f"homeassistant/binary_sensor/philips2200/{sens_id}/config"
            payload = {
                "name": name,
                "unique_id": f"p2200_stat_{sens_id}",
                "state_topic": f"philips2200/stat/{sens_id}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": "philips2200/stat/availability",
                "icon": icon,
                "entity_category": category,
                "device": device_info
            }
            if dev_class:
                payload["device_class"] = dev_class
            self.client.publish(topic, json.dumps(payload), retain=True)

        # --- 3. STATUS (TEXT SENSORS) ---
        sensors = [
            ("bean_led", "Bohnen", "mdi:coffee-outline", "diagnostic"),
            ("cup_size_led", "Tasse", "mdi:cup", "diagnostic")
        ]

        for sens_id, name, icon, category in sensors:
            old_topic = f"homeassistant/sensor/philips2200/{sens_id}/config"
            self.client.publish(old_topic, "", retain=True)

            topic = f"homeassistant/sensor/philips2200/{sens_id}_clean/config"
            payload = {
                "name": name,
                "unique_id": f"p2200_stat_{sens_id}_clean",
                "state_topic": f"philips2200/stat/{sens_id}",
                "availability_topic": "philips2200/stat/availability",
                "icon": icon,
                "entity_category": category,
                "device": device_info
            }
            self.client.publish(topic, json.dumps(payload), retain=True)

        # --- 4. SCHALTER: PI-STEUERUNG / HANDBETRIEB ---
        # ON  = Pi steuert die Maschine (MITM)
        # OFF = Handbetrieb: Relais fallen ab, Display und Mainboard direkt verbunden
        topic = "homeassistant/switch/philips2200/mitm/config"
        payload = {
            "name": "Pi-Steuerung",
            "unique_id": "p2200_sw_mitm",
            "command_topic": "philips2200/cmnd/mitm",
            "state_topic": "philips2200/stat/mitm",
            "payload_on": "ON",
            "payload_off": "OFF",
            "json_attributes_topic": "philips2200/stat/mitm_attr",
            "icon": "mdi:swap-horizontal",
            "availability_topic": "philips2200/stat/availability",
            "device": device_info
        }
        self.client.publish(topic, json.dumps(payload), retain=True)

        # --- 5. BUS-DIAGNOSE (Frames pro Minute je Richtung) ---
        bus_sensors = [
            ("bus_main_fpm", "Bus Mainboard Frames/min"),
            ("bus_disp_fpm", "Bus Display Frames/min"),
        ]
        for sens_id, name in bus_sensors:
            topic = f"homeassistant/sensor/philips2200/{sens_id}/config"
            payload = {
                "name": name,
                "unique_id": f"p2200_diag_{sens_id}",
                "state_topic": f"philips2200/stat/{sens_id}",
                "availability_topic": "philips2200/stat/availability",
                "unit_of_measurement": "Frames/min",
                "state_class": "measurement",
                "icon": "mdi:swap-horizontal",
                "entity_category": "diagnostic",
                "device": device_info
            }
            self.client.publish(topic, json.dumps(payload), retain=True)

        print("[MQTT] Auto-Discovery abgeschlossen. Alte Geister-Entitäten wurden bereinigt.")
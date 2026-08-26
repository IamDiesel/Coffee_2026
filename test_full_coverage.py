import unittest
from unittest.mock import MagicMock, patch
import time
import requests

from my_config import MQTT_BROKER_IP, MQTT_USER, MQTT_PASSWORD, HA_URL, HA_TOKEN

from protocol import PhilipsProtocol
from controller import CoffeeController
from mqtt_manager import MqttManager


class PhilipsFullCoverageTest(unittest.TestCase):
    def setUp(self):
        # ==========================================
        # 1. MOCKING DER HARDWARE (UART)
        # ==========================================
        self.hw = MagicMock()
        self.mock_main = MagicMock()
        self.mock_disp = MagicMock()
        self.hw.get_mainboard_stream.return_value = self.mock_main
        self.hw.get_display_stream.return_value = self.mock_disp
        self.mock_main.inWaiting.return_value = 0
        self.mock_disp.inWaiting.return_value = 0

        # ==========================================
        # 2. ECHTE VERBINDUNG ZU HOME ASSISTANT (MQTT)
        # ==========================================
        self.proto = PhilipsProtocol()
        # 2. ECHTE VERBINDUNG ZU HOME ASSISTANT (MQTT)
        self.proto = PhilipsProtocol()
        # HIER ÄNDERN: client_id hinzugefügt!
        self.mqtt = MqttManager(broker_ip=MQTT_BROKER_IP, username=MQTT_USER, password=MQTT_PASSWORD,
                                client_id="Philips2200_Test_Runner")
        self.mqtt.start()

        connected = False
        for _ in range(20):
            if self.mqtt.client.is_connected():
                connected = True
                break
            time.sleep(0.1)
        if not connected:
            self.mqtt.stop()
            self.fail(f"KRITISCHER FEHLER: Keine WLAN-Verbindung zum MQTT Broker ({MQTT_BROKER_IP}).")

        try:
            headers = {"Authorization": f"Bearer {HA_TOKEN}", "content-type": "application/json"}
            res = requests.get(f"{HA_URL}/config", headers=headers, timeout=2)
            res.raise_for_status()
        except Exception as e:
            self.mqtt.stop()
            self.fail(f"KRITISCHER FEHLER: Konnte Home Assistant API nicht erreichen. Token/URL prüfen! ({e})")

        self.ctrl = CoffeeController(self.hw, self.proto, self.mqtt)

        # ==========================================
        # 3. ENTITY MAPPING (Angepasst an die _clean Versionen)
        # ==========================================
        self.HA_ENTITY_MAP = {
            'espresso_led': 'binary_sensor.philips_2200_series_espresso_led',
            'double_espresso': 'binary_sensor.philips_2200_series_double_espresso_led',
            'coffee_led': 'binary_sensor.philips_2200_series_coffee_led',
            'double_coffee': 'binary_sensor.philips_2200_series_double_coffee_led',
            'hot_water_led': 'binary_sensor.philips_2200_series_hot_water_led',
            'steam_led': 'binary_sensor.philips_2200_series_steam_led',
            'water_empty': 'binary_sensor.philips_2200_series_wassertank_leer',
            'waste_full': 'binary_sensor.philips_2200_series_mullbehalter_voll',
            'error_active': 'binary_sensor.philips_2200_series_allgemeiner_fehler',
            # Die neuen, sauberen Sensoren
            'bean_led': 'sensor.philips_2200_series_bohnen',
            'cup_size_led': 'sensor.philips_2200_series_tasse'
        }

    def tearDown(self):
        self.mqtt.stop()

    # ==========================================
    # HELPER: HOME ASSISTANT AUSLESEN
    # ==========================================
    def _read_from_ha(self, entity_id):
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "content-type": "application/json"}
        try:
            response = requests.get(f"{HA_URL}/states/{entity_id}", headers=headers, timeout=2)
            response.raise_for_status()
            state = response.json().get('state')
            return state.upper() if isinstance(state, str) else state
        except Exception as e:
            return f"API_ERROR: {e}"

    def print_header(self, title, precondition, action, expected):
        print(f"\n{'=' * 85}")
        print(f"TESTCASE:        {title}")
        print(f"PRECONDITION:    {precondition}")
        print(f"ACTION:          {action}")
        print(f"EXPECTED RESULT: {expected}")
        print(f"{'-' * 85}")

    def _format_val(self, val):
        if isinstance(val, (bytes, bytearray)):
            return " ".join(format(x, "02x") for x in val)
        return str(val)

    def _check_val(self, name, expected, actual, measure_point):
        exp_str = self._format_val(expected)
        act_str = self._format_val(actual)
        match = (str(expected).upper() == str(actual).upper())

        print(f"  [{'OK' if match else 'FAIL'}] {name}")
        print(f"       SOLL:      {exp_str}")
        print(f"       IST:       {act_str}")
        self.assertTrue(match, f"Abweichung in {name}! SOLL: {exp_str}, IST: {act_str}")

    def _check_all_ha_entities(self, expected_overrides):
        baseline = {
            'espresso_led': 'OFF', 'double_espresso': 'OFF',
            'coffee_led': 'OFF', 'double_coffee': 'OFF',
            'hot_water_led': 'OFF', 'steam_led': 'OFF',
            'water_empty': 'OFF', 'waste_full': 'OFF',
            'error_active': 'OFF', 'bean_led': 'Off',
            'cup_size_led': 'Off'
        }
        baseline.update(expected_overrides)

        print("\n  [Synchronisiere mit Home Assistant (Warte max 5 Sekunden)...]")

        final_states = {}
        # SMART POLLING: Wir geben HA bis zu 5 Sekunden Zeit
        for attempt in range(10):
            all_match = True
            for internal_key, expected_state in baseline.items():
                ha_entity_id = self.HA_ENTITY_MAP[internal_key]
                state = self._read_from_ha(ha_entity_id)
                final_states[internal_key] = state

                if str(state).upper() != str(expected_state).upper():
                    all_match = False

            if all_match:
                break
            time.sleep(0.5)

        print("  --- Auswertung der Home Assistant API ---")
        for internal_key, expected_state in baseline.items():
            ha_entity_id = self.HA_ENTITY_MAP[internal_key]
            self._check_val(
                f"Sensor: {ha_entity_id}",
                expected_state,
                final_states[internal_key],
                f"HA REST API ({HA_URL})"
            )

    # ==========================================
    # TEST: UART PASSTHROUGH & E2E HA UPDATE
    # ==========================================
    def test_e2e_mainboard_to_ha(self):
        self.print_header(
            "End-to-End: Mainboard zu Home Assistant",
            "19-Byte Status-Paket vom Mainboard liegt an (Espresso aktiv).",
            "Controller liest Bus aus -> sendet via MQTT -> HA.",
            "Die HA REST API muss den aktualisierten Wert 'ON' für Espresso zurückgeben."
        )
        self.mock_main.inWaiting.side_effect = [19, 0]
        valid_packet = bytearray(b'\xd5\x55' + b'\x00' * 17)
        valid_packet[3] = 0x07
        self.mock_main.read.side_effect = [b'\xd5', bytes(valid_packet[1:])]

        self.ctrl._process_mainboard_stream()

        expected_ha_states = {'espresso_led': 'ON'}
        self._check_all_ha_entities(expected_ha_states)

    # ==========================================
    # TEST: COMPLEX STATES (Double + Sensoren)
    # ==========================================
    def test_e2e_complex_states(self):
        self.print_header(
            "End-to-End: Komplexe Zustandsmatrix",
            "Mainboard sendet 'Double Coffee', Stufe 3 Bohnen, Stufe 3 Tasse.",
            "Controller decodiert und schickt Batch-Update per MQTT an HA.",
            "HA REST API muss alle Modifier sauber getrennt anzeigen."
        )
        self.mock_main.inWaiting.side_effect = [19, 0]
        packet = bytearray(b'\xd5\x55' + b'\x00' * 17)
        packet[5] = 0x38  # Double Coffee
        packet[11] = 0x07  # Tassengröße UI aktiv
        packet[10] = 0x3F  # Tassengröße Stufe 3
        packet[9] = 0x07  # Bohnen UI aktiv
        packet[8] = 0x3F  # Bohnen Stufe 3

        self.mock_main.read.side_effect = [b'\xd5', bytes(packet[1:])]
        self.ctrl._process_mainboard_stream()

        expected_ha_states = {
            'coffee_led': 'ON',
            'double_coffee': 'ON',
            'cup_size_led': '3',
            'bean_led': '3'
        }
        self._check_all_ha_entities(expected_ha_states)

    # ==========================================
    # TEST: ERROR LEDS (ISOLIERT)
    # ==========================================
    def test_e2e_error_leds(self):
        self.print_header(
            "End-to-End: Fehler-Sensoren (Wasser, Müll, Allgemein)",
            "Mainboard sendet Wasser Leer und Müll Voll.",
            "MQTT übermittelt Fehlerzustände.",
            "HA REST API muss die Problem-Sensoren auf 'ON' schalten."
        )
        self.mock_main.inWaiting.side_effect = [19, 0]
        packet = bytearray(b'\xd5\x55' + b'\x00' * 17)
        packet[14] = 0x38  # Wasser Leer
        packet[15] = 0x07  # Müll Voll

        self.mock_main.read.side_effect = [b'\xd5', bytes(packet[1:])]
        self.ctrl._process_mainboard_stream()

        self._check_all_ha_entities({'water_empty': 'ON', 'waste_full': 'ON'})


if __name__ == '__main__':
    unittest.main(verbosity=0)

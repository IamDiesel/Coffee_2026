import unittest
from protocol import PhilipsProtocol


class TestPhilipsCommands(unittest.TestCase):
    def setUp(self):
        self.protocol = PhilipsProtocol()

    def helper_hex_to_bytes(self, hex_string):
        """Entfernt Leerzeichen aus dem String und macht Bytes daraus"""
        clean_hex = hex_string.replace(" ", "").strip()
        return bytes.fromhex(clean_hex)

    def test_01_commands_match_recorded_hex(self):
        """Prüft, ob die Befehle exakt den aufgezeichneten Hex-Werten entsprechen"""
        print("\n--- Starte Hex-Byte Verifizierung ---")

        testcases = {
            "Power Off No Clean": ("d5550000000302010000210c", self.protocol.CMD_POWER_OFF),
            "Espresso": ("d5 55 00 00 00 03 02 02 00 00 35 1a", self.protocol.CMD_ESPRESSO),
            "Hot Water": ("d5 55 00 00 00 03 02 04 00 00 1d 36", self.protocol.CMD_HOT_WATER),
            "Coffee": ("d5 55 00 00 00 03 02 08 00 00 05 2b", self.protocol.CMD_COFFEE),
            "Steam": ("d5 55 00 00 00 03 02 10 00 00 35 11", self.protocol.CMD_STEAM),
            "Bean Size": ("d5 55 00 00 00 03 02 00 02 00 35 18", self.protocol.CMD_BEAN),
            "Cup Size": ("d5 55 00 00 00 03 02 00 04 00 1c 32", self.protocol.CMD_CUP),
            "Play": ("d5 55 00 00 00 03 02 00 00 01 25 05", self.protocol.CMD_PLAY),
            "Beep": ("d5 55 0a 00 00 03 02 00 00 00 32 25", self.protocol.CMD_BEEP),
            "Power On (No Clean)": ("d5 55 01 00 00 03 02 00 00 00 19 10", self.protocol.CMD_POWER_ON_NO_CLEAN)
        }

        for name, (expected_hex, actual_bytes) in testcases.items():
            expected_bytes = self.helper_hex_to_bytes(expected_hex)
            self.assertEqual(
                expected_bytes,
                actual_bytes,
                f"FEHLER bei '{name}': {actual_bytes.hex()} stimmt nicht mit Aufzeichnung überein!"
            )
            print(f"OK: {name.ljust(20)} -> {actual_bytes.hex()}")

    def test_02_mainboard_status_decoder(self):
        """Simuliert eine Antwort vom Mainboard und prüft die MQTT Payloads"""
        print("\n--- Starte Mainboard Decoder Test ---")

        # Simuliere ein leeres 18-Byte Paket (Alles auf 0x00)
        dummy_packet = bytearray(b'\xd5\x55' + b'\x00' * 16)

        # Setze Byte 14 auf 0x38 ("Water empty" laut Wörterbuch)
        dummy_packet[14] = 0x38
        # Setze Byte 15 auf 0x07 ("Waste full")
        dummy_packet[15] = 0x07

        # Lass die Protocol-Klasse das Byte-Array auswerten
        status = self.protocol.decode_mainboard_message(dummy_packet)

        # Prüfe, ob die Warnungen korrekt in MQTT ON/OFF übersetzt werden
        self.assertEqual(status['water_empty'], 'ON')
        self.assertEqual(status['waste_full'], 'ON')
        self.assertEqual(status['error_active'], 'OFF')  # Sollte aus sein
        print("OK: Fehler-Zustände wurden korrekt übersetzt.")


if __name__ == '__main__':
    unittest.main()
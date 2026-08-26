class PhilipsProtocol:
    def __init__(self):
        # Dictionaries aus der originalen Firmware (Status-Dekodierung)
        self.statesDrinkSelection = {
            0x00: 'Off', 0x03: 'Half Brightness', 0x07: 'Full Brightness',
            0x38: 'Double', 0x3F: 'Full Brightness'
        }
        self.beanLEDStates = {0x00: '1LED/Off', 0x38: '2LED', 0x3F: '3LED'}
        self.beanLEDStatesCtrl = {0x07: 'Show LED Group', 0x38: 'Powder Selected', 0x00: 'Off'}
        self.sizeLEDStates = {0x00: '1LED/Off', 0x38: '2LED', 0x3F: '3LED', 0x07: 'TopLED'}
        self.sizeAquaLEDStates = {0x00: 'Off', 0x07: 'Show LED Group', 0x38: 'Aqua Clean Orange'}
        self.emptyWaterLEDStates = {0x00: 'No error', 0x38: 'Water empty'}
        self.wasteWarningLEDStates = {0x00: 'No error', 0x07: 'Waste full', 0x38: 'Error active'}
        self.calcCleanLEDStates = {0x00: 'Both Off', 0x38: 'Calc / Clean orange', 0x07: 'unknown'}
        self.playPauseLEDStates = {0x00: 'Off', 0x07: 'On'}

        # --- DAS NEUE XOR-CHECKSUMMEN SYSTEM ---
        # Der Header (immer gleich)
        self.HEADER = b'\xd5\x55'

        # Das Basis-Payload (Standby/Idle Zustand) - 8 Bytes
        self.BASE_PAYLOAD = bytearray([0x00, 0x00, 0x00, 0x03, 0x02, 0x00, 0x00, 0x00])
        # Die zugehörige Basis-Checksumme
        self.BASE_CRC = 0x2D01

        # Lookup Table: Mapping (Byte-Index, Neuer Wert) -> XOR-Delta für die Checksumme
        # Diese Deltas wurden direkt aus deinen funktionierenden Hex-Strings extrahiert!
        self.XOR_LOOKUP = {
            # Byte 0: System-Befehle
            (0, 0x0A): 0x1F24,  # Beep
            (0, 0x01): 0x3411,  # Power On (No Clean)

            # Byte 5: Getränke & Power Off
            (5, 0x01): 0x0C0D,  # Power Off
            (5, 0x02): 0x181B,  # Espresso
            (5, 0x04): 0x3037,  # Hot Water
            (5, 0x08): 0x282A,  # Coffee
            (5, 0x10): 0x1810,  # Steam

            # Byte 6: Einstellungen (Bohnen / Tassen)
            (6, 0x02): 0x1819,  # Bean
            (6, 0x04): 0x3133,  # Cup

            # Byte 7: Action
            (7, 0x01): 0x0804,  # Play/Pause
        }

        # Behalte die komplexe Boot-Sequenz als Konstante (da sie viele Bytes gleichzeitig ändert)
        self.CMD_POWER_ON_CLEAN = b'\xd5\x55\x02\x01\x02\x00\x02\x00\x00\x00\x38\x15'

    def calculate_checksum(self, payload):
        """Berechnet die Philips XOR-Checksumme für ein 8-Byte Payload-Array"""
        crc = self.BASE_CRC

        for i in range(8):
            target_val = payload[i]
            base_val = self.BASE_PAYLOAD[i]

            if target_val != base_val:
                # Hole das Delta aus der Lookup Table
                delta = self.XOR_LOOKUP.get((i, target_val))
                if delta is not None:
                    crc ^= delta
                else:
                    print(f"[Protokoll-Warnung] Unbekanntes Checksummen-Delta für Byte {i} = {hex(target_val)}")

        return crc.to_bytes(2, byteorder='big')

    def build_command(self, byte_updates):
        """
        Baut ein fertiges 12-Byte Kommando auf Basis von gewünschten Änderungen.
        :param byte_updates: Ein Dictionary mit {Index: Neuer_Wert}
        """
        # 1. Starte mit dem Basis-Array
        payload = bytearray(self.BASE_PAYLOAD)

        # 2. Überschreibe die angeforderten Bytes
        for idx, val in byte_updates.items():
            payload[idx] = val

        # 3. Berechne die Checksumme über die XOR-Tabelle
        crc_bytes = self.calculate_checksum(payload)

        # 4. Setze Header, Payload und Checksumme zusammen
        return self.HEADER + payload + crc_bytes

    def get_cmd(self, command_name):
        """Hilfsfunktion: Ersetzt die alten statischen Konstanten durch dynamische Generierung"""
        commands = {
            'espresso': {5: 0x02},
            'coffee': {5: 0x08},
            'hot_water': {5: 0x04},
            'steam': {5: 0x10},
            'play': {7: 0x01},
            'power_off': {5: 0x01},
            'bean': {6: 0x02},
            'cup': {6: 0x04},
            'power_on_no_clean': {0: 0x01},
            'beep': {0: 0x0A}
        }

        if command_name == 'power_on_clean':
            return self.CMD_POWER_ON_CLEAN

        if command_name in commands:
            return self.build_command(commands[command_name])
        return None

    def decode_mainboard_message(self, input_bytes):
        """Übersetzt das rohe Hex-Array vom Mainboard in saubere MQTT-Payloads"""
        status = {}
        if len(input_bytes) < 16:
            return status  # Zu kurz, ignorieren

        try:
            # Espresso
            val3 = self.statesDrinkSelection.get(input_bytes[3], 'Off')
            status['espresso_led'] = 'OFF' if val3 == 'Off' else 'ON'
            status['double_espresso'] = 'ON' if val3 == 'Double' else 'OFF'

            # Coffee
            val5 = self.statesDrinkSelection.get(input_bytes[5], 'Off')
            status['coffee_led'] = 'OFF' if val5 == 'Off' else 'ON'
            status['double_coffee'] = 'ON' if val5 == 'Double' else 'OFF'

            # Hot Water & Steam
            status['hot_water_led'] = 'OFF' if self.statesDrinkSelection.get(input_bytes[4], 'Off') == 'Off' else 'ON'
            status['steam_led'] = 'OFF' if self.statesDrinkSelection.get(input_bytes[6], 'Off') == 'Off' else 'ON'

            # Alarm-Sensoren
            status['water_empty'] = 'ON' if self.emptyWaterLEDStates.get(input_bytes[14],
                                                                         'No error') == 'Water empty' else 'OFF'

            val15 = self.wasteWarningLEDStates.get(input_bytes[15], 'No error')
            status['waste_full'] = 'ON' if val15 == 'Waste full' else 'OFF'
            status['error_active'] = 'ON' if val15 == 'Error active' else 'OFF'

            # Cup Size
            val10 = self.sizeLEDStates.get(input_bytes[10], '1LED/Off')
            val11 = self.sizeAquaLEDStates.get(input_bytes[11], 'Off')
            status['cup_size_led'] = 'Off'
            if val11 == 'Show LED Group':
                if val10 == '1LED/Off':
                    status['cup_size_led'] = '1'
                elif val10 == '2LED':
                    status['cup_size_led'] = '2'
                elif val10 == '3LED':
                    status['cup_size_led'] = '3'
                elif val10 == 'TopLED':
                    status['cup_size_led'] = 'TopLED'

            # Bean Size
            val8 = self.beanLEDStates.get(input_bytes[8], '1LED/Off')
            val9 = self.beanLEDStatesCtrl.get(input_bytes[9], 'Off')
            status['bean_led'] = 'Off'
            if val9 == 'Show LED Group':
                if val8 == '1LED/Off':
                    status['bean_led'] = '1'
                elif val8 == '2LED':
                    status['bean_led'] = '2'
                elif val8 == '3LED':
                    status['bean_led'] = '3'
            elif val9 == 'Powder Selected':
                status['bean_led'] = 'Powder'

            # Play / Pause LED
            val16 = self.playPauseLEDStates.get(input_bytes[16], 'Off')
            status['play_led'] = 'ON' if val16 == 'On' else 'OFF'

            # Calc / Clean LED
            val12 = self.calcCleanLEDStates.get(input_bytes[12], 'Both Off')
            status['calc_clean_led'] = 'ON' if val12 == 'Calc / Clean orange' else 'OFF'

        except Exception as e:
            print(f"[Protocol] Error decoding message: {e}")

        return status
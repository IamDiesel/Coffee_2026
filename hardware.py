import time
import subprocess

# Versuche RPi/Serial zu laden. Schlägt fehl auf Windows/Mac (Test-Modus)
try:
    import RPi.GPIO as GPIO
    import serial

    HARDWARE_AVAILABLE = True
except ImportError:
    HARDWARE_AVAILABLE = False


class RealHardware:
    """Steuert die echte Kaffeemaschine über UART und Relais"""

    def __init__(self, ser_mainboard="/dev/ttyAMA3", ser_display="/dev/ttyAMA2"):
        self.RELAIS_PWR_DISP_GPIO = 17
        self.RELAIS_TX_RX_GPIO = 27

        # GPIO Init
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.RELAIS_PWR_DISP_GPIO, GPIO.OUT)
        GPIO.setup(self.RELAIS_TX_RX_GPIO, GPIO.OUT)
        self.relais_on()

        # Serial Init
        self.dev_display = serial.Serial(ser_display, write_timeout=1, baudrate=115200, bytesize=8, parity='N',
                                         stopbits=1)
        self.dev_mainboard = serial.Serial(ser_mainboard, write_timeout=1, baudrate=115200, bytesize=8, parity='N',
                                           stopbits=1)

    def relais_on(self):
        """Aktiviert den MITM Modus"""
        GPIO.output(self.RELAIS_PWR_DISP_GPIO, GPIO.HIGH)
        GPIO.output(self.RELAIS_TX_RX_GPIO, GPIO.LOW)

    def relais_off(self):
        """Deaktiviert MITM, Maschine läuft normal"""
        GPIO.output(self.RELAIS_PWR_DISP_GPIO, GPIO.HIGH)
        GPIO.output(self.RELAIS_TX_RX_GPIO, GPIO.HIGH)

    def toggle_display_power(self, t_off=0.4):
        GPIO.output(self.RELAIS_PWR_DISP_GPIO, GPIO.LOW)
        time.sleep(t_off)
        GPIO.output(self.RELAIS_PWR_DISP_GPIO, GPIO.HIGH)

    def get_display_stream(self):
        return self.dev_display

    def get_mainboard_stream(self):
        return self.dev_mainboard

    def shutdown(self):
        self.relais_off()
        self.dev_display.close()
        self.dev_mainboard.close()


class MockHardware:
    """Simuliert die Maschine für die Entwicklung auf dem Laptop"""

    def __init__(self):
        print("[MOCK HW] Initialisiert - Keine echten GPIOs/Seriell")

        # Wir simulieren Dummy-Streams, damit die Hauptschleife nicht abstürzt
        class DummyStream:
            def inWaiting(self): return 0

            def read(self, bytes): return b''

            def write(self, data):
                # Schreibt die gesendeten Befehle formatiert in die Konsole
                hex_str = " ".join(format(x, "02x") for x in data)
                print(f"  --> [MOCK TX]: {hex_str}")

        self.dev_display = DummyStream()
        self.dev_mainboard = DummyStream()

    def relais_on(self):
        print("[MOCK HW] Relais ON (MITM aktiv)")

    def relais_off(self):
        print("[MOCK HW] Relais OFF (Passthrough)")

    def toggle_display_power(self, t_off=0.4):
        print(f"[MOCK HW] Toggle Display Power ({t_off}s)")

    def get_display_stream(self):
        return self.dev_display

    def get_mainboard_stream(self):
        return self.dev_mainboard

    def shutdown(self):
        print("[MOCK HW] Herunterfahren")
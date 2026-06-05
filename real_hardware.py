import serial
import RPi.GPIO as GPIO
import time
from hardware_interface import CoffeeMachineHardware


class RealHardware(CoffeeMachineHardware):
    def __init__(self, port_main="/dev/ttyAMA3", port_disp="/dev/ttyAMA2"):
        print("[HARDWARE] Initialisiere echte Serielle Ports und GPIOs...")

        self.RELAIS_PWR_DISP_GPIO = 17
        self.RELAIS_TX_RX_GPIO = 27

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.RELAIS_PWR_DISP_GPIO, GPIO.OUT)
        GPIO.setup(self.RELAIS_TX_RX_GPIO, GPIO.OUT)

        self._relais_on()

        try:
            self.dev_mainboard = serial.Serial(port_main, baudrate=115200, timeout=1)
            self.dev_display = serial.Serial(port_disp, baudrate=115200, timeout=1)
            print(f"[HARDWARE] Serielle Ports ({port_main} & {port_disp}) erfolgreich geöffnet!")
        except serial.SerialException as e:
            print(f"[HARDWARE ERROR] Serielle Ports konnten nicht geöffnet werden: {e}")
            self.cleanup()
            raise

    def _relais_on(self):
        # KORREKT FÜR ACTIVE-LOW RELAIS:
        GPIO.output(self.RELAIS_PWR_DISP_GPIO, GPIO.HIGH)  # Strom AN
        GPIO.output(self.RELAIS_TX_RX_GPIO, GPIO.LOW)  # Relais zieht an -> MITM Modus
        print("[HARDWARE] Relais: MITM Modus AKTIV. Pi steuert den Bus.")

    def relais_off(self):
        # KORREKT FÜR ACTIVE-LOW RELAIS:
        GPIO.output(self.RELAIS_PWR_DISP_GPIO, GPIO.HIGH)  # Strom BLEIBT AN
        GPIO.output(self.RELAIS_TX_RX_GPIO, GPIO.HIGH)  # Relais fällt ab -> BYPASS Modus
        print("[HARDWARE] Relais: BYPASS Modus AKTIV. Maschine läuft autonom.")

    def toggle_display_power(self, duration: float = 0.4):
        print(f"[HARDWARE] Toggle Display Power für {duration}s...")
        GPIO.output(self.RELAIS_PWR_DISP_GPIO, GPIO.LOW)  # Strom AUS
        time.sleep(duration)
        GPIO.output(self.RELAIS_PWR_DISP_GPIO, GPIO.HIGH)  # Strom AN

    def get_mainboard_stream(self):
        return self.dev_mainboard

    def get_display_stream(self):
        return self.dev_display

    def update_gui(self, status_dict: dict):
        pass

    def attach_mqtt(self, mqtt_manager):
        pass

    def cleanup(self):
        """Zwingt das System mechanisch in den Failsafe-Modus und gibt Ressourcen frei."""
        print("[HARDWARE] Cleanup: Schalte Relais in den sicheren Bypass...")
        try:
            self.relais_off()
            print("[HARDWARE] GPIOs im Failsafe eingerastet (Display ON, TX/RX Bypass).")
        except Exception as e:
            print(f"[HARDWARE] Warnung beim GPIO Cleanup: {e}")

        if hasattr(self, 'dev_mainboard') and self.dev_mainboard.is_open:
            self.dev_mainboard.close()

        if hasattr(self, 'dev_display') and self.dev_display.is_open:
            self.dev_display.close()
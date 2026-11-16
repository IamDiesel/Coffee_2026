import time
import sys
import signal
import builtins
from datetime import datetime

# --- ZEITSTEMPEL FUER ALLE LOGAUSGABEN ---
# coffee26.log hatte bisher keine Uhrzeiten; ohne die laesst sich ein Ausfall
# nicht mit Wecker, Power-On oder Neustart in Verbindung bringen.
_orig_print = builtins.print


def _ts_print(*args, **kwargs):
    _orig_print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *args, **kwargs)


builtins.print = _ts_print

# Eigene Module importieren
import my_config as secrets
from mqtt_manager import MqttManager
from protocol import PhilipsProtocol
from controller import CoffeeController
from wifi_watchdog import Wifi_Deamon

# --- HARDWARE WEICHE ---
try:
    from real_hardware import RealHardware

    HARDWARE_AVAILABLE = True
except ImportError:
    HARDWARE_AVAILABLE = False
    from digital_twin import DigitalTwinHardware

TEST_MODE = not HARDWARE_AVAILABLE

shutdown_requested = False


def trigger_shutdown(signum=None, frame=None):
    """Fängt OS-Signale ab und leitet Failsafe ein."""
    global shutdown_requested
    shutdown_requested = True
    print(f"\n[MAIN] Beendigungs-Signal ({signum}) empfangen. Leite Failsafe-Shutdown ein...")


def main():
    global shutdown_requested

    signal.signal(signal.SIGINT, trigger_shutdown)
    signal.signal(signal.SIGTERM, trigger_shutdown)

    if TEST_MODE:
        print("=" * 50)
        print("STARTE PHILIPS 2200 MQTT (DIGITAL TWIN MODUS)")
        print("=" * 50)
        hardware = DigitalTwinHardware()
    else:
        print("=" * 50)
        print("STARTE PHILIPS 2200 MQTT (LIVE HARDWARE MODUS)")
        print("=" * 50)
        hardware = RealHardware()

    client_name = "Philips2200_Twin" if TEST_MODE else "Philips2200_Pi"

    protocol = PhilipsProtocol()
    mqtt = MqttManager(
        broker_ip=secrets.MQTT_BROKER_IP,
        username=secrets.MQTT_USER,
        password=secrets.MQTT_PASSWORD,
        client_id=client_name
    )
    controller = CoffeeController(
        hardware, protocol, mqtt,
        auto_bypass=getattr(secrets, "AUTO_BYPASS_ON_POWER_FAIL", True)
    )

    wifi_dog = None
    if not TEST_MODE and hasattr(secrets, 'GATEWAY_IP'):
        # Übergebe die MQTT IP an den Watchdog für den dynamischen Check
        ha_ip = secrets.MQTT_BROKER_IP if hasattr(secrets, 'MQTT_BROKER_IP') else "192.168.2.44"
        wifi_dog = Wifi_Deamon(gateway_ip=secrets.GATEWAY_IP, ha_ip=ha_ip)

    if TEST_MODE:
        hardware.attach_mqtt(mqtt)

    # 3. Systeme hochfahren
    try:
        if wifi_dog:
            wifi_dog.start()

            # --- NEU: RACE CONDITION FIX ---
            # Wir warten, bis der Watchdog das Netzwerk als bereit meldet,
            # bevor wir versuchen, MQTT zu verbinden!
            print("[MAIN] Warte auf Netzwerkfreigabe durch Watchdog...")
            wait_count = 0
            while not wifi_dog.network_ready and wait_count < 60:
                if shutdown_requested:
                    sys.exit(0)
                time.sleep(1)
                wait_count += 1

            if not wifi_dog.network_ready:
                print("[MAIN] Warnung: Netzwerk immer noch nicht bereit. Versuche MQTT trotzdem...")

        # MQTT verbinden (wird jetzt erst ausgeführt, wenn das WLAN da ist)
        mqtt.start()
        time.sleep(1)

        controller.start()

        while not shutdown_requested:
            if not controller.is_alive():
                print("[MAIN] KRITISCH: Controller-Thread ist unerwartet beendet worden!")
                break
            time.sleep(1)

    except Exception as e:
        print(f"\n[MAIN] Unerwarteter Fehler: {e}")
    finally:
        print("[MAIN] Führe sicheres Cleanup durch und stelle FAILSAFE her...")

        try:
            if hasattr(controller, 'stop'):
                controller.stop()
        except Exception as e:
            pass

        try:
            mqtt.stop()
        except Exception as e:
            pass

        try:
            if wifi_dog:
                wifi_dog.stop()
        except Exception as e:
            pass

        try:
            if hasattr(hardware, 'cleanup'):
                hardware.cleanup()
            elif not TEST_MODE:
                hardware.relais_off()
            print("[MAIN] System sicher beendet. Maschine ist autonom.")
        except Exception as e:
            print(f"[MAIN] KRITISCHER FEHLER beim Hardware-Cleanup: {e}")

        sys.exit(0)


if __name__ == "__main__":
    main()

import time
import os
from threading import Thread
import secrets


class Wifi_Deamon(Thread):
    def __init__(self, gateway_ip=secrets.GATEWAY_IP, ha_ip=secrets.HA_IP, connection_name=secrets.WIFI_SSID):
        super().__init__()
        self.gateway_ip = gateway_ip
        self.ha_ip = ha_ip
        self.connection_name = connection_name
        self._retries = 0
        self._running = False
        self.network_ready = False

        if os.name == 'posix':
            self.interface = self.get_interface()
            if self.interface:
                self.shell_command(f"sudo iwconfig {self.interface} power off")

    def stop(self):
        self._running = False

    def run(self):
        if os.name != 'posix':
            print("[WIFI] Watchdog deaktiviert (Nicht auf Linux)")
            self.network_ready = True
            return

        self._running = True

        print(f"[WIFI] Watchdog gestartet. Warte auf Netzwerk ({self.gateway_ip} oder {self.ha_ip})...")
        for i in range(60):
            if not self._running:
                return
            if self.is_host_reachable(self.gateway_ip) or self.is_host_reachable(self.ha_ip):
                print(f"[WIFI] Netzwerk ist nach {i} Sekunden erreichbar. Starte Überwachung.")
                self.network_ready = True
                break
            time.sleep(1)

        if not self.network_ready:
            print("[WIFI] Timeout: Netzwerk beim Start nicht gefunden. Gehe in Fehlerbehebung...")

        # --- HAUPTSCHLEIFE ---
        while self._running:
            if not self.is_host_reachable(self.gateway_ip) and not self.is_host_reachable(self.ha_ip):
                self._retries += 1
                self.network_ready = False
                print(f"[WIFI] Netzwerk nicht erreichbar! Versuch {self._retries}/10")
                self.reset_WLAN()
            else:
                if self._retries > 0:
                    print("[WIFI] Netzwerk wieder stabil erreichbar!")
                self._retries = 0
                self.network_ready = True

            if self._retries > 10:
                print("[WIFI] Rebooting System...")
                time.sleep(5)
                self.shell_command("sudo reboot")

            for _ in range(10):
                if not self._running:
                    break
                time.sleep(1)

    def is_host_reachable(self, ip_address):
        try:
            res = self.shell_command(
                f"ping {ip_address} -c 1 -W 2 | grep 'received' | awk -F',' '{{ print $2}}' | awk '{{ print $1}}'")
            return int(res.strip()) > 0
        except:
            return False

    def shell_command(self, input_cmd):
        return os.popen(input_cmd).read()

    def get_interface(self):
        try:
            res = self.shell_command("iwgetid")
            if "ESSID" in res:
                return res.split()[0].strip()

            fallback = self.shell_command("ip -br link show | grep wlan | awk '{print $1}'").strip()
            if fallback:
                interfaces = fallback.split('\n')
                for iface in interfaces:
                    if "wlan0" in iface:
                        return "wlan0"
                return interfaces[0].strip()
        except Exception:
            pass
        return "wlan0"

    def reset_WLAN(self):
        print(f"[WIFI] Starte sanften Reconnect für Verbindung '{self.connection_name}'...")

        # Stufe 1: Normaler Versuch über nmcli
        self.shell_command(f"sudo nmcli connection up {self.connection_name}")

        # Stufe 2: Eskalation (Neustart der Dienste)
        if self._retries > 0 and self._retries % 3 == 0:
            print("[WIFI] Eskalationsstufe erreicht. Versuche regulären Neustart des NetworkManagers...")

            # Regulärer Versuch. '2>&1' fängt Fehlermeldungen (wie den systemd-Timeout) ab.
            restart_log = self.shell_command("sudo systemctl restart NetworkManager.service 2>&1").strip().lower()

            time.sleep(5)

            # Linux-Befehl 'timeout 5' bricht die Abfrage nach 5 Sekunden hart ab, falls systemd hängt
            status = self.shell_command("timeout 5 systemctl is-active NetworkManager.service 2>&1").strip()

            # WENN der Restart-Befehl einen Timeout geworfen hat ODER der Status nicht 'active' ist:
            if "timeout" in restart_log or status != "active":
                print(
                    f"[WIFI] Regulärer Neustart gescheitert (Log: {restart_log} | Status: {status}). Löse Deadlock durch harten Kill aus...")

                # Gnadenloser Kill der hängenden Prozesse
                self.shell_command("sudo systemctl kill -s SIGKILL wpa_supplicant.service")
                self.shell_command("sudo systemctl kill -s SIGKILL NetworkManager.service")

                print("[WIFI] Warte auf Freigabe der Hardware-Ressourcen...")
                time.sleep(10)

                print("[WIFI] Starte Netzwerk-Dienste frisch...")
                self.shell_command("sudo systemctl start wpa_supplicant.service")
                self.shell_command("sudo systemctl start NetworkManager.service")

                time.sleep(15)
            else:
                print("[WIFI] Regulärer Neustart war erfolgreich. Warte auf Verbindungsaufbau...")
                time.sleep(15)
        else:
            time.sleep(5)
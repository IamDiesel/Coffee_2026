import time
import os
from threading import Thread


class Wifi_Deamon(Thread):
    def __init__(self, gateway_ip="192.168.2.1", ha_ip="192.168.2.44"):
        super().__init__()
        self.gateway_ip = gateway_ip
        self.ha_ip = ha_ip
        self._retries = 0
        self._running = False

        # NEU: Dieses Flag sagt der main.py, wann sie starten darf
        self.network_ready = False

        if os.name == 'posix':
            self.interface = self.get_interface()
            if self.interface:
                self.shell_command(f"sudo iwconfig {self.interface} power off")

    def stop(self):
        """Beendet die Watchdog-Schleife sauber."""
        self._running = False

    def run(self):
        if os.name != 'posix':
            print("[WIFI] Watchdog deaktiviert (Nicht auf Linux)")
            self.network_ready = True
            return

        self._running = True

        # --- DYNAMISCHER BOOT-CHECK ---
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
                self.shell_command("sudo nmcli radio wifi on")
                time.sleep(10)
                self.shell_command("sudo reboot")

            # 10 Sekunden warten
            for _ in range(10):
                if not self._running:
                    break
                time.sleep(1)

    def is_host_reachable(self, ip_address):
        try:
            res = self.shell_command(
                f"ping {ip_address} -c 1 -W 1 | grep 'received' | awk -F',' '{{ print $2}}' | awk '{{ print $1}}'")
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

        # Sicherer Fallback: Wenn alles fehlschlägt, nimm immer wlan1
        return "wlan1"

    def reset_WLAN(self):
        if not self.interface:
            self.interface = "wlan1"  # Fallback-Garantie gegen den "dev argument" Fehler

        print(f"[WIFI] Führe harten WLAN-Reset durch auf Interface: {self.interface}")
        self.shell_command(f"sudo ip link set {self.interface} down")
        time.sleep(5)
        self.shell_command("sudo nmcli radio wifi off")
        time.sleep(5)
        self.shell_command("sudo nmcli radio wifi on")
        time.sleep(5)
        self.shell_command(f"sudo ip link set {self.interface} up")
        time.sleep(15)
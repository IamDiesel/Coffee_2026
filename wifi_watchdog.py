import logging
import os
import re
import subprocess
import time
from logging.handlers import RotatingFileHandler
from threading import Thread

import my_config as secrets


class Wifi_Deamon(Thread):
    def __init__(self, gateway_ip=secrets.GATEWAY_IP, ha_ip=secrets.HA_IP, connection_name=secrets.WIFI_SSID):
        super().__init__()
        self.gateway_ip = gateway_ip
        self.ha_ip = ha_ip
        self.connection_name = connection_name
        self._retries = 0
        self._health_failures = 0
        self._running = False
        self._recovery_in_progress = False
        self._command_hang_detected = False
        self.network_ready = False
        self.interface = "wlan0"

        self.command_timeout = 15
        self.initial_wait_seconds = 60
        self.loop_sleep_seconds = 10
        self.recovery_wait_seconds = 20
        self.health_failure_threshold = 3
        self.reboot_threshold = 4

        self.logger = logging.getLogger("wifi_watchdog")
        self._configure_logger()

        if os.name == "posix":
            self.interface = self.get_interface() or "wlan0"
            self._disable_wifi_power_save()

    def stop(self):
        self._running = False
        self.logger.info("Stop requested")

    def run(self):
        if os.name != "posix":
            self.logger.info("Watchdog disabled on non-Linux systems")
            self.network_ready = True
            return

        self._running = True
        self.logger.info(
            "Watchdog started. Waiting for network via router (%s)...",
            self.gateway_ip,
        )

        try:
            self._wait_for_initial_network()
        except Exception:
            self.logger.exception("Initial network check failed unexpectedly")

        while self._running:
            try:
                state = self.collect_network_state()

                if state["healthy"]:
                    if self._retries > 0 or self._health_failures > 0:
                        self.logger.info("Network is reachable again")
                    self._retries = 0
                    self._health_failures = 0
                    self._command_hang_detected = False
                    self.network_ready = True
                else:
                    self._health_failures += 1
                    self.network_ready = False

                    if self._health_failures < self.health_failure_threshold:
                        self.logger.warning(
                            "Network health check failed (%d/%d); waiting before recovery: %s",
                            self._health_failures,
                            self.health_failure_threshold,
                            self._format_state(state),
                        )
                        self._sleep_with_stop(self.loop_sleep_seconds)
                        continue

                    self._retries += 1
                    self.logger.warning(
                        "Network not healthy (attempt %d/%d): %s",
                        self._retries,
                        self.reboot_threshold,
                        self._format_state(state),
                    )

                    if self._retries >= self.reboot_threshold:
                        self._reboot_system()
                        return

                    self.reset_WLAN(state)

                    if self._command_hang_detected:
                        self.logger.error("Recovery command hung; escalating directly to reboot")
                        self._reboot_system()
                        return
            except Exception:
                self.network_ready = False
                self.logger.exception("Unhandled error in watchdog loop")

            self._sleep_with_stop(self.loop_sleep_seconds)

    def is_host_reachable(self, ip_address):
        if not ip_address:
            return False

        command = ["ping", "-c", "1", "-W", "2"]
        if self.interface:
            command.extend(["-I", self.interface])
        command.append(ip_address)

        result = self._run_command(command, timeout=5, label=f"Ping {ip_address}", log_errors=False)
        return result is not None and result.returncode == 0

    def shell_command(self, input_cmd):
        if isinstance(input_cmd, str):
            result = self._run_command(input_cmd, timeout=self.command_timeout, use_shell=True, label=input_cmd)
        else:
            result = self._run_command(input_cmd, timeout=self.command_timeout, label=self._command_name(input_cmd))

        if result is None:
            return ""
        return result.stdout or ""

    def get_interface(self):
        try:
            result = self._run_command(
                ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"],
                timeout=5,
                label="Detect WiFi interface",
                log_errors=False,
            )
            if result and result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split(":", 3)
                    if len(parts) >= 3 and parts[1] == "wifi" and parts[0]:
                        return parts[0].strip()

            result = self._run_command(
                ["ip", "-br", "link", "show"],
                timeout=5,
                label="Detect WiFi interface",
                log_errors=False,
            )
            if result and result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "wlan" in line or line.split() and line.split()[0].startswith("wl"):
                        return line.split()[0].strip()
        except Exception:
            pass
        return "wlan0"

    def reset_WLAN(self, state=None):
        if os.name != "posix":
            return False

        if self._recovery_in_progress:
            self.logger.debug("Recovery already in progress, skipping")
            return False

        self._recovery_in_progress = True
        self._command_hang_detected = False
        try:
            if state is None:
                state = self.collect_network_state()

            if state.get("device_state", "").startswith("connecting") and self._retries == 1:
                self.logger.info("NetworkManager is already connecting; waiting before intervention")
                if self._wait_for_network_ready(self.recovery_wait_seconds):
                    self.logger.info("Network recovered without an additional recovery command")
                    self._retries = 0
                    self._health_failures = 0
                    self.network_ready = True
                    return True
                self.logger.warning("NetworkManager did not finish connecting within the wait period")
                return False

            actions = self._build_recovery_actions(state)
            self.logger.info(
                "Starting recovery stage %d (%s)",
                self._recovery_stage(),
                self._format_state(state),
            )

            for label, command, timeout, settle_seconds, verify_timeout in actions:
                if not self._running:
                    return False

                self.logger.info("Recovery step: %s", label)
                result = self._run_command(command, timeout=timeout, label=label)

                if result is None:
                    self.logger.warning("Recovery step timed out: %s", label)
                    self._command_hang_detected = True
                    return False
                elif result.returncode != 0:
                    stderr = (result.stderr or "").strip()
                    stdout = (result.stdout or "").strip()
                    self.logger.warning(
                        "Recovery step failed rc=%s: %s",
                        result.returncode,
                        stderr or stdout or label,
                    )
                else:
                    self.logger.info("Recovery step completed rc=0: %s", label)

                if settle_seconds:
                    self._sleep_with_stop(settle_seconds)

                if verify_timeout and self._wait_for_network_ready(verify_timeout):
                    self.logger.info("Network recovered after: %s", label)
                    self._disable_wifi_power_save()
                    self._retries = 0
                    self._health_failures = 0
                    self.network_ready = True
                    return True

            self.logger.warning("Recovery stage did not restore connectivity")
            return False
        finally:
            self._recovery_in_progress = False

    def collect_network_state(self):
        if os.name != "posix":
            return {
                "interface_exists": True,
                "device_state": "connected",
                "has_ip": True,
                "default_route": True,
                "gateway_reachable": True,
                "ha_reachable": True,
                "network_reachable": True,
                "healthy": True,
            }

        if not self.interface or not self._interface_exists():
            detected_interface = self.get_interface() or "wlan0"
            if detected_interface != self.interface:
                self.logger.info(
                    "WiFi interface changed from %s to %s",
                    self.interface,
                    detected_interface,
                )
                self.interface = detected_interface

        device_info = self._nmcli_device_info()
        interface_exists = self._interface_exists()
        has_ip = self._interface_has_ipv4_address()
        default_route = self._default_route_via_interface()
        gateway_reachable = self.is_host_reachable(self.gateway_ip)
        ha_reachable = self.is_host_reachable(self.ha_ip)

        device_state = device_info.get("state", "unknown")
        connected = device_state.startswith("connected")
        network_reachable = gateway_reachable or ha_reachable
        healthy = interface_exists and connected and has_ip and default_route and network_reachable

        return {
            "interface_exists": interface_exists,
            "device_state": device_state,
            "connection": device_info.get("connection", ""),
            "has_ip": has_ip,
            "default_route": default_route,
            "gateway_reachable": gateway_reachable,
            "ha_reachable": ha_reachable,
            "network_reachable": network_reachable,
            "healthy": healthy,
        }

    def _configure_logger(self):
        if self.logger.handlers:
            self.logger.setLevel(logging.INFO)
            self.logger.propagate = False
            return

        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        self.logger.addHandler(stream_handler)

        try:
            log_dir = os.path.dirname(os.path.abspath(__file__))
            log_path = os.path.join(log_dir, "wifi_watchdog.log")
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        except Exception as exc:
            self.logger.warning("Could not initialize file logging: %s", exc)

    def _run_command(self, command, timeout=None, use_shell=False, label=None, log_errors=True):
        timeout = self.command_timeout if timeout is None else timeout
        started = time.monotonic()

        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        environment["LANG"] = "C"

        try:
            completed = subprocess.run(
                command,
                shell=use_shell,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            if log_errors:
                self.logger.warning("%s timed out after %.1fs", label or self._command_name(command), timeout)
            return None
        except Exception as exc:
            if log_errors:
                self.logger.exception("%s failed to start: %s", label or self._command_name(command), exc)
            return None

        duration = time.monotonic() - started
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        name = label or self._command_name(command)

        if completed.returncode == 0:
            self.logger.debug("%s ok in %.2fs", name, duration)
        elif log_errors:
            self.logger.warning(
                "%s failed rc=%s in %.2fs: %s",
                name,
                completed.returncode,
                duration,
                stderr or stdout or "no output",
            )

        return completed

    def _command_name(self, command):
        if isinstance(command, str):
            return command
        return " ".join(str(part) for part in command)

    def _with_sudo(self, command):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return list(command)
        return ["sudo", "-n", *command]

    def _sleep_with_stop(self, seconds):
        for _ in range(int(seconds)):
            if not self._running:
                return False
            time.sleep(1)
        return self._running

    def _wait_for_initial_network(self):
        for second in range(self.initial_wait_seconds):
            if not self._running:
                return False

            state = self.collect_network_state()
            if state["healthy"]:
                self.network_ready = True
                self.logger.info("Network reachable after %d seconds", second)
                return True

            time.sleep(1)

        self.logger.warning("Timeout waiting for initial network readiness")
        return False

    def _wait_for_network_ready(self, timeout_seconds):
        deadline = time.monotonic() + timeout_seconds
        while self._running and time.monotonic() < deadline:
            if self.collect_network_state()["healthy"]:
                return True
            time.sleep(1)
        return False

    def _interface_exists(self):
        result = self._run_command(
            ["ip", "link", "show", "dev", self.interface],
            timeout=5,
            label="Check interface presence",
            log_errors=False,
        )
        return result is not None and result.returncode == 0

    def _interface_has_ipv4_address(self):
        result = self._run_command(
            ["ip", "-4", "addr", "show", "dev", self.interface],
            timeout=5,
            label="Check IPv4 address",
            log_errors=False,
        )
        if result is None or result.returncode != 0:
            return False
        return re.search(r"\binet\s+\d+\.\d+\.\d+\.\d+/\d+", result.stdout or "") is not None

    def _default_route_via_interface(self):
        result = self._run_command(
            ["ip", "route", "show", "default"],
            timeout=5,
            label="Check default route",
            log_errors=False,
        )
        if result is None or result.returncode != 0:
            return False

        for line in (result.stdout or "").splitlines():
            tokens = line.split()
            if "default" in tokens and "dev" in tokens:
                try:
                    dev_index = tokens.index("dev")
                    if tokens[dev_index + 1] != self.interface:
                        continue

                    if self.gateway_ip and "via" in tokens:
                        via_index = tokens.index("via")
                        return tokens[via_index + 1] == self.gateway_ip
                    return True
                except (ValueError, IndexError):
                    continue
        return False

    def _nmcli_device_info(self):
        result = self._run_command(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            timeout=5,
            label="Check nmcli device state",
            log_errors=False,
        )
        if result is None or result.returncode != 0:
            return {}

        for line in (result.stdout or "").splitlines():
            parts = line.split(":", 3)
            if not parts or parts[0] != self.interface:
                continue

            return {
                "device": parts[0] if len(parts) > 0 else self.interface,
                "type": parts[1] if len(parts) > 1 else "",
                "state": parts[2] if len(parts) > 2 else "unknown",
                "connection": parts[3] if len(parts) > 3 else "",
            }

        return {}

    def _format_state(self, state):
        return (
            f"iface={self.interface} "
            f"present={state['interface_exists']} "
            f"nm_state={state['device_state']} "
            f"ip={state['has_ip']} "
            f"route={state['default_route']} "
            f"gateway={state['gateway_reachable']} "
            f"ha={state['ha_reachable']}"
        )

    def _recovery_stage(self):
        if self._retries <= 1:
            return 1
        if self._retries <= 2:
            return 2
        if self._retries <= 3:
            return 3
        return 4

    def _build_recovery_actions(self, state):
        iface = self.interface or "wlan0"
        connection = self.connection_name
        stage = self._recovery_stage()
        reconnect_command = self._with_sudo(
            [
                "nmcli",
                "-w",
                "20",
                "connection",
                "up",
                "id",
                connection,
                "ifname",
                iface,
            ]
        )

        if not state.get("interface_exists", True) or state.get("device_state") == "unknown":
            return [
                (
                    "Restart NetworkManager",
                    self._with_sudo(["systemctl", "restart", "NetworkManager.service"]),
                    30,
                    5,
                    self.recovery_wait_seconds,
                ),
                (
                    "Reconnect saved connection",
                    reconnect_command,
                    25,
                    0,
                    self.recovery_wait_seconds,
                ),
            ]

        if stage == 1:
            actions = []
            if state.get("device_state", "").startswith("connected"):
                actions.append(
                    (
                        "Disconnect interface",
                        self._with_sudo(["nmcli", "device", "disconnect", iface]),
                        15,
                        2,
                        0,
                    )
                )
            actions.append(
                (
                    "Reconnect saved connection",
                    reconnect_command,
                    25,
                    0,
                    self.recovery_wait_seconds,
                )
            )
            return actions

        if stage == 2:
            return [
                (
                    "Disconnect interface",
                    self._with_sudo(["nmcli", "device", "disconnect", iface]),
                    15,
                    2,
                    0,
                ),
                (
                    "Reconnect saved connection",
                    reconnect_command,
                    25,
                    0,
                    self.recovery_wait_seconds,
                ),
            ]

        if stage == 3:
            return [
                (
                    "Restart NetworkManager",
                    self._with_sudo(["systemctl", "restart", "NetworkManager.service"]),
                    30,
                    5,
                    self.recovery_wait_seconds,
                ),
                (
                    "Reconnect saved connection",
                    reconnect_command,
                    25,
                    0,
                    self.recovery_wait_seconds,
                ),
            ]

        return []

    def _disable_wifi_power_save(self):
        if not self.interface:
            return

        attempts = [
            (
                self._with_sudo(["iw", "dev", self.interface, "set", "power_save", "off"]),
                "Disable WiFi power save",
            ),
            (self._with_sudo(["iwconfig", self.interface, "power", "off"]), "Disable WiFi power save"),
        ]

        for command, label in attempts:
            result = self._run_command(command, timeout=5, label=label, log_errors=False)
            if result is not None and result.returncode == 0:
                self.logger.info("WiFi power save disabled on %s", self.interface)
                return

        self.logger.warning("Could not disable WiFi power save on %s", self.interface)

    def _reboot_system(self):
        self.logger.error("Rebooting system after repeated recovery failures")
        self._run_command(self._with_sudo(["sync"]), timeout=10, label="Sync filesystems")

        result = self._run_command(
            self._with_sudo(["systemctl", "reboot"]),
            timeout=10,
            label="System reboot",
        )
        if result is not None and result.returncode == 0:
            self.logger.warning("Reboot request accepted; waiting for shutdown")
            self._sleep_with_stop(20)
            self.logger.critical("System is still running after the normal reboot request")
        else:
            self.logger.critical("Normal reboot failed; trying forced reboot")

        result = self._run_command(
            self._with_sudo(["systemctl", "--force", "--force", "reboot"]),
            timeout=10,
            label="Forced system reboot",
        )
        if result is not None and result.returncode == 0:
            time.sleep(5)
            self.logger.critical("System is still running after the forced reboot request")
        else:
            self.logger.critical("Forced systemctl reboot failed")

        self.logger.critical("Trying final kernel forced reboot")
        self._run_command(
            self._with_sudo(["reboot", "-f"]),
            timeout=10,
            label="Kernel forced reboot",
        )

        self._running = False

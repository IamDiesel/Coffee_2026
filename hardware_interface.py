from abc import ABC, abstractmethod

class CoffeeMachineHardware(ABC):
    """
    Abstrakte Basisklasse (Interface) für die Hardware-Abstraktionsschicht.
    Definiert alle Methoden, die der CoffeeController zwingend benötigt.
    """

    @abstractmethod
    def get_mainboard_stream(self):
        """Gibt das serielle Stream-Objekt für die Kommunikation mit dem Mainboard zurück."""
        pass

    @abstractmethod
    def get_display_stream(self):
        """Gibt das serielle Stream-Objekt für die Kommunikation mit dem Display zurück."""
        pass

    @abstractmethod
    def toggle_display_power(self, duration: float):
        """Toggelt den Strom zum Display für einen Hardware-Reset."""
        pass

    @abstractmethod
    def relais_off(self):
        """Notfall/Bypass: Deaktiviert den MITM-Modus und verbindet MB & Display direkt."""
        pass

    @abstractmethod
    def update_gui(self, status_dict: dict):
        """Leitet Status-Updates an die Hardware-UI weiter (z.B. für den Digital Twin)."""
        pass

    @abstractmethod
    def attach_mqtt(self, mqtt_manager):
        """Erlaubt der Hardware, den MQTT-Traffic mitzusniffen."""
        pass

    def set_mitm(self, active: bool):
        """Schaltet zwischen Pi-Steuerung (MITM, True) und Handbetrieb (Bypass, False).

        Standard-Implementierung fuer Hardware ohne Relais (z. B. Digital Twin):
        Handbetrieb wird auf relais_off() abgebildet, MITM ist ein No-Op.
        """
        if not active:
            self.relais_off()
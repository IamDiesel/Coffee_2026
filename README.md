
---

# Alarm2Coffee - Philips 2200 MQTT Controller

Dieses Projekt integriert einen Philips 2200 Serie Kaffeevollautomaten vollständig in Home Assistant via MQTT. Das System läuft auf einem **Raspberry Pi** und fungiert als intelligente Zwischenschicht (Man-in-the-Middle) in der internen Kommunikation der Kaffeemaschine.

Details zur Hardware, der genauen Verkabelung und den benötigten Bauteilen findest du hier:
👉 **[Hardware & Verkabelung auf GitHub (IamDiesel/Alarm2Coffee)](https://github.com/IamDiesel/Alarm2Coffee)**

---

## 🏗 Systemarchitektur & Funktionsweise

Die Kaffeemaschine besteht intern aus zwei Hauptkomponenten, die über einen seriellen Bus (UART, 115200 Baud) kommunizieren:

1. Das **Mainboard** (Steuerung der Aktoren, Sensoren, Pumpen)
2. Das **Display** (Touch-Eingaben, LEDs)

### Man-in-the-Middle (MITM) & Failsafe-Relais

Der Raspberry Pi klinkt sich physisch in diese UART-Verbindung ein. Dies geschieht über Active-Low Relais-Module, die von den GPIO-Pins des Pi gesteuert werden:

* **MITM-Modus (Normalbetrieb):** Das Relais zieht an (`GPIO LOW`). Die TX/RX-Leitungen werden über den Raspberry Pi umgeleitet. Das Python-Skript empfängt die Display-Eingaben, leitet sie an das Mainboard weiter (Passthrough) und vice versa. Zusätzlich kann das Skript eigene Befehle (über MQTT empfangen) in den Datenstrom injizieren.
* **Failsafe / Bypass-Modus:** Fällt der Raspberry Pi aus, stürzt das Skript ab oder wird das System neu gestartet, fallen die Relais stromlos ab (`GPIO HIGH` / Ruhezustand Normally Closed). Dadurch wird die UART-Verbindung zwischen Display und Mainboard physisch hardwareseitig wiederhergestellt. Die Maschine funktioniert in diesem Zustand komplett autonom weiter ("Bypass").

---

## 🧠 Protokoll & CRC-Berechnung (Reverse Engineering)

Die Befehle vom Display zum Mainboard (z. B. "Bezug Espresso") sind 12 Bytes lang. Das Protokoll nutzt eine komplexe zyklische Redundanzprüfung (CRC) für die letzten beiden Bytes. Anstatt das proprietäre CRC-Polynom klassisch zu berechnen, nutzt das Skript einen hocheffizienten **XOR-Delta-Algorithmus**.

**Die Logik dahinter:**
Da die Maschine streng linear arbeitet, kann die exakte Checksumme für jeden Tastendruck dynamisch über XOR-Offsets berechnet werden:

1. Das **Basis-Payload** (Standby-Zustand ohne gedrückte Tasten) hat eine bekannte, fixe Checksumme (`0x2D01`).
2. Jeder Tastendruck ändert ein spezifisches Byte in der Payload (z.B. Espresso ändert Byte 5 auf `0x02`).
3. Die Differenz der Checksumme zwischen Standby und "Espresso gedrückt" lässt sich über eine XOR-Verknüpfung ermitteln: `0x2D01 ^ 0x351A = 0x181B`.
4. Das Protokoll hält in einer Lookup-Tabelle (`XOR_LOOKUP`) diese Deltas bereit.

Das Skript baut so jeden beliebigen 12-Byte-Befehl "on the fly" zusammen und berechnet die mathematisch exakte CRC in Mikrosekunden, ohne auf statische Hex-Strings angewiesen zu sein.

---

## ⚙️ Installation & Einrichtung

### 1. Python Virtual Environment (venv) anlegen

Um Abhängigkeitskonflikte auf dem Raspberry Pi zu vermeiden, wird das Projekt in einer virtuellen Umgebung ausgeführt.

```bash
# In das Projektverzeichnis wechseln (Pfade ggf. anpassen)
cd /home/fuchsi/Documents/philips_pi_26

# venv erstellen (hier im Home-Verzeichnis unter .coffee)
python3 -m venv /home/fuchsi/.coffee

# venv aktivieren und Pakete installieren
source /home/fuchsi/.coffee/bin/activate
pip install paho-mqtt pyserial requests

```

### 2. Autostart via Systemd einrichten

Damit das Skript nach einem Neustart des Raspberry Pi automatisch, mit den nötigen Rechten (Root für GPIOs) und erst nach erfolgreicher WLAN-Verbindung startet, wird ein Systemd-Service angelegt.

Erstelle die Datei `/etc/systemd/system/coffee26.service`:

```bash
sudo nano /etc/systemd/system/coffee26.service

```

Füge folgenden Inhalt ein:

```ini
[Unit]
Description=Philips 2200 MQTT Controller (Coffee26)
Wants=network-online.target
After=network-online.target 

[Service]
User=root
WorkingDirectory=/home/fuchsi/Documents/philips_pi_26
# Nutzt direkt das Python-Binary aus der virtuellen Umgebung
ExecStart=/home/fuchsi/.coffee/bin/python3 /home/fuchsi/Documents/philips_pi_26/main.py

# --- LOGGING KONFIGURATION ---
# Erzwingt die sofortige Ausgabe von print() in Python
Environment=PYTHONUNBUFFERED=1 
# Schreibt die regulären Logs in diese Datei
StandardOutput=append:/var/log/coffee26.log 
# Schreibt Fehlermeldungen/Crashes in dieselbe Datei
StandardError=append:/var/log/coffee26.log 

# Automatischer Neustart bei Abstürzen
Restart=always 
RestartSec=5 

# Beende das Skript sauber (sendet SIGTERM), damit der Hardware-Failsafe greift!
KillSignal=SIGTERM 

[Install]
WantedBy=multi-user.target

```

**Service aktivieren und starten:**

```bash
# Systemd neu laden
sudo systemctl daemon-reload
# Dienst für den Autostart aktivieren
sudo systemctl enable coffee26.service
# Dienst starten
sudo systemctl start coffee26.service

```

**Logs in Echtzeit lesen:**

```bash
tail -f /var/log/coffee26.log

```

---

## 📱 Home Assistant Integration

<img width="1071" height="918" alt="image" src="https://github.com/user-attachments/assets/cc10ee06-51ad-44f0-93ac-d7c7e646cef9" />


Das Skript unterstützt vollständiges **MQTT Auto-Discovery**. Sobald das Skript startet, tauchen alle Schalter (Befehle) und Binärsensoren (LED-Status, Warnungen) automatisch in Home Assistant auf. Es werden keine zusätzlichen Helfer (Input Booleans) oder Automatisierungen mehr benötigt.

### Dashboard YAML

<img width="1047" height="763" alt="image" src="https://github.com/user-attachments/assets/4978a4ed-07ae-4031-9d33-998629bc799c" />


Erstelle eine neue Ansicht in deinem Dashboard und kopiere diesen YAML-Code hinein. Er kombiniert die Status-LEDs der Maschine direkt mit den ausführenden Buttons für ein sauberes Interface:

```yaml
type: sections
max_columns: 2
title: Kaffee
path: kaffee
icon: mdi:coffee
sections:
  - type: grid
    cards:
      - type: tile
        entity: button.philips_2200_series_power_on
        name: Power On
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.philips_2200_series_power_on
        hide_state: true
      - type: tile
        entity: button.philips_2200_series_power_off
        name: Power Off
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.philips_2200_series_power_off
        hide_state: true
      - show_name: true
        show_icon: true
        type: button
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.philips_2200_series_espresso
        entity: binary_sensor.philips_2200_series_espresso_led
        show_state: false
        name: Espresso
        icon: mdi:cup
      - show_name: true
        show_icon: true
        type: button
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.philips_2200_series_coffee
        entity: binary_sensor.philips_2200_series_coffee_led
        show_state: false
        name: Coffee
        icon: mdi:coffee
      - show_name: true
        show_icon: true
        type: button
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.philips_2200_series_hot_water
        entity: binary_sensor.philips_2200_series_hot_water_led
        show_state: false
        name: Hot Water
        icon: mdi:tea-outline
      - show_name: true
        show_icon: true
        type: button
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.philips_2200_series_steam
        entity: binary_sensor.philips_2200_series_steam_led
        show_state: false
        name: Steam
        icon: mdi:weather-windy
  - type: grid
    cards:
      - show_name: true
        show_icon: true
        type: button
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.philips_2200_series_play_pause
        entity: binary_sensor.philips_2200_series_play_pause_led
        grid_options:
          columns: 12
          rows: 2
        name: Start
      - type: conditional
        conditions:
          - condition: state
            entity: sensor.philips_2200_series_bohnen
            state: "1"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: perform-action
            perform_action: button.press
            target:
              entity_id: button.philips_2200_series_bean_strength
          entity: sensor.philips_2200_series_bohnen
          icon: mdi:network-strength-1
          hold_action:
            action: none
          name: Strength
      - type: conditional
        conditions:
          - condition: state
            entity: sensor.philips_2200_series_bohnen
            state: "2"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: perform-action
            perform_action: button.press
            target:
              entity_id: button.philips_2200_series_bean_strength
          entity: sensor.philips_2200_series_bohnen
          icon: mdi:network-strength-3
          hold_action:
            action: none
          name: Strength
      - type: conditional
        conditions:
          - condition: state
            entity: sensor.philips_2200_series_bohnen
            state: "3"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: perform-action
            perform_action: button.press
            target:
              entity_id: button.philips_2200_series_bean_strength
          entity: sensor.philips_2200_series_bohnen
          icon: mdi:network-strength-4
          hold_action:
            action: none
          name: Strength
      - type: conditional
        conditions:
          - condition: state
            entity: sensor.philips_2200_series_bohnen
            state: "Off"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: perform-action
            perform_action: button.press
            target:
              entity_id: button.philips_2200_series_bean_strength
          entity: sensor.philips_2200_series_bohnen
          icon: mdi:cancel
          hold_action:
            action: none
          name: Strength
      - type: conditional
        conditions:
          - condition: state
            entity: sensor.philips_2200_series_tasse
            state: "1"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: perform-action
            perform_action: button.press
            target:
              entity_id: button.philips_2200_series_cup_size
          entity: sensor.philips_2200_series_tasse
          icon: mdi:network-strength-1
          hold_action:
            action: none
          name: Size
      - type: conditional
        conditions:
          - condition: state
            entity: sensor.philips_2200_series_tasse
            state: "2"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: perform-action
            perform_action: button.press
            target:
              entity_id: button.philips_2200_series_cup_size
          entity: sensor.philips_2200_series_tasse
          icon: mdi:network-strength-3
          hold_action:
            action: none
          name: Size
      - type: conditional
        conditions:
          - condition: state
            entity: sensor.philips_2200_series_tasse
            state: "3"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: perform-action
            perform_action: button.press
            target:
              entity_id: button.philips_2200_series_cup_size
          entity: sensor.philips_2200_series_tasse
          icon: mdi:network-strength-4
          hold_action:
            action: none
          name: Size
      - type: conditional
        conditions:
          - condition: state
            entity: sensor.philips_2200_series_tasse
            state: "Off"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: perform-action
            perform_action: button.press
            target:
              entity_id: button.philips_2200_series_cup_size
          entity: sensor.philips_2200_series_tasse
          icon: mdi:cancel
          hold_action:
            action: none
          name: Size
      - type: conditional
        conditions:
          - condition: state
            entity: binary_sensor.philips_2200_series_double_coffee_led
            state: "on"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: none
          entity: binary_sensor.philips_2200_series_double_coffee_led
          name: Coffee
          hold_action:
            action: none
          icon: mdi:dice-2
      - type: conditional
        conditions:
          - condition: state
            entity: binary_sensor.philips_2200_series_double_espresso_led
            state: "on"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: none
          entity: binary_sensor.philips_2200_series_double_espresso_led
          name: Espresso
          hold_action:
            action: none
          icon: mdi:dice-2
      - type: conditional
        conditions:
          - condition: state
            entity: binary_sensor.philips_2200_series_wassertank_leer
            state: "on"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: none
          entity: binary_sensor.philips_2200_series_wassertank_leer
          name: No Water
          hold_action:
            action: none
          icon: mdi:water-off
      - type: conditional
        conditions:
          - condition: state
            entity: binary_sensor.philips_2200_series_mullbehalter_voll
            state: "on"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: none
          entity: binary_sensor.philips_2200_series_mullbehalter_voll
          name: Waste full
          hold_action:
            action: none
          icon: mdi:delete-variant
      - type: conditional
        conditions:
          - condition: state
            entity: binary_sensor.philips_2200_series_allgemeiner_fehler
            state: "on"
        grid_options:
          columns: 6
          rows: 1
        card:
          show_name: true
          show_icon: true
          type: button
          tap_action:
            action: none
          entity: binary_sensor.philips_2200_series_allgemeiner_fehler
          name: Error
          hold_action:
            action: none
          icon: mdi:alert-circle
    column_span: 1
  - type: grid
    cards:
      - type: heading
        heading_style: title
        icon: mdi:alarm
        badges:
          - type: entity
            entity: input_boolean.philips_alarm_daniel
          - type: entity
            entity: sensor.pixel_9_pro_next_alarm
          - type: entity
            show_state: true
            show_icon: true
            entity: input_boolean.philips_alarm_fox
        heading: Mit Wecker einschalten
      - type: tile
        entity: input_boolean.philips_alarm_daniel
        tap_action:
          action: toggle
        name: Alarm2Coffee Daniel
        grid_options:
          columns: 12
          rows: 1
      - type: tile
        entity: input_boolean.philips_alarm_fox
        tap_action:
          action: toggle
        name: Alarm2Coffee Fox
        grid_options:
          columns: 12
          rows: 1
      - type: tile
        entity: sensor.pixel_9_pro_next_alarm
        grid_options:
          columns: 12
          rows: 2
        name: Nächster Wecker Daniel
        hide_state: false
        show_entity_picture: false
        vertical: true
        state_content:
          - state
          - Local Time
      - show_name: true
        show_icon: true
        type: button
        tap_action:
          action: perform-action
          perform_action: button.press
          target:
            entity_id: button.philips_2200_series_power_on_no_clean
        entity: button.philips_2200_series_power_on_no_clean
        grid_options:
          columns: 12
          rows: 2
        name: Power On (Dirty)
        icon: mdi:liquid-spot

```

### ⏰ Automatisierung: Alarm2Coffee (Android Wecker)

Schaltet die Maschine automatisch 1 Minute vor dem Klingeln des Android-Weckers (via HA Companion App) ein.

```yaml
alias: Alarm2Coffee Daniel
description: "Schaltet die Kaffeemaschine 1 Minute vor dem Handy-Wecker ein."
mode: single
triggers:
  - trigger: template
    value_template: >-
      {{(now()+timedelta(0,60)).strftime('%a %h %d %H:%M %Z %Y') ==
      (((state_attr('sensor.pixel_9_pro_next_alarm', 'Time in Milliseconds') |
      int / 1000) ) | timestamp_custom('%a %h %d %H:%M %Z %Y'))}}
conditions:
  - condition: state
    entity_id: input_boolean.philips_alarm_daniel
    state: "on"
actions:
  - action: button.press
    target:
      entity_id: button.philips_2200_series_power_on
  - action: notify.notify 
    data:
      message: Kaffeemaschine wärmt sich auf ☕
      title: Alarm2Coffee

```

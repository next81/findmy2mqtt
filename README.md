<!-- Copyright (c) 2026 Andreas Planer - https://github.com/next81/findmy2mqtt -->
<!-- Licensed under the PolyForm Strict License 1.0.0: https://polyformproject.org/licenses/strict/1.0.0 -->

# findmy2mqtt

`findmy2mqtt` verbindet Apple Wo ist? / Find My über `pyicloud` mit MQTT.

Das Projekt wurde speziell für FHEM entwickelt, die MQTT-Schnittstelle ist jedoch systemunabhängig und kann auch mit Home Assistant, openHAB, Node-RED oder eigenen MQTT-Anwendungen verwendet werden.

> `findmy2mqtt` ist keine offizielle Apple-Integration. Änderungen an Apples iCloud-/Find-My-Webdiensten können die Funktion beeinflussen.

## Architektur

```text
Apple iCloud / Find My
          │
          │ HTTPS
          ▼
       pyicloud
          │
          │ API
          ▼
     findmy2mqtt
          │
          │ MQTT
          ▼
      MQTT Broker
          │
          ├── FHEM
          ├── Home Assistant
          ├── openHAB
          ├── Node-RED
          └── andere MQTT-Consumer
```

Bei FHEM kann `MQTT2_SERVER` direkt als MQTT-Broker verwendet werden.

## Funktionen

- mehrere Apple-Accounts in einem Daemon
- iPhone, iPad, Apple Watch, Mac und weitere Geräte aus Apples normalem Find-My-Gerätedienst
- Standort mit Latitude/Longitude, Genauigkeit und Zeitstempel
- Kennzeichnung veralteter Standortdaten
- Akkustand und Akkustatus, sofern Apple diese liefert
- Gerätemodell und Geräteklasse
- retained MQTT-Statusmeldungen
- Apple-Login mit 2FA
- persistente iCloud-Sessions
- `locate` per CLI über Gerätenamen oder `deviceId`
- `locate` per MQTT
- systemd-Service

### Noch nicht unterstützt

- AirTags
- Drittanbieter-Find-My-Items

## MQTT-Topic-Schema

Alle gerätebezogenen Topics folgen demselben Schema:

```text
findmy/<account>/<deviceId>/<befehl>
```

`deviceId` ist die von findmy2mqtt topic-sicher kodierte Apple-Geräte-ID.

Beispiele:

```text
findmy/person1/ABCDEF123456/state
findmy/person1/ABCDEF123456/locate
```

### Status

Der Status wird retained unter folgendem Topic publiziert:

```text
findmy/<account>/<deviceId>/state
```

Beispiel-Payload:

```json
{
  "state": "located",
  "name": "Person1 iPhone",
  "account": "person1",
  "deviceId": "ABCDEF123456",
  "deviceClass": "iPhone",
  "model": "iPhone17,1",
  "modelName": "iPhone 16 Pro",
  "battery": 83,
  "batteryStatus": "NotCharging",
  "latitude": 52.123456,
  "longitude": 9.123456,
  "accuracy": 8,
  "locationTime": "2026-08-13 10:30:00",
  "locationTimestamp": 1786609800,
  "locationOld": 0
}
```

Fehlende Werte werden nicht publiziert.

### Locate per MQTT

```text
findmy/<account>/<deviceId>/locate
```

Beispiel:

```text
findmy/person1/ABCDEF123456/locate
```

Eine Payload wird nicht benötigt. Command-Nachrichten dürfen nicht retained gesendet werden; retained Commands werden ignoriert.

## MQTT-Client-ID

Jedes Apple-Gerät erhält eine persistente MQTT-Client-ID aus Apple-Account und `deviceId`, z. B.:

```text
fm_person1_6cc1460c2a
```

Der Anzeigename ist nicht Bestandteil der MQTT-Identität.

## Projektstruktur

- `findmy2mqtt.py` – Start, Logging und Signalbehandlung
- `lib/apple.py` – `pyicloud`, Apple-Login, 2FA, Sessions, Geräte und Locate
- `lib/bridge.py` – Polling, Command-Queue und Apple↔MQTT-Orchestrierung
- `lib/cli.py` – Kommandozeile
- `lib/config.py` – Konfiguration, Accounts und Credential-Pfade
- `lib/device.py` – Normalisierung der Apple-Gerätedaten
- `lib/mqtt.py` – MQTT-Status-Publisher und Command-Subscriber
- `lib/utils.py` – allgemeine Hilfsfunktionen

## Installation auf Raspberry Pi OS / Raspbian

### 1. Systempakete installieren

```bash
sudo apt update
sudo apt install -y \
  ca-certificates \
  python3 \
  python3-full \
  python3-pip \
  python3-venv
```

### 2. Projekt laden

Repository klonen oder herunterladen und in das Projektverzeichnis wechseln:

```bash
cd findmy2mqtt
```

### 3. Installieren

```bash
sudo ./install.sh
```

Installiert werden unter anderem:

```text
/opt/findmy2mqtt/
/etc/findmy2mqtt/config.json
/var/lib/findmy2mqtt/
/etc/systemd/system/findmy2mqtt.service
/usr/local/bin/findmy2mqtt
```

Zusätzlich werden der Systembenutzer `findmy2mqtt` und ein Python-venv unter `/opt/findmy2mqtt/venv/` angelegt.

## Konfiguration

Standardpfad:

```text
/etc/findmy2mqtt/config.json
```

Beispiel:

```json
{
  "stateDir": "/var/lib/findmy2mqtt",
  "pollInterval": 300,
  "mqtt": {
    "host": "127.0.0.1",
    "port": 1883,
    "username": null,
    "password": null,
    "passwordFile": null,
    "tls": false,
    "keepalive": 60,
    "topicPrefix": "findmy",
    "qos": 0
  },
  "accounts": [
    {
      "name": "person1",
      "appleId": "person1@example.com",
      "passwordFile": "person1.password",
      "familyDevices": false
    },
    {
      "name": "person2",
      "appleId": "person2@example.com",
      "passwordFile": "person2.password",
      "familyDevices": false
    }
  ]
}
```

`familyDevices` legt fest, ob über die Apple-Familie freigegebene Geräte für den Account mit abgefragt werden.

## CLI

Die Standardkonfiguration `/etc/findmy2mqtt/config.json` wird automatisch verwendet:

```bash
sudo findmy2mqtt set-password person1
sudo findmy2mqtt auth person1
sudo findmy2mqtt devices person1
sudo findmy2mqtt locate person1 "Person1 iPhone"
sudo findmy2mqtt locate person1 ABCDEF123456
sudo findmy2mqtt check
sudo findmy2mqtt once
```

Eine abweichende Konfiguration kann mit `--config` angegeben werden:

```bash
sudo findmy2mqtt --config /pfad/config.json devices person1
```

Passwörter werden unter `/var/lib/findmy2mqtt/credentials/`, Sessions unter `/var/lib/findmy2mqtt/sessions/<account>/` gespeichert.

## Dauerbetrieb

```bash
sudo systemctl enable --now findmy2mqtt
```

Status:

```bash
systemctl status findmy2mqtt
```

Log:

```bash
journalctl -u findmy2mqtt -f
```

## FHEM

Ein vorhandener `MQTT2_SERVER` kann direkt als Broker verwendet werden. `readingList` verarbeitet das eingehende `/state`-Topic, `setList` publiziert `/locate`.

### Vollständiges FHEM-Gerätebeispiel

Beispiel für Account `person1`, `deviceId` `ABCDEF123456`, MQTT-Client-ID `fm_person1_b5b2d80bb3` und `MQTT2_FHEM_Server`:

```text
defmod FindMy_person1_iPhone MQTT2_DEVICE fm_person1_b5b2d80bb3
attr FindMy_person1_iPhone IODev MQTT2_FHEM_Server
attr FindMy_person1_iPhone alias Person1 iPhone
attr FindMy_person1_iPhone group Find My
attr FindMy_person1_iPhone icon smartphone
attr FindMy_person1_iPhone room Anwesenheit
attr FindMy_person1_iPhone readingList fm_person1_b5b2d80bb3:findmy/person1/ABCDEF123456/state:.* { json2nameValue($EVENT) }
attr FindMy_person1_iPhone setList locate:noArg { my $id=ReadingsVal($NAME,"deviceId",""); return undef if $id eq ""; return "findmy/person1/$id/locate 1"; }
attr FindMy_person1_iPhone webCmd locate
attr FindMy_person1_iPhone stateFormat { my $n=ReadingsVal($name,"name","findmy2mqtt"); my $s=ReadingsVal($name,"state","unknown"); my $b=ReadingsVal($name,"battery","-"); my $t=ReadingsVal($name,"locationTime","-"); return "$n: $s | Akku: $b % | Standort: $t"; }
attr FindMy_person1_iPhone webCmd locate
```

Mögliche Readings:

```text
state              located
name               Person1 iPhone
account            person1
deviceId           ABCDEF123456
deviceClass        iPhone
model              iPhone17,1
modelName          iPhone 16 Pro
battery            83
batteryStatus      NotCharging
latitude           52.123456
longitude          9.123456
accuracy           8
locationTime       2026-08-13 10:30:00
locationTimestamp  1786609800
locationOld        0
```

`set FindMy_person1_iPhone locate` publiziert auf:

```text
findmy/person1/ABCDEF123456/locate
```

## Lizenz und Copyright

Copyright (c) 2026 Andreas Planer - https://github.com/next81/findmy2mqtt

Lizenz: PolyForm Strict License 1.0.0  
https://polyformproject.org/licenses/strict/1.0.0

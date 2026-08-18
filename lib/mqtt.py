# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""MQTT-Ein- und Ausgabe von findmy2mqtt.

Status-Publisher und Command-Subscriber sind getrennte Clients. Dadurch bleibt
die FHEM/MQTT-Gerätezuordnung pro Apple-Gerät stabil, während Kommandos zentral
über einen eigenen Control-Client empfangen werden.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Any, Callable, Mapping

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:
    raise SystemExit(
        "paho-mqtt is not installed. Install requirements.txt in the virtualenv."
    ) from exc

from . import __version__
from .config import AccountConfig, Config, mqtt_password
from .utils import short_hash, slugify

LOG = logging.getLogger("findmy2mqtt.mqtt")


# Home Assistant erhält für jedes mögliche Feld eine eigene Entity. Die
# Definition ist absichtlich unabhängig vom ersten Apple-Snapshot, weil einzelne
# Geräte manche Werte erst bei einem späteren Poll liefern.
HOME_ASSISTANT_SENSORS: tuple[tuple[str, str, Mapping[str, Any]], ...] = (
    ("state", "Location state", {}),
    ("name", "Name", {"entity_category": "diagnostic"}),
    ("account", "Account", {"entity_category": "diagnostic"}),
    ("deviceId", "Device ID", {"entity_category": "diagnostic"}),
    ("deviceClass", "Device class", {"entity_category": "diagnostic"}),
    ("model", "Model", {"entity_category": "diagnostic"}),
    ("modelName", "Model name", {"entity_category": "diagnostic"}),
    (
        "battery",
        "Battery",
        {
            "device_class": "battery",
            "state_class": "measurement",
            "unit_of_measurement": "%",
        },
    ),
    ("batteryStatus", "Battery status", {}),
    ("deviceStatus", "Device status", {"entity_category": "diagnostic"}),
    (
        "latitude",
        "Latitude",
        {"unit_of_measurement": "°", "suggested_display_precision": 6},
    ),
    (
        "longitude",
        "Longitude",
        {"unit_of_measurement": "°", "suggested_display_precision": 6},
    ),
    (
        "accuracy",
        "Accuracy",
        {
            "device_class": "distance",
            "state_class": "measurement",
            "unit_of_measurement": "m",
        },
    ),
    ("locationTime", "Location time", {}),
    ("locationTimestamp", "Location timestamp", {}),
    ("positionType", "Position type", {"entity_category": "diagnostic"}),
    ("locationType", "Location type", {"entity_category": "diagnostic"}),
)


def home_assistant_discovery(
    config: Config,
    account: AccountConfig,
    deviceId: str,
    clientId: str,
    stateTopic: str,
    payload: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Erzeuge ein Home-Assistant-Device-Discovery-Payload für ein Gerät."""
    components: dict[str, dict[str, Any]] = {}

    for field, name, options in HOME_ASSISTANT_SENSORS:
        components[f"sensor_{field}"] = {
            "p": "sensor",
            "name": name,
            "unique_id": f"{clientId}_{field}",
            "state_topic": stateTopic,
            "value_template": f"{{{{ value_json.get('{field}') }}}}",
            **options,
        }

    components["binary_sensor_locationOld"] = {
        "p": "binary_sensor",
        "name": "Location old",
        "unique_id": f"{clientId}_locationOld",
        "state_topic": stateTopic,
        "value_template": "{{ value_json.get('locationOld') }}",
        "payload_on": "1",
        "payload_off": "0",
        "device_class": "problem",
    }
    components["device_tracker_location"] = {
        "p": "device_tracker",
        "name": None,
        "unique_id": f"{clientId}_location",
        "json_attributes_topic": stateTopic,
        "json_attributes_template": (
            "{{ {'latitude': value_json.get('latitude'), "
            "'longitude': value_json.get('longitude'), "
            "'gps_accuracy': value_json.get('accuracy'), "
            "'battery_level': value_json.get('battery')} | tojson }}"
        ),
        "source_type": "gps",
    }
    components["button_locate"] = {
        "p": "button",
        "name": "Locate",
        "unique_id": f"{clientId}_locate",
        "command_topic": stateTopic.removesuffix("/state") + "/locate",
        "payload_press": "1",
        "retain": False,
        "icon": "mdi:map-marker-radius",
    }
    components["text_message"] = {
        "p": "text",
        "name": "Message",
        "unique_id": f"{clientId}_message",
        "command_topic": stateTopic.removesuffix("/state") + "/message",
        "min": 1,
        "max": 255,
        "mode": "text",
        "retain": False,
        "icon": "mdi:message-text",
    }

    device: dict[str, Any] = {
        "identifiers": [f"findmy2mqtt:{account.slug}:{deviceId}"],
        "name": str(payload.get("name") or deviceId),
        "manufacturer": "Apple",
    }
    model = payload.get("modelName") or payload.get("model")
    if model:
        device["model"] = str(model)

    discoveryTopic = (
        f"{config.mqtt.discoveryPrefix}/device/{clientId}/config"
    )
    discoveryPayload = {
        "device": device,
        "origin": {
            "name": "findmy2mqtt",
            "sw_version": __version__,
            "support_url": "https://github.com/next81/findmy2mqtt",
        },
        "components": components,
        "qos": config.mqtt.qos,
    }
    return discoveryTopic, discoveryPayload


def configure_client_auth(client: mqtt.Client, config: Config) -> None:
    # Alle MQTT-Clients verwenden identische Zugangsdaten und TLS-Einstellungen.
    # Die zentrale Funktion verhindert Abweichungen zwischen Publish und Subscribe.
    if config.mqtt.username:
        client.username_pw_set(
            config.mqtt.username,
            mqtt_password(config),
        )

    if config.mqtt.tls:
        client.tls_set()


def command_topics(config: Config) -> tuple[str, str]:
    # Nur tatsächlich unterstützte Befehle abonnieren. Ein generisches #
    # würde auch die eigenen /state-Publishes als Commands empfangen.
    prefix = config.mqtt.topicPrefix
    return (
        f"{prefix}/+/+/locate",
        f"{prefix}/+/+/message",
    )


def parse_command_topic(
    config: Config,
    topic: str,
) -> tuple[str, str, str] | None:
    prefixParts = config.mqtt.topicPrefix.split("/")
    topicParts = topic.split("/")

    # Nach dem Prefix müssen immer genau Account, deviceId und Befehl folgen.
    if len(topicParts) != len(prefixParts) + 3:
        return None

    if topicParts[: len(prefixParts)] != prefixParts:
        return None

    accountSlug, deviceId, command = topicParts[-3:]
    if not accountSlug or not deviceId or not command:
        return None

    return accountSlug, deviceId, command


class DevicePublisher:
    def __init__(
        self,
        config: Config,
        account: AccountConfig,
        deviceId: str,
    ) -> None:
        self.config = config
        self.account = account
        self.deviceId = deviceId
        # deviceKey wird ausschließlich für die kompakte MQTT-Client-ID benötigt;
        # die vollständige Apple-deviceId bleibt im Topic und in der Payload sichtbar.
        self.deviceKey = short_hash(f"{account.slug}:{deviceId}", 12)

        # Nur die MQTT-Client-ID wird gekürzt/gehasht. Im Topic steht bewusst
        # die echte Apple-deviceId, damit sie direkt für Befehle nutzbar ist.
        self.clientId = f"fm_{account.slug[:12]}_{self.deviceKey[:10]}"
        self.topic = (
            f"{config.mqtt.topicPrefix}/"
            f"{account.slug}/"
            f"{deviceId}/state"
        )
        self.discoveryTopic = (
            f"{config.mqtt.discoveryPrefix}/device/{self.clientId}/config"
        )

        self._connected = threading.Event()
        # Pro Apple-Gerät wird ein eigener MQTT-Client verwendet. MQTT2_SERVER kann
        # eingehende Nachrichten dadurch über die Client-ID einem Device zuordnen.
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.clientId,
            protocol=mqtt.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        configure_client_auth(self._client, config)
        self._started = False
        self._startupMessagesPublished = False

    def _on_connect(
        self,
        client,
        userdata,
        flags,
        reasonCode,
        properties,
    ) -> None:
        if reasonCode == 0:
            LOG.info("MQTT connected: %s", self.clientId)
            self._connected.set()
        else:
            LOG.error(
                "MQTT connect rejected for %s: %s",
                self.clientId,
                reasonCode,
            )
            self._connected.clear()

    def _on_disconnect(
        self,
        client,
        userdata,
        disconnectFlags,
        reasonCode,
        properties,
    ) -> None:
        self._connected.clear()

        if reasonCode != 0:
            LOG.warning(
                "MQTT disconnected for %s: %s",
                self.clientId,
                reasonCode,
            )

    def ensure_connected(self) -> None:
        # Lazy Connect: Ein MQTT-Client wird erst geöffnet, wenn für das Gerät
        # tatsächlich erstmals ein Status publiziert werden soll.
        if self._started:
            return

        self._client.connect(
            self.config.mqtt.host,
            self.config.mqtt.port,
            self.config.mqtt.keepalive,
        )
        self._client.loop_start()
        self._started = True

        if not self._connected.wait(timeout=8):
            self.close()
            raise RuntimeError(
                f"MQTT connection timeout for client {self.clientId}"
            )

    def publish(self, payload: Mapping[str, Any]) -> None:
        self.ensure_connected()

        if not self._startupMessagesPublished:
            discoveryTopic, discoveryPayload = home_assistant_discovery(
                self.config,
                self.account,
                self.deviceId,
                self.clientId,
                self.topic,
                payload,
            )
            discoveryBody = json.dumps(
                discoveryPayload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            discoveryInfo = self._client.publish(
                discoveryTopic,
                discoveryBody,
                qos=self.config.mqtt.qos,
                retain=True,
            )
            if discoveryInfo.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(
                    f"MQTT discovery publish failed for "
                    f"{self.clientId}: rc={discoveryInfo.rc}"
                )

            self._startupMessagesPublished = True

        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        # Standort und Gerätestatus können schnell veralten und werden deshalb
        # nicht im Broker gespeichert.
        info = self._client.publish(
            self.topic,
            body,
            qos=self.config.mqtt.qos,
            retain=False,
        )

        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(
                f"MQTT publish failed for {self.clientId}: rc={info.rc}"
            )

    def close(self) -> None:
        if not self._started:
            return

        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._started = False
            self._startupMessagesPublished = False
            self._connected.clear()


class CommandSubscriber:
    def __init__(
        self,
        config: Config,
        commandHandler: Callable[[str, str, str, bytes], None],
    ) -> None:
        self.config = config
        self.commandHandler = commandHandler
        self._connected = threading.Event()
        self._started = False

        hostSlug = slugify(socket.gethostname(), maxlen=12)
        # Der Control-Client gehört zum Dienst, nicht zu einem einzelnen Gerät.
        # Der Hostname verhindert Kollisionen bei mehreren findmy2mqtt-Instanzen.
        self.clientId = f"fm_control_{hostSlug}"
        self.topics = command_topics(config)

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.clientId,
            protocol=mqtt.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        configure_client_auth(self._client, config)

    def _on_connect(
        self,
        client,
        userdata,
        flags,
        reasonCode,
        properties,
    ) -> None:
        if reasonCode != 0:
            LOG.error("MQTT command client rejected: %s", reasonCode)
            self._connected.clear()
            return

        # Bei jeder neuen Verbindung erneut abonnieren, weil der Client
        # absichtlich keine dauerhafte Broker-Session voraussetzt.
        subscriptions = [
            (topic, self.config.mqtt.qos)
            for topic in self.topics
        ]
        result, messageId = client.subscribe(subscriptions)

        if result != mqtt.MQTT_ERR_SUCCESS:
            LOG.error(
                "MQTT subscribe failed for %s: rc=%s",
                ", ".join(self.topics),
                result,
            )
            self._connected.clear()
            return

        LOG.info(
            "MQTT command listener subscribed: %s",
            ", ".join(self.topics),
        )
        self._connected.set()

    def _on_disconnect(
        self,
        client,
        userdata,
        disconnectFlags,
        reasonCode,
        properties,
    ) -> None:
        self._connected.clear()

        if reasonCode != 0:
            LOG.warning("MQTT command client disconnected: %s", reasonCode)

    def _on_message(self, client, userdata, message) -> None:
        # Befehle dürfen nie retained ausgeführt werden: Ein alter Locate-Befehl
        # könnte sonst nach jedem Reconnect erneut ein Gerät klingeln lassen.
        if message.retain:
            LOG.warning("Ignoring retained command on %s", message.topic)
            return

        # Die Syntax wird strikt geprüft, bevor irgendeine Apple-Aktion ausgelöst
        # wird. Zufällige Nachrichten unter dem Prefix können so nichts ausführen.
        parsed = parse_command_topic(self.config, message.topic)
        if parsed is None:
            LOG.warning("Ignoring malformed command topic: %s", message.topic)
            return

        accountSlug, deviceId, command = parsed

        # locate benötigt keine Payload. message reicht die unveränderten Bytes
        # an den Worker weiter, damit JSON und einfacher UTF-8-Text möglich bleiben.
        self.commandHandler(
            accountSlug,
            deviceId,
            command,
            bytes(message.payload),
        )

    def start(self) -> None:
        # Der Subscriber muss vor dem ersten Poll aktiv sein, damit locate-Kommandos
        # auch während längerer Apple-Abfragen angenommen und gequeued werden können.
        if self._started:
            return

        self._client.connect(
            self.config.mqtt.host,
            self.config.mqtt.port,
            self.config.mqtt.keepalive,
        )
        self._client.loop_start()
        self._started = True

        if not self._connected.wait(timeout=8):
            self.close()
            raise RuntimeError("MQTT command listener connection timeout")

    def close(self) -> None:
        if not self._started:
            return

        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._started = False
            self._connected.clear()

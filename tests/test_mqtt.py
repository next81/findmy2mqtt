# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Regressionstests für MQTT-Status und Home-Assistant-Discovery."""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


try:
    import paho.mqtt.client as mqttClientModule
except ImportError:
    pahoModule = types.ModuleType("paho")
    pahoModule.__path__ = []
    mqttPackage = types.ModuleType("paho.mqtt")
    mqttPackage.__path__ = []
    mqttClientModule = types.ModuleType("paho.mqtt.client")
    mqttPackage.client = mqttClientModule
    pahoModule.mqtt = mqttPackage
    sys.modules["paho"] = pahoModule
    sys.modules["paho.mqtt"] = mqttPackage
    sys.modules["paho.mqtt.client"] = mqttClientModule


if not hasattr(mqttClientModule, "CallbackAPIVersion"):
    mqttClientModule.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
if not hasattr(mqttClientModule, "MQTTv311"):
    mqttClientModule.MQTTv311 = 4
if not hasattr(mqttClientModule, "MQTT_ERR_SUCCESS"):
    mqttClientModule.MQTT_ERR_SUCCESS = 0
if not hasattr(mqttClientModule, "Client"):
    mqttClientModule.Client = Mock


from lib.config import AccountConfig, Config, MqttConfig
from lib.mqtt import DevicePublisher, HOME_ASSISTANT_SENSORS


class DevicePublisherTests(unittest.TestCase):
    def make_config(self) -> Config:
        return Config(
            mqtt=MqttConfig(
                host="127.0.0.1",
                qos=1,
                discoveryPrefix="homeassistant",
            ),
            pollInterval=300,
            stateDir=Path("/tmp/findmy2mqtt-tests"),
            accounts=(),
        )

    @patch("lib.mqtt.mqtt.Client")
    def test_first_publish_sends_discovery_and_non_retained_state(self, clientClass) -> None:
        client = clientClass.return_value
        client.publish.return_value = types.SimpleNamespace(
            rc=mqttClientModule.MQTT_ERR_SUCCESS
        )
        config = self.make_config()
        account = AccountConfig(
            name="Person 1",
            appleId="person1@example.com",
            passwordFile="person1.password",
        )
        publisher = DevicePublisher(config, account, "ABCDEF123456")
        publisher.ensure_connected = Mock()
        payload = {
            "state": "located",
            "name": "Person1 iPhone",
            "account": "person_1",
            "deviceId": "ABCDEF123456",
            "deviceClass": "iPhone",
            "model": "iPhone17,1",
            "modelName": "iPhone 16 Pro",
            "battery": 83,
            "locationOld": 0,
            "latitude": 52.1,
            "longitude": 9.1,
            "accuracy": 8,
        }

        publisher.publish(payload)

        calls = client.publish.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[0], publisher.discoveryTopic)
        self.assertTrue(calls[0].kwargs["retain"])
        discovery = json.loads(calls[0].args[1])
        components = discovery["components"]
        expectedSensors = {
            f"sensor_{field}" for field, _name, _options in HOME_ASSISTANT_SENSORS
        }
        self.assertTrue(expectedSensors.issubset(components))
        self.assertIn("binary_sensor_locationOld", components)
        self.assertIn("device_tracker_location", components)
        self.assertEqual(
            components["button_locate"]["command_topic"],
            "findmy/person_1/ABCDEF123456/locate",
        )
        self.assertEqual(
            components["text_message"]["command_topic"],
            "findmy/person_1/ABCDEF123456/message",
        )

        self.assertEqual(calls[1].args[0], publisher.topic)
        self.assertFalse(calls[1].kwargs["retain"])
        self.assertEqual(json.loads(calls[1].args[1]), payload)

        publisher.publish(payload)

        self.assertEqual(len(client.publish.call_args_list), 3)
        self.assertFalse(client.publish.call_args_list[2].kwargs["retain"])


if __name__ == "__main__":
    unittest.main()

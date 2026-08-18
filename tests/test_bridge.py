# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Regressionstests für die Account-Behandlung der Bridge."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# Die Bridge-Tests benötigen keine echten Apple- oder MQTT-Clients. Die Stubs
# halten die Tests auch auf Entwicklungsrechnern ohne Laufzeitabhängigkeiten
# importierbar.
try:
    import pyicloud  # noqa: F401
except ImportError:
    pyicloudModule = types.ModuleType("pyicloud")
    pyicloudModule.PyiCloudService = type("PyiCloudService", (), {})
    sys.modules["pyicloud"] = pyicloudModule

try:
    import paho.mqtt.client  # noqa: F401
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

try:
    import yaml  # noqa: F401
except ImportError:
    yamlModule = types.ModuleType("yaml")
    yamlModule.safe_load = lambda stream: None
    sys.modules["yaml"] = yamlModule

from lib.bridge import Bridge
from lib.config import AccountConfig, Config, MqttConfig, account_password_path


class BridgeRefreshTests(unittest.TestCase):
    def make_config(self, stateDir: Path) -> Config:
        return Config(
            mqtt=MqttConfig(host="127.0.0.1"),
            pollInterval=300,
            stateDir=stateDir,
            accounts=(
                AccountConfig(
                    name="Miriam",
                    appleId="miriam@example.com",
                    passwordFile="miriam.password",
                ),
            ),
        )

    def test_missing_password_skips_account_without_refresh_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporaryDirectory:
            config = self.make_config(Path(temporaryDirectory))
            bridge = Bridge(config)

            with (
                patch.object(bridge, "refresh_account") as refreshAccount,
                self.assertLogs("findmy2mqtt.bridge", level="WARNING") as logs,
            ):
                ok = bridge.refresh_all()

            self.assertTrue(ok)
            refreshAccount.assert_not_called()
            self.assertIn("password file is not configured yet", logs.output[0])
            self.assertNotIn("Traceback", logs.output[0])

    def test_configured_password_still_refreshes_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporaryDirectory:
            config = self.make_config(Path(temporaryDirectory))
            account = config.accounts[0]
            passwordPath = account_password_path(config, account)
            passwordPath.parent.mkdir(parents=True)
            passwordPath.write_text("secret", encoding="utf-8")
            bridge = Bridge(config)

            with patch.object(
                bridge,
                "refresh_account",
                return_value=[],
            ) as refreshAccount:
                ok = bridge.refresh_all(publish=False)

            self.assertTrue(ok)
            refreshAccount.assert_called_once_with(account, publish=False)


if __name__ == "__main__":
    unittest.main()

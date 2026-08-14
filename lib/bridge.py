# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Zentrale Laufzeitsteuerung von findmy2mqtt.

Hier werden die unabhängigen Apple- und MQTT-Module zusammengeführt: periodisches
Status-Polling, Publisher-Lebenszyklus und die serielle Abarbeitung von Commands.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any

from .apple import AppleAccount
from .config import AccountConfig, Config, account_password_path
from .device import device_payload
from .mqtt import CommandSubscriber, DevicePublisher
from .utils import slugify

LOG = logging.getLogger("findmy2mqtt.bridge")


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        # Die AppleAccount-Objekte leben über mehrere Polls hinweg, damit gültige
        # pyicloud-Sessions und deren interner Gerätestand wiederverwendet werden.
        self.appleAccounts = {
            account.slug: AppleAccount(config, account)
            for account in config.accounts
        }
        self.publishers: dict[tuple[str, str], DevicePublisher] = {}
        self.stopEvent = threading.Event()

        # MQTT-Callbacks dürfen den Netzwerkthread nicht mit Apple-HTTP-Aufrufen
        # blockieren. Kommandos werden deshalb in einem eigenen Worker verarbeitet.
        self.commandQueue: queue.Queue[tuple[str, str, str, bytes] | None] = queue.Queue()
        self.commandThread: threading.Thread | None = None
        self.commandSubscriber: CommandSubscriber | None = None

    def stop(self, *_args: Any) -> None:
        self.stopEvent.set()

    def refresh_account(
        self,
        account: AccountConfig,
        *,
        publish: bool = True,
    ) -> list[dict[str, Any]]:
        apple = self.appleAccounts[account.slug]
        result: list[dict[str, Any]] = []

        # Ein Poll eines Accounts liefert alle Geräte in einem Snapshot. Jedes
        # Gerät wird anschließend unabhängig normalisiert und über seinen Client publiziert.
        for deviceId, device in apple.devices():
            payload = device_payload(account, deviceId, device)
            result.append(payload)

            if not publish:
                continue

            # Publisher werden anhand Account + deviceId gecacht. Dadurch bleibt
            # die MQTT-Client-ID über alle folgenden Polls derselben Laufzeit stabil.
            key = (account.slug, deviceId)
            publisher = self.publishers.get(key)

            if publisher is None:
                publisher = DevicePublisher(
                    self.config,
                    account,
                    deviceId,
                )
                self.publishers[key] = publisher

            publisher.publish(payload)
            LOG.info(
                "Published %s (%s) -> %s",
                payload.get("name", deviceId),
                account.name,
                publisher.topic,
            )

        return result

    def refresh_all(self, *, publish: bool = True) -> bool:
        ok = True

        for account in self.config.accounts:
            passwordPath = account_password_path(self.config, account)

            # Ein Account darf bereits konfiguriert werden, bevor sein Passwort
            # interaktiv hinterlegt wurde. Dieser erwartete Einrichtungszustand
            # ist kein Refresh-Fehler und soll daher keinen Stacktrace erzeugen.
            if not passwordPath.exists():
                LOG.warning(
                    "Skipping Apple account %s: password file is not "
                    "configured yet (%s)",
                    account.name,
                    passwordPath,
                )
                continue

            try:
                self.refresh_account(account, publish=publish)
            except Exception:
                ok = False
                apple = self.appleAccounts.get(account.slug)

                if apple is not None:
                    # Nur das API-Objekt verwerfen; persistierte Sessiondaten bleiben
                    # erhalten und können beim nächsten Versuch wiederverwendet werden.
                    apple.reset()

                LOG.exception(
                    "Refresh failed for Apple account %s",
                    account.name,
                )

        return ok

    # Diese Methode wird direkt aus Pahos Netzwerkthread aufgerufen und darf
    # deshalb nur schnell enqueuen, niemals selbst Apple-HTTP ausführen.
    def enqueue_command(
        self,
        accountSlug: str,
        deviceId: str,
        command: str,
        payload: bytes,
    ) -> None:
        self.commandQueue.put((accountSlug, deviceId, command, payload))

    def _command_worker(self) -> None:
        while not self.stopEvent.is_set():
            try:
                queuedCommand = self.commandQueue.get(timeout=1.0)
            except queue.Empty:
                continue

            if queuedCommand is None:
                self.commandQueue.task_done()
                return

            accountSlug, deviceId, command, payload = queuedCommand
            accountSlug = slugify(accountSlug)

            try:
                apple = self.appleAccounts.get(accountSlug)
                if apple is None:
                    raise RuntimeError(
                        f"unknown Apple account in MQTT topic: {accountSlug}"
                    )

                # Der Dispatcher ist bewusst explizit. Neue MQTT-Befehle können
                # später hier ergänzt werden, ohne den Topic-Parser zu verändern.
                if command == "locate":
                    locatedDeviceId, deviceName = apple.locate(deviceId)
                    LOG.info(
                        "MQTT locate request sent to %s [%s] via account %s",
                        deviceName,
                        locatedDeviceId,
                        accountSlug,
                    )

                elif command == "message":
                    options = self._parse_message_payload(payload)
                    messageDeviceId, deviceName = apple.display_message(
                        deviceId,
                        options["message"],
                        subject=options["subject"],
                        sound=options["sound"],
                        vibrate=options["vibrate"],
                        strobe=options["strobe"],
                    )
                    LOG.info(
                        "MQTT message sent to %s [%s] via account %s",
                        deviceName,
                        messageDeviceId,
                        accountSlug,
                    )

                else:
                    LOG.warning(
                        "Ignoring unsupported MQTT command '%s' for %s/%s",
                        command,
                        accountSlug,
                        deviceId,
                    )
            except Exception:
                LOG.exception(
                    "MQTT command failed: account=%s deviceId=%s command=%s",
                    accountSlug,
                    deviceId,
                    command,
                )
            finally:
                self.commandQueue.task_done()

    @staticmethod
    def _parse_message_payload(payload: bytes) -> dict[str, Any]:
        try:
            text = payload.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError("message payload must be valid UTF-8") from exc

        if not text:
            raise RuntimeError("message payload must not be empty")

        # Einfacher Text ist für Smart-Home-Regeln bequem. JSON wird nur dann
        # benötigt, wenn Betreff, Ton, Vibration oder Strobe gesetzt werden sollen.
        if not text.startswith("{"):
            return {
                "message": text,
                "subject": "findmy2mqtt",
                "sound": False,
                "vibrate": False,
                "strobe": False,
            }

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid JSON message payload") from exc

        if not isinstance(data, dict):
            raise RuntimeError("JSON message payload must be an object")

        message = str(data.get("message", "")).strip()
        if not message:
            raise RuntimeError("JSON message payload requires 'message'")

        return {
            "message": message,
            "subject": str(data.get("subject", "findmy2mqtt")).strip() or "findmy2mqtt",
            "sound": bool(data.get("sound", False)),
            "vibrate": bool(data.get("vibrate", False)),
            "strobe": bool(data.get("strobe", False)),
        }

    def _start_command_listener(self) -> None:
        # Ein einzelner Worker serialisiert Commands. Das vermeidet parallele
        # Aktionen auf demselben pyicloud-Account und vereinfacht Fehlerbehandlung.
        self.commandThread = threading.Thread(
            target=self._command_worker,
            name="findmy2mqtt-command-worker",
            daemon=True,
        )
        self.commandThread.start()

        self.commandSubscriber = CommandSubscriber(
            self.config,
            self.enqueue_command,
        )
        self.commandSubscriber.start()

    def run(self) -> None:
        LOG.info(
            "findmy2mqtt starting; poll interval=%ss",
            self.config.pollInterval,
        )

        # Status-Polling und MQTT-Kommandos laufen parallel.
        self._start_command_listener()

        try:
            while not self.stopEvent.is_set():
                # monotonic() verhindert, dass NTP-/Zeitsprünge das Poll-Intervall
                # verlängern oder verkürzen. Die Apple-Abfragedauer wird abgezogen.
                started = time.monotonic()
                self.refresh_all(publish=True)
                elapsed = time.monotonic() - started
                delay = max(1.0, self.config.pollInterval - elapsed)
                self.stopEvent.wait(delay)
        finally:
            self.close()

        LOG.info("findmy2mqtt stopped")

    def close(self) -> None:
        self.stopEvent.set()

        if self.commandSubscriber is not None:
            try:
                self.commandSubscriber.close()
            except Exception:
                LOG.exception("Error while closing MQTT command listener")
            self.commandSubscriber = None

        if self.commandThread is not None:
            # Sentinel beendet den Worker auch dann sofort, wenn gerade keine
            # neuen MQTT-Nachrichten eintreffen.
            self.commandQueue.put(None)
            self.commandThread.join(timeout=5)
            self.commandThread = None

        for publisher in list(self.publishers.values()):
            try:
                publisher.close()
            except Exception:
                LOG.exception("Error while closing MQTT client")

        self.publishers.clear()

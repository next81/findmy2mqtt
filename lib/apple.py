# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Apple-/pyicloud-Anbindung von findmy2mqtt.

Dieses Modul kapselt Session-Aufbau, 2FA, Geräteabfrage und ``locate``. Andere
Module müssen dadurch keine pyicloud-spezifischen Methoden oder Feldnamen kennen.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Mapping

try:
    from pyicloud import PyiCloudService
except ImportError as exc:
    raise SystemExit(
        "pyicloud is not installed. Install requirements.txt in the virtualenv."
    ) from exc

from .utils import decode_device_id, encode_device_id

from .config import (
    AccountConfig,
    Config,
    account_password_path,
    read_secret_file,
)

LOG = logging.getLogger("findmy2mqtt.apple")


def session_dir(config: Config, account: AccountConfig) -> Path:
    # Jeder Apple-Account erhält ein eigenes Cookie-/Session-Verzeichnis.
    # Mehrere Personen beeinflussen dadurch ihre Authentifizierung nicht gegenseitig.
    path = config.stateDir / "sessions" / account.slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_api(
    config: Config,
    account: AccountConfig,
    *,
    authenticate: bool = True,
) -> PyiCloudService:
    password = read_secret_file(account_password_path(config, account))

    # PyiCloudService liest die persistierten Cookies aus dem Account-Verzeichnis.
    # Eine noch gültige vertrauenswürdige Session vermeidet dadurch neue 2FA-Abfragen.
    return PyiCloudService(
        account.appleId,
        password=password,
        cookie_directory=str(session_dir(config, account)),
        # Öffentlich heißt die Option familyDevices. pyicloud erwartet intern
        # weiterhin den vorgegebenen Parameter with_family.
        with_family=account.familyDevices,
        # findmy2mqtt steuert sein Polling selbst; der pyicloud-Monitor läuft
        # bewusst seltener und dient nur als Fallback.
        refresh_interval=max(config.pollInterval * 2, 60),
        authenticate=authenticate,
    )


def interactive_auth(config: Config, account: AccountConfig) -> None:
    # Authentifizierung bleibt absichtlich ein interaktiver CLI-Schritt und läuft
    # nicht im Daemon. Ein Service-Neustart kann daher niemals auf Eingaben warten.
    api = create_api(config, account)
    LOG.info("Apple login succeeded for %s", account.name)

    securityKeys = getattr(api, "security_key_names", None)
    if securityKeys:
        LOG.warning(
            "Account %s has security keys registered: %s",
            account.name,
            ", ".join(map(str, securityKeys)),
        )

    # Apple zeigt beim Login häufig bereits automatisch einen Code auf einem
    # vertrauenswürdigen Gerät an. Diesen verwenden wir zuerst und fordern nicht
    # ungefragt einen zweiten Code per SMS oder Geräte-Prompt an.
    if getattr(api, "requires_2fa", False):
        LOG.info("Two-factor authentication is required.")

        code = input(
            "Apple verification code (Enter = request new code): "
        ).strip()

        if not code:
            requestCode = getattr(api, "request_2fa_code", None)
            if not callable(requestCode) or not requestCode():
                raise RuntimeError("Apple could not request a new verification code")

            deliveryMethod = getattr(api, "two_factor_delivery_method", "unknown")
            if deliveryMethod and deliveryMethod != "unknown":
                print(f"Verification code requested via: {deliveryMethod}")

            code = input("New Apple verification code: ").strip()
            if not code:
                raise RuntimeError("empty verification code")

        validateCode = getattr(api, "validate_2fa_code", None)
        if not callable(validateCode) or not validateCode(code):
            raise RuntimeError("Apple rejected the verification code")

        # pyicloud 2.6.5 vertraut die Session nach erfolgreicher Code-Prüfung
        # bereits in validate_2fa_code(); ein zweiter Trust-Aufruf ist unnötig.
        LOG.info("2FA completed for %s", account.name)

    # Kompatibilitätsweg für ältere Accounts mit Apples früherem 2SA-Verfahren.
    if getattr(api, "requires_2sa", False):
        trustedDevices = list(getattr(api, "trusted_devices", []) or [])

        if not trustedDevices:
            raise RuntimeError(
                "legacy two-step verification is required "
                "but no trusted device is available"
            )

        print("Trusted devices:")
        for index, trustedDevice in enumerate(trustedDevices):
            label = (
                trustedDevice.get("deviceName")
                or trustedDevice.get("phoneNumber")
                or str(trustedDevice)
            )
            print(f"  [{index}] {label}")

        choice = input("Device [0]: ").strip() or "0"
        trustedDevice = trustedDevices[int(choice)]
        sendCode = getattr(api, "send_verification_code", None)
        validateCode = getattr(api, "validate_verification_code", None)

        if not callable(sendCode) or not callable(validateCode):
            raise RuntimeError(
                "installed pyicloud does not expose legacy 2SA methods"
            )

        if not sendCode(trustedDevice):
            raise RuntimeError("could not request legacy verification code")

        code = input("Apple verification code: ").strip()
        if not validateCode(trustedDevice, code):
            raise RuntimeError("Apple rejected the legacy verification code")

        LOG.info("Legacy verification completed for %s", account.name)


class AppleAccount:
    def __init__(self, config: Config, account: AccountConfig) -> None:
        self.config = config
        self.account = account
        self._api: PyiCloudService | None = None

        # Polling und MQTT-Kommandos können parallel eintreffen. pyicloud-Zugriffe
        # eines Accounts werden deshalb serialisiert, um Session-Rennen zu vermeiden.
        self._lock = threading.RLock()

    def reset(self) -> None:
        with self._lock:
            self._api = None

    def api(self) -> PyiCloudService:
        # Ein API-Objekt wird pro Account wiederverwendet, solange die Session
        # funktioniert. ``reset()`` verwirft nur dieses Objekt, nicht die Cookies.
        with self._lock:
            if self._api is None:
                api = create_api(self.config, self.account)

                if (
                    getattr(api, "requires_2fa", False)
                    or getattr(api, "requires_2sa", False)
                ):
                    raise RuntimeError(
                        f"{self.account.name}: authentication required; "
                        f"run 'findmy2mqtt auth {self.account.name}'"
                    )

                self._api = api

            return self._api

    def devices(self) -> list[tuple[str, Any]]:
        # Refresh und Auslesen bleiben unter demselben Lock, damit ein paralleler
        # MQTT-locate-Aufruf nicht in pyiclouds internen Gerätestand hineinläuft.
        with self._lock:
            manager = self.api().devices
            self._refresh_manager(manager)
            return self._device_items(manager)

    def locate(self, selector: str) -> tuple[str, str]:
        # Die CLI akzeptiert Anzeigenamen, die öffentliche topic-sichere deviceId
        # sowie rohe Apple-IDs aus älteren Ausgaben.
        selector = selector.strip()
        selectorNormalized = selector.casefold()
        if not selectorNormalized:
            raise RuntimeError("device selector must not be empty")

        decodedSelector = decode_device_id(selector).casefold()

        with self._lock:
            manager = self.api().devices
            self._refresh_manager(manager)
            devices = getattr(manager, "devices", manager)
            if callable(devices):
                devices = devices()

            idMatches: list[tuple[str, Any]] = []
            nameMatches: list[tuple[str, Any]] = []

            if isinstance(devices, Mapping) or hasattr(devices, "items"):
                iterable = list(devices.items())
            else:
                iterable = [(str(index), device) for index, device in enumerate(devices)]

            for collectionKey, device in iterable:
                rawDeviceId = self._raw_device_id(device, str(collectionKey))
                publicDeviceId = encode_device_id(rawDeviceId)

                # Neben der neuen topic-sicheren ID bleiben die rohe Apple-ID und
                # der frühere Collection-Key als Kompatibilitäts-Aliase gültig.
                idAliases = {
                    publicDeviceId.casefold(),
                    rawDeviceId.casefold(),
                    str(collectionKey).casefold(),
                }

                if (
                    selectorNormalized in idAliases
                    or decodedSelector == rawDeviceId.casefold()
                ):
                    idMatches.append((publicDeviceId, device))
                    continue

                deviceName = self._device_name(device, publicDeviceId)
                if deviceName.casefold() == selectorNormalized:
                    nameMatches.append((publicDeviceId, device))

            matches = idMatches or nameMatches

            if not matches:
                raise RuntimeError(
                    f"device '{selector}' not found in Apple account "
                    f"'{self.account.name}'"
                )

            if len(matches) > 1:
                choices = ", ".join(
                    f"{self._device_name(device, deviceId)} [{deviceId}]"
                    for deviceId, device in matches
                )
                raise RuntimeError(
                    f"device name '{selector}' is ambiguous; "
                    f"use the device ID: {choices}"
                )

            deviceId, device = matches[0]
            deviceName = self._device_name(device, deviceId)

            # play_sound() verwendet intern weiterhin die rohe Apple-ID aus dem
            # pyicloud-Geräteobjekt; die topic-sichere ID muss hier nicht zurückgeschrieben werden.
            playSound = getattr(device, "play_sound", None)
            if not callable(playSound):
                raise RuntimeError(
                    f"locate is not supported for device '{deviceName}'"
                )

            playSound()
            return deviceId, deviceName

    @staticmethod
    def _device_name(device: Any, deviceId: str) -> str:
        name = getattr(device, "name", None)
        if callable(name):
            name = name()
        if name:
            return str(name).strip()

        data = getattr(device, "data", {}) or {}
        if callable(data):
            data = data()
        if isinstance(data, Mapping):
            fallback = data.get("name") or data.get("deviceDisplayName")
            if fallback:
                return str(fallback).strip()

        return deviceId

    @staticmethod
    def _refresh_manager(manager: Any) -> None:
        refresh = getattr(manager, "refresh", None)

        if callable(refresh):
            try:
                refresh(locate=True)
            except TypeError:
                # Fallback für pyicloud-Versionen ohne locate-Argument.
                refresh()

    @staticmethod
    def _raw_device_id(device: Any, fallback: str | None = None) -> str:
        # play_sound() sendet bei pyicloud exakt das Rohdatenfeld ``id`` an Apple.
        # Dieses Feld bleibt intern unverändert; erst an der MQTT-Grenze wird es kodiert.
        data = getattr(device, "data", {}) or {}
        if callable(data):
            data = data()

        if isinstance(data, Mapping):
            rawId = data.get("id")
            if rawId not in (None, ""):
                return str(rawId)

        if fallback not in (None, ""):
            return str(fallback)

        raise RuntimeError("Apple device does not contain a device ID")

    @classmethod
    def _device_items(cls, manager: Any) -> list[tuple[str, Any]]:
        # Bei aktuellen pyicloud-Versionen ist manager.devices ein Mapping, dessen
        # Key ebenfalls die Apple-ID ist. Wir lesen trotzdem das Rohdatenfeld aus,
        # damit die ID exakt der von play_sound() verwendeten ID entspricht.
        devices = getattr(manager, "devices", manager)
        if callable(devices):
            devices = devices()

        if isinstance(devices, Mapping) or hasattr(devices, "items"):
            result: list[tuple[str, Any]] = []
            for key, device in devices.items():
                result.append((encode_device_id(cls._raw_device_id(device, str(key))), device))
            return result

        result: list[tuple[str, Any]] = []
        for index, device in enumerate(devices):
            result.append((encode_device_id(cls._raw_device_id(device, str(index))), device))

        return result

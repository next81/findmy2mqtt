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
MAX_2FA_ATTEMPTS = 3


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


def _security_key_names(api: PyiCloudService) -> list[str]:
    return [
        str(name)
        for name in (getattr(api, "security_key_names", None) or [])
    ]


def _print_2fa_delivery(api: PyiCloudService) -> None:
    deliveryMethod = getattr(api, "two_factor_delivery_method", "unknown")
    if deliveryMethod and deliveryMethod != "unknown":
        print(f"Verification code requested via: {deliveryMethod}")

    deliveryNotice = getattr(api, "two_factor_delivery_notice", None)
    if deliveryNotice:
        print(str(deliveryNotice))


def _request_2fa_code(
    api: PyiCloudService,
    account: AccountConfig,
) -> bool:
    """Start pyicloud's active 2FA route, refreshing stale auth state once."""
    requestCode = getattr(api, "request_2fa_code", None)
    if not callable(requestCode):
        raise RuntimeError(
            "installed pyicloud does not expose 2FA code delivery"
        )

    refreshed = False

    # Ein gültiges, aber noch nicht vertrautes Session-Token kann einen alten
    # Prozess überleben. Der für die Code-Prüfung benötigte Bridge-Kontext wird
    # von pyicloud dagegen absichtlich nicht persistiert. Ohne Delivery-Methode
    # würde request_2fa_code() erst nach einem langen Bridge-Timeout scheitern.
    if getattr(api, "two_factor_delivery_method", "unknown") == "unknown":
        refreshed = True
        if not _refresh_2fa_state(api, account):
            return False

    def request_active_code() -> bool:
        # Der Trusted-Device-Ablauf wartet auf Apples Push-Antwort und wechselt
        # erst nach einem Timeout auf SMS. Der Hinweis muss vor dem blockierenden
        # Aufruf sichtbar sein, damit eine vorschnelle Eingabe nicht im
        # Terminalpuffer landet.
        print(
            "Requesting a new Apple verification code. This can take up to "
            "30 seconds; wait for the code prompt before pressing Enter.",
            flush=True,
        )
        return bool(requestCode())

    if request_active_code():
        _print_2fa_delivery(api)
        return True

    if not refreshed:
        if not _refresh_2fa_state(api, account):
            return False
        if request_active_code():
            _print_2fa_delivery(api)
            return True

    raise RuntimeError("Apple could not request a new verification code")


def _read_2fa_code() -> str:
    """Read a non-empty code without treating a buffered Enter as failure."""
    while True:
        code = input(
            "Apple verification code from the latest request: "
        ).strip()
        if code:
            return code

        print(
            "No verification code entered. Wait for the latest code and "
            "try again.",
            flush=True,
        )


def _refresh_2fa_state(
    api: PyiCloudService,
    account: AccountConfig,
) -> bool:
    """Create fresh in-memory HSA2 challenge state for an untrusted session."""
    authenticate = getattr(api, "authenticate", None)
    if not callable(authenticate):
        raise RuntimeError("Apple could not start two-factor authentication")

    LOG.info(
        "Stored Apple session for %s cannot continue 2FA; "
        "starting a fresh authentication challenge.",
        account.name,
    )
    authenticate(force_refresh=True)

    if not getattr(api, "requires_2fa", False):
        return False

    securityKeys = _security_key_names(api)
    if securityKeys:
        raise RuntimeError(
            f"{account.name}: Apple requires a registered security key; "
            "numeric verification codes are not available"
        )

    return True


def interactive_auth(config: Config, account: AccountConfig) -> None:
    # Authentifizierung bleibt absichtlich ein interaktiver CLI-Schritt und läuft
    # nicht im Daemon. Ein Service-Neustart kann daher niemals auf Eingaben warten.
    api = create_api(config, account)
    LOG.info("Apple login succeeded for %s", account.name)

    securityKeys = _security_key_names(api)
    if securityKeys:
        raise RuntimeError(
            f"{account.name}: Apple requires a registered security key "
            f"({', '.join(securityKeys)}); findmy2mqtt currently supports "
            "numeric verification codes only"
        )

    if getattr(api, "requires_2fa", False):
        LOG.info("Two-factor authentication is required.")

        for attempt in range(1, MAX_2FA_ATTEMPTS + 1):
            if not _request_2fa_code(api, account):
                LOG.info("Apple session for %s is already trusted.", account.name)
                break

            code = _read_2fa_code()

            validateCode = getattr(api, "validate_2fa_code", None)
            if not callable(validateCode):
                raise RuntimeError(
                    "installed pyicloud does not expose 2FA code validation"
                )

            # Der HSA2-Bridge-Ablauf kann intern einen HTTP-409-Status als
            # verifyStatus weiterreichen und dennoch erfolgreich abschließen.
            # Maßgeblich ist ausschließlich pyiclouds boolescher Rückgabewert.
            if validateCode(code):
                LOG.info("2FA completed for %s", account.name)
                break

            if attempt == MAX_2FA_ATTEMPTS:
                raise RuntimeError(
                    "Apple rejected the verification code "
                    f"{MAX_2FA_ATTEMPTS} times"
                )

            LOG.warning(
                "Apple rejected the verification code; requesting a new code "
                "(%s/%s).",
                attempt,
                MAX_2FA_ATTEMPTS,
            )

        # pyicloud 2.6.5 vertraut die Session nach erfolgreicher Code-Prüfung
        # bereits in validate_2fa_code(); ein zweiter Trust-Aufruf ist unnötig.

    if getattr(api, "requires_2sa", False):
        raise RuntimeError(
            f"{account.name}: legacy Apple two-step authentication (2SA) "
            "is not supported; use two-factor authentication (2FA/HSA2)"
        )


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

                if getattr(api, "requires_2fa", False):
                    raise RuntimeError(
                        f"{self.account.name}: authentication required; "
                        f"run 'findmy2mqtt auth {self.account.name}'"
                    )

                if getattr(api, "requires_2sa", False):
                    raise RuntimeError(
                        f"{self.account.name}: legacy Apple two-step "
                        "authentication (2SA) is not supported"
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
        with self._lock:
            deviceId, device, deviceName = self._resolve_device(selector)

            # pyicloud sendet beim Locate die im Geräteobjekt gespeicherte rohe
            # Apple-ID. Die öffentliche, topic-sichere deviceId bleibt unverändert.
            playSound = getattr(device, "play_sound", None)
            if not callable(playSound):
                raise RuntimeError(
                    f"locate is not supported for device '{deviceName}'"
                )

            playSound()
            return deviceId, deviceName

    def display_message(
        self,
        selector: str,
        message: str,
        *,
        subject: str = "findmy2mqtt",
        sound: bool = False,
        vibrate: bool = False,
        strobe: bool = False,
    ) -> tuple[str, str]:
        message = str(message).strip()
        if not message:
            raise RuntimeError("message must not be empty")

        with self._lock:
            deviceId, device, deviceName = self._resolve_device(selector)

            # pyicloud prüft selbst, ob Apple für das Gerät Messaging unterstützt.
            displayMessage = getattr(device, "display_message", None)
            if not callable(displayMessage):
                raise RuntimeError(
                    f"message is not supported for device '{deviceName}'"
                )

            displayMessage(
                subject=subject or "findmy2mqtt",
                message=message,
                sounds=bool(sound),
                vibrate=bool(vibrate),
                strobe=bool(strobe),
            )
            return deviceId, deviceName

    def _resolve_device(self, selector: str) -> tuple[str, Any, str]:
        # CLI und MQTT verwenden dieselbe Auflösung. Akzeptiert werden
        # Anzeigename, topic-sichere deviceId, rohe Apple-ID und ältere Keys.
        selector = selector.strip()
        selectorNormalized = selector.casefold()
        if not selectorNormalized:
            raise RuntimeError("device selector must not be empty")

        decodedSelector = decode_device_id(selector).casefold()

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
            iterable = [
                (str(index), device)
                for index, device in enumerate(devices)
            ]

        for collectionKey, device in iterable:
            rawDeviceId = self._raw_device_id(device, str(collectionKey))
            publicDeviceId = encode_device_id(rawDeviceId)

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

        # Anzeigenamen können mehrfach vorkommen, die deviceId ist eindeutig.
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
        return deviceId, device, self._device_name(device, deviceId)

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

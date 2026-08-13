# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Kommandozeilenschnittstelle von findmy2mqtt.

Alle Befehle verwenden standardmäßig /etc/findmy2mqtt/config.yaml. Der Wrapper
/usr/local/bin/findmy2mqtt sorgt dafür, dass Benutzer den venv-Pfad nicht kennen müssen.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os

from . import __version__
from .apple import AppleAccount, interactive_auth
from .bridge import Bridge
from .config import (
    AccountConfig,
    Config,
    DEFAULT_CONFIG_PATH,
    account_password_path,
    find_account,
)

LOG = logging.getLogger("findmy2mqtt.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="findmy2mqtt"
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"configuration file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
    )

    # Die CLI trennt einmalige Verwaltungsaktionen klar vom dauerhaften ``run``.
    # Dadurch bleiben Auth/2FA und Passwortabfragen außerhalb des Daemons.
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run continuously")
    sub.add_parser("once", help="refresh all accounts and publish once")
    sub.add_parser("check", help="refresh all accounts without MQTT publishing")

    auth = sub.add_parser(
        "auth",
        help="perform interactive Apple authentication/2FA",
    )
    auth.add_argument("account", help="account name from config")

    devices = sub.add_parser(
        "devices",
        help="list devices for one account",
    )
    devices.add_argument("account", help="account name from config")

    password = sub.add_parser(
        "password",
        help="store an Apple password in the configured password file",
    )
    password.add_argument("account", help="account name from config")

    locate = sub.add_parser(
        "locate",
        help="make an Apple Find My device play its locate sound",
    )
    locate.add_argument("account", help="account name from config")
    locate.add_argument(
        "device",
        help="exact Apple device name or Apple device ID",
    )

    message = sub.add_parser(
        "message",
        help="display a message on an Apple Find My device",
    )
    message.add_argument("account", help="account name from config")
    message.add_argument(
        "device",
        help="exact Apple device name or Apple device ID",
    )
    message.add_argument(
        "subject",
        help="message subject, e.g. alert",
    )
    message.add_argument("message", help="message text")
    message.add_argument("--sound", action="store_true", help="play a sound")
    message.add_argument("--vibrate", action="store_true", help="vibrate")
    message.add_argument("--strobe", action="store_true", help="request strobe")

    return parser


def set_password(config: Config, account: AccountConfig) -> None:
    # Das Passwort wird einmal verdeckt abgefragt und nur in der konfigurierten
    # Secret-Datei abgelegt; bei Bedarf kann es jederzeit neu gesetzt werden.
    path = account_password_path(config, account)
    path.parent.mkdir(parents=True, exist_ok=True)

    password = getpass.getpass(f"Apple password for {account.appleId}: ")

    if not password:
        raise RuntimeError("empty password")

    path.write_text(password, encoding="utf-8")
    os.chmod(path, 0o600)
    print(f"Password stored in {path}")


def list_devices(config: Config, account: AccountConfig) -> None:
    # Für die reine Geräteliste wird derselbe Normalisierungsweg wie beim Daemon
    # genutzt, aber publish=False verhindert jede MQTT-Verbindung.
    bridge = Bridge(config)

    try:
        items = bridge.refresh_account(account, publish=False)

        rows = [
            (
                str(item.get("name", "?")),
                str(item.get("deviceClass", "?")),
                str(item.get("modelName", item.get("model", "?"))),
                str(item.get("deviceId", "?")),
            )
            for item in items
        ]

        headers = ("Name", "DeviceClass", "Model", "DeviceId")
        widths = [
            max(len(headers[index]), *(len(row[index]) for row in rows))
            if rows
            else len(headers[index])
            for index in range(len(headers))
        ]

        print(
            " | ".join(
                headers[index].ljust(widths[index])
                for index in range(len(headers))
            )
        )
        print("-+-".join("-" * width for width in widths))

        for row in rows:
            print(
                " | ".join(
                    row[index].ljust(widths[index])
                    for index in range(len(row))
                )
            )
    finally:
        bridge.close()


def locate_device(
    config: Config,
    account: AccountConfig,
    selector: str,
) -> None:
    appleAccount = AppleAccount(config, account)

    # Interaktiv ist ein exakter Name bequem; für Automatisierung ist die
    # eindeutige deviceId robuster und wird deshalb ebenfalls akzeptiert.
    deviceId, deviceName = appleAccount.locate(selector)
    print(f"Locate request sent to {deviceName} [{deviceId}]")


def send_message(
    config: Config,
    account: AccountConfig,
    selector: str,
    message: str,
    *,
    subject: str,
    sound: bool,
    vibrate: bool,
    strobe: bool,
) -> None:
    appleAccount = AppleAccount(config, account)
    deviceId, deviceName = appleAccount.display_message(
        selector,
        message,
        subject=subject,
        sound=sound,
        vibrate=vibrate,
        strobe=strobe,
    )
    print(f"Message sent to {deviceName} [{deviceId}]")


def run_once(config: Config, *, publish: bool) -> int:
    # ``check`` und ``once`` unterscheiden sich ausschließlich darin, ob der
    # erfolgreich gelesene Snapshot zusätzlich an MQTT publiziert wird.
    bridge = Bridge(config)

    try:
        ok = bridge.refresh_all(publish=publish)
        return 0 if ok else 2
    finally:
        bridge.close()


def execute_command(args: argparse.Namespace, config: Config) -> int | None:
    if args.command == "password":
        set_password(config, find_account(config, args.account))
        return 0

    if args.command == "auth":
        interactive_auth(config, find_account(config, args.account))
        return 0

    if args.command == "devices":
        list_devices(config, find_account(config, args.account))
        return 0

    if args.command == "locate":
        locate_device(
            config,
            find_account(config, args.account),
            args.device,
        )
        return 0

    if args.command == "message":
        send_message(
            config,
            find_account(config, args.account),
            args.device,
            args.message,
            subject=args.subject,
            sound=args.sound,
            vibrate=args.vibrate,
            strobe=args.strobe,
        )
        return 0

    if args.command == "check":
        return run_once(config, publish=False)

    if args.command == "once":
        return run_once(config, publish=True)

    # Der Einstiegspunkt setzt für den Daemon vorher SIGTERM/SIGINT-Handler.
    if args.command == "run":
        return None

    raise RuntimeError(f"unsupported command: {args.command}")

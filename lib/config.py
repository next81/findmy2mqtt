# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Konfiguration und Secret-Pfade von findmy2mqtt.

Die öffentliche YAML-Konfiguration verwendet camelCase. Bibliotheksspezifische
Namen wie ``with_family`` bleiben auf die interne pyicloud-Anbindung begrenzt.
"""

from __future__ import annotations

import yaml
from dataclasses import dataclass
from pathlib import Path

from .utils import slugify


DEFAULT_CONFIG_PATH = Path("/etc/findmy2mqtt/config.yaml")


@dataclass(frozen=True)
class AccountConfig:
    name: str
    appleId: str
    passwordFile: str
    familyDevices: bool = False

    @property
    def slug(self) -> str:
        return slugify(self.name)


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    passwordFile: str | None = None
    tls: bool = False
    keepalive: int = 60
    topicPrefix: str = "findmy"
    qos: int = 0


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    pollInterval: int
    stateDir: Path
    accounts: tuple[AccountConfig, ...]


def resolve_relative(pathText: str, root: Path) -> Path:
    # Relative Secret-Dateien werden absichtlich unter stateDir aufgelöst.
    # Dadurch landen Passwörter nicht versehentlich neben der lesbaren Config.
    path = Path(pathText).expanduser()
    return path if path.is_absolute() else root / path


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    # YAML erlaubt Kommentare direkt in der Konfiguration und bleibt dadurch
    # auch bei mehreren Accounts übersichtlich und gut von Hand pflegbar.
    with path.open("r", encoding="utf-8") as fileHandle:
        raw = yaml.safe_load(fileHandle)

    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a YAML mapping")

    stateDir = Path(raw.get("stateDir", "/var/lib/findmy2mqtt")).expanduser()
    pollInterval = int(raw.get("pollInterval", 300))

    # Sehr kurze Intervalle erzeugen unnötig viele Zugriffe auf Apples
    # inoffizielle Web-Endpunkte und werden deshalb bewusst verhindert.
    if pollInterval < 30:
        raise ValueError("pollInterval must be at least 30 seconds")

    mqttRaw = raw.get("mqtt") or {}
    if not mqttRaw.get("host"):
        raise ValueError("mqtt.host is required")

    topicPrefix = str(mqttRaw.get("topicPrefix", "findmy")).strip("/")
    if not topicPrefix:
        raise ValueError("mqtt.topicPrefix must not be empty")

    # Ab hier wird die lose YAML-Struktur in unveränderliche Dataclasses
    # überführt. Nach dem Start arbeitet der Dienst damit ohne Seiteneffekte.
    mqtt = MqttConfig(
        host=str(mqttRaw["host"]),
        port=int(mqttRaw.get("port", 1883)),
        username=mqttRaw.get("username"),
        password=mqttRaw.get("password"),
        passwordFile=mqttRaw.get("passwordFile"),
        tls=bool(mqttRaw.get("tls", False)),
        keepalive=int(mqttRaw.get("keepalive", 60)),
        topicPrefix=topicPrefix,
        qos=int(mqttRaw.get("qos", 0)),
    )

    if mqtt.qos not in (0, 1, 2):
        raise ValueError("mqtt.qos must be 0, 1 or 2")

    accounts: list[AccountConfig] = []
    slugs: set[str] = set()

    for entry in raw.get("accounts") or []:
        name = str(entry.get("name", "")).strip()
        appleId = str(entry.get("appleId", "")).strip()
        passwordFile = str(entry.get("passwordFile", "")).strip()

        if not name or not appleId or not passwordFile:
            raise ValueError(
                "each account requires name, appleId and passwordFile"
            )

        slug = slugify(name)

        # Der Slug steckt direkt im MQTT-Topic. Doppelte Slugs würden
        # sonst Kommandos und Zustände verschiedener Accounts vermischen.
        if slug in slugs:
            raise ValueError(f"duplicate account name/slug: {name}")

        slugs.add(slug)
        accounts.append(
            AccountConfig(
                name=name,
                appleId=appleId,
                passwordFile=passwordFile,
                familyDevices=bool(entry.get("familyDevices", False)),
            )
        )

    if not accounts:
        raise ValueError("at least one account is required")

    return Config(
        mqtt=mqtt,
        pollInterval=pollInterval,
        stateDir=stateDir,
        accounts=tuple(accounts),
    )


def account_password_path(config: Config, account: AccountConfig) -> Path:
    return resolve_relative(
        account.passwordFile,
        config.stateDir / "credentials",
    )


def read_secret_file(path: Path) -> str:
    # Secrets werden nie aus Umgebungsvariablen oder Readings bezogen, sondern
    # ausschließlich aus den dafür vorgesehenen Dateien gelesen.
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except FileNotFoundError as exc:
        raise RuntimeError(f"password file does not exist: {path}") from exc

    if not value:
        raise RuntimeError(f"password file is empty: {path}")

    return value


def mqtt_password(config: Config) -> str | None:
    # passwordFile hat Vorrang vor einem direkt in der Config gesetzten Passwort.
    # Das erlaubt eine bequeme Config, ohne Credentials dort ablegen zu müssen.
    if config.mqtt.passwordFile:
        path = resolve_relative(
            config.mqtt.passwordFile,
            config.stateDir / "credentials",
        )
        return read_secret_file(path)

    return config.mqtt.password


def find_account(config: Config, name: str) -> AccountConfig:
    # CLI-Aufrufe dürfen den kurzen Account-Namen oder die Apple-ID verwenden.
    # MQTT selbst nutzt ausschließlich den stabil normalisierten Slug.
    wanted = slugify(name)

    for account in config.accounts:
        if account.slug == wanted or account.appleId.lower() == name.lower():
            return account

    raise KeyError(f"unknown account: {name}")

# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Kleine, zustandslose Hilfsfunktionen.

Hier liegt nur Logik, die weder Apple-, MQTT- noch Konfigurationszustand benötigt.
So bleiben die fachlichen Module leichter testbar.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote



def encode_device_id(value: str) -> str:
    # Apple verwendet für Geräte-IDs u. a. Base64-Zeichen wie "/" und "+".
    # "/" würde eine zusätzliche MQTT-Topic-Ebene erzeugen, "+" ist ein
    # MQTT-Wildcard-Zeichen. Percent-Encoding hält die ID vollständig reversibel.
    return quote(str(value), safe="-_.~")


def decode_device_id(value: str) -> str:
    # MQTT-Kommandos enthalten die topic-sichere ID. Vor dem Vergleich mit
    # pyicloud wird exakt die ursprüngliche Apple-ID wiederhergestellt.
    # FHEM maskiert Sonderzeichen in automatisch erzeugten readingList-RegExps
    # als ``\xHH``. Wird ein solcher Topic in ``devicetopic`` übernommen,
    # erreicht uns das Prozentzeichen der kodierten ID deshalb als ``\x25``.
    value = re.sub(r"\\x25", "%", str(value), flags=re.IGNORECASE)
    return unquote(value)


def slugify(value: str, maxlen: int = 24) -> str:
    # Slugs landen in MQTT-Topics und Client-IDs. Wir beschränken sie daher auf
    # ein portables ASCII-Subset statt beliebige Anzeigenamen durchzureichen.
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return (value or "account")[:maxlen]


def short_hash(value: str, length: int = 10) -> str:
    # Der Hash dient nur zur kompakten, stabilen MQTT-Client-ID; er ist kein
    # Sicherheitsmechanismus und ersetzt die echte deviceId im Topic nicht.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def json_scalar(value: Any) -> Any:
    # MQTT-Payloads bleiben absichtlich flach. Komplexe pyicloud-Objekte werden
    # deshalb notfalls in Text umgewandelt statt verschachtelt serialisiert.
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def first_value(*values: Any) -> Any:
    # Apple benennt identische Felder je nach Gerät/Endpunkt unterschiedlich.
    # Diese Funktion kapselt die Prioritätsreihenfolge der bekannten Varianten.
    for value in values:
        if value is not None and value != "":
            return value
    return None


def epoch_seconds(value: Any) -> int | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    if isinstance(value, (int, float)):
        number = float(value)

        # Apple liefert Zeitstempel abhängig vom Endpunkt teils in Millisekunden.
        if number > 10_000_000_000:
            number /= 1000.0

        return int(number)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None

        try:
            return epoch_seconds(float(text))
        except ValueError:
            pass

        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None

    return None


def local_time_string(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None

    return datetime.fromtimestamp(timestamp).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def battery_percent(value: Any) -> int | None:
    if value is None:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    # pyicloud kann 0..1 oder bereits einen Prozentwert liefern.
    if 0 <= number <= 1.0:
        number *= 100.0

    if number < 0:
        return None

    return int(round(min(number, 100.0)))

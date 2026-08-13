# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Normalisierung der von pyicloud gelieferten Gerätedaten.

Apple liefert nicht für jedes Gerätemodell dieselben Felder. Dieses Modul bildet
die Varianten auf eine kleine, stabile MQTT-Payload mit camelCase-Feldern ab.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import AccountConfig
from .utils import (
    battery_percent,
    epoch_seconds,
    first_value,
    json_scalar,
    local_time_string,
)


def get_device_data(device: Any) -> dict[str, Any]:
    # pyicloud stellt ``data`` versionsabhängig als Attribut oder aufrufbares
    # Objekt bereit. Beide Formen werden hier auf ein normales dict reduziert.
    data = getattr(device, "data", {}) or {}
    if callable(data):
        data = data()

    return dict(data) if isinstance(data, Mapping) else {}


def get_device_location(
    device: Any,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    location = getattr(device, "location", None)

    if callable(location):
        location = location()

    # Manche pyicloud-Versionen legen die Position nur im Rohdatenobjekt ab.
    if not location:
        location = data.get("location")

    return dict(location) if isinstance(location, Mapping) else {}


def device_payload(
    account: AccountConfig,
    deviceId: str,
    device: Any,
) -> dict[str, Any]:
    data = get_device_data(device)
    location = get_device_location(device, data)

    # Anzeigename und Modellinformationen existieren je nach Apple-Gerät an
    # unterschiedlichen Stellen. Die Reihenfolge bevorzugt die höherwertigen
    # pyicloud-Eigenschaften und fällt anschließend auf Rohdaten zurück.
    name = first_value(
        getattr(device, "name", None),
        data.get("name"),
        data.get("deviceDisplayName"),
        deviceId,
    )

    model = first_value(
        getattr(device, "model", None),
        data.get("rawDeviceModel"),
    )

    modelName = first_value(
        getattr(device, "model_name", None),
        data.get("deviceDisplayName"),
        data.get("modelDisplayName"),
    )

    deviceClass = first_value(
        getattr(device, "device_type", None),
        data.get("deviceClass"),
        data.get("deviceType"),
        "device",
    )

    # Auch Standort-Metadaten variieren zwischen Apple-Endpunkten. Vor dem
    # Publizieren werden Zeitstempel und Old-Flag in ein einheitliches Format
    # überführt.
    locationTimestamp = epoch_seconds(
        first_value(
            location.get("timeStamp"),
            location.get("timestamp"),
            location.get("locationTime"),
        )
    )

    locationOld = bool(
        first_value(
            location.get("isOld"),
            location.get("locationOld"),
            False,
        )
    )

    hasLocation = (
        location.get("latitude") is not None
        and location.get("longitude") is not None
    )

    # ``state`` beschreibt ausschließlich die Qualität des zuletzt gelieferten
    # Standorts und ist nicht mit dem Online-Zustand des Geräts gleichzusetzen.
    state = "located" if hasLocation else "noLocation"
    if hasLocation and locationOld:
        state = "old"

    payload: dict[str, Any] = {
        "state": state,
        "name": json_scalar(name),
        "account": account.slug,
        "deviceId": deviceId,
        "deviceClass": json_scalar(deviceClass),
        "model": json_scalar(model),
        "modelName": json_scalar(modelName),
        "battery": battery_percent(
            first_value(data.get("batteryLevel"), data.get("battery"))
        ),
        "batteryStatus": json_scalar(data.get("batteryStatus")),
        "deviceStatus": json_scalar(
            first_value(data.get("deviceStatus"), data.get("statusCode"))
        ),
        "latitude": json_scalar(location.get("latitude")),
        "longitude": json_scalar(location.get("longitude")),
        "accuracy": json_scalar(
            first_value(
                location.get("horizontalAccuracy"),
                location.get("accuracy"),
            )
        ),
        "locationTime": local_time_string(locationTimestamp),
        "locationTimestamp": locationTimestamp,
        "locationOld": 1 if locationOld else 0,
        "positionType": json_scalar(location.get("positionType")),
        "locationType": json_scalar(location.get("locationType")),
    }

    # Fehlende Apple-Werte werden nicht als leere MQTT-Readings veröffentlicht.
    return {key: value for key, value in payload.items() if value is not None}

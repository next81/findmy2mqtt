# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Regressionstests für topic-sichere Apple-Geräte-IDs."""

from __future__ import annotations

import unittest

from lib.utils import decode_device_id, encode_device_id


class DeviceIdEncodingTests(unittest.TestCase):
    def test_round_trip_for_base64_style_device_id(self) -> None:
        rawDeviceId = (
            "AXZlr7unNbzdUnJqHftm5fexS5AmhrPhEQkDtZCDh5iJKTRMFPJXdKBnl2nwC"
            "/dVjDYFFm37HUSCVQ=="
        )

        encodedDeviceId = encode_device_id(rawDeviceId)

        self.assertIn("%2F", encodedDeviceId)
        self.assertTrue(encodedDeviceId.endswith("%3D%3D"))
        self.assertEqual(decode_device_id(encodedDeviceId), rawDeviceId)

    def test_decodes_fhem_escaped_percent_signs(self) -> None:
        fhemDeviceId = (
            "AXZlr7unNbzdUnJqHftm5fexS5AmhrPhEQkDtZCDh5iJKTRMFPJXdKBnl2nwC"
            "\\x252FdVjDYFFm37HUSCVQ\\x253D\\x253D"
        )

        self.assertEqual(
            decode_device_id(fhemDeviceId),
            "AXZlr7unNbzdUnJqHftm5fexS5AmhrPhEQkDtZCDh5iJKTRMFPJXdKBnl2nwC"
            "/dVjDYFFm37HUSCVQ==",
        )


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Regressionstests für den interaktiven Apple-2FA-Ablauf."""

from __future__ import annotations

import sys
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch


# Die Tests prüfen ausschließlich unseren Adapter. So bleiben sie auch ohne
# installierte Laufzeitabhängigkeiten auf einem Entwicklungsrechner ausführbar.
try:
    import pyicloud  # noqa: F401
except ImportError:
    pyicloudModule = types.ModuleType("pyicloud")
    pyicloudModule.PyiCloudService = type("PyiCloudService", (), {})
    sys.modules["pyicloud"] = pyicloudModule

try:
    import yaml  # noqa: F401
except ImportError:
    yamlModule = types.ModuleType("yaml")
    yamlModule.safe_load = lambda stream: None
    sys.modules["yaml"] = yamlModule

from lib.apple import (
    MAX_2FA_ATTEMPTS,
    _discard_buffered_terminal_input,
    interactive_auth,
)


class FakeApi:
    def __init__(
        self,
        *,
        requestResults: list[bool],
        validationResults: list[bool] | None = None,
        securityKeys: list[str] | None = None,
        trustOnRefresh: bool = False,
        requires2fa: bool = True,
        requires2sa: bool = False,
        deliveryMethod: str = "trusted_device",
    ) -> None:
        self.requires_2fa = requires2fa
        self.requires_2sa = requires2sa
        self.security_key_names = securityKeys
        self.two_factor_delivery_method = deliveryMethod
        self.two_factor_delivery_notice = None
        self.requestResults = list(requestResults)
        self.validationResults = list(validationResults or [True])
        self.trustOnRefresh = trustOnRefresh
        self.events: list[str] = []

    def request_2fa_code(self) -> bool:
        self.events.append("request")
        return self.requestResults.pop(0)

    def authenticate(self, *, force_refresh: bool = False) -> None:
        self.events.append(f"authenticate:{force_refresh}")
        if self.trustOnRefresh:
            self.requires_2fa = False
        else:
            self.two_factor_delivery_method = "trusted_device"

    def validate_2fa_code(self, code: str) -> bool:
        self.events.append(f"validate:{code}")
        result = self.validationResults.pop(0)
        if result:
            self.requires_2fa = False
        return result


class InteractiveAuthTests(unittest.TestCase):
    account = SimpleNamespace(name="jamo")
    config = SimpleNamespace()

    def run_auth(self, api: FakeApi, codes: list[str]) -> str:
        codeIterator = iter(codes)
        output = StringIO()

        def read_code(prompt: str) -> str:
            self.assertIn("latest request", prompt)
            api.events.append("input")
            return next(codeIterator)

        def discard_buffered_input() -> None:
            api.events.append("discard")

        with (
            patch("lib.apple.create_api", return_value=api),
            patch(
                "lib.apple._discard_buffered_terminal_input",
                side_effect=discard_buffered_input,
            ),
            patch("builtins.input", side_effect=read_code),
            redirect_stdout(output),
        ):
            interactive_auth(self.config, self.account)

        return output.getvalue()

    def test_requests_bridge_challenge_before_reading_code(self) -> None:
        api = FakeApi(requestResults=[True])

        output = self.run_auth(api, ["123456"])

        self.assertEqual(
            api.events,
            ["request", "discard", "input", "validate:123456"],
        )
        self.assertIn("This can take up to 30 seconds", output)
        self.assertIn("wait for the code prompt", output)

    def test_ignores_buffered_empty_input_before_code(self) -> None:
        api = FakeApi(requestResults=[True])

        output = self.run_auth(api, ["", "123456"])

        self.assertEqual(
            api.events,
            ["request", "discard", "input", "input", "validate:123456"],
        )
        self.assertIn("No verification code entered", output)

    def test_discards_terminal_input_with_posix_tcflush(self) -> None:
        calls: list[tuple[int, int]] = []
        fakeStdin = SimpleNamespace(
            isatty=lambda: True,
            fileno=lambda: 17,
        )
        fakeTermios = types.ModuleType("termios")
        fakeTermios.TCIFLUSH = 23
        fakeTermios.tcflush = lambda fd, queue: calls.append((fd, queue))

        with (
            patch("lib.apple.sys.stdin", fakeStdin),
            patch.dict(sys.modules, {"termios": fakeTermios}),
        ):
            _discard_buffered_terminal_input()

        self.assertEqual(calls, [(17, 23)])

    def test_refreshes_untrusted_persisted_session_once(self) -> None:
        api = FakeApi(
            requestResults=[True],
            deliveryMethod="unknown",
        )

        self.run_auth(api, ["234567"])

        self.assertEqual(
            api.events,
            [
                "authenticate:True",
                "request",
                "discard",
                "input",
                "validate:234567",
            ],
        )

    def test_requests_fresh_challenge_after_rejected_code(self) -> None:
        api = FakeApi(
            requestResults=[True, True],
            validationResults=[False, True],
        )

        self.run_auth(api, ["111111", "345678"])

        self.assertEqual(
            api.events,
            [
                "request",
                "discard",
                "input",
                "validate:111111",
                "request",
                "discard",
                "input",
                "validate:345678",
            ],
        )

    def test_stops_after_bounded_number_of_rejected_codes(self) -> None:
        api = FakeApi(
            requestResults=[True] * MAX_2FA_ATTEMPTS,
            validationResults=[False] * MAX_2FA_ATTEMPTS,
        )

        with self.assertRaisesRegex(RuntimeError, "3 times"):
            self.run_auth(api, ["111111", "222222", "333333"])

        self.assertEqual(api.events.count("request"), MAX_2FA_ATTEMPTS)

    def test_accepts_session_that_becomes_trusted_during_refresh(self) -> None:
        api = FakeApi(
            requestResults=[],
            trustOnRefresh=True,
            deliveryMethod="unknown",
        )

        self.run_auth(api, [])

        self.assertEqual(api.events, ["authenticate:True"])

    def test_reports_missing_delivery_route_after_refresh(self) -> None:
        api = FakeApi(requestResults=[False, False])

        with self.assertRaisesRegex(
            RuntimeError,
            "could not request a new verification code",
        ):
            self.run_auth(api, [])

        self.assertEqual(
            api.events,
            ["request", "authenticate:True", "request"],
        )

    def test_rejects_security_key_challenge_before_numeric_prompt(self) -> None:
        api = FakeApi(
            requestResults=[],
            securityKeys=["YubiKey 5"],
        )

        with self.assertRaisesRegex(RuntimeError, "YubiKey 5"):
            self.run_auth(api, [])

        self.assertEqual(api.events, [])

    def test_rejects_legacy_two_step_authentication(self) -> None:
        api = FakeApi(
            requestResults=[],
            requires2fa=False,
            requires2sa=True,
        )

        with self.assertRaisesRegex(RuntimeError, "2FA/HSA2"):
            self.run_auth(api, [])

        self.assertEqual(api.events, [])


if __name__ == "__main__":
    unittest.main()

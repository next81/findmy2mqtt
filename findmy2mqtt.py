#!/usr/bin/env python3
# Copyright (c) 2026 Andreas Planer
# Repository: https://github.com/next81/findmy2mqtt
# Licensed under the PolyForm Strict License 1.0.0
# https://polyformproject.org/licenses/strict/1.0.0

"""Schlanker Programmeinstieg für findmy2mqtt.

Die eigentliche Fachlogik liegt in ``lib/``. Diese Datei hält nur CLI-Parsing,
Logging, Konfigurationsladen und die Signalbehandlung des Daemons zusammen.
"""

from __future__ import annotations

import logging
import signal
from pathlib import Path

from lib.bridge import Bridge
from lib.cli import build_parser, execute_command
from lib.config import load_config


LOG = logging.getLogger("findmy2mqtt")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    args = build_parser().parse_args()
    configure_logging(args.verbose)

    try:
        config = load_config(Path(args.config))
        result = execute_command(args, config)

        # Nur der Daemon-Befehl "run" wird hier weitergeführt.
        # Alle einmaligen CLI-Kommandos sind zu diesem Zeitpunkt bereits erledigt.
        if result is not None:
            return result

        # Der dauerhafte Dienst wird erst nach erfolgreichem Laden der
        # Konfiguration erzeugt. So scheitert ein fehlerhafter Start früh und
        # systemd kann den Fehler eindeutig protokollieren.
        bridge = Bridge(config)
        # SIGTERM kommt insbesondere von systemd. Beide Signale setzen nur das
        # Stop-Event; das eigentliche Aufräumen erfolgt zentral in Bridge.close().
        signal.signal(signal.SIGTERM, bridge.stop)
        signal.signal(signal.SIGINT, bridge.stop)
        bridge.run()
        return 0

    except KeyboardInterrupt:
        return 130

    except Exception as exc:
        LOG.error("%s", exc)

        if args.verbose:
            LOG.exception("Detailed error")

        return 1


if __name__ == "__main__":
    raise SystemExit(main())

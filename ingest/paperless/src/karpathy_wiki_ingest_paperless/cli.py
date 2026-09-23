"""Command-line entry point and daemon lifecycle for the Paperless plugin."""

from __future__ import annotations

import argparse
import logging
import signal
import threading

from karpathy_wiki_ingest.shared import TargetedAnonymizer

from .config import Settings
from .ingestor import Ingestor, run_continuously


def main() -> None:
    parser = argparse.ArgumentParser(description="Redact tagged Paperless documents")
    parser.add_argument("--once", action="store_true", help="run one synchronization")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    anonymizer = TargetedAnonymizer.from_file(settings.redactions_path)
    ingestor = Ingestor(settings, anonymizer)
    if arguments.once:
        _, failed = ingestor.run_once()
        if failed:
            raise SystemExit(1)
        return
    stop_event = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop_event.set())
    run_continuously(ingestor, settings, stop_event)

"""Diagnostics entry point — run component tests against a live server stack."""
import argparse
import logging
import logging.config
import os
import sys
import time
from typing import List

from cltl.combot.infra.config.k8config import K8LocalConfigurationContainer

logging.config.fileConfig(
    os.environ.get("CLTL_LOGGING_CONFIG", "config/logging.config"),
    disable_existing_loggers=False,
)
logger = logging.getLogger(__name__)

# Must happen after logging is configured so components that import at module
# level don't produce noise before the logger is set up.
from components.test_storage import StorageTest
from components.test_vad import VadTest
from components.test_asr import AsrTest
from components.test_eliza import ElizaTest
from event_bus import create_event_bus
from scenario import create_test_scenario, stop_test_scenario
from storage_client import StorageClient

ALL_TESTS = [StorageTest(), VadTest(), AsrTest(), ElizaTest()]
ALL_NAMES = [t.name for t in ALL_TESTS]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run diagnostics against the server stack.")
    parser.add_argument(
        "--components",
        nargs="+",
        metavar="COMPONENT",
        choices=ALL_NAMES,
        default=ALL_NAMES,
        help=f"Components to test (default: all). Choices: {', '.join(ALL_NAMES)}",
    )
    return parser.parse_args()


def _run_selected(selected_names: List[str]) -> bool:
    K8LocalConfigurationContainer.load_configuration()
    config_manager = K8LocalConfigurationContainer().config_manager

    bus = create_event_bus(config_manager)
    storage = StorageClient.from_config(config_manager)

    scenario_id = create_test_scenario(bus)
    # Give services a moment to receive and process the ScenarioStarted event.
    time.sleep(2.0)

    selected = [t for t in ALL_TESTS if t.name in selected_names]
    results = {}

    try:
        for test in selected:
            logger.info("--- Running %s ---", test.name)
            try:
                passed = test.run(scenario_id, bus, storage)
            except Exception:
                logger.exception("Unexpected error in %s", test.name)
                passed = False
            results[test.name] = passed
    finally:
        stop_test_scenario(bus, scenario_id)
        bus.close()

    return _report(results)


def _report(results: dict) -> bool:
    print("\n=== Diagnostics Results ===")
    all_passed = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {status}  {name}")
        all_passed = all_passed and passed
    print()
    return all_passed


if __name__ == "__main__":
    args = _parse_args()
    success = _run_selected(args.components)
    sys.exit(0 if success else 1)

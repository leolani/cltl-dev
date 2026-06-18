"""Abstract base class for component diagnostic tests."""
import logging
import time
from abc import ABC, abstractmethod

from cltl.combot.infra.event.kombu import KombuEventBus

from storage_client import StorageClient

logger = logging.getLogger(__name__)


class ComponentTest(ABC):
    """Run a targeted diagnostic against one server component."""

    #: Short identifier used with ``--components`` on the CLI.
    name: str

    @abstractmethod
    def run(self, scenario_id: str, bus: KombuEventBus, storage: StorageClient) -> bool:
        """Publish the component's input event(s), await its output, assert correctness.

        Returns True when all assertions pass, False otherwise.
        """

    def _timed(self, label: str, fn):
        """Run *fn* and log elapsed time under *label*."""
        start = time.monotonic()
        result = fn()
        elapsed = time.monotonic() - start
        logger.info("%s completed in %.2fs", label, elapsed)
        return result

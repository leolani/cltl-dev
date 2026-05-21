import logging

from eliza_app.template.api import DemoProcessor

logger = logging.getLogger(__name__)


class HelloWorldProcessor(DemoProcessor):
    """
    Dummy implementation of the component.
    """
    def respond(self, statement: str) -> str:
        logger.debug("Responding to statement: %s", statement)

        return f"Hello World! I heard '{statement}'." if statement else "Hello World!"

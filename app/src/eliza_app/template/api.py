import abc


class DemoProcessor(abc.ABC):
    """
    Abstract class representing the interface of the component.
    """
    def respond(self, statement: str) -> str:
        raise NotImplementedError()

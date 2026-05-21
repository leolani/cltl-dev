"""HTTP client for the ChatUI REST API."""
import time

import requests


class ChatClient:
    """Thin wrapper around the ChatUI HTTP endpoints for use in integration tests."""

    def __init__(self, base_url: str = "http://localhost:8003/chatui"):
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self.initial_sequence: int = 0

    def start_session(self) -> str:
        """Create or retrieve the current chat session and return the chat id.

        Also snapshots the current utterance count into self.initial_sequence so
        that callers can start polling from after any pre-existing messages
        (e.g. agent greeting sent before the test began).
        """
        response = self._session.get(f"{self._base_url}/chat/current")
        _raise_for_status(response)
        chat_id = response.json()["id"]
        all_utterances = self.fetch_all(chat_id)
        self.initial_sequence = len(all_utterances)
        return chat_id

    def send(self, chat_id: str, text: str) -> None:
        """Send a user utterance to the active chat session."""
        response = self._session.post(
            f"{self._base_url}/chat/{chat_id}",
            data=text,
        )
        _raise_for_status(response)

    def receive(self, chat_id: str, *, from_sequence: int = 0, timeout: float = 15.0) -> list[str]:
        """
        Poll for agent responses until at least one arrives or the timeout elapses.

        Returns a list of response texts from the agent speaker.
        Raises TimeoutError if no response arrives within *timeout* seconds.
        """
        deadline = time.monotonic() + timeout
        last_response: list[str] = []
        while True:
            last_response = self._fetch_responses(chat_id, from_sequence)
            if last_response:
                return last_response
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)

        raise TimeoutError(
            f"No response from agent for chat '{chat_id}' within {timeout}s. "
            f"Last server response: {last_response!r}"
        )

    def fetch_all(self, chat_id: str, *, from_sequence: int = 0) -> list[dict]:
        """Return all utterances (both speakers) from the given sequence number."""
        response = self._session.get(
            f"{self._base_url}/chat/{chat_id}",
            params={"from": from_sequence},
        )
        _raise_for_status(response)
        return response.json()

    def _fetch_responses(self, chat_id: str, from_sequence: int) -> list[str]:
        # No explicit speaker param — relies on the server-side default (external_input=True)
        # which filters the response to agent utterances only.
        response = self._session.get(
            f"{self._base_url}/chat/{chat_id}",
            params={"from": from_sequence},
        )
        _raise_for_status(response)
        return [utterance["text"] for utterance in response.json()]


def _raise_for_status(response: requests.Response) -> None:
    if not response.ok:
        raise requests.HTTPError(
            f"{response.status_code} {response.reason} — body: {response.text!r}",
            response=response,
        )

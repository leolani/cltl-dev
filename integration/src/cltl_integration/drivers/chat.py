"""HTTP client for the ChatUI REST API.

Ported from ``app/docker-app/tests/integration/helpers/chat_client.py``, with the
base URL injected rather than hardcoded to ``localhost:8003`` so the same client
drives the in-process dispatcher and a compose stack's published port.
"""
import time
from typing import List

import requests

AGENT_SPEAKER_FALLBACK = "Leolani"


class ChatClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._timeout = timeout
        self.initial_sequence = 0
        self._agent_speaker = AGENT_SPEAKER_FALLBACK

    def current(self) -> dict:
        response = self._session.get(f"{self._base_url}/chat/current", timeout=self._timeout)
        _raise_for_status(response)
        return response.json()

    def start_session(self) -> str:
        """Return the current chat id, snapshotting what is already in it.

        The agent may have spoken before the test connected (an init greeting,
        say), so callers need the sequence number to poll from.
        """
        payload = self.current()
        chat_id = payload["id"]
        existing = self.fetch_all(chat_id)
        self.initial_sequence = len(existing)
        if existing:
            self._agent_speaker = existing[0]["speaker"]
        return chat_id

    def await_scenario(self, timeout: float = 15.0) -> str:
        """Block until ChatUI has seen a ScenarioStarted event; return its id.

        The right readiness signal is the "scenario_id" field, not the "id"
        field: MemoryChats mints a chat id eagerly on the first call, so "id" is
        always present even before any scenario exists. Posting before the
        scenario arrives raises ValueError inside the service.
        """
        deadline = time.monotonic() + timeout
        payload = None
        while time.monotonic() < deadline:
            payload = self.current()
            if payload.get("scenario_id"):
                return payload["scenario_id"]
            time.sleep(0.05)

        raise TimeoutError(
            f"ChatUI had no active scenario within {timeout}s. Last /chat/current: {payload!r}")

    def send(self, chat_id: str, text: str) -> None:
        response = self._session.post(
            f"{self._base_url}/chat/{chat_id}", data=text, timeout=self._timeout)
        _raise_for_status(response)

    def upload_image(self, chat_id: str, data: bytes, width: int, height: int,
                     content_type: str = "image/png") -> dict:
        """POST the bytes; returns the minted id and the URL to fetch them back.

        `width` and `height` are required by the service rather than decoded
        from the bytes: they are the frame every region is expressed in, and a
        wrong size makes every segment silently wrong.
        """
        response = self._session.post(
            f"{self._base_url}/chat/{chat_id}/image",
            params={"width": width, "height": height},
            data=data,
            headers={"Content-Type": content_type},
            timeout=self._timeout)
        _raise_for_status(response)

        return response.json()

    def fetch_image(self, chat_id: str, image_id: str) -> bytes:
        response = self._session.get(
            f"{self._base_url}/chat/{chat_id}/image/{image_id}", timeout=self._timeout)
        _raise_for_status(response)

        return response.content

    def delete_image(self, chat_id: str, image_id: str) -> None:
        response = self._session.delete(
            f"{self._base_url}/chat/{chat_id}/image/{image_id}", timeout=self._timeout)
        _raise_for_status(response)

    def annotate(self, chat_id: str, image_id: str, regions: List[dict]) -> dict:
        """Submit the regions: records the signal and echoes it into the chat."""
        response = self._session.post(
            f"{self._base_url}/chat/{chat_id}/image/{image_id}/annotations",
            json={"regions": regions},
            timeout=self._timeout)
        _raise_for_status(response)

        return response.json()

    def fetch_all(self, chat_id: str, from_sequence: int = 0) -> List[dict]:
        response = self._session.get(
            f"{self._base_url}/chat/{chat_id}",
            params={"from": from_sequence},
            timeout=self._timeout)
        _raise_for_status(response)
        return response.json()

    def receive(self, chat_id: str, from_sequence: int = 0,
                timeout: float = 15.0) -> List[str]:
        """Poll until the agent replies, or fail with what was seen instead."""
        deadline = time.monotonic() + timeout
        last: List[dict] = []
        while True:
            last = self.fetch_all(chat_id, from_sequence=from_sequence)
            responses = [u["text"] for u in last if u["speaker"] == self._agent_speaker]
            if responses:
                return responses
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)

        raise TimeoutError(
            f"No reply from {self._agent_speaker!r} in chat {chat_id} within {timeout}s. "
            f"Utterances from sequence {from_sequence}: {last!r}")


def _raise_for_status(response: requests.Response) -> None:
    if not response.ok:
        raise requests.HTTPError(
            f"{response.status_code} {response.reason} - body: {response.text!r}",
            response=response)

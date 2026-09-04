"""Images for tests that need one, synthesised rather than committed.

The repo holds no image fixtures, and the audio drivers already take this line:
a generated signal is reproducible, is described by the code that uses it, and
cannot rot.

Hand-rolled from `zlib` and `struct` rather than encoded with cv2 or Pillow.
cltl-chat-ui treats cv2 as optional — see `ChatUiService._decode_rgb` — so the
fixture must not be the thing that decides whether a test can run, and the file
this produces has to be byte-stable for the "came back unchanged" assertion.

Named image.py, not images.py: `cltl_integration.images` at the package root is
the docker-image checker `make docker-images` runs.
"""
import struct
import zlib
from typing import Sequence, Tuple

PNG_MIME_TYPE = "image/png"


def png(width: int = 64, height: int = 48, color: Sequence[int] = (255, 0, 0)) -> bytes:
    """A valid truecolour (8-bit RGB) PNG of a single colour."""
    if len(color) != 3:
        raise ValueError(f"Expected an RGB triple, was {color!r}")

    scanlines = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(scanlines))
            + _chunk(b"IEND", b""))


def _chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data

    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)


def region(x0: int, y0: int, x1: int, y1: int, label: str = "") -> dict:
    """One entry of an annotation request's `regions` array."""
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "label": label}


def bounds_of(mention) -> Tuple[int, int, int, int]:
    """The segment bounds of a mention, whether it is an object or plain JSON.

    A mention read back from `image.json` is a dict; one taken off the event bus
    in tier 1 is an EMISSOR `Mention`.
    """
    segment = mention["segment"][0] if isinstance(mention, dict) else mention.segment[0]
    raw = segment["bounds"] if isinstance(segment, dict) else segment.bounds

    return tuple(raw)


def label_of(mention) -> str:
    annotation = (mention["annotations"][0] if isinstance(mention, dict)
                  else mention.annotations[0])
    value = annotation["value"] if isinstance(annotation, dict) else annotation.value
    if isinstance(value, dict):
        return value.get("label") or ""

    return getattr(value, "label", "") or ""

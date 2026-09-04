"""A person holds a conversation in the browser, in each deployment.

Three runs of the same scenario, differing only in what is underneath it: this
process, the container images, and the images split across two networks. The
question put to the person is the same each time, because from the browser these
should be indistinguishable — and that is the property worth checking by eye.

What each adds over the automatic suite:

* tier 1 — the page itself. ``ChatUiService``'s HTML and JavaScript are loaded by
  nothing else in the harness; every automatic test speaks to its REST endpoints.
* tier 2 — the same, from inside the image. A static asset that ``setup.py``
  fails to package is invisible until someone opens the page.
* the split — that a deployment across two machines is not merely correct on the
  bus but usable, with the round trip a person can feel.

The image annotator at the bottom of this file is here for a stronger reason
than the others. Its whole input is a gesture — a drag across an image in a
browser — and no automatic test can make that gesture. Everything below the
browser is covered by tests/slices/test_chatui_image.py, which posts the same
coordinates over HTTP; what only a person can tell us is whether the rectangle
they dragged is the rectangle that got recorded.
"""
import pytest

from cltl_integration.__main__ import report
from cltl_integration.drivers.bdi import publish_intention
from cltl_integration.drivers.chat import ChatClient
from cltl_integration.drivers.conversation import (GREETING, INTENTION_TOPIC,
                                                   TEXT_OUT, TOPICS)
from cltl_integration.drivers.image import bounds_of, label_of
from cltl_integration.drivers.scenario import start_scenario
from cltl_integration.topology import CHATUI_IMAGE, CSPLIT, TEXT_PIPELINE

pytestmark = pytest.mark.manual

IMAGE_TOPIC = "cltl.topic.image"
TEXT_IN = "cltl.topic.text_in"
SCENARIO_TOPIC = "cltl.topic.scenario"

INSTRUCTIONS = """
    Open the chat page above and hold a short conversation:

      1. The agent asks '{greeting}' — answer 'yes'.
      2. Say something about how you feel.
      3. Check that the agent's reply appears, attributed to it, and that
         your own message is still there above it.

    Then come back here.
"""


def _open(runner):
    """Publish the intention that opens a scenario, and wait for the greeting.

    Without it the chat UI renders nothing at all, and the person would be
    looking at an empty page wondering what they did wrong.
    """
    runner.probe.subscribe(*TOPICS)
    publish_intention(runner.event_bus, INTENTION_TOPIC, "init")
    runner.probe.await_event(
        TEXT_OUT, lambda event: GREETING in event.payload.signal.text, timeout=60)

    client = ChatClient(runner.url("chatui"))
    client.await_scenario(timeout=60)

    return client


def _drive(runner, scenario, ask) -> None:
    """Hand the topology to the person, then check what they left behind."""
    client = _open(runner)
    # The demo launcher's own report, so that what a person is shown here is
    # what `make demo-text-pipeline` shows. No Ctrl-C hint: this one ends with
    # an answer to the question below, not with a signal.
    report(scenario, runner, runner.storage_path, closing=None)

    confirmed = ask(INSTRUCTIONS.format(greeting=GREETING))

    history = client.fetch_all(client.current()["id"])
    print(f"\n  The service recorded {len(history)} utterance(s):")
    for utterance in history:
        speaker = "agent" if utterance["speaker"] else "you"
        print(f"    {speaker:6} {utterance['text']!r}")

    assert confirmed, "the person said the chat UI did not work"
    # The person's answer is not the only evidence. A "y" pressed without
    # looking cannot make an empty conversation pass.
    assert len(history) >= 3, (
        f"the conversation should hold the greeting, an answer and a reply; "
        f"the service recorded {history}")
    assert any(utterance["speaker"] for utterance in history), "the agent never spoke"
    assert any(not utterance["speaker"] for utterance in history), "you never spoke"


def test_the_chat_ui_works_in_process(inprocess, ask):
    """The page, the JavaScript and the REST endpoints behind them."""
    _drive(inprocess(TEXT_PIPELINE), TEXT_PIPELINE, ask)


@pytest.mark.compose
def test_the_chat_ui_works_from_the_image(compose, ask):
    """The same, served by cltl-chat-ui's own container."""
    _drive(compose(TEXT_PIPELINE), TEXT_PIPELINE, ask)


@pytest.mark.compose
def test_the_chat_ui_works_across_the_split(split, ask):
    """The same again, with the agent on the far side of a network boundary."""
    _drive(split(CSPLIT), CSPLIT, ask)


IMAGE_INSTRUCTIONS = """
    Open the chat page above. Beside the conversation there is an Image panel.

      1. Choose any image file.
      2. Drag a rectangle across something recognisable in it, and type what
         that thing is in the box that appears below.
      3. Draw a second rectangle somewhere else and name that too.
      4. Press Submit, and check that the image appears in the chat with both
         labels under it.

    Then come back here.
"""


def test_the_image_annotator_works_in_process(inprocess, ask):
    """The one path whose input is a gesture rather than an HTTP request."""
    runner = inprocess(CHATUI_IMAGE)
    runner.probe.subscribe(IMAGE_TOPIC, TEXT_IN)

    scenario = start_scenario(runner.event_bus, SCENARIO_TOPIC)
    client = ChatClient(runner.url("chatui"))
    client.await_scenario(timeout=60)
    report(CHATUI_IMAGE, runner, runner.storage_path, closing=None)

    confirmed = ask(IMAGE_INSTRUCTIONS)

    signals = [event.payload.signal for event in runner.probe.events(IMAGE_TOPIC)]
    mentions = [mention for signal in signals for mention in signal.mentions]
    print(f"\n  The service recorded {len(signals)} image signal(s):")
    for signal in signals:
        print(f"    {signal.id} {tuple(signal.ruler.bounds)} {signal.files}")
        for mention in signal.mentions:
            print(f"      {bounds_of(mention)}  {label_of(mention)!r}")

    assert confirmed, "the person said the image annotator did not work"
    # As with the conversation above, the person's answer is not the only
    # evidence: a "y" pressed without looking cannot make an empty submission
    # or an unlabelled box pass.
    assert signals, "no image signal was published"
    assert len(mentions) >= 2, f"expected at least two regions, got {len(mentions)}"
    assert all(label_of(mention) for mention in mentions), (
        f"every region should carry a label: {[label_of(m) for m in mentions]}")
    assert all(bounds_of(mention) != tuple(signals[0].ruler.bounds) for mention in mentions), (
        "every region covers the whole image, which is not what was asked for")
    # cltl-eliza is not in this topology, so this is about the event, not the
    # silence of a module that is not running.
    assert not runner.probe.events(TEXT_IN), (
        "submitting an image published on the utterance topic, which would make "
        "the agent answer it")

    for signal in signals:
        stored = runner.storage_path / "emissor" / scenario.id / "image" / f"{signal.id}.png"
        assert stored.exists(), f"the pixels never reached the scenario folder: {stored}"

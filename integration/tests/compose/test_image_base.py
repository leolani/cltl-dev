"""Each component image is built on the base its Dockerfile names, and the slim
ones really are slim.

Worth a test rather than a code comment because the regression it guards is
silent in both directions. `docker_base` in `util/make/makefile.component.mk`
defaulted to the full `cltl-base` and `makefile.docker.mk` passed it as
`--build-arg base_image=...` unconditionally, so it overrode the `ARG base_image`
default in every Dockerfile. Components that had already been pointed at
`cltl-base-slim` kept being built on the 4 GB base, and nothing said so: the
image works, the tests pass, it is just eight times the size it should be.

The inverse is equally quiet. An image moved to the slim base keeps working right
up to the first request that needs a library only the full base has — a Pillow
import, a `cv2.imwrite`, a PyAudio device — and that request may be a deployment
away. So this does not merely compare a label: it imports the component's own
container and the third-party modules it reaches for, inside the image.

Marked ``compose`` because it needs the built images on the daemon. It starts no
stack, so it costs a second or two.
"""
import json
import subprocess

import pytest

from cltl_integration.modules import BY_KEY, MODULES
from cltl_integration.runner.compose import COMPONENT_ROOT

pytestmark = pytest.mark.compose

SLIM_BASE = "ghcr.io/leolani/cltl-base-slim"
FULL_BASE = "ghcr.io/leolani/cltl-base"

# A module key is not a directory name: cltl-chat-ui's key is "chatui" and
# cltl-emissor-data's is "emissor". The image name is, modulo the registry
# prefix, which is also what the component's own makefile derives it from.
COMPONENT_DIRS = {module.key: module.image.rsplit("/", 1)[-1] for module in MODULES}

# What each image must be able to import, beyond its own DI container. These are
# the module-level third-party imports that a wrong base image takes away:
#
#   PIL      cltl/monitoring/render.py draws the labelled boxes with it, and
#            api.py and memory.py annotate with PIL.Image.Image.
#   gtts     cltl.backend.source.local_tts imports it unguarded, and
#            cltl_service.backend.backend_container imports that at module level.
#   cv2      cltl.backend.impl.cached_storage writes stored images as PNG, and
#            cltl_service.chatui.service decodes uploads. Both import it inside a
#            `try`, so a missing cv2 degrades silently rather than failing — which
#            is exactly why it is asserted here.
#   soundfile  cltl.backend.impl.cached_storage reads and writes the audio.
REQUIRED_IMPORTS = {
    "backend": ("gtts", "cv2", "soundfile", "requests"),
    "chatui": ("cv2", "numpy"),
    "context": (),
    "monitoring": ("PIL.Image", "PIL.ImageDraw", "PIL.ImageFont"),
    # cltl.emissordata.file_storage guards both of these, and degrades to
    # `audio_loader = None` / `image_loader = None` when either is missing — it
    # logs a warning and carries on storing signals whose pixels and samples it
    # can no longer read back. So for this component the assertion is not about
    # the build failing; it is about the image being able to do its job at all.
    "emissor": ("cv2", "soundfile", "numpy"),
}

# Nothing on the slim base may reach these. torch and friends are the bulk of the
# 4 GB full base; pyaudio is the one that needs a compiler and portaudio headers.
FORBIDDEN_IMPORTS = ("torch", "transformers", "pyaudio", "whisper", "matplotlib")

# Generous, and deliberately so: the point is a ceiling that catches a silent fall
# back to the full base, not a byte budget to tune. The slim images sit just under
# 500 MB and the full base is eight times that.
#
# The number is compared against `docker image inspect`'s `Size`, which is not
# what `docker images` prints: with the containerd snapshotter the former is the
# compressed content size (the full base reports ~956 MB) and the latter the
# unpacked disk usage (~3.93 GB for the same image). So the ceiling has to sit
# below the *compressed* size of the full base, not below its disk usage --
# 1.2 GB would have let a full-base build through.
SLIM_CEILING_BYTES = 700_000_000


def _declared_base(component_dir: str) -> str:
    """The base image the component's own Dockerfile names.

    Read from the Dockerfile rather than hardcoded here, so this test tracks the
    components' intent instead of asserting a second, drifting copy of it.
    """
    dockerfile = COMPONENT_ROOT.parent / component_dir / "Dockerfile"
    assert dockerfile.is_file(), f"no Dockerfile for {component_dir}"

    for line in dockerfile.read_text().splitlines():
        if line.startswith("ARG base_image="):
            return line.split("=", 1)[1].strip()

    raise AssertionError(f"{dockerfile} declares no `ARG base_image`")


def _inspect(image: str) -> dict:
    result = subprocess.run(["docker", "image", "inspect", image],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f"{image} is not on the daemon — run `make -C integration docker-images`: "
        f"{result.stderr}")

    return json.loads(result.stdout)[0]


def _run(image: str, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c", code],
                          capture_output=True, text=True, timeout=300)


# The components this repo builds on the slim base. "emissor" is
# cltl-emissor-data — see COMPONENT_DIRS above on keys versus directory names.
# cltl-asr, cltl-vad and cltl-eliza still name the full base in their Dockerfiles:
# they carry whisper, webrtcvad and the NLP stack respectively.
SLIM_KEYS = ("backend", "chatui", "context", "monitoring", "emissor")


@pytest.fixture(scope="module", params=SLIM_KEYS)
def slim_module(request):
    return BY_KEY[request.param]


class TestDeclaredBase:
    """The Dockerfiles say slim, and the built images have to agree."""

    @pytest.mark.parametrize("key", SLIM_KEYS)
    def test_dockerfile_declares_the_slim_base(self, key):
        declared = _declared_base(COMPONENT_DIRS[key])

        assert declared.startswith(SLIM_BASE), (
            f"{COMPONENT_DIRS[key]}/Dockerfile names {declared}; these components "
            f"process audio and images that arrived over the event bus or HTTP and "
            f"open no capture device, so they belong on {SLIM_BASE}")

    @pytest.mark.parametrize("key", SLIM_KEYS)
    def test_the_built_image_is_not_the_full_base(self, key):
        """Size is the cheap half of the evidence; the import checks are the rest.

        A build that ignores the Dockerfile's `ARG base_image` leaves no trace in
        the image's own labels — it is the same Dockerfile either way — so bulk is
        all there is to look at. On its own that makes for a brittle assertion,
        which is why `TestTheImageCanDoItsJob.test_the_heavy_stack_is_absent`
        asserts the same conclusion from what the image can import.
        """
        size = _inspect(f"{BY_KEY[key].image}:latest")["Size"]

        assert size < SLIM_CEILING_BYTES, (
            f"{BY_KEY[key].image}:latest reports {size / 1e6:.0f} MB, over the "
            f"{SLIM_CEILING_BYTES / 1e6:.0f} MB ceiling, which means it was built on "
            f"{FULL_BASE} despite its Dockerfile naming the slim base. Check that "
            f"`docker_base` is unset so --build-arg does not override it.")


class TestTheImageCanDoItsJob:
    """A slim base that silently drops a library the component needs."""

    def test_the_di_container_imports(self, slim_module):
        module_path, class_name = slim_module.container_ref()

        result = _run(f"{slim_module.image}:latest",
                      f"import importlib; "
                      f"m = importlib.import_module({module_path!r}); "
                      f"assert hasattr(m, {class_name!r}), {class_name!r}; "
                      f"print('ok')")

        assert result.returncode == 0, (
            f"{slim_module.image}:latest cannot import {slim_module.container}:\n"
            f"{result.stderr}")

    def test_the_third_party_imports_it_needs_are_present(self, slim_module):
        required = REQUIRED_IMPORTS[slim_module.key]
        if not required:
            pytest.skip(f"{slim_module.key} needs nothing beyond its own dependencies")

        code = "; ".join(f"import {name}" for name in required) + "; print('ok')"
        result = _run(f"{slim_module.image}:latest", code)

        assert result.returncode == 0, (
            f"{slim_module.image}:latest is missing one of {required}. The slim base "
            f"carries these; a component image that cannot import them will fail at "
            f"runtime, not at startup:\n{result.stderr}")

    def test_the_heavy_stack_is_absent(self, slim_module):
        """Not a size assertion in disguise: each of these costs a real dependency.

        `pyaudio` in particular has no aarch64 wheel, so its presence means the
        image compiled it — and therefore carries portaudio headers and a
        toolchain it has no use for.
        """
        code = (f"import importlib; "
                f"present = [n for n in {FORBIDDEN_IMPORTS!r} "
                f"           if importlib.util.find_spec(n) is not None]; "
                f"print(','.join(present))")
        result = _run(f"{slim_module.image}:latest", code)
        assert result.returncode == 0, result.stderr

        present = [name for name in result.stdout.strip().split(",") if name]

        assert not present, (
            f"{slim_module.image}:latest carries {present}, which the slim base does "
            f"not provide — so this image is built on {FULL_BASE}")


class TestHealthcheck:
    """The probe has to exist in the image that declares it."""

    def test_the_healthcheck_does_not_shell_out_to_curl(self, slim_module):
        """python:3.10-slim ships no curl, so a curl probe reports unhealthy forever.

        Nothing in the compose file gates on a component's health today — only
        rabbitmq has a `condition: service_healthy` depending on it — which is
        precisely why this would go unnoticed until someone added one.
        """
        config = _inspect(f"{slim_module.image}:latest")["Config"]
        test = config.get("Healthcheck", {}).get("Test", [])

        assert test, f"{slim_module.image}:latest declares no HEALTHCHECK"
        assert not any("curl" in part for part in test), (
            f"{slim_module.image}:latest probes with curl ({test}), which "
            f"{SLIM_BASE} does not contain")

    def test_the_healthcheck_command_runs(self, slim_module):
        """The probe, run verbatim, against an image where nothing is listening.

        Taken from the image's own HEALTHCHECK rather than retyped, so this cannot
        drift from what Docker actually executes. Exit 1 is the expected outcome —
        there is no server in a bare `docker run` — and what it proves is that the
        command is executable at all and its imports resolve, which is what curl's
        absence broke. A probe that cannot run exits 2 on a SyntaxError or 127 on a
        missing binary, so the distinction is the whole point.
        """
        test = _inspect(f"{slim_module.image}:latest")["Config"]["Healthcheck"]["Test"]
        # ["CMD", "python", "-c", "<probe>"] — drop compose's CMD marker.
        argv = test[1:] if test and test[0] in ("CMD", "CMD-SHELL") else test

        result = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", argv[0],
             f"{slim_module.image}:latest", *argv[1:]],
            capture_output=True, text=True, timeout=300)

        assert result.returncode == 1, (
            f"the healthcheck probe in {slim_module.image}:latest exited "
            f"{result.returncode}, not 1 — it is not runnable in the image that "
            f"declares it:\n{result.stdout}\n{result.stderr}")
        assert "Traceback" not in result.stdout + result.stderr, (
            f"the probe reports a failure as a traceback, which ends up in "
            f"`docker inspect .State.Health.Log`:\n{result.stdout}\n{result.stderr}")

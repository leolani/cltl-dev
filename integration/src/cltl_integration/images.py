"""Check that the container images tier 2 needs are actually on the daemon.

Run by ``make test-compose`` before pytest. Worth a separate step: the compose
file pins no digests and every component tags ``latest``, so a stack built from
last month's source comes up perfectly happily and reports green. Failing here
with the build command is more useful than passing a test of stale code.
"""
import sys

from cltl_integration.modules import MODULES
from cltl_integration.runner.compose import missing_images
from cltl_integration.topology import Topology

BUILD_HINT = """
Build the component images from the current source:

    for c in {components}; do make -C ../$c docker-ghcr-build; done

They layer on ghcr.io/leolani/cltl-base, which is published:

    docker pull ghcr.io/leolani/cltl-base:latest
    docker pull rabbitmq:3.12-management
"""


def main() -> int:
    everything = Topology(name="all", modules=tuple(module.key for module in MODULES))
    missing = missing_images(everything)
    if not missing:
        print(f"All {len(MODULES) + 1} tier-2 images are present.")
        return 0

    components = " ".join(image.rsplit("/", 1)[-1].split(":")[0]
                          for image in missing if "leolani" in image)
    print(f"Missing images: {', '.join(missing)}", file=sys.stderr)
    print(BUILD_HINT.format(components=components or "<component>"), file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())

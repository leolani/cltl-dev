"""Build-proof tests: the offline venv really does contain the platform.

These exist because a green build was, until now, no evidence of anything. The
``venv:`` recipe in ``util/make/makefile.py.base.mk`` chained its steps with
``;`` rather than ``&&``, so a failed ``pip install`` left the recipe exiting 0
and the following ``touch venv`` marked the target up to date. A venv containing
only pip/setuptools/wheel looked identical to a working one.

They are also the cheapest packaging test in the repo: this component installs
the *published sdists* from ``cltl-requirements/leolani``, not the source trees,
so a module whose ``setup.py`` fails to package a file fails here and nowhere
else.
"""
import importlib

import pytest

from cltl_integration.modules import BY_KEY, MODULES, Module

# Modules whose container cannot currently be imported from the published sdist.
# Entries are marked xfail(strict=True), so fixing the packaging bug turns the
# test red and forces the entry to be removed rather than quietly lingering.
#
# Empty on purpose. It held ``cltl-context`` until its container was moved from
# the namespace root (src/cltl_service/context_container.py, which
# find_namespace_packages(include=['cltl.*', 'cltl_service.*']) does not match)
# into src/cltl_service/context/container.py. Every component's setup.py uses
# that same include list, so the next component to put a module at the namespace
# root hits the identical bug and belongs here until it is fixed.
KNOWN_UNPACKAGED = {}


def _param(module: Module):
    marks = []
    if module.key in KNOWN_UNPACKAGED:
        marks.append(pytest.mark.xfail(strict=True, reason=KNOWN_UNPACKAGED[module.key]))
    return pytest.param(module, id=module.key, marks=marks)


MODULE_PARAMS = [_param(module) for module in MODULES]


class TestRegistry:
    def test_registry_is_populated(self):
        assert MODULES, "the module registry must not be empty"

    def test_keys_are_unique(self):
        keys = [module.key for module in MODULES]
        assert len(keys) == len(set(keys)), f"duplicate keys in MODULES: {keys}"

    def test_by_key_covers_every_module(self):
        assert set(BY_KEY) == {module.key for module in MODULES}

    @pytest.mark.parametrize("module", MODULES, ids=lambda m: m.key)
    def test_container_ref_is_well_formed(self, module: Module):
        module_path, class_name = module.container_ref()
        assert module_path and class_name

    def test_known_unpackaged_keys_exist(self):
        """Guard against the xfail list drifting out of sync with the registry."""
        assert set(KNOWN_UNPACKAGED) <= set(BY_KEY), (
            f"KNOWN_UNPACKAGED names modules that are not in the registry: "
            f"{set(KNOWN_UNPACKAGED) - set(BY_KEY)}"
        )


class TestPlatformIsInstalled:
    """If the offline install silently did nothing, these are what catch it."""

    def test_combot_infra_is_importable(self):
        from cltl.combot.infra.event.memory import SynchronousEventBus

        bus = SynchronousEventBus()
        received = []
        bus.subscribe("test.topic", received.append)

        from cltl.combot.infra.event import Event

        bus.publish("test.topic", Event.for_payload("hello"))

        assert [event.payload for event in received] == ["hello"]

    def test_emissor_round_trips_an_event(self):
        """Kombu serialises events with these functions; if they break, tier 2 breaks."""
        from cltl.combot.infra.event.api import Event

        from cltl_integration.serialization import deserializer, serializer

        restored = deserializer(serializer(Event.for_payload({"text": "hello"})))

        assert restored.payload["text"] == "hello"

    @pytest.mark.parametrize("module", MODULE_PARAMS)
    def test_container_class_is_importable(self, module: Module):
        module_path, class_name = module.container_ref()

        imported = importlib.import_module(module_path)

        assert hasattr(imported, class_name), (
            f"{module_path} does not define {class_name}"
        )
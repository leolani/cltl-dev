SHELL = /bin/bash

project_dependencies ?= $(addprefix $(project_root)/, \
		emissor \
		cltl-requirements \
		cltl-combot \
		cltl-backend \
		cltl-context \
		cltl-emissor-data \
		cltl-vad \
		cltl-asr \
		cltl-eliza \
		cltl-chat-ui)

git_remote ?= https://github.com/leolani

# This component references the shared build system in the meta-repo root rather
# than carrying a nested util/ submodule copy. It is a consumer of the platform,
# never a published artifact, so a nested copy would only add a second thing to
# keep in sync (cltl-requirements/util/ is already stale for exactly that reason).
include ../util/make/makefile.base.mk
include ../util/make/makefile.git.mk

# makefile.py.base.mk is deliberately NOT included. Its `build` target chains to
# `py-install`, which copies an sdist into cltl-requirements/leolani — the
# registry every component and Dockerfile installs from. This component must
# never publish itself there, so it defines its own venv/test recipes below.

project_mirror ?= $(project_root)/cltl-requirements/mirror
project_repo ?= $(project_root)/cltl-requirements/leolani

PYTEST ?= venv/bin/pytest
PYTEST_FLAGS ?= -v

TIER1_MARKERS = not compose and not manual
TIER2_MARKERS = compose and not manual
MANUAL_MARKERS = manual


.PHONY: build
build: venv

# Unlike util/make/makefile.py.base.mk, every step is chained with && so a failed
# install fails the target. A venv holding only pip/setuptools/wheel must never
# be reported as a successful build.
# The `|| rm -rf venv` is not belt and braces: `python -m venv` creates the
# directory before pip runs, and make treats an existing directory as an
# up-to-date target, so a half-installed venv makes every later `make build`
# report "Nothing to be done" over an environment that cannot import anything.
venv: requirements.txt VERSION
	$(info Create virtual environment for $(project_name))

	python -m venv venv
	{ source venv/bin/activate && \
		pip install --upgrade pip && \
		pip install wheel && \
		pip install -r requirements.txt --upgrade --upgrade-strategy eager --pre \
			--no-index --find-links="$(project_mirror)" --find-links="$(project_repo)"; \
	} || { rm -rf venv; exit 1; }
	touch venv


.PHONY: test
test: build
	$(PYTEST) -m "$(TIER1_MARKERS)" $(PYTEST_FLAGS)

# Tier 2 runs the images, so check they exist before pytest spends three
# minutes finding out. See src/cltl_integration/images.py.
.PHONY: docker-images
docker-images: build
	@PYTHONPATH=src venv/bin/python -m cltl_integration.images

.PHONY: test-compose
test-compose: docker-images
	$(PYTEST) -m "$(TIER2_MARKERS)" $(PYTEST_FLAGS)

.PHONY: test-all
test-all: docker-images
	$(PYTEST) -m "not manual" $(PYTEST_FLAGS)

# Manual tests need a person at the keyboard, so pytest's output capture has to
# go (-s) or the prompts never appear. Not part of test/test-compose/test-all.
# A manual test that also needs images says so itself, naming the missing ones.
.PHONY: test-manual
test-manual: build
	$(PYTEST) -m "$(MANUAL_MARKERS)" $(PYTEST_FLAGS) -s


# Re-render the committed speech fixtures from espeak-ng. Needed only when a
# phrase is added to src/cltl_integration/fixtures.py; the output is
# byte-reproducible, so `make speech-fixtures && git diff fixtures/` is empty
# unless something actually changed. Tests prefer the binary and fall back to
# these files, so a machine without espeak-ng still runs the audio tiers.
.PHONY: speech-fixtures
speech-fixtures: build
	@PYTHONPATH=src venv/bin/python -m cltl_integration.fixtures


# The demo front end: the same topologies the tests use, run without assertions.
# `make demos` lists them; `make demo-<name>` runs one and blocks until Ctrl-C.
# Underscores and hyphens both work, so demo-text-pipeline finds text_pipeline.
# Pass flags through DEMO_FLAGS, e.g.
#     make demo-audio_pipeline DEMO_FLAGS='--tier compose --say "I feel sad"'
.PHONY: demos
demos: build
	@PYTHONPATH=src venv/bin/python -m cltl_integration --list

# Deliberately not .PHONY: GNU make does not honour a pattern there, and nothing
# on disk is ever named demo-<something>, so the rule runs regardless.
demo-%: build
	@PYTHONPATH=src venv/bin/python -m cltl_integration $* $(DEMO_FLAGS)


# This component is a consumer of the registry, never a producer: it ships no
# image and no sdist. The target exists so that `make docker-ghcr-build` at the
# repo root, which walks every component, does not stop here.
.PHONY: docker-ghcr-build docker-ghcr-push
docker-ghcr-build docker-ghcr-push:
	@echo "Skip $@ for $(project_name): it publishes no image"


.PHONY: clean
clean: integration-clean

.PHONY: integration-clean
integration-clean:
	$(info Clean $(project_name))
	@rm -rf venv .pytest_cache storage .demo
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +

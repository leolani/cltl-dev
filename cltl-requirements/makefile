SHELL = /bin/bash

project_root = $(realpath ..)
project_name = $(notdir $(realpath .))
project_version = $(shell cat VERSION)

# We use this makefile from tests
makefile_dir := $(dir $(lastword $(MAKEFILE_LIST)))
include $(makefile_dir)util/make/makefile.base.mk
include $(makefile_dir)util/make/makefile.git.mk


mirror := mirror
mirror_lock := requirements.lock
test_mirror_lock := requirements.test.lock
artifacts := leolani


.DEFAULT_GOAL := build


.PHONY: clean
clean:
	$(info Clean $(project_name))
	@rm -rf $(mirror) $(mirror_lock) $(test_mirror_lock) $(artifacts) docker.base.lock docker.slim.lock
	@mkdir $(mirror) $(artifacts)

$(mirror_lock): requirements.txt
	$(info Download to mirror: $(shell python --version))
	@pip install --upgrade pip; \
	pip install wheel; \
	pip download --requirement requirements.txt -d mirror \
		| tee $(shell tty) | grep Collecting | cut -f 2 -d ' ' > $(mirror_lock)

$(test_mirror_lock): requirements.test.txt
	$(info Download test dependencies to mirror)
	@pip download --requirement requirements.test.txt -d $(mirror) \
		| tee /dev/stderr | grep Collecting | cut -f 2 -d ' ' > $(test_mirror_lock)

.PHONY:
build: $(mirror_lock) $(test_mirror_lock) $(artifacts)

$(artifacts):
	@mkdir -p $(artifacts)


install: docker

.PHONY: docker
docker: docker.base.lock docker.slim.lock

# Prerequisites are the two files the build actually reads. `requirements.txt` was
# named here instead of `requirements.base.txt`, which Dockerfile.base COPYs, so
# editing the base dependency list did not rebuild the image while editing an
# unrelated file did. The Dockerfile belongs here for the same reason.
docker.base.lock: requirements.base.txt Dockerfile.base
	DOCKER_BUILDKIT=1 docker build -t ghcr.io/leolani/cltl-base:$(project_version) -t ghcr.io/leolani/cltl-base:latest -f Dockerfile.base .
	@touch docker.base.lock

.PHONY: docker-ghcr-build
docker-ghcr-build: docker

.PHONY: docker-ghcr-push
docker-ghcr-push: docker-ghcr-base-push docker-ghcr-slim-push

.PHONY: docker-ghcr-base-push
docker-ghcr-base-push: docker-ghcr-build
	docker push ghcr.io/leolani/cltl-base:$(project_version)
	docker push ghcr.io/leolani/cltl-base:latest

docker.slim.lock: requirements.slim.txt Dockerfile.slim
	DOCKER_BUILDKIT=1 docker build -t ghcr.io/leolani/cltl-base-slim:$(project_version) -t ghcr.io/leolani/cltl-base-slim:latest -f Dockerfile.slim .
	@touch docker.slim.lock

.PHONY: docker-slim
docker-slim: docker.slim.lock

# Named docker-ghcr-slim-push to match the .PHONY above and what docker-ghcr-push
# depends on. It was defined as `docker-slim-ghcr-push`, so the dependency
# resolved to a .PHONY name with no recipe -- which make considers satisfied
# without doing anything. `make docker-ghcr-push` pushed the full base and
# silently skipped the slim one, leaving the registry's cltl-base-slim:latest
# behind every component image built on it.
.PHONY: docker-ghcr-slim-push
docker-ghcr-slim-push: docker-slim
	docker push ghcr.io/leolani/cltl-base-slim:$(project_version)
	docker push ghcr.io/leolani/cltl-base-slim:latest

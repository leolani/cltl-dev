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
artifacts := leolani


.DEFAULT_GOAL := build


.PHONY: clean
clean:
	$(info Clean $(project_name))
	@rm -rf $(mirror) $(mirror_lock) $(artifacts) docker.base.lock docker.slim.lock
	@mkdir $(mirror) $(artifacts)

$(mirror_lock): requirements.txt
	$(info Download to mirror: $(shell python --version))
	@pip install --upgrade pip; \
	pip install wheel; \
	pip download --requirement requirements.txt -d mirror \
		| tee $(shell tty) | grep Collecting | cut -f 2 -d ' ' > $(mirror_lock)

.PHONY:
build: $(mirror_lock) $(artifacts)

$(artifacts):
	@mkdir -p $(artifacts)


install: docker

.PHONY: docker
docker: docker.base.lock

docker.base.lock: requirements.txt
	DOCKER_BUILDKIT=1 docker build -t ghcr.io/leolani/cltl-base:$(project_version) -t ghcr.io/leolani/cltl-base:latest -f Dockerfile.base .
	@touch docker.base.lock

.PHONY: docker-ghcr-build
docker-ghcr-build: docker.base.lock

.PHONY: docker-ghcr-push
docker-ghcr-push: docker-ghcr-build
	docker push ghcr.io/leolani/cltl-base:$(project_version)
	docker push ghcr.io/leolani/cltl-base:latest

docker.slim.lock: requirements.slim.txt
	DOCKER_BUILDKIT=1 docker build -t ghcr.io/leolani/cltl-base-slim:$(project_version) -t ghcr.io/leolani/cltl-base-slim:latest -f Dockerfile.slim .
	@touch docker.slim.lock

.PHONY: docker-slim
docker-slim: docker.slim.lock

.PHONY: docker-slim-ghcr-push
docker-slim-ghcr-push: docker-slim
	docker push ghcr.io/leolani/cltl-base-slim:$(project_version)
	docker push ghcr.io/leolani/cltl-base-slim:latest

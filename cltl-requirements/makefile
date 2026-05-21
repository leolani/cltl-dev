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
	@rm -rf $(mirror) $(mirror_lock) $(artifacts) docker.base.lock
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
	DOCKER_BUILDKIT=1 docker build -t cltl/cltl-base:latest -f Dockerfile.base .
	@touch docker.base.lock

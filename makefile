SHELL = /bin/bash

project_name ?= "eliza-app"

project_components = $(addprefix ${project_root}/, \
		emissor \
		cltl-requirements \
		cltl-combot \
		cltl-backend \
		cltl-context \
		cltl-eliza \
		cltl-chat-ui \
		cltl-emissor-data \
		cltl-vad \
		cltl-asr \
		app)

git_local ?= ..
git_remote ?= https://github.com/leolani


include util/make/makefile.parent.mk
include util/make/makefile.git.mk

.PHONY: docker-ghcr-build
docker-ghcr-build:
	$(MAKE) target=docker-ghcr-build

.PHONY: docker-ghcr-push
docker-ghcr-push:
	$(MAKE) target=docker-ghcr-push


submodules := $(shell git submodule | xargs -L1 | cut -f 2 -d ' ' | grep -v util | xargs)

.PHONY: update-build
update-build:
	-git submodule foreach 'git submodule update --remote util | :'

	for sm in $(submodules); do \
		cd $$sm; \
		(git diff --cached --quiet \
		  && git add util \
			&& git commit -m "Updated cltl-build") \
			  || (echo "Stash is not empty"; exit 1); \
		cd -; \
	done

	(git diff --cached --quiet \
	  && git add $(submodules) \
		&& git commit -m "Updated cltl-build") \
		  || (echo "Stash is not empty"; exit 1)

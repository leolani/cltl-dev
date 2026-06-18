SHELL = /bin/bash
artifact_name = "cltl.eliza-app"

project_dependencies ?= $(addprefix $(project_root)/, \
		emissor \
		cltl-combot \
		cltl-requirements \
		cltl-backend \
		cltl-context \
		cltl-emissor-data \
		cltl-vad \
		cltl-asr \
		cltl-eliza \
		cltl-chat-ui)

git_remote ?= https://github.com/leolani

include util/make/makefile.base.mk
include util/make/makefile.component.mk
include util/make/makefile.docker.mk
include util/make/makefile.py.base.mk
include util/make/makefile.git.mk

.PHONY: build
build: venv

.PHONY: docker-ghcr-build
docker-ghcr-build:
	DOCKER_BUILDKIT=1 docker build \
		--build-context leolani=${project_repo} \
		--build-arg base_image=${docker_base} \
		-f docker-app/app.Dockerfile \
		-t ${ghcr_registry}/eliza-app:${docker_version} \
		-t ${ghcr_registry}/eliza-app:latest \
		.

.PHONY: docker-ghcr-push
docker-ghcr-push: docker-ghcr-build
	docker push ${ghcr_registry}/eliza-app:${docker_version}
	docker push ${ghcr_registry}/eliza-app:latest

INTEGRATION_DIR = tests/integration

.PHONY: test-integration-text test-integration-csplit-text \
        test-integration-audio test-integration-csplit-audio \
        test-integration

test-integration-text:
	cd docker-app && source ../venv/bin/activate && \
		pytest $(INTEGRATION_DIR)/test_text_conversation.py -v

test-integration-csplit-text:
	cd docker-app && source ../venv/bin/activate && \
		pytest $(INTEGRATION_DIR)/test_csplit_text_conversation.py -v

test-integration-audio:
	cd docker-app && source ../venv/bin/activate && \
		pytest $(INTEGRATION_DIR)/test_audio_conversation.py -v

test-integration-csplit-audio:
	cd docker-app && source ../venv/bin/activate && \
		pytest $(INTEGRATION_DIR)/test_csplit_audio_conversation.py -v

test-integration: test-integration-text test-integration-csplit-text \
                  test-integration-audio test-integration-csplit-audio

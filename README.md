# cltl-dev

Development environment for the Leolani platform.

This repository contains every component of the platform as _git_ submodules and
is the root from which the shared
[build commands](https://github.com/leolani/cltl-build/tree/main/make) are run
across them.

It ships **no application**. Each component publishes its own
`ghcr.io/leolani/cltl-*` image and its own sdist into `cltl-requirements/`; what
a deployment looks like is decided by the deployment. The example ELIZA app that
used to live in `app/` was removed once `integration/` covered what it was
actually being used for.

## Check-out

Clone this repository including all submodules:

        git clone --recurse-submodules -j8 <this repository>

## Build

From the repository root. Run it twice — the first pass builds the dependencies,
the second links them:

        make build
        make build

## See it running

`integration/` composes the modules and runs them, with assertions (the tests) or
without (the demos):

        make -C integration test           # tier 1: in-process, ~4 min, no Docker
        make -C integration test-compose   # tier 2: the real images, ~20 min
        make -C integration demos          # what there is to run
        make -C integration demo-text-pipeline

`make -C integration demo-text-pipeline` prints the URLs it discovered, chat page
included, and blocks until Ctrl-C. See
[`integration/README.md`](integration/README.md) and
[`docs/integration-testing-design.md`](docs/integration-testing-design.md).

## Development

Follow the instructions in [cltl-combot](https://github.com/leolani/cltl-combot).

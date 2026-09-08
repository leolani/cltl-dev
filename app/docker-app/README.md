# Eliza App — Docker Compose Setup

This directory contains the Docker Compose configuration for running the Eliza App as a set of
containerised microservices connected through a RabbitMQ message bus.

## Prerequisites

| Tool | Minimum version |
|---|---|
| Docker | 23.0 (BuildKit required) |
| Docker Compose | 2.10 (for `additional_contexts` and `args` under `build`) |

Before building the component images you must build the shared base image once:

```bash
cd ../../cltl-requirements
make docker          # builds cltl/cltl-base:latest
```

## Directory layout

```
docker-app/
├── app.Dockerfile       # Dockerfile for the eliza-app orchestration container
├── app.py               # Entry point executed inside eliza-app
├── docker-compose.yml   # Service definitions
├── config/
│   ├── default.config   # Full application configuration (mounted read-only)
│   ├── custom.config    # Overrides for default.config (mounted read-only)
│   └── logging.config   # Python logging configuration
└── storage/             # Persistent data written by running containers
    ├── audio/
    ├── emissor/
    ├── event_log/
    └── rabbitmq/
```

## Services

### rabbitmq

The RabbitMQ message broker that connects all components.

| Detail | Value |
|---|---|
| Image | `rabbitmq:3.12-management` |
| AMQP port | `5672` (host) → `5672` (container) |
| Management UI | `15672` (host) → `15672` (container) |
| Default user | `eliza` / `eliza123` |
| Data volume | `./storage/rabbitmq` |

All other services declare `depends_on: rabbitmq: condition: service_healthy` and will not start
until RabbitMQ reports healthy.

### eliza-backend

Provides a REST API for raw audio/video signals and manages device access.

| Detail | Value |
|---|---|
| Image | `cltl/eliza-backend` |
| Port | `8001` (host) → `8000` (container) |
| Config mount | `./config` → `/cltl-backend/config` |
| Storage mount | `./storage` → `/cltl-backend/storage` |

### eliza-vad

Voice Activity Detection — subscribes to microphone events and publishes VAD annotations.

| Detail | Value |
|---|---|
| Image | `cltl/eliza-vad` |
| Config mount | `./config` → `/cltl-vad/config` |
| Storage mount | `./storage` → `/cltl-vad/storage` |

### eliza-asr

Automatic Speech Recognition — transcribes audio referenced in VAD events to text.  
Default implementation: Whisper (`base` model, English).

| Detail | Value |
|---|---|
| Image | `cltl/eliza-asr` |
| Config mount | `./config` → `/cltl-asr/config` |
| Storage mount | `./storage` → `/cltl-asr/storage` |

### eliza-emissor

Persists all EMISSOR-structured events (scenarios, signals, annotations) to disk.

| Detail | Value |
|---|---|
| Image | `cltl/eliza-emissor` |
| Port | `8002` (host) → `8000` (container) |
| Config mount | `./config` → `/cltl-emissor-data/config` |
| Storage mount | `./storage` → `/cltl-emissor-data/storage` |

### eliza-eliza

The ELIZA conversational module — subscribes to text input events and publishes responses.

| Detail | Value |
|---|---|
| Image | `cltl/eliza-eliza` |
| Config mount | `./config` → `/cltl-eliza/config` |
| Storage mount | `./storage` → `/cltl-eliza/storage` |

### eliza-context

Manages conversation context and state transitions (BDI model, keyword detection, scenario lifecycle).

| Detail | Value |
|---|---|
| Image | `cltl/eliza-context` |
| Config mount | `./config` → `/cltl-context/config` |
| Storage mount | `./storage` → `/cltl-context/storage` |

### eliza-chatui

Web-based chat interface — subscribes to text output events and publishes user text input.

| Detail | Value |
|---|---|
| Image | `cltl/eliza-chatui` |
| Port | `8003` (host) → `8000` (container) |
| Config mount | `./config` → `/cltl-chatui/config` |
| Storage mount | `./storage` → `/cltl-chatui/storage` |

### eliza-monitoring

Serves a per-scenario view of what the platform perceived: the most recent image of a
conversation, with the regions annotated on it. The chat UI's **Monitoring** tab embeds this
page in an iframe, which is why the port is published — the browser loads it directly, so
`[cltl.chat-ui] monitoring_url` must name a host port (`http://localhost:8004/monitoring`) and
never the compose service name.

| Detail | Value |
|---|---|
| Image | `ghcr.io/leolani/cltl-monitoring` |
| Port | `8004` (host) → `8000` (container) |
| Config mount | `./config` → `/cltl-monitoring/config` |
| Storage mount | `./storage` → `/cltl-monitoring/storage` |

### eliza-app

Top-level orchestration container that coordinates startup and wires services together. Starts only
after all eight component services above are healthy.

| Detail | Value |
|---|---|
| Image | `cltl/eliza-app` |
| Build context | `app/` (parent of this directory) |
| Config mount | `./config` → `/app/config` |
| Storage mount | `./storage` → `/app/storage` |

## Network

### Monolithic stack (`docker-compose.yml`)

All services join a single `eliza-network` bridge network. Inter-service hostnames match the
`container_name` values (e.g. `rabbitmq`, `eliza-backend`). These names are already used in
`config/default.config`:

```ini
[cltl.event.kombu]
server: amqp://eliza:eliza123@rabbitmq:5672/

[cltl.backend]
server_audio_url: http://eliza-backend:8000/host
```

### Client/server split (`docker-compose.server.yml` + `docker-compose.client.yml`)

The two stacks use **independent** Docker networks and communicate only through the server's
exposed host ports:

| Server port (host) | Purpose |
|---|---|
| `5672` | RabbitMQ AMQP — client services subscribe and publish here |
| `8001` | Storage REST API — client backend uploads audio/image here |

**Server stack** creates `eliza-network` for its own internal service-to-service communication.
It does not share this network with the client stack.

**Client stack** creates `eliza-client-network` as its own isolated bridge. All four client
services (`eliza-backend`, `eliza-context`, `eliza-chatui`, `eliza-app`) have
`extra_hosts: host.docker.internal:host-gateway` so they can resolve the server's host
address on Linux. On macOS/Windows Docker Desktop this name is resolved automatically and the
`extra_hosts` entry is a harmless no-op.

#### Same-machine deployment (local testing)

No configuration change is needed. The server's ports (5672, 8001) are exposed on the host, and
`host.docker.internal` inside each client container resolves to that host. Start the server
stack first, then the client stack:

```bash
docker compose -f docker-compose.server.yml up -d --wait
docker compose -f docker-compose.client.yml up -d --wait
```

#### Different-machine deployment (production)

Edit `config-client/custom.config` and replace `host.docker.internal` with the server machine's
IP address or hostname:

```ini
[cltl.event.kombu]
server: amqp://eliza:eliza123@<SERVER_IP>:5672/

[cltl.backend.remote_storage]
storage_url: http://<SERVER_IP>:8001/storage
```

Ensure TCP ports 5672 and 8001 are reachable from the client machine (check firewall rules on
the server side). The two stacks can then be started independently on their respective machines.

## Build

Docker Compose builds each image on first use. To pre-build all images explicitly:

```bash
docker compose build
```

Every `build` stanza passes two extra arguments:

| Argument | Purpose |
|---|---|
| `additional_contexts: leolani: ../../cltl-requirements/leolani` | Supplies local cltl.* package tarballs to each component image at build time |
| `args: base_image: cltl/cltl-base:latest` | Selects the shared base image |

### Overriding the base image

To use a different registry or tag for the base image, set `BASE_IMAGE` in a `.env` file next to
`docker-compose.yml`:

```
BASE_IMAGE=myregistry.example.com/cltl-base:v1.2
```

Then update `docker-compose.yml` to reference `${BASE_IMAGE}` in the `args:` section of each
service, or rebuild just the base image under the expected name:

```bash
cd ../../cltl-requirements
DOCKER_BUILDKIT=1 docker build -t myregistry.example.com/cltl-base:v1.2 -f Dockerfile.base .
```

## Running

The `eliza-backend` container does **not** open the microphone or camera directly. Instead it
connects to a `BackendServer` running on the **host machine**, which provides raw audio and image
over HTTP. You must start that server before bringing up the containers.

### 1. Start the host backend server

Install `cltl-backend` on the host (e.g. in its own virtual environment) and run:

```bash
python -m cltl.backend.server \
  --rate 16000 \
  --channels 1 \
  --frame_duration 30 \
  --port 8000
```

Leave this process running. It exposes:
- `GET /audio` — streaming 16-bit PCM audio from the microphone
- `GET /image` — single JPEG frame from the camera

### 2. Start the Docker Compose stack

```bash
# From this directory
docker compose up
```

To run in the background:

```bash
docker compose up -d
docker compose logs -f    # follow all logs
docker compose logs -f eliza-app eliza-asr    # follow specific services
```

Once all services are healthy, the application greets the user. At first startup the ASR module
may download the Whisper model, which can take several minutes.

The `eliza-backend` container reaches the host server via `host.docker.internal:8000` (configured
in `config/default.config`). On Linux the `extra_hosts: host.docker.internal:host-gateway` entry
in `docker-compose.yml` ensures this name resolves correctly; on Docker Desktop for Mac/Windows it
works automatically.

### Endpoints

| Service | URL |
|---|---|
| Chat UI | http://localhost:8003/chatui/static/chat.html |
| Backend REST API | http://localhost:8001 |
| EMISSOR data API | http://localhost:8002/emissor |
| Monitoring | http://localhost:8004/monitoring/static/monitoring.html?scenario=&lt;id&gt; |
| RabbitMQ management | http://localhost:15672 (user: `eliza`, pass: `eliza123`) |

## Configuration

All services share the same `config/` directory, mounted at different paths inside each container.
The active configuration is the merger of `default.config` and `custom.config`; settings in
`custom.config` override `default.config`.

Key sections in `default.config`:

| Section | Purpose |
|---|---|
| `[cltl.audio]` | Audio sampling rate (16 kHz), channels, frame size |
| `[cltl.asr]` | ASR implementation (`whisper`, `google`, `wav2vec`, `speechbrain`) and model |
| `[cltl.vad.webrtc]` | Activity window, threshold, gap, and padding in milliseconds |
| `[cltl.event.kombu]` | RabbitMQ connection string, exchange, compression |
| `[cltl.bdi]` | BDI state-machine transitions between conversation intentions |
| `[cltl.emissor-data]` | Path for EMISSOR scenario storage |

To change any setting without editing `default.config`, add the section and key to `custom.config`.
For example, to switch ASR to Google:

```ini
[cltl.asr]
implementation: google
```

### Logging

Logging is configured via `config/logging.config` (Python `fileConfig` format). The default
configuration writes `DEBUG`-level output from all loggers to stdout, with `INFO` for AMQP
internals and `WARNING` for Werkzeug.

The path is passed to each container via the `CLTL_LOGGING_CONFIG` environment variable.

## Stopping and cleaning up

```bash
docker compose down          # stop and remove containers
docker compose down -v       # also remove named volumes
```

Persistent data in `./storage/` is not removed by `docker compose down`. Remove it manually if
you want a clean slate:

```bash
rm -rf storage/emissor storage/event_log storage/audio storage/image
```

The RabbitMQ data in `storage/rabbitmq/` can also be deleted to reset queue state.

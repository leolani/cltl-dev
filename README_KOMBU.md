# External Messaging (Kombu + RabbitMQ)

> **Historical.** This records a migration that landed. The fixes to
> `cltl-combot` and `emissor` described below are in the code today, but the
> `app/` directory it refers to no longer exists — it was removed along with the
> root `docker-compose.yml`. For a working Kombu deployment see
> `integration/compose/docker-compose.yml` and `integration/config/tier-compose.config`;
> to run one, `make -C integration demo-<name> DEMO_FLAGS='--tier compose'`.

This document describes how the platform was moved onto external messaging with
Kombu and RabbitMQ instead of the in-memory synchronous event bus.

## Changes Made

### 1. EventBus Implementation
- **Fixed bug** in `cltl-combot/src/cltl/combot/infra/event/kombu.py` unsubscribe method
- **Added** proper resource cleanup with `close()` method
- **Added** custom serializer support via `event_bus_serializer` property in `KombuEventBusContainer`
  - Returns tuple of `(serializer_func, deserializer_func)` for complete round-trip serialization
  - Allows applications to use their own serialization logic (e.g., emissor utilities)
- **Updated** `app/py-app/app.py` to use `KombuEventBusContainer` instead of `SynchronousEventBusContainer`
- **Modified emissor core** at the source (`emissor/emissor/representation/util.py`)
  - **Added `PickleableDict` class** that extends `dict` with attribute access (`obj.attr` syntax)
  - **Updated `object_hook` function** to use `PickleableDict` instead of non-pickleable JSON namedtuples
  - **Comprehensive solution** that fixes compatibility issues system-wide:
    - **Fully Pickleable**: Compatible with external messaging systems (Kombu/RabbitMQ)
    - **Has `keys()` method**: Required by emissor `marshal()` function
    - **Supports attribute access**: Maintains object-like interface (`obj.attr`) for existing code
    - **Property-aware assignment**: Handles read-only properties and property setters correctly
    - **Backwards compatible**: Drop-in replacement for existing JSON namedtuples
- **Simplified `event_bus_serializer`** in `ApplicationContainer` to use standard emissor utilities
- **Added** comprehensive unit tests in `tests/infra/event/test_kombu_event_bus.py` for custom serializer functionality

### 2. Configuration
- **Updated** `app/py-app/config/default.config` with RabbitMQ connection settings
- **Created** `app/py-app/config/local.config` for local development without Docker
- **Added** Docker Compose setup with RabbitMQ

### 3. Infrastructure
- **Created** `docker-compose.yml` with RabbitMQ service
- **Created** `app/Dockerfile` for containerizing the application

## Running Options

### Option 1: Docker Compose (Recommended)
```bash
# Start RabbitMQ and the application
docker-compose up --build

# Access the application
http://localhost:8000/chatui/static/chat.html

# Access RabbitMQ Management UI
http://localhost:15672
# Username: eliza, Password: eliza123
```

### Option 2: Local Development with External RabbitMQ
```bash
# 1. Start RabbitMQ locally
docker run -d --name rabbitmq \
  -p 5672:5672 -p 15672:15672 \
  -e RABBITMQ_DEFAULT_USER=eliza \
  -e RABBITMQ_DEFAULT_PASS=eliza123 \
  rabbitmq:3.12-management

# 2. Update configuration to use localhost
# Edit app/py-app/config/default.config:
# server: amqp://eliza:eliza123@localhost:5672/

# 3. Run the application
cd app
source venv/bin/activate
cd py-app
python app.py
```

### Option 3: Local Development with In-Memory EventBus
```bash
# Revert to original synchronous event bus
# Edit app/py-app/app.py and change:
# from cltl.combot.infra.event.kombu import KombuEventBusContainer
# to:
# from cltl.combot.infra.event.memory import SynchronousEventBusContainer

# Then change:
# class InfraContainer(KombuEventBusContainer, ...)
# to:
# class InfraContainer(SynchronousEventBusContainer, ...)

cd app
source venv/bin/activate
cd py-app
python app.py
```

## Configuration Details

### RabbitMQ Settings
- **Host**: rabbitmq (Docker) or localhost (local)
- **Port**: 5672 (AMQP)
- **Management UI**: 15672
- **Username**: eliza
- **Password**: eliza123
- **Exchange**: cltl.combot
- **Exchange Type**: direct
- **Compression**: bzip2

### Event Topics
The application uses these topics for communication:
- `cltl.topic.microphone` - Audio input
- `cltl.topic.vad` - Voice activity detection
- `cltl.topic.text_in` - Text input/ASR results
- `cltl.topic.text_out` - Text output/responses
- `cltl.topic.scenario` - Application scenarios
- `cltl.topic.intention` - Intent management
- `cltl.topic.desire` - Desire management

## Troubleshooting

### Connection Issues
1. **RabbitMQ not accessible**: Ensure RabbitMQ is running and accessible
2. **Authentication failed**: Check username/password in configuration
3. **Network issues**: Verify Docker network or localhost connectivity

### Application Issues
1. **Import errors**: Ensure all CLTL modules are properly installed
2. **Permission errors**: Check storage directory permissions
3. **Port conflicts**: Ensure ports 8000, 5672, and 15672 are available

### Monitoring
- **RabbitMQ Management UI**: Monitor queues, exchanges, and connections at http://localhost:15672
- **Application logs**: Check console output for EventBus connection status
- **Event flow**: Use RabbitMQ management to see message flow between components

## Benefits of External Messaging

1. **Scalability**: Components can run on different machines
2. **Reliability**: Message persistence and delivery guarantees
3. **Monitoring**: Visibility into message flow via RabbitMQ management
4. **Flexibility**: Easy to add/remove components without affecting others
5. **Production Ready**: Suitable for production deployments
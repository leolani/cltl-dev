import logging.config
import os

import time
from cltl.combot.event.bdi import IntentionEvent, Intention
from cltl.combot.event.emissor import SIG, MEN
from cltl.combot.infra.container import InfraContainer as _InfraContainer
from cltl.combot.infra.di_container import singleton
from cltl.combot.infra.event.api import Event, PAYLOAD
from cltl.combot.infra.event.memory import SynchronousEventBus
from cltl.combot.infra.event_log import LogWriter
from cltl_service.asr.container import ASRContainer
from cltl_service.backend.backend_container import BackendContainer
from cltl_service.chatui.container import ChatUIContainer
from cltl_service.combot.event_log.service import EventLogService
from cltl_service.context.container import ContextComponentsContainer
from cltl_service.eliza.container import ElizaContainer
from cltl_service.emissordata.container import EmissorStorageContainer
from cltl_service.monitoring.container import MonitoringContainer
from cltl_service.vad.container import VADContainer
from eliza_app_service.context.service import ContextService
from emissor.representation.util import serializer as emissor_serializer, marshal, unmarshal, register_type_var
from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

logging.config.fileConfig(os.environ.get('CLTL_LOGGING_CONFIG', default='config/logging.config'),
                          disable_existing_loggers=False)
logger = logging.getLogger(__name__)


# Register TypeVar for usage with emissor serialization utils
register_type_var(PAYLOAD)
register_type_var(SIG)
register_type_var(MEN)


def serializer(obj):
    """Serialize events into deserializable JSON using emissor utilities."""
    return marshal(obj, cls=Event)


def deserializer(obj):
    """Deserialize events into proper Python objects using emissor utilities."""
    return unmarshal(obj, cls=Event)


class InfraContainer(_InfraContainer):
    """Application-level InfraContainer that wires emissor serialization and selects the event bus implementation."""

    @property
    @singleton
    def event_bus_serializer(self):
        return serializer, deserializer

    @property
    @singleton
    def event_bus(self):
        config = self.config_manager.get_config("cltl.event")
        implementation = config.get("implementation")
        if implementation == "internal":
            return SynchronousEventBus()
        elif implementation == "kombu":
            return super().event_bus
        else:
            raise ValueError("Unknown implementation: " + implementation)


class ApplicationContainer(InfraContainer,
                           ElizaContainer, ContextComponentsContainer,
                           ChatUIContainer, MonitoringContainer,
                           ASRContainer, VADContainer,
                           EmissorStorageContainer, BackendContainer):
    @property
    @singleton
    def context_service(self) -> ContextService:
        return ContextService.from_config(self.event_bus, self.resource_manager, self.config_manager)

    @property
    @singleton
    def log_writer(self):
        config = self.config_manager.get_config("cltl.event_log")
        return LogWriter(config.get("log_dir"), emissor_serializer)

    @property
    @singleton
    def event_log_service(self):
        return EventLogService.from_config(self.log_writer, self.event_bus, self.config_manager)

    def start(self):
        logger.info("Start EventLog")
        super().start()
        self.context_service.start()
        self.event_log_service.start()

    def stop(self):
        try:
            logger.info("Stop EventLog")
            self.event_log_service.stop()
        finally:
            try:
                self.context_service.stop()
            finally:
                super().stop()
                logger.info("Stop EventBus")
                if hasattr(self.event_bus, 'close'):
                    self.event_bus.close()


def main():
    ApplicationContainer.load_configuration()
    logger.info("Initialized Application")
    application = ApplicationContainer()

    with application as started_app:
        logger.info("Starting the application")
        time.sleep(1)

        intention_topic = started_app.config_manager.get_config("cltl.bdi").get("topic_intention")
        init_event = Event.for_payload(IntentionEvent([Intention("init", None)]))
        started_app.event_bus.publish(intention_topic, init_event)

        logger.info("Started 'init' intention")

        routes = {
            '/storage': started_app.storage_service.app,
            '/emissor': started_app.emissor_data_service.app,
            '/chatui': started_app.chatui_service.app,
        }
        if started_app.server:
            routes['/host'] = started_app.server.app
        # Optional, and `@singleton` cannot hold None, so this is False rather
        # than None when the [cltl.monitoring] section is absent.
        if started_app.monitoring_service:
            routes['/monitoring'] = started_app.monitoring_service.app

        web_app = DispatcherMiddleware(Flask("Eliza app"), routes)

        run_simple('0.0.0.0', 8000, web_app, threaded=True, use_reloader=False, use_debugger=False)

        started_app.event_bus.publish(intention_topic, Event.for_payload(IntentionEvent([Intention("terminate", None)])))
        time.sleep(1)


if __name__ == '__main__':
    main()

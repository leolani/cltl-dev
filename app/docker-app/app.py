import logging.config
import os
import time
from threading import Thread

from app_service.context.service import ContextService
from cltl.combot.event.bdi import IntentionEvent, Intention
from cltl.combot.event.emissor import SIG, MEN
from cltl.combot.infra.config.k8config import K8LocalConfigurationContainer
from cltl.combot.infra.container import InfraContainer
from cltl.combot.infra.di_container import singleton
from cltl.combot.infra.event.api import Event, PAYLOAD
from cltl.combot.infra.event.memory import SynchronousEventBus
from cltl.combot.infra.event_log import LogWriter
from cltl_service.combot.event_log.service import EventLogService
from emissor.representation.util import serializer as emissor_serializer, marshal, unmarshal, register_type_var
from flask import Flask

logging.config.fileConfig(os.environ.get('CLTL_LOGGING_CONFIG', 'config/logging.config'),
                          disable_existing_loggers=False)
logger = logging.getLogger(__name__)

register_type_var(PAYLOAD)
register_type_var(SIG)
register_type_var(MEN)


def serializer(obj):
    return marshal(obj, cls=Event)


def deserializer(obj):
    return unmarshal(obj, cls=Event)


class ApplicationContainer(InfraContainer):
    @property
    @singleton
    def event_bus_serializer(self):
        return serializer, deserializer

    @property
    @singleton
    def event_bus(self):
        config = self.config_manager.get_config("cltl.event")
        if config.get("implementation") == "internal":
            return SynchronousEventBus()
        return super().event_bus

    @property
    @singleton
    def context_service(self) -> ContextService:
        return ContextService.from_config(self.event_bus, self.resource_manager, self.config_manager)

    @property
    @singleton
    def log_writer(self) -> LogWriter:
        config = self.config_manager.get_config("cltl.event_log")
        return LogWriter(config.get("log_dir"), emissor_serializer)

    @property
    @singleton
    def event_log_service(self) -> EventLogService:
        return EventLogService.from_config(self.log_writer, self.event_bus, self.config_manager)

    def start(self):
        logger.info("Start App")
        super().start()
        self.event_log_service.start()
        self.context_service.start()

    def stop(self):
        logger.info("Stop App")
        self.context_service.stop()
        self.event_log_service.stop()
        try:
            if hasattr(self.event_bus, 'close'):
                self.event_bus.close()
        finally:
            super().stop()


_health_app = Flask(__name__)


@_health_app.route('/health')
def health():
    return 'OK', 200


def _start_health_server():
    _health_app.run(host='0.0.0.0', port=8000)


def main():
    ApplicationContainer.load_configuration()
    logger.info("Initialized Application")
    application = ApplicationContainer()

    with application:
        Thread(target=_start_health_server, daemon=True).start()
        logger.info("Starting application")
        time.sleep(1)

        intention_topic = application.config_manager.get_config("cltl.bdi").get("topic_intention")
        application.event_bus.publish(intention_topic, Event.for_payload(IntentionEvent([Intention("init", None)])))
        logger.info("Published 'init' intention")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        application.event_bus.publish(intention_topic, Event.for_payload(IntentionEvent([Intention("terminate", None)])))
        time.sleep(1)


if __name__ == '__main__':
    main()

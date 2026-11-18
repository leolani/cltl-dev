"""TLS options the Kombu event bus passes for an ``amqps://`` broker.

The deployed event bus must verify the broker's *hostname*, not only its
certificate chain; py-amqp does that only when ``server_hostname`` is present
in the ssl options. Checked on a bus built through the real constructor, against
the connection kombu would open, without a broker: constructing a
``Connection`` does not connect.
"""
import ssl
from types import SimpleNamespace

import pytest
from cltl.combot.infra.event.kombu import KombuEventBus, _ssl_options


class _Config(dict):
    def get(self, key, default=None):
        return super().get(key, default)


@pytest.mark.parametrize("server", ["amqp://guest:guest@rabbitmq:5672//", "", None])
def test_plain_amqp_gets_no_tls(server):
    assert _ssl_options(server, _Config()) is False


def test_amqps_requires_a_verified_certificate_and_hostname():
    options = _ssl_options("amqps://u:p@broker.example:5671//", _Config())

    assert options["cert_reqs"] == ssl.CERT_REQUIRED
    assert "server_hostname" in options and options["server_hostname"] is None
    assert "ca_certs" not in options


def test_private_ca_is_passed_through():
    options = _ssl_options("amqps://broker/", _Config(ssl_ca_certs="/etc/ca.pem"))

    assert options["ca_certs"] == "/etc/ca.pem"


def test_the_bus_hands_the_options_to_its_connection():
    config = _Config(server="amqps://u:p@broker.example:5671//", exchange="x")
    bus = KombuEventBus("json", SimpleNamespace(get_config=lambda section: config))

    ssl_options = bus.connection.ssl

    assert ssl_options["cert_reqs"] == ssl.CERT_REQUIRED
    assert "server_hostname" in ssl_options

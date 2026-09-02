"""Test-wide guarantees.

The suite is offline by design: every source it exercises is represented by a
fixture captured from a real probe run, not by a live call. That property is
worth enforcing rather than trusting, for two reasons. A test that quietly
reaches the network is slow and flaky and fails for reasons that have nothing
to do with the change under review. And a workflow that needs no secrets can
run on a fork's pull request as completely as on a branch.

So the socket layer is closed for the duration of every test. A test that tries
to open a connection fails immediately, naming itself, rather than passing on a
machine with a network and failing in CI.
"""

from __future__ import annotations

import socket

import pytest

_real_socket = socket.socket
_real_create_connection = socket.create_connection


class NetworkAccessAttempted(AssertionError):
    pass


def _refuse(*args, **kwargs):
    raise NetworkAccessAttempted(
        "this test tried to open a network connection; the suite is offline by "
        "design — capture a fixture from a probe run instead"
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    yield

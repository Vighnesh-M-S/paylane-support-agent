import pytest

from paylane_agent import mock_data
from paylane_agent.session import Session
from paylane_agent.tools import dispatch

ASHA = {"email": "asha.rao@example.com", "last4": "4242"}


@pytest.fixture(autouse=True)
def fresh_backend():
    mock_data.reset()
    yield


@pytest.fixture
def session():
    return Session()


@pytest.fixture
def verified(session):
    assert dispatch(session, "verify_customer", ASHA)["verified"] is True
    return session

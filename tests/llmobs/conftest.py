import os

import mock
import pytest

from ddtrace.llmobs import LLMObs as llmobs_service
from tests.llmobs._utils import logs_vcr
from tests.utils import DummyTracer
from tests.utils import override_global_config
from tests.utils import request_token


@pytest.fixture(autouse=True)
def vcr_logs(request):
    marks = [m for m in request.node.iter_markers(name="vcr_logs")]
    assert len(marks) < 2
    if marks:
        mark = marks[0]
        cass = mark.kwargs.get("cassette", request_token(request).replace(" ", "_").replace(os.path.sep, "_"))
        with logs_vcr.use_cassette("%s.yaml" % cass):
            yield
    else:
        yield


def pytest_configure(config):
    config.addinivalue_line("markers", "vcr_logs: mark test to use recorded request/responses")


@pytest.fixture
def mock_llmobs_writer():
    patcher = mock.patch("ddtrace.llmobs._llmobs.LLMObsWriter")
    LLMObsWriterMock = patcher.start()
    m = mock.MagicMock()
    LLMObsWriterMock.return_value = m
    yield m
    patcher.stop()


@pytest.fixture
def ddtrace_global_config():
    config = {}
    return config


def default_global_config():
    return {"_dd_api_key": "<not-a-real-api_key>"}


@pytest.fixture
def LLMObs(mock_llmobs_writer, ddtrace_global_config):
    global_config = default_global_config()
    global_config.update(ddtrace_global_config)
    with override_global_config(global_config):
        dummy_tracer = DummyTracer()
        llmobs_service.enable(tracer=dummy_tracer)
        yield llmobs_service
        llmobs_service.disable()

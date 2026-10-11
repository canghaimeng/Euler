"""Personal controls need no browser credential and still honor origin/project scope."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from euler.observability.human_api import _authorize


def request(headers=None):
    return Request({
        'type': 'http', 'http_version': '1.1', 'method': 'POST',
        'scheme': 'http', 'server': ('testserver', 80), 'path': '/api/team/ask',
        'root_path': '', 'query_string': b'',
        'headers': [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    })


def test_personal_controls_need_no_token(monkeypatch):
    monkeypatch.delenv('EULER_CONTROL_TOKEN', raising=False)
    monkeypatch.setenv('EULER_CONTROL_ACTOR', 'human:researcher')
    assert _authorize(request({'origin': 'http://testserver'}), {}) == 'human:researcher'
    assert _authorize(request(), {}) == 'human:researcher'
    # An obsolete deployment variable must not reintroduce a hidden UI password.
    monkeypatch.setenv('EULER_CONTROL_TOKEN', 'obsolete-fixture')
    assert _authorize(request(), {}) == 'human:researcher'


@pytest.mark.parametrize('headers', [
    {'origin': 'https://unrelated.example'},
    {'origin': 'null'},
    {'sec-fetch-site': 'cross-site'},
])
def test_personal_controls_reject_cross_origin_requests(headers):
    with pytest.raises(HTTPException) as error:
        _authorize(request(headers), {})
    assert error.value.status_code == 403


def test_an_unconfigured_project_remains_readonly():
    with pytest.raises(HTTPException) as error:
        _authorize(request({'origin': 'http://testserver'}), None)
    assert error.value.status_code == 403

from dataclasses import replace

import pytest

from advogada_find.config import _bool, Settings


@pytest.mark.parametrize('value,expected', [('true',True),('1',True),('yes',True),
                                         ('false',False),('0',False),('off',False)])
def test_strict_boolean(monkeypatch, value, expected):
    monkeypatch.setenv('DRY_RUN',value)
    assert _bool('DRY_RUN',True) is expected


@pytest.mark.parametrize('value', ['tru','flase','','2'])
def test_invalid_boolean_rejected(monkeypatch,value):
    monkeypatch.setenv('DRY_RUN',value)
    with pytest.raises(ValueError):
        Settings()


def test_environment_read_at_instantiation(monkeypatch):
    monkeypatch.setenv('DRY_RUN','true')
    assert Settings().dry_run
    monkeypatch.setenv('DRY_RUN','false')
    assert not Settings().dry_run


@pytest.mark.parametrize('change', [dict(max_emails_per_day=0),dict(crawl_delay_seconds=-1),
    dict(send_window_start='17:00',send_window_end='08:00'),dict(send_weekdays=(8,)),
    dict(smtp_starttls=False,smtp_ssl=False),dict(smtp_port=0)])
def test_invalid_limits_and_security(settings,change):
    with pytest.raises(ValueError):
        replace(settings,**change)

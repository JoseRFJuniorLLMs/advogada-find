from dataclasses import replace
import socket

import pytest

from advogada_find.config import Settings
from advogada_find.app import AdvogadaFind


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Unit tests must never connect to network services')
    monkeypatch.setattr(socket.socket, 'connect', blocked)


@pytest.fixture
def settings(tmp_path):
    cv = tmp_path / 'cv.pdf'
    cv.write_bytes(b'%PDF-1.4\nunit-test fixture')
    template = tmp_path / 'application.txt'
    template.write_text('Olá {company}, meu nome é {name}. {site} {email}', encoding='utf-8')
    return Settings(db_path=tmp_path/'test.db', cities_file=tmp_path/'missing.csv',
        cv_path=cv, email_template=template, applicant_name='Test Applicant',
        smtp_user='web2ajax@gmail.com', smtp_password='test-only', from_email='web2ajax@gmail.com',
        applicant_email='web2ajax@gmail.com', dry_run=False,
        min_send_interval_seconds=0, max_send_jitter_seconds=0, crawl_delay_seconds=0)


def seed(db, domain='acme.com.br', *, approved=True, email=None, kind='rh'):
    cid = db.upsert_company('Acme', domain, 'https://'+domain+'/', 'Campinas', 'SP', 'test', 'q')
    db.upsert_contact(cid, email or 'rh@'+domain, kind, 100, 'https://'+domain+'/carreiras')
    if approved:
        db.approve_company(cid, 'Domínio oficial, escritório de advocacia em Campinas')
    return cid


@pytest.fixture
def make_app(settings):
    def make(**changes):
        app = AdvogadaFind(replace(settings, **changes))
        app._inside_window = lambda: True
        return app
    return make

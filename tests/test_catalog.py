import csv
import threading
from unittest.mock import Mock

import pytest

from advogada_find.catalog import import_cities, export_catalog
from conftest import seed


def test_ibge_city_import_is_idempotent_and_preserves_priority(make_app,monkeypatch):
    app=make_app(); app.db.add_city('Campinas','SP',100)
    response=Mock(); response.json.return_value=[{'nome':'Campinas'},{'nome':'São Carlos'}]
    monkeypatch.setattr('advogada_find.catalog.httpx.get',Mock(return_value=response))
    assert import_cities(app.db,'SP')==2
    assert import_cities(app.db,'SP')==2
    cities=app.db.list_cities()
    assert len(cities)==2 and cities[0]['priority']==100


def test_invalid_state_never_calls_ibge(make_app,monkeypatch):
    app=make_app(); get=Mock(); monkeypatch.setattr('advogada_find.catalog.httpx.get',get)
    with pytest.raises(ValueError): import_cities(app.db,'XX')
    get.assert_not_called()


def test_failed_import_does_not_touch_city_queue(make_app,monkeypatch):
    app=make_app()
    response=Mock(); response.raise_for_status.side_effect=RuntimeError('unavailable')
    monkeypatch.setattr('advogada_find.catalog.httpx.get',Mock(return_value=response))
    with pytest.raises(RuntimeError): import_cities(app.db,'SP')
    assert app.db.list_cities()==[]


def test_catalog_export_keeps_contact_provenance(make_app,tmp_path):
    app=make_app(); cid=seed(app.db)
    with app.db.connect() as con:
        con.execute('UPDATE companies SET name=? WHERE id=?',('=formula',cid))
    output=tmp_path/'catalog.csv'
    assert export_catalog(app.db,output)==1
    with output.open(encoding='utf-8-sig',newline='') as fh:
        row=next(csv.DictReader(fh))
    assert row['company_name']=="'=formula"
    assert row['email']=='rh@acme.com.br'
    assert row['source_url']=='https://acme.com.br/carreiras'
    assert row['verified']=='1'


def test_daemon_sender_runs_while_discovery_is_blocked(make_app,monkeypatch):
    app=make_app(); started=threading.Event(); stopped=threading.Event()
    def scan_loop(stop):
        started.set()
        stop.wait(3)
        stopped.set()
    def send_once():
        assert started.wait(1)
        assert not stopped.is_set()
        raise KeyboardInterrupt()
    monkeypatch.setattr(app,'_scan_loop',scan_loop)
    monkeypatch.setattr(app,'send_once',send_once)
    app.daemon()
    assert stopped.is_set()

import json
from dataclasses import replace

import pytest

from advogada_find.dashboard import snapshot
from conftest import seed


def test_dashboard_reports_smtp_acceptance_not_delivery(make_app):
    app=make_app(); seed(app.db); row=app.db.claim_next(3)
    app.db.mark_sent(row['application_id'],'subject','<test@example.com>')
    result=snapshot(app.s,{})
    assert result['stats']['sent']==1
    assert result['applications'][0]['status']=='sent'
    assert result['applications'][0]['email']=='rh@acme.com.br'
    assert result['coverage']['total']==0
    assert 'smtp_password' not in json.dumps(result)
    assert 'test-only' not in json.dumps(result)


def test_dashboard_filters_are_parameterized(make_app):
    app=make_app(); seed(app.db); row=app.db.claim_next(3)
    app.db.mark_sent(row['application_id'],'subject','<test@example.com>')
    assert snapshot(app.s,{'state':['SP'],'status':['sent']})['total']==1
    assert snapshot(app.s,{'state':['PR']})['total']==0
    assert snapshot(app.s,{'q':["' OR 1=1 --"]})['total']==0
    assert snapshot(app.s,{'page':['not-a-number']})['page']==1
    with pytest.raises(ValueError): snapshot(app.s,{'status':['invalid']})


def test_dashboard_is_read_only(make_app):
    app=make_app(); seed(app.db)
    before=app.db.list_applications()
    snapshot(app.s,{})
    assert app.db.list_applications()==before

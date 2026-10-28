import sqlite3
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from advogada_find.db import Database, SCHEMA, iso, utcnow
from advogada_find.crawler import Contact
from advogada_find.locking import sender_lock
from conftest import seed


def test_unverified_company_requires_review(make_app):
    app=make_app(); cid=seed(app.db,approved=False)
    assert app.db.preview_next(3) is None
    app.db.approve_company(cid,'Site oficial de escritório de advocacia em Campinas')
    assert app.db.preview_next(3)['company_id']==cid


def test_subdomain_company_dedup(make_app):
    app=make_app(); first=seed(app.db,'acme.com.br')
    second=seed(app.db,'jobs.acme.com.br')
    assert first==second
    assert app.db.stats()['companies']==1


def test_private_suffix_tenants_are_separate(make_app):
    app=make_app()
    assert seed(app.db,'first.github.io') != seed(app.db,'second.github.io')


def test_stale_contacts_are_deactivated_atomically(make_app):
    app=make_app(); cid=seed(app.db,email='contato@acme.com.br',kind='contato')
    app.db.set_careers_signal(cid,True)
    assert app.db.preview_next(3)
    app.db.replace_contacts(cid,[],False)
    assert app.db.preview_next(3) is None
    assert app.db.stats()['contacts']==0


def test_generic_contact_loses_eligibility_without_career_signal(make_app):
    app=make_app(contact_requires_careers_signal=True); cid=seed(app.db,email='contato@acme.com.br',kind='contato')
    app.db.set_careers_signal(cid,False)
    assert app.db.preview_next(3) is None


def test_law_firm_office_contact_queued_by_default(make_app):
    app=make_app(); cid=seed(app.db,email='contato@acme.com.br',kind='contato')
    app.db.set_careers_signal(cid,False)
    assert app.db.preview_next(3)['email']=='contato@acme.com.br'


def test_targets_follow_city_priority(make_app):
    app=make_app(); app.db.add_city('Campinas','SP',10); app.db.add_city('Brasília','DF',100)
    seed(app.db,'campinas.com.br')
    cid=app.db.upsert_company('Brasília Advogados','brasilia.adv.br','https://brasilia.adv.br/','Brasília','DF','test','q')
    app.db.upsert_contact(cid,'contato@brasilia.adv.br','contato',55,'https://brasilia.adv.br/')
    app.db.approve_company(cid,'Escritório de advocacia em Brasília')
    targets=app.db.list_targets(3)
    assert [t['domain'] for t in targets]==['brasilia.adv.br','campinas.com.br']
    assert app.db.preview_next(3)['domain']=='brasilia.adv.br'


def test_contact_score_can_drop_on_refresh(make_app):
    app=make_app(); cid=seed(app.db)
    app.db.replace_contacts(cid,[Contact('rh@acme.com.br','rh',50,'https://acme.com.br/')],False)
    assert app.db.preview_next(3)['score']==50


def test_expired_sending_requires_reconciliation(make_app):
    app=make_app(); seed(app.db)
    row=app.db.claim_next(3)
    with app.db.connect() as con:
        con.execute('UPDATE applications SET lease_until=?',(iso(utcnow()-timedelta(seconds=1)),))
    assert app.db.recover_interrupted()==1
    assert app.db.stats()['uncertain']==1
    assert app.db.claim_next(3) is None
    app.db.reconcile(row['application_id'],'not-sent','Conferido no provedor: mensagem não foi enviada')
    assert app.db.claim_next(3)


def test_active_sender_cannot_be_recovered_by_another_process(make_app):
    app=make_app(); seed(app.db); app.db.claim_next(3)
    with app.db.connect() as con:
        con.execute('UPDATE applications SET lease_until=?',(iso(utcnow()-timedelta(seconds=1)),))
    with sender_lock(app.db.path) as locked:
        assert locked
        assert app.db.recover_interrupted()==0
        assert app.db.stats()['sending']==1
    assert app.db.recover_interrupted()==1


def test_reconcile_sent_never_queues_again(make_app):
    app=make_app(); seed(app.db); row=app.db.claim_next(3)
    app.db.mark_uncertain(row['application_id'],'No acknowledgement')
    app.db.reconcile(row['application_id'],'sent','Confirmada mensagem enviada no provedor')
    assert app.db.claim_next(3) is None
    assert app.db.stats()['sent']==1


def test_reconcile_only_allows_uncertain_and_requires_evidence(make_app):
    app=make_app(); seed(app.db); row=app.db.claim_next(3)
    with pytest.raises(ValueError):
        app.db.reconcile(row['application_id'],'sent','confirmed')
    app.db.mark_uncertain(row['application_id'],'unknown')
    with pytest.raises(ValueError):
        app.db.reconcile(row['application_id'],'not-sent','')


def test_concurrent_claims_share_daily_reservation(make_app):
    app=make_app(); seed(app.db,'first.com.br'); seed(app.db,'second.com.br')
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(lambda _: app.db.claim_next(3,max_per_day=1),range(2)))
    assert sum(row is not None for row in rows)==1


def test_uncertain_send_consumes_daily_slot(make_app):
    app=make_app(); seed(app.db,'first.com.br'); seed(app.db,'second.com.br')
    row=app.db.claim_next(3,max_per_day=1)
    app.db.mark_uncertain(row['application_id'],'unknown')
    assert app.db.claim_next(3,max_per_day=1) is None


def test_minimum_interval_survives_restart(make_app):
    app=make_app(); seed(app.db,'first.com.br'); seed(app.db,'second.com.br')
    row=app.db.claim_next(3,interval_seconds=1200)
    app.db.mark_sent(row['application_id'],'subject','<test@example.com>')
    reopened=Database(app.db.path); reopened.init()
    assert reopened.claim_next(3,interval_seconds=1200) is None


def test_day_limit_uses_configured_timezone(make_app,monkeypatch):
    app=make_app(); seed(app.db); row=app.db.claim_next(3)
    app.db.mark_sent(row['application_id'],'subject','<test@example.com>')
    now=datetime(2026,10,10,2,30,tzinfo=timezone.utc) # 23:30 on Oct 9 in Sao Paulo
    with app.db.connect() as con:
        con.execute('UPDATE applications SET sent_at=?',(iso(datetime(2026,10,9,14,tzinfo=timezone.utc)),))
    monkeypatch.setattr('advogada_find.db.utcnow',lambda: now)
    assert app.db.sent_today_count('America/Sao_Paulo')==1
    assert app.db.sent_today_count('UTC')==0


def test_connection_context_releases_file_handle(make_app):
    app=make_app()
    with app.db.connect() as con:
        con.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute('SELECT 1')


def test_legacy_migration_keeps_sent_subdomain_blocked(tmp_path):
    path=tmp_path/'legacy.db'
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA)
        for domain in ('acme.com.br','jobs.acme.com.br'):
            cid=con.execute('''INSERT INTO companies(name,domain,website,city,state,source,first_seen_at,last_seen_at)
                VALUES('Acme',?,'https://acme.com.br/','Campinas','SP','test',?,?)''',(domain,iso(),iso())).lastrowid
            ct=con.execute("INSERT INTO contacts(company_id,email,kind,score,source_url,discovered_at) VALUES(?,'rh@acme.com.br','rh',100,'url',?)",(cid,iso())).lastrowid
            if domain.startswith('jobs.'):
                con.execute("INSERT INTO applications(company_id,contact_id,email,status,sent_at) VALUES(?,?,'rh@acme.com.br','sent',?)",(cid,ct,iso()))
    db=Database(path); db.init(); db.init()
    db.approve_company(1,'Official software company in Campinas')
    assert db.claim_next(3) is None
    assert db.stats()['sent']==1


def test_legacy_sending_is_recovered_after_migration(tmp_path):
    db=Database(tmp_path/'legacy.db'); db.init(); seed(db)
    db.claim_next(3)
    with db.connect() as con:
        con.execute('UPDATE applications SET lease_until=NULL')
    db.init()
    assert db.recover_interrupted()==1


def test_seed_does_not_override_manual_city_priority(make_app,tmp_path):
    app=make_app(); app.db.add_city('Campinas','SP',12)
    csv=tmp_path/'cities.csv'; csv.write_text('city,state,priority\nCampinas,SP,100\n')
    app.db.seed_cities(csv)
    assert app.db.next_due_city()['priority']==12


def test_priority_states_go_first_then_rest_of_brazil(make_app):
    app=make_app(priority_states=('DF','RJ','SP'))
    for domain,city,state in [('bh.adv.br','Belo Horizonte','MG'),('rio.adv.br','Rio de Janeiro','RJ'),
                              ('sp.adv.br','São Paulo','SP')]:
        cid=app.db.upsert_company(domain,domain,'https://'+domain+'/',city,state,'test','q')
        app.db.upsert_contact(cid,'rh@'+domain,'rh',100,'https://'+domain+'/')
        app.db.approve_company(cid,'Escritório de advocacia')
    assert [t['state'] for t in app.db.list_targets(3)][-1]=='MG'
    assert {t['state'] for t in app.db.list_targets(3)[:2]}=={'RJ','SP'}


def test_priority_states_reject_unknown_uf(tmp_path):
    with pytest.raises(ValueError):
        Database(tmp_path/'x.db',priority_states=("DF'--",))

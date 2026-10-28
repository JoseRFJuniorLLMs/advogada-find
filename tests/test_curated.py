import csv
from datetime import timedelta

import pytest

from advogada_find.crawler import Contact, CrawlResult
from advogada_find.curated import import_curated
from advogada_find.db import iso, utcnow
from conftest import seed


def add(db, domain, city='Brasília', state='DF', email=None):
    email = email or 'contato@' + domain
    ct = Contact(email, 'contato', 55, 'https://' + domain + '/contato')
    result = CrawlResult([ct], False, 'site oficial; advocacia e cidade confirmadas', True)
    return db.add_curated_verified(name='Escritório', domain=domain, website='https://' + domain + '/',
        city=city, state=state, batch='lote-1', result=result, email=email)


def test_curated_exact_address_and_df_then_fallback(make_app):
    app = make_app()
    automatic = seed(app.db, 'automatico.com.br')
    other = add(app.db, 'goias.com.br', 'Luziânia', 'GO')
    df = add(app.db, 'brasilia.com.br')
    app.db.upsert_contact(df, 'rh@brasilia.com.br', 'rh', 100, 'https://brasilia.com.br/carreiras')
    targets = app.db.list_targets(3)
    assert [t['company_id'] for t in targets] == [df, other]
    assert targets[0]['email'] == 'contato@brasilia.com.br'
    for expected in (df, other):
        row = app.db.claim_next(3)
        assert row['company_id'] == expected
        app.db.mark_sent(row['application_id'], 'subject', '<test@example.com>')
    assert app.db.preview_next(3)['company_id'] == automatic


def test_pending_retry_and_uncertain_do_not_trigger_fallback(make_app):
    app = make_app(); seed(app.db, 'automatico.com.br'); add(app.db, 'brasilia.com.br')
    row = app.db.claim_next(3)
    app.db.mark_failed(row['application_id'], 'retry', retryable=True)
    with app.db.connect() as con:
        con.execute('UPDATE applications SET next_attempt_at=? WHERE id=?', (iso(utcnow()+timedelta(hours=1)), row['application_id']))
    assert app.db.preview_next(3) is None
    with app.db.connect() as con:
        con.execute("UPDATE applications SET status='uncertain' WHERE id=?", (row['application_id'],))
    assert app.db.preview_next(3) is None


def test_scanner_cannot_replace_reviewed_company_or_contact(make_app):
    app = make_app(); cid = add(app.db, 'brasilia.com.br')
    assert app.db.upsert_company('New', 'brasilia.com.br', 'https://brasilia.com.br/new', 'X', 'DF', 'scan', 'q') == cid
    app.db.rename_company(cid, 'Scanner name')
    app.db.replace_contacts(cid, [Contact('rh@brasilia.com.br','rh',100,'https://brasilia.com.br/carreiras')], False, eligible=False)
    row = app.db.preview_next(3)
    assert row['email'] == 'contato@brasilia.com.br'
    assert row['company_name'] == 'Escritório'


def test_import_rejects_external_source_and_unverified_contact(make_app, tmp_path, monkeypatch):
    app=make_app(); path=tmp_path/'lista.csv'
    rows=[dict(name='A',city='Brasília',state='DF',website='https://a.com.br/',email='contato@a.com.br',source_url='https://outra.com.br/'),
          dict(name='B',city='Brasília',state='DF',website='https://b.com.br/',email='contato@b.com.br',source_url='https://b.com.br/'),
          dict(name='C',city='Brasília',state='DF',website='https://c.com.br/',email='contato@c.com.br',source_url='https://c.com.br/')]
    with path.open('w', encoding='utf-8', newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    class FakeCrawler:
        def __init__(self, settings): pass
        def close(self): pass
        def crawl(self, source, domain, city):
            if domain == 'b.com.br': return CrawlResult([], False, '', False, 'sem_evidência')
            return CrawlResult([Contact('contato@c.com.br','contato',55,'https://c.com.br/')], False, 'verificado', True)
    monkeypatch.setattr('advogada_find.curated.WebsiteCrawler', FakeCrawler)
    result=import_curated(app.db, app.s, path, 'test')
    assert result['imported'] == 1
    assert [item['line'] for item in result['review']] == [2,3]
    assert app.db.preview_next(3)['email'] == 'contato@c.com.br'


def test_history_is_preserved_for_existing_sent_firm(make_app):
    app=make_app(); cid=seed(app.db, 'brasilia.com.br'); row=app.db.claim_next(3)
    app.db.mark_sent(row['application_id'], 'subject', '<test@example.com>')
    add(app.db, 'brasilia.com.br')
    assert app.db.preview_next(3) is None
    assert app.db.stats()['sent'] == 1


def test_registry_scanner_cannot_change_curated_company_identifier(make_app):
    app=make_app(); cid=add(app.db, 'brasilia.com.br')
    app.db.mark_registry_lookup('12345678000190', cid)
    with app.db.connect() as con:
        company=con.execute('SELECT cnpj_basic FROM companies WHERE id=?', (cid,)).fetchone()
    assert company['cnpj_basic'] is None


def test_cli_import_without_verified_contacts_exits_nonzero(monkeypatch):
    import sys
    from advogada_find import cli
    monkeypatch.setattr(cli, 'Settings', lambda: object())
    monkeypatch.setattr(cli, 'AdvogadaFind', lambda settings: type('App', (), {'db': object(), 's': settings})())
    monkeypatch.setattr(cli, 'import_curated', lambda *args: {'imported': 0, 'processed': 1, 'review': []})
    monkeypatch.setattr(sys, 'argv', ['advogada-find', 'import-curated', '--input', 'invalid.csv', '--batch', 'test'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


def test_conflicting_cnpj_requires_review_and_preserves_existing(make_app):
    app=make_app(); cid=seed(app.db, 'brasilia.com.br')
    with app.db.connect() as con:
        con.execute('UPDATE companies SET cnpj_basic=? WHERE id=?', ('12345678',cid))
    ct=Contact('contato@brasilia.com.br','contato',55,'https://brasilia.com.br/contato')
    result=CrawlResult([ct],False,'verificado',True,cnpj_basic='87654321')
    with pytest.raises(ValueError, match='CNPJ divergente'):
        app.db.add_curated_verified(name='Escritório',domain='brasilia.com.br',website='https://brasilia.com.br/',
            city='Brasília',state='DF',batch='test',result=result,email=ct.email)
    with app.db.connect() as con:
        assert con.execute('SELECT cnpj_basic FROM companies WHERE id=?',(cid,)).fetchone()['cnpj_basic']=='12345678'
        assert con.execute('SELECT COUNT(*) n FROM curated_queue').fetchone()['n']==0

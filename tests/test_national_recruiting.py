import threading
from datetime import datetime, timedelta, timezone

from advogada_find.crawler import Contact, CrawlError, CrawlResult, WebsiteCrawler, curated_recruitment_alias, score_email
from advogada_find.curated import import_curated
from advogada_find.national import recheck, scheduled_recheck
from conftest import seed


def test_institutional_alias_is_brand_not_person():
    assert curated_recruitment_alias('pfp@pfpadvogados.com.br')
    assert curated_recruitment_alias('bsaa@bsaa.adv.br')
    assert curated_recruitment_alias('bgp@bgpadv.com.br')
    assert curated_recruitment_alias('tavares@tavaresoffice.com.br')
    assert not curated_recruitment_alias('joao@exemplo.com.br')
    assert not curated_recruitment_alias('joao.silva@joaoadv.com.br')
    assert score_email('administrativo@schiefler.adv.br', True)[1] > 0


def test_official_recruitment_page_allows_brand_in_reviewed_csv(make_app, tmp_path, monkeypatch):
    import csv
    app = make_app()
    url = 'https://pfpadvogados.com.br/trabalhe-conosco'
    row = dict(name='PFP Advocacia',city='São Paulo',state='SP',
               website='https://pfpadvogados.com.br/',email='pfp@pfpadvogados.com.br',
               source_url=url)
    path = tmp_path / 'curated.csv'
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)

    class FakeCrawler:
        def __init__(self, settings):
            pass

        def close(self):
            pass

        def crawl(self, source, domain, city):
            return CrawlResult([], True, 'verified official careers site', False,
                               'canal_corporativo_inelegivel', '', 'PFP Advocacia',
                               (('pfp@pfpadvogados.com.br', url),))

    monkeypatch.setattr('advogada_find.curated.WebsiteCrawler', FakeCrawler)
    result = import_curated(app.db, app.s, path, 'carreiras-2026-11')
    assert result['imported'] == 1
    assert app.db.preview_next(3)['email'] == 'pfp@pfpadvogados.com.br'


def test_noncareer_brand_alias_never_enters_verified_queue(make_app, tmp_path, monkeypatch):
    import csv
    app = make_app()
    row = dict(name='PFP Advocacia', city='São Paulo', state='SP',
               website='https://pfpadvogados.com.br/', email='pfp@pfpadvogados.com.br',
               source_url='https://pfpadvogados.com.br/contato')
    path = tmp_path / 'curated.csv'
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)

    class FakeCrawler:
        def __init__(self, settings):
            pass

        def close(self):
            pass

        def crawl(self, source, domain, city):
            return CrawlResult([], False, '', False, 'canal_corporativo_inelegivel')

    monkeypatch.setattr('advogada_find.curated.WebsiteCrawler', FakeCrawler)
    result = import_curated(app.db, app.s, path, 'no-publication')
    assert result['imported'] == 0


def test_manual_recheck_pages_by_id_with_verified_sites(make_app, monkeypatch):
    app = make_app()
    first = seed(app.db, 'first.adv.br')
    second = seed(app.db, 'second.adv.br', approved=False)
    visited = []

    def crawl(self, website, domain, **kwargs):
        visited.append(domain)
        return CrawlResult([], False)

    monkeypatch.setattr(WebsiteCrawler, 'crawl', crawl)
    page = recheck(app, 1, include_verified=True)
    assert page['next_after_id'] == first
    page = recheck(app, 1, include_verified=True, after_id=page['next_after_id'])
    assert page['next_after_id'] == second
    assert visited == ['first.adv.br', 'second.adv.br']


def test_scheduled_recheck_prioritizes_and_resumes_through_cooldown(make_app, monkeypatch):
    app = make_app(national_recheck_batch_size=1, city_rescan_days=3)
    ordinary = seed(app.db, 'ordinary.adv.br', approved=False)
    priority = seed(app.db, 'priority.adv.br', approved=False)
    excluded = seed(app.db, 'excluded.adv.br', approved=False)
    seed(app.db, 'verified.adv.br')
    with app.db.connect() as con:
        con.execute("UPDATE companies SET verification_note='motivo=canal_corporativo_inelegivel',"
                    "cnpj_basic='12345678' WHERE id=?", (priority,))
        con.execute("UPDATE companies SET cnpj_basic='87654321' WHERE id=?", (excluded,))
        con.execute("INSERT INTO registry_exclusions VALUES ('87654321')")
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    monkeypatch.setattr('advogada_find.national.iso', lambda timestamp=None: (timestamp or now).isoformat())
    monkeypatch.setattr('advogada_find.national.utcnow', lambda: now)
    visited = []

    def crawl(self, website, domain, *, city, registry_cnpj):
        visited.append((domain, registry_cnpj))
        if domain == 'priority.adv.br':
            raise CrawlError('offline')
        return CrawlResult([], False)

    monkeypatch.setattr(WebsiteCrawler, 'crawl', crawl)
    result = scheduled_recheck(app)
    assert result['next_after_id'] == priority and result['errors'] == 1

    restarted = make_app(national_recheck_batch_size=1, city_rescan_days=3)
    assert scheduled_recheck(restarted)['next_after_id'] == ordinary
    assert scheduled_recheck(restarted)['selected'] == 0
    assert scheduled_recheck(restarted) is None
    assert visited == [('priority.adv.br', '12345678'), ('ordinary.adv.br', '')]

    now += timedelta(days=3)
    assert scheduled_recheck(restarted)['next_after_id'] == priority


def test_discovery_recheck_refills_queue_without_sending(make_app, monkeypatch):
    app = make_app(cities_scope='configured', national_recheck_batch_size=1)
    cid = seed(app.db, 'acme.adv.br', approved=False)
    stop = threading.Event()
    contact = Contact('rh@acme.adv.br', 'rh', 100, 'https://acme.adv.br/carreiras')
    monkeypatch.setattr(WebsiteCrawler, 'crawl',
                        lambda *args, **kwargs: CrawlResult([contact], True, 'official', True))
    monkeypatch.setattr(app, 'scan_next_due_city', lambda: None)
    monkeypatch.setattr(app, 'scan_registry_company', stop.set)

    app._scan_loop(stop)

    target = app.db.preview_next(app.s.max_send_attempts)
    assert target['company_id'] == cid and target['email'] == contact.email
    assert app.db.list_applications() == []

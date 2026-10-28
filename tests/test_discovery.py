import pytest

from advogada_find.discovery import SearchDiscovery, DiscoveryError, CompanyCandidate, normalize
from advogada_find.crawler import WebsiteCrawler, CrawlResult, Contact


@pytest.mark.parametrize('url', ['http://127.0.0.1/','https://10.0.0.1/',
    'ftp://acme.com.br/file','https://user:password@acme.com.br/','https://acme.com.br:8080/',
    'https://linkedin.com/company/acme','https://foo.linkedin.com/','https://localhost/'])
def test_invalid_or_third_party_urls_rejected(url):
    assert normalize(url) is None


@pytest.mark.parametrize('url', ['https://www.tjdft.jus.br/', 'https://www.mpdft.mp.br/',
    'https://www.agu.gov.br/', 'https://www.defensoria.df.def.br/', 'https://www.oabdf.org.br/',
    'https://www.jusbrasil.com.br/advogados', 'https://www.escavador.com/sobre/1',
    'https://www.migalhas.com.br/', 'https://www.conjur.com.br/'])
def test_public_bodies_and_legal_directories_are_not_employers(url):
    assert normalize(url) is None


def test_law_firm_site_is_candidate():
    assert normalize('https://www.silvaadvogados.adv.br/contato')[0] == 'silvaadvogados.adv.br'


def test_root_and_subdomain_share_identity():
    assert normalize('https://jobs.acme.com.br/vagas')[0]==normalize('https://www.acme.com.br/')[0]


def test_brave_failure_falls_back_and_records_source(settings,monkeypatch):
    from dataclasses import replace
    discovery=SearchDiscovery(replace(settings,brave_search_api_key='test-only'))
    def unavailable(*args): raise RuntimeError('unavailable')
    monkeypatch.setattr(discovery,'_brave',unavailable)
    monkeypatch.setattr(discovery,'_ddgs',lambda _: [{'title':'Acme','href':'https://acme.com.br/'}])
    candidates=discovery.discover('Campinas','SP')
    assert len(candidates)==1 and candidates[0].source=='ddgs'


def test_total_search_failure_is_not_empty_success(settings,monkeypatch):
    discovery=SearchDiscovery(settings)
    def unavailable(*args): raise RuntimeError('unavailable')
    monkeypatch.setattr(discovery,'_ddgs',unavailable)
    with pytest.raises(DiscoveryError):
        discovery.discover('Campinas','SP')


def test_empty_successful_search_is_valid(settings,monkeypatch):
    discovery=SearchDiscovery(settings)
    monkeypatch.setattr(discovery,'_ddgs',lambda _: [])
    assert discovery.discover('Campinas','SP')==[]


def test_failed_city_retried_not_marked_scanned(make_app,monkeypatch):
    app=make_app(); app.db.add_city('Campinas','SP')
    def unavailable(*args): raise DiscoveryError('six queries failed')
    monkeypatch.setattr(app.discovery,'discover',unavailable)
    assert app.scan_next_due_city()
    with app.db.connect() as con:
        row=con.execute('SELECT * FROM cities').fetchone()
    assert row['scan_count']==0 and row['last_scanned_at'] is None
    assert row['last_error']=='six queries failed'
    assert app.db.next_due_city() is None


def test_partial_search_keeps_valid_results_but_retries_city(make_app,monkeypatch):
    app=make_app();app.db.add_city('Campinas','SP')
    candidate=CompanyCandidate('Acme','https://acme.com.br/','acme.com.br','test','q')
    def partial(*args):raise DiscoveryError('one query failed',[candidate])
    monkeypatch.setattr(app.discovery,'discover',partial)
    contact=Contact('rh@acme.com.br','rh',100,'https://acme.com.br/carreiras')
    monkeypatch.setattr(WebsiteCrawler,'crawl',lambda *_,**__: CrawlResult([contact],True,'evidence',True))
    app.scan_next_due_city()
    assert app.db.preview_next(3)['email']=='rh@acme.com.br'
    with app.db.connect() as con:
        city=con.execute('SELECT * FROM cities').fetchone()
    assert city['scan_count']==0 and city['last_error']=='one query failed'



def test_search_prefers_working_backend_then_falls_back(settings, monkeypatch):
    calls = []
    class FakeSearch:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def text(self, query, **kwargs):
            calls.append(kwargs['backend'])
            if kwargs['backend'] == 'bing':
                raise RuntimeError('unavailable')
            return [{'title': 'Acme', 'href': 'https://acme.com.br'}]
    monkeypatch.setattr('advogada_find.discovery.DDGS', FakeSearch)
    rows = SearchDiscovery(settings)._ddgs('empresa de software Campinas SP')
    assert calls == ['bing', 'yahoo']
    assert rows == [{'title': 'Acme', 'href': 'https://acme.com.br'}]


def test_all_search_backends_failing_remains_failure(settings, monkeypatch):
    class FakeSearch:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def text(self, query, **kwargs): raise RuntimeError('unavailable')
    monkeypatch.setattr('advogada_find.discovery.DDGS', FakeSearch)
    with pytest.raises(RuntimeError):
        SearchDiscovery(settings)._ddgs('empresa')


def test_search_engines_are_not_company_candidates():
    assert normalize('https://www.bing.com/search?q=software') is None
    assert normalize('https://search.yahoo.com/search?p=software') is None



def test_city_discovery_includes_recruiting_search_without_extra_queries(settings, monkeypatch):
    discovery = SearchDiscovery(settings)
    queries = []
    monkeypatch.setattr(discovery, '_ddgs', lambda query: queries.append(query) or [])
    assert discovery.discover('Brasília', 'DF') == []
    assert len(queries) == 6
    assert all('advoca' in q or 'advogados' in q for q in queries)
    assert any('currículo' in q and 'trabalhe conosco' in q for q in queries)
    assert all('Brasília' in q and 'DF' in q for q in queries)



def test_search_upgrades_known_firm_to_its_recruiting_page(settings, monkeypatch):
    discovery = SearchDiscovery(settings)
    monkeypatch.setattr(discovery, '_ddgs', lambda q: [
        {'title': 'Acme Advogados', 'href': 'https://acme.com.br/'},
        {'title': 'Acme - vagas', 'href': 'https://acme.com.br/trabalhe-conosco#enviar'}])
    candidates = discovery.discover('Campinas', 'SP')
    assert len(candidates) == 1
    assert candidates[0].website == 'https://acme.com.br/trabalhe-conosco'
    assert candidates[0].domain == 'acme.com.br'


def test_non_recruiting_search_page_still_normalizes_to_root(settings, monkeypatch):
    discovery = SearchDiscovery(settings)
    monkeypatch.setattr(discovery, '_ddgs', lambda q: [
        {'title': 'Acme', 'href': 'https://acme.com.br/produtos?proxima=vagas'}])
    assert discovery.discover('Campinas', 'SP')[0].website == 'https://acme.com.br/'



def test_search_tries_another_backend_when_first_returns_only_ad_links(settings, monkeypatch):
    calls = []
    class FakeSearch:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def text(self, query, **kwargs):
            calls.append(kwargs['backend'])
            if kwargs['backend'] == 'bing':
                return [{'title': 'Anúncio', 'href': 'https://www.bing.com/aclick?id=1'}]
            return [{'title': 'Acme', 'href': 'https://acme.com.br/carreiras'}]
    monkeypatch.setattr('advogada_find.discovery.DDGS', FakeSearch)
    rows = SearchDiscovery(settings)._ddgs('empresa')
    assert calls == ['bing', 'yahoo']
    assert rows[0]['href'] == 'https://acme.com.br/carreiras'

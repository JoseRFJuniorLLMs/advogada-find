from dataclasses import replace

import httpx
import pytest

from advogada_find.crawler import WebsiteCrawler, CrawlError, career_signal


def crawler_with(settings,handler):
    crawler=WebsiteCrawler(settings)
    crawler.client.close()
    crawler.client=httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=False)
    return crawler


def html(text):
    return httpx.Response(200,headers={'Content-Type':'text/html'},text=text)


def robots():
    return httpx.Response(200,text='User-agent: *\nAllow: /')


def test_external_redirect_is_never_requested(settings):
    seen=[]
    def handler(req):
        seen.append(str(req.url))
        if req.url.path=='/robots.txt': return robots()
        return httpx.Response(302,headers={'Location':'https://external.com.br/carreiras'})
    crawler=crawler_with(settings,handler)
    try:
        with pytest.raises(CrawlError): crawler.crawl('https://acme.com.br/','acme.com.br')
        assert not any('external.com.br' in url for url in seen)
    finally: crawler.close()


def test_subdomain_robots_respected(settings):
    seen=[]
    def handler(req):
        seen.append(str(req.url))
        if req.url.path=='/robots.txt':
            return httpx.Response(200,text='User-agent: *\nDisallow: /' if req.url.host=='jobs.acme.com.br' else 'User-agent: *\nAllow: /')
        if req.url.host=='acme.com.br': return html('<a href="https://jobs.acme.com.br/carreiras">Carreiras</a>')
        return html('rh@acme.com.br')
    crawler=crawler_with(settings,handler)
    try:
        assert crawler.crawl('https://acme.com.br/','acme.com.br').contacts==[]
        assert 'https://jobs.acme.com.br/robots.txt' in seen
        assert 'https://jobs.acme.com.br/carreiras' not in seen
    finally: crawler.close()


def test_redirect_checks_destination_robots_and_final_source(settings):
    def handler(req):
        if req.url.path=='/robots.txt': return robots()
        if req.url.path=='/': return httpx.Response(302,headers={'Location':'/carreiras'})
        return html('Envie seu currículo: rh@acme.com.br')
    crawler=crawler_with(settings,handler)
    try:
        result=crawler.crawl('https://acme.com.br/','acme.com.br')
        assert result.contacts[0].source_url=='https://acme.com.br/carreiras'
        assert result.careers_signal
    finally: crawler.close()


def test_robots_fetched_once_per_origin(settings):
    counts=[]
    def handler(req):
        if req.url.path=='/robots.txt': counts.append(str(req.url)); return robots()
        if req.url.path=='/': return html('<a href="/carreiras">Carreiras</a><a href="/contato">Contato</a>')
        return html('rh@acme.com.br')
    crawler=crawler_with(settings,handler)
    try:
        crawler.crawl('https://acme.com.br/','acme.com.br')
        assert len(counts)==1
    finally: crawler.close()


@pytest.mark.parametrize('code',[429,500,503])
def test_unavailable_robots_does_not_allow_crawl(settings,code):
    crawler=crawler_with(settings,lambda req: httpx.Response(code))
    try:
        with pytest.raises(CrawlError): crawler.crawl('https://acme.com.br/','acme.com.br')
    finally: crawler.close()


def test_parking_not_career_signal(settings):
    def handler(req):
        if req.url.path=='/robots.txt': return robots()
        return html('Software que administra vagas de estacionamento. contato@acme.com.br')
    crawler=crawler_with(replace(settings,contact_requires_careers_signal=True),handler)
    try:
        result=crawler.crawl('https://acme.com.br/','acme.com.br')
        assert not result.careers_signal and result.contacts==[]
    finally: crawler.close()


def test_law_firm_office_address_is_channel_by_default(settings):
    def handler(req):
        if req.url.path=='/robots.txt': return robots()
        return html('<title>Acme Advogados</title>Escritório de advocacia em Brasília, OAB/DF. '
                    'contato@acme.com.br dra.maria@acme.com.br')
    crawler=crawler_with(settings,handler)
    try:
        result=crawler.crawl('https://acme.com.br/','acme.com.br',city='Brasília')
        assert not result.careers_signal and result.eligible
        # A lawyer's personal address is never a recipient.
        assert [(c.email,c.kind) for c in result.contacts]==[('contato@acme.com.br','contato')]
    finally: crawler.close()


def test_only_visible_email_and_valid_mailto_extracted(settings):
    def handler(req):
        if req.url.path=='/robots.txt': return robots()
        return html('<script>"rh@acme.com.br"</script><!--jobs@acme.com.br--><a href="MAILTO:careers%40acme.com.br">Enviar</a>')
    crawler=crawler_with(settings,handler)
    try:
        result=crawler.crawl('https://acme.com.br/','acme.com.br')
        assert [c.email for c in result.contacts]==['careers@acme.com.br']
    finally: crawler.close()


def test_https_downgrade_rejected(settings):
    def handler(req):
        if req.url.path=='/robots.txt': return robots()
        return httpx.Response(302,headers={'Location':'http://acme.com.br/carreiras'})
    crawler=crawler_with(settings,handler)
    try:
        with pytest.raises(CrawlError): crawler.crawl('https://acme.com.br/','acme.com.br')
    finally: crawler.close()



def test_public_obfuscated_and_multi_mailto_contacts_keep_domain_and_role_rules(settings):
    key = 42
    encoded = (bytes([key]) + bytes(c ^ key for c in b'recrutamento@acme.com.br')).hex()
    foreign = (bytes([key]) + bytes(c ^ key for c in b'jobs@foreign.com.br')).hex()
    def handler(req):
        if req.url.path == '/robots.txt': return robots()
        return html(f"""<title>Acme</title>Escritório de advocacia em Campinas.
        <a href="mailto:jobs%40acme.com.br;privacy%40acme.com.br,jobs%40foreign.com.br">contatos</a>
        <span data-cfemail="{encoded}">[email protected]</span>
        <span data-cfemail="{foreign}">[email protected]</span>
        <span data-cfemail="not-a-hex-value">[email protected]</span>
        rh [at] acme [dot] com [dot] br; vagas [arroba] acme [ponto] com [ponto] br; joao (at) acme (dot) com (dot) br
        <script>"vagas@acme.com.br"</script>""")
    crawler = crawler_with(settings, handler)
    try:
        result = crawler.crawl('https://acme.com.br/', 'acme.com.br', city='Campinas')
        assert result.eligible
        assert {c.email for c in result.contacts} == {
            'jobs@acme.com.br', 'recrutamento@acme.com.br', 'rh@acme.com.br', 'vagas@acme.com.br'}
        assert all(c.source_url == 'https://acme.com.br/' for c in result.contacts)
    finally:
        crawler.close()


def test_career_links_prioritized_when_page_budget_is_small(settings):
    seen = []
    def handler(req):
        seen.append(req.url.path)
        if req.url.path == '/robots.txt': return robots()
        if req.url.path == '/':
            return html('<a href="/contato">Contato</a><a href="/carreiras">Carreiras</a>')
        if req.url.path == '/carreiras': return html('Envie seu currículo: rh@acme.com.br')
        return html('sem contatos')
    crawler = crawler_with(replace(settings, max_pages_per_site=2), handler)
    try:
        result = crawler.crawl('https://acme.com.br/', 'acme.com.br')
        assert [c.email for c in result.contacts] == ['rh@acme.com.br']
        assert '/carreiras' in seen and '/contato' not in seen
    finally:
        crawler.close()



def test_search_result_career_page_and_root_are_both_crawled(settings):
    seen = []
    def handler(req):
        seen.append(req.url.path)
        if req.url.path == '/robots.txt': return robots()
        if req.url.path == '/trabalhe-conosco':
            return html('<title>Acme vagas</title>Envie currículo: vagas@acme.com.br')
        if req.url.path == '/':
            return html('<title>Acme</title>Escritório de advocacia em Campinas')
        return httpx.Response(404)
    crawler = crawler_with(replace(settings, max_pages_per_site=2), handler)
    try:
        result = crawler.crawl('https://acme.com.br/trabalhe-conosco', 'acme.com.br', city='Campinas')
        assert result.eligible
        assert [c.email for c in result.contacts] == ['vagas@acme.com.br']
        assert result.contacts[0].source_url == 'https://acme.com.br/trabalhe-conosco'
        assert seen == ['/robots.txt', '/trabalhe-conosco', '/']
    finally:
        crawler.close()

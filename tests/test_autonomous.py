import csv
import zipfile
from dataclasses import replace
from io import StringIO

import httpx
import pytest

from advogada_find.crawler import WebsiteCrawler, CrawlResult, Contact
from advogada_find.discovery import CompanyCandidate
from advogada_find.registry import import_cnpj
from advogada_find.catalog import import_cities
from conftest import seed


def make_crawler(settings,text):
    crawler=WebsiteCrawler(settings); crawler.client.close()
    def handler(req):
        if req.url.path=='/robots.txt': return httpx.Response(404)
        return httpx.Response(200,headers={'content-type':'text/html'},text=text)
    crawler.client=httpx.Client(transport=httpx.MockTransport(handler))
    return crawler


def test_company_classified_automatically(settings):
    crawler=make_crawler(settings,'<title>Trabalhe conosco | Acme Advogados</title><p>Escritório de advocacia em Campinas, SP.</p><p>Envie seu currículo: rh@acme.com.br</p>')
    try:
        result=crawler.crawl('https://acme.com.br/','acme.com.br',city='Campinas')
        assert result.eligible and result.contacts[0].email=='rh@acme.com.br'
        assert result.name=='Acme Advogados'
    finally: crawler.close()


def test_scan_replaces_search_title_with_firm_name(make_app,monkeypatch):
    app=make_app()
    monkeypatch.setattr(app.discovery,'discover',lambda *_: [CompanyCandidate('Trabalhe conosco','https://acme.com.br/','acme.com.br','test','q')])
    result=CrawlResult([Contact('rh@acme.com.br','rh',100,'https://acme.com.br/')],True,'evidence',True,name='Acme Advogados')
    monkeypatch.setattr(WebsiteCrawler,'crawl',lambda *_,**__: result)
    app.scan_city('Campinas','SP')
    assert app.db.preview_next(3)['company_name']=='Acme Advogados'


@pytest.mark.parametrize('text,reason',[
    ('<title>Acme</title>Escritório de advocacia em Curitiba. rh@acme.com.br','cidade_nao_identificada'),
    ('<title>Acme</title>Padaria em Campinas. rh@acme.com.br','atividade_juridica_nao_identificada'),
    ('<title>Guia de empresas</title>Escritório de advocacia em Campinas. rh@acme.com.br','diretorio_ou_noticias'),
    ('<title>Acme</title>Escritório de advocacia em Campinas. joao@acme.com.br','canal_corporativo_inelegivel'),
])
def test_ineligible_companies_discarded_without_human_queue(settings,text,reason):
    crawler=make_crawler(settings,text)
    try:
        result=crawler.crawl('https://acme.com.br/','acme.com.br',city='Campinas')
        assert not result.eligible and reason in result.rejection_reason
    finally: crawler.close()


def test_scan_puts_eligible_company_in_queue_without_approval(make_app,monkeypatch):
    app=make_app()
    monkeypatch.setattr(app.discovery,'discover',lambda *_: [CompanyCandidate('Acme','https://acme.com.br/','acme.com.br','test','q')])
    result=CrawlResult([Contact('rh@acme.com.br','rh',100,'https://acme.com.br/carreiras')],True,'evidence',True)
    monkeypatch.setattr(WebsiteCrawler,'crawl',lambda *_,**__: result)
    app.scan_city('Campinas','SP')
    assert app.db.preview_next(3)['email']=='rh@acme.com.br'


def test_rescan_disqualifies_previous_company_without_human_queue(make_app,monkeypatch):
    app=make_app(); seed(app.db)
    monkeypatch.setattr(app.discovery,'discover',lambda *_: [CompanyCandidate('Acme','https://acme.com.br/','acme.com.br','test','q')])
    monkeypatch.setattr(WebsiteCrawler,'crawl',lambda *_,**__: CrawlResult([],False,'not eligible',False,'no_contacts'))
    app.scan_city('Campinas','SP')
    assert app.db.preview_next(3) is None
    assert app.db.stats()['rejected']==1


def test_failed_crawl_suspends_old_contact_until_success(make_app,monkeypatch):
    app=make_app(); seed(app.db)
    monkeypatch.setattr(app.discovery,'discover',lambda *_: [CompanyCandidate('Acme','https://acme.com.br/','acme.com.br','test','q')])
    def fail(*args,**kwargs): raise RuntimeError('unavailable')
    monkeypatch.setattr(WebsiteCrawler,'crawl',fail)
    with pytest.raises(RuntimeError): app.scan_city('Campinas','SP')
    assert app.db.preview_next(3) is None


def test_national_municipality_import_includes_multiple_states(make_app,monkeypatch):
    app=make_app()
    response=httpx.Response(200,json=[{'id':3509502,'nome':'Campinas'},
        {'id':4106902,'nome':'Curitiba'},{'id':1302603,'nome':'Manaus'}],request=httpx.Request('GET','https://ibge.test'))
    monkeypatch.setattr('advogada_find.catalog.httpx.get',lambda *_,**__: response)
    assert import_cities(app.db)==3
    assert {row['state'] for row in app.db.list_cities()}=={'SP','PR','AM'}


def write_rows(path,rows):
    buffer=StringIO(); writer=csv.writer(buffer,delimiter=';'); writer.writerows(rows)
    if path.suffix=='.zip':
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('data.csv',buffer.getvalue().encode('latin-1'))
    else: path.write_text(buffer.getvalue(),encoding='latin-1')


def establishment(basic,state,city,*,status='02',cnae='6911701',secondary=''):
    row=['']*30
    row[0:3]=[basic,'0001','00']; row[3]='1'; row[4]='Acme Software'
    row[5]=status; row[11]=cnae; row[12]=secondary; row[19]=state; row[20]=city
    row[27]='personal-address@gmail.com'
    return row


def test_receita_import_streams_nationwide_and_ignores_taxpayer_email(make_app,tmp_path):
    app=make_app()
    municipalities=tmp_path/'Municipios.zip'; write_rows(municipalities,[['6291','CAMPINAS'],['7535','CURITIBA']])
    establishments=tmp_path/'Estabelecimentos0.zip'
    write_rows(establishments,[establishment('12345678','SP','6291'),
        establishment('87654321','PR','7535',cnae='4711302',secondary='6911701'),
        establishment('11111111','SP','6291',status='08'),
        establishment('22222222','SP','6291',cnae='4711302')])
    companies=tmp_path/'Empresas0.zip'
    write_rows(companies,[['12345678','ACME SOFTWARE LTDA','','','','',''],
                          ['87654321','ACME DADOS LTDA','','','','','']])
    result=import_cnpj(app.db,[establishments],municipalities,[companies])
    assert result['matched']==2 and result['scanned']==4 and result['scope']=='BR'
    assert {r['state'] for r in app.db.list_registry()}=={'SP','PR'}
    assert app.db.list_registry()[0]['legal_name']=='ACME SOFTWARE LTDA'
    assert app.db.list_contacts()==[]
    import_cnpj(app.db,[establishments],municipalities,[companies])
    assert len(app.db.list_registry())==2


def test_registry_cnpj_must_match_site(settings):
    crawler=make_crawler(settings,'<title>Acme</title>Escritório de advocacia em Campinas. rh@acme.com.br CNPJ 12.345.678/0001-00')
    try:
        assert crawler.crawl('https://acme.com.br/','acme.com.br',city='Campinas',registry_cnpj='12345678000299').eligible
        assert not crawler.crawl('https://acme.com.br/','acme.com.br',city='Campinas',registry_cnpj='99999999000100').eligible
    finally: crawler.close()


def test_same_cnpj_on_different_domains_cannot_repeat(make_app):
    app=make_app(); first=seed(app.db,'first.com.br'); second=seed(app.db,'second.com.br')
    with app.db.connect() as con:
        con.execute("UPDATE companies SET cnpj_basic='12345678'")
    row=app.db.claim_next(3); app.db.mark_sent(row['application_id'],'subject','<test@example.com>')
    assert app.db.claim_next(3) is None

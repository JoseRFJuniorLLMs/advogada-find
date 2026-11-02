from advogada_find.crawler import Contact, CrawlResult, curated_recruitment_alias, score_email
from advogada_find.curated import import_curated


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

import csv
from pathlib import Path

import pytest
from advogada_find.prospecting import (
    PageParser, ProspectionDB, SiteFetcher, choose_pages, export_data,
    import_seeds, prospect_site, role_score, safe_url, same_official_domain,
)


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url, site):
        self.calls.append((url, site))
        return self.pages.get(url), url


def test_safe_urls():
    assert safe_url('escritorio.com.br') == 'https://escritorio.com.br/'
    for address in ('http://localhost/a', 'http://127.0.0.1', 'http://10.0.0.1',
                    'http://user:pass@corp.com.br', 'file:///secret',
                    'http://corp.local', 'https://corp.com.br:8080'):
        with pytest.raises(ValueError):
            safe_url(address)


def test_role_filter():
    site = 'https://www.escritorio.com.br/'
    assert role_score('rh@escritorio.com.br', site) == 100
    assert role_score('contato@escritorio.com.br', site) == 60
    assert role_score('maria.silva@escritorio.com.br', site) == 0
    assert role_score('rh@gmail.com', site) == 0
    assert not same_official_domain('https://escritorio.com.br.evil.com', site)


def test_confirmed_contact_has_official_proof():
    home = 'https://escritorio.com.br/'
    careers = 'https://escritorio.com.br/carreiras'
    fetcher = FakeFetcher({
        home: '<html><body>Sociedade de Advogados em Brasília <a href="/carreiras">Trabalhe conosco</a><p>contato@escritorio.com.br</p></body></html>',
        careers: '<html><body>Envie para <a href="mailto:rh@escritorio.com.br">RH</a><p>rh@gmail.com</p></body></html>',
    })
    result = prospect_site(home, fetcher=fetcher, delay=0)
    assert result['status'] == 'confirmed'
    assert result['email'] == 'rh@escritorio.com.br'
    assert result['source_url'] == careers


def test_no_legal_identity_goes_to_review():
    result = prospect_site('https://firma.com.br/', fetcher=FakeFetcher({
        'https://firma.com.br/': 'Fale com rh@firma.com.br'}), delay=0)
    assert result['status'] == 'review'
    assert result['email'] == ''


def test_no_external_or_personal_emails():
    result = prospect_site('https://firma.com.br/', fetcher=FakeFetcher({
        'https://firma.com.br/': 'Sociedade de advogados: joao@firma.com.br e rh@gmail.com'}), delay=0)
    assert result['status'] == 'review'


def test_robots_are_enforced():
    fetcher = SiteFetcher()
    def mock_get(url, site):
        if url.endswith('/robots.txt'):
            return 'User-agent: *\nDisallow: /carreiras', url
        return 'Sociedade de advogados. rh@firma.com.br', url
    fetcher._get = mock_get
    rules = fetcher._robot('https://firma.com.br/', 'https://firma.com.br/')
    assert rules.can_fetch('AdvogadaFind-Prospecting/1.0', 'https://firma.com.br/')
    assert not rules.can_fetch('AdvogadaFind-Prospecting/1.0', 'https://firma.com.br/carreiras')


def test_institutional_phone_from_site():
    result = prospect_site('https://firma.com.br/', fetcher=FakeFetcher({
        'https://firma.com.br/': 'Advogados: contato@firma.com.br <a href="tel:+55 (61) 3030-5050">Telefone da banca</a>'
    }), delay=0)
    assert result['status'] == 'confirmed'
    assert result['phone'] == '+556130305050'


def test_db_csv_import_export_review(tmp_path):
    seeds = tmp_path / 'seeds.csv'
    with seeds.open('w', encoding='utf-8', newline='') as fd:
        writer = csv.DictWriter(fd, fieldnames=['name', 'city', 'state', 'website', 'email', 'source_url', 'cargo'])
        writer.writeheader()
        writer.writerow({'name': 'ABC Advogados', 'city': 'Brasília', 'state': 'DF',
                         'website': 'https://abc.com.br', 'email': 'rh@abc.com.br',
                         'source_url': 'https://diretorio.example/abc', 'cargo': 'RH'})
        writer.writerow({'name': 'ABC Advogados', 'city': 'Brasília', 'state': 'DF',
                         'website': 'https://abc.com.br', 'email': '', 'source_url': ''})
        writer.writerow({'name': 'Mesmo site com WWW', 'city': 'Brasília', 'state': 'DF',
                         'website': 'https://www.abc.com.br/', 'email': '', 'source_url': ''})
        writer.writerow({'name': 'sem site', 'city': 'Brasília', 'state': 'DF',
                         'website': '', 'email': '', 'source_url': ''})
    db = ProspectionDB(tmp_path / 'prospeccao.db')
    outcome = import_seeds(db, str(seeds), 'diretorio')
    assert outcome['added'] == 1
    assert outcome['duplicate'] == 2
    assert len(outcome['errors']) == 1
    row = db.list()[0]
    assert row['status'] == 'pending'
    assert row['email'] == ''  # imported email is never treated as confirmed
    assert row['suggested_email'] == 'rh@abc.com.br'

    fake = FakeFetcher({'https://abc.com.br/': 'ABC sociedade de advogados. Contato: rh@abc.com.br'})
    outcome = db.scan(row['id'], fetcher=fake, delay=0)
    assert outcome['status'] == 'confirmed'
    accepted = tmp_path / 'curated.csv'
    assert export_data(db, accepted) == 1
    with accepted.open(encoding='utf-8', newline='') as fd:
        lines = list(csv.DictReader(fd))
    assert lines[0]['source_url'] == 'https://abc.com.br/'
    db.reset(row['id'], reject=True)
    assert export_data(db, accepted) == 0
    review = tmp_path / 'review.csv'
    assert export_data(db, review, review=True) == 1
    with review.open(encoding='utf-8', newline='') as fd:
        reviewed = list(csv.DictReader(fd))
    assert reviewed[0]['suggested_email'] == 'rh@abc.com.br'
    db.close()


def test_parser_discards_javascript_embedded_email():
    p = PageParser()
    p.feed('<script>"rh@escritorio.com.br"</script><p>Contate <a href="mailto:contato@escritorio.com.br">a equipe</a>.</p>')
    _, _, emails, _ = p.results()
    assert 'rh@escritorio.com.br' not in emails
    assert 'contato@escritorio.com.br' in emails


def test_hunter_verification_valid_and_invalid(tmp_path):
    db = ProspectionDB(tmp_path / 'p.db')
    db.add({'name': 'XYZ Advogados', 'city': 'Brasília', 'state': 'DF', 'website': 'https://xyz.com.br/'})
    db.scan(db.list()[0]['id'], fetcher=FakeFetcher({'https://xyz.com.br/': 'Sociedade de advogados, rh@xyz.com.br'}), delay=0)
    office_id = db.list()[0]['id']
    db.record_verification(office_id, 'valid')
    assert db.get(office_id)['status'] == 'confirmed'
    assert db.get(office_id)['verification_status'] == 'valid'
    db.record_verification(office_id, 'invalid')
    assert db.get(office_id)['status'] == 'review'
    assert export_data(db, tmp_path / 'approved.csv') == 0
    assert export_data(db, tmp_path / 'review.csv', review=True) == 1
    db.close()


def test_no_false_verification_of_unconfirmed(tmp_path):
    db = ProspectionDB(tmp_path / 'p.db')
    db.add({'name': 'XYZ Advogados', 'city': 'Brasília', 'state': 'DF', 'website': 'https://xyz.com.br/'})
    with pytest.raises(ValueError):
        db.record_verification(db.list()[0]['id'], 'valid')
    db.close()

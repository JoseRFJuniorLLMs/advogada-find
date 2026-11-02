# Import a reviewed list only after rechecking each official website.
from __future__ import annotations

import csv
from .catalog import STATES
from .crawler import WebsiteCrawler, CrawlError, Contact, CrawlResult, curated_recruitment_alias
from .discovery import normalize
from .domains import valid_url

REQUIRED = {'name', 'city', 'state', 'website', 'email', 'source_url'}


def import_curated(db, settings, csv_path, batch):
    if not batch.strip():
        raise ValueError('Informe o nome do lote')
    result = {'batch': batch.strip(), 'processed': 0, 'imported': 0, 'review': []}
    crawler = WebsiteCrawler(settings)
    try:
        with csv_path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not REQUIRED.issubset(reader.fieldnames):
                raise ValueError('CSV precisa das colunas: ' + ', '.join(sorted(REQUIRED)))
            for number, row in enumerate(reader, 2):
                result['processed'] += 1
                try:
                    if not all(isinstance(row.get(key), str) and row[key].strip() for key in REQUIRED):
                        raise ValueError('campo obrigatório ausente')
                    state = row['state'].strip().upper()
                    if state not in STATES:
                        raise ValueError('UF inválida')
                    normalized = normalize(row['website'].strip())
                    if not normalized:
                        raise ValueError('site inválido ou bloqueado')
                    domain, website = normalized
                    source = row['source_url'].strip()
                    if not valid_url(source, domain):
                        raise ValueError('fonte não pertence ao domínio oficial')
                    checked = crawler.crawl(source, domain, city=row['city'].strip())
                    address = row['email'].strip().lower()
                    published = dict(checked.career_publications)
                    blockers = ('cidade_nao_identificada', 'atividade_juridica_nao_identificada',
                                'identidade_site_ausente', 'diretorio_ou_noticias',
                                'cnpj_nao_confirmado_no_site')
                    if (not any(ct.email == address for ct in checked.contacts)
                            and address in published and curated_recruitment_alias(address)
                            and not any(x in checked.rejection_reason for x in blockers)):
                        checked = CrawlResult(
                            checked.contacts + [Contact(address, 'recrutamento', 85, published[address])],
                            checked.careers_signal, checked.evidence, True, '', checked.cnpj_basic,
                            checked.name, checked.career_publications)
                    if not checked.eligible:
                        raise ValueError('site não passou na verificação: ' + checked.rejection_reason)
                    db.add_curated_verified(name=checked.name or row['name'], domain=domain, website=website,
                        city=row['city'], state=state, batch=batch, result=checked, email=row['email'])
                    result['imported'] += 1
                except (ValueError, CrawlError) as exc:
                    result['review'].append({'line': number, 'reason': str(exc)})
    finally:
        crawler.close()
    return result

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx
from ddgs import DDGS

from .config import Settings
from .domains import company_domain, valid_url
from .names import firm_name

log = logging.getLogger(__name__)
BLOCKED = {'linkedin.com', 'glassdoor.com', 'glassdoor.com.br', 'indeed.com',
           'indeed.com.br', 'facebook.com', 'instagram.com', 'youtube.com', 'x.com',
           'twitter.com', 'google.com', 'reclameaqui.com.br', 'wikipedia.org',
           'gupy.io', 'jusbrasil.com.br', 'bing.com', 'yahoo.com',
           'duckduckgo.com', 'brave.com', 'yandex.com', 'mojeek.com', 'startpage.com',
           # Legal news, lawyer directories and job boards are not employers.
           'escavador.com', 'jus.com.br', 'conjur.com.br', 'migalhas.com.br',
           'oab.org.br', 'oabdf.org.br', 'oabrj.org.br', 'catho.com.br', 'vagas.com.br',
           'infojobs.com.br', 'empregos.com.br', 'trabalhabrasil.com.br', 'solides.com.br',
           'getninjas.com.br', 'doctoralia.com.br', 'whatsapp.com', 'wa.me',
           'tiktok.com', 'linktr.ee'}
# Courts, prosecutors, public defenders and government hire by public exam, not by e-mail.
BLOCKED_SUFFIXES = ('.gov.br', '.jus.br', '.mp.br', '.leg.br', '.def.br', '.edu.br')
QUERIES = ['escritório de advocacia {city} {state}', 'advogados associados {city} {state}',
           'advocacia direito administrativo servidores públicos {city} {state}',
           'escritório de advocacia cível trabalhista {city} {state}',
           'advocacia criminal {city} {state}',
           'escritório de advocacia {city} {state} trabalhe conosco envie currículo']


class DiscoveryError(RuntimeError):
    def __init__(self, message: str, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


@dataclass(frozen=True)
class CompanyCandidate:
    name: str
    website: str
    domain: str
    source: str
    source_query: str
    cnpj: str = ''


def normalize(url: str) -> tuple[str, str] | None:
    if not url:
        return None
    if '://' not in url:
        url = 'https://' + url
    if not valid_url(url):
        return None
    parsed = urlparse(url)
    domain = company_domain(parsed.hostname)
    if domain in BLOCKED or ('.' + domain).endswith(BLOCKED_SUFFIXES):
        return None
    return domain, f'{parsed.scheme}://{parsed.netloc}/'


def _recruiting_page(url: str, domain: str) -> str | None:
    """Preserve useful search result paths only within the verified domain."""
    if '://' not in url:
        url = 'https://' + url
    if not valid_url(url, domain):
        return None
    path = unquote(urlparse(url).path).casefold()
    if any(term in path for term in ('carreira', 'trabalhe', 'vaga', 'jobs', 'careers',
                                     'recrutamento', 'recruit', 'curriculo', 'candidatura',
                                     'talentos', 'faca-parte')):
        return url.split('#', 1)[0]
    return None


class SearchDiscovery:
    def __init__(self, settings: Settings):
        self.s = settings

    def _brave(self, query: str) -> list[dict]:
        headers = {'Accept': 'application/json', 'X-Subscription-Token': self.s.brave_search_api_key,
                   'User-Agent': self.s.user_agent}
        with httpx.Client(timeout=self.s.http_timeout_seconds, headers=headers) as client:
            response = client.get('https://api.search.brave.com/res/v1/web/search',
                params={'q': query, 'count': min(self.s.max_results_per_query, 20), 'country': 'BR'})
            response.raise_for_status()
            return [{'title': x.get('title', ''), 'href': x.get('url', '')}
                    for x in response.json().get('web', {}).get('results', [])]

    def _ddgs(self, query: str) -> list[dict]:
        # Prefer providers which responded in production; retain DDGS auto as a
        # fallback because availability varies by region and over time.
        last_error = None
        saw_empty = False
        with DDGS(timeout=self.s.http_timeout_seconds) as search:
            for backend in ('bing', 'yahoo', 'auto'):
                try:
                    rows = search.text(query, region='br-pt', safesearch='moderate',
                                       max_results=self.s.max_results_per_query, backend=backend)
                except Exception as exc:
                    last_error = exc
                    log.info('search_provider_failed backend=%s error=%s', backend, type(exc).__name__)
                    continue
                # A provider may return only its own ad redirect URLs. Those
                # are not company leads; try the next provider in that case.
                usable = [{'title': x.get('title', ''), 'href': x.get('href', '')}
                          for x in (rows or []) if normalize(x.get('href', ''))]
                if usable:
                    return usable
                saw_empty = True
        if saw_empty:
            return []
        if last_error:
            raise last_error
        return []

    def discover(self, city: str, state: str) -> list[CompanyCandidate]:
        seen, output, failed = {}, [], 0
        for pattern in QUERIES:
            query = pattern.format(city=city, state=state)
            source = 'ddgs'
            try:
                if self.s.brave_search_api_key:
                    try:
                        rows = self._brave(query)
                        source = 'brave'
                    except Exception as exc:
                        log.warning('search_fallback provider=brave error=%s', type(exc).__name__)
                        rows = self._ddgs(query)
                else:
                    rows = self._ddgs(query)
            except Exception as exc:
                failed += 1
                log.warning('search_failed city=%s provider=%s error=%s', city, source, type(exc).__name__)
                continue
            for row in rows:
                normalized = normalize(row.get('href', ''))
                if not normalized:
                    log.info('candidate_rejected reason=domain_or_url')
                    continue
                domain, website = normalized
                career_page = _recruiting_page(row.get('href', ''), domain)
                if domain in seen:
                    index = seen[domain]
                    previous = output[index]
                    if career_page and not _recruiting_page(previous.website, domain):
                        output[index] = CompanyCandidate(previous.name, career_page, domain,
                                                         source, query, previous.cnpj)
                    continue
                seen[domain] = len(output)
                # The crawl replaces this with the site's own name when it finds one.
                title = firm_name(row.get('title') or '') or domain
                output.append(CompanyCandidate(title[:160], career_page or website, domain, source, query))
        if failed:
            # Partial searches are not silently considered complete either.
            raise DiscoveryError(f'{failed}/{len(QUERIES)} consultas falharam; cidade deve ser retentada',output)
        return output

    def discover_registered(self, name: str, city: str, state: str, cnpj: str) -> list[CompanyCandidate]:
        query = f'"{name}" "{city}" {state} site oficial'
        source = 'ddgs+receita'
        if self.s.brave_search_api_key:
            try:
                rows = self._brave(query)
                source = 'brave+receita'
            except Exception:
                rows = self._ddgs(query)
        else:
            rows = self._ddgs(query)
        output,seen = [],set()
        for row in rows:
            normalized = normalize(row.get('href',''))
            if normalized and normalized[0] not in seen:
                domain,website=normalized
                seen.add(domain)
                output.append(CompanyCandidate(name,website,domain,source,query,cnpj))
        return output

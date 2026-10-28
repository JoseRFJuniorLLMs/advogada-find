from __future__ import annotations

import logging
import re
import time
import unicodedata
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import Settings
from .domains import company_domain, valid_url
from .names import firm_name

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.I)
CAREER_TERMS = ('carreiras', 'trabalhe conosco', 'trabalhe-conosco', 'vagas',
                'jobs', 'careers', 'recrutamento', 'talentos', 'banco de talentos',
                'faca parte', 'faca-parte')
LINK_TERMS = CAREER_TERMS + ('contato', 'contact', 'fale conosco', 'fale-conosco',
                            'sobre', 'about', 'quem somos', 'institucional', 'localizacao',
                            'curriculo', 'candidatura', 'oportunidades', 'escritorio',
                            'o escritorio', 'equipe', 'nossa equipe')
REJECT = {'noreply', 'no-reply', 'abuse', 'privacy', 'privacidade', 'dpo',
          'security', 'postmaster', 'webmaster', 'suporte'}
ROLE_SCORES = {
    'rh': ('rh', 100), 'hr': ('rh', 100), 'recrutamento': ('recrutamento', 100),
    'recruiting': ('recrutamento', 100), 'vagas': ('vagas', 98), 'jobs': ('jobs', 98),
    'careers': ('careers', 98), 'carreiras': ('careers', 98), 'talentos': ('talentos', 96),
    'talent': ('talentos', 96), 'people': ('people', 90), 'peopleops': ('people', 90),
    'curriculo': ('curriculos', 99), 'curriculos': ('curriculos', 99), 'cv': ('curriculos', 97),
    'trabalheconosco': ('curriculos', 99), 'selecao': ('recrutamento', 97),
    'processoseletivo': ('recrutamento', 97), 'bancodetalentos': ('talentos', 96),
    # Small law firms often publish only a generic office address.
    'contato': ('contato', 55), 'contact': ('contato', 55), 'faleconosco': ('contato', 52),
    'escritorio': ('contato', 50), 'secretaria': ('contato', 48), 'atendimento': ('contato', 46),
    'info': ('contato', 45),
}


class CrawlError(RuntimeError):
    pass


@dataclass(frozen=True)
class Contact:
    email: str
    kind: str
    score: int
    source_url: str


@dataclass(frozen=True)
class CrawlResult:
    contacts: list[Contact]
    careers_signal: bool
    evidence: str = ''
    eligible: bool = False
    rejection_reason: str = ''
    cnpj_basic: str = ''
    name: str = ''


def fold(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text.lower())
                   if not unicodedata.combining(c))


def score_email(email: str, careers_signal: bool) -> tuple[str, int]:
    """careers_signal=True also means generic office addresses are allowed."""
    if EMAIL_RE.fullmatch(email) is None:
        return 'reject', 0
    local = email.split('@', 1)[0].lower()
    normalized = re.sub(r'[^a-z0-9]', '', local)
    if normalized in {re.sub(r'[^a-z0-9]', '', x) for x in REJECT} or 'noreply' in normalized:
        return 'reject', 0
    kind, score = ROLE_SCORES.get(normalized, ('reject', 0))
    if kind == 'contato' and not careers_signal:
        return 'reject', 0
    return kind, score


def same_domain(email_domain: str, domain: str) -> bool:
    return email_domain == domain or email_domain.endswith('.' + domain)


def career_signal(text: str, url: str) -> bool:
    path = fold(urlparse(url).path)
    if re.search(r'/(?:carreiras?|careers?|jobs|recrutamento|trabalhe[-_]conosco)(?:/|$)', path):
        return True
    text = fold(text)
    return bool(re.search(r'\b(?:trabalhe conosco|junte-se (?:a|ao)|oportunidades (?:de emprego|profissionais)|'
                          r'vagas (?:abertas|de emprego|disponiveis para|para advogad[oa]s?)|'
                          r'envie (?:seu |o seu )?curriculo|banco de talentos|processo seletivo|'
                          r'faca parte (?:do nosso time|da nossa equipe)|recrutamento|careers|jobs)\b', text))


# Some public pages show addresses as "rh [at] empresa [dot] com" to deter bots.
# Only decode explicit separators; the existing role/domain checks still apply.
OBFUSCATED_EMAIL_RE = re.compile(
    r'\b([A-Z0-9._%+-]+)\s*(?:\[\s*(?:at|arroba)\s*\]|\(\s*(?:at|arroba)\s*\)|\s+arroba\s+)\s*'
    r'([A-Z0-9-]+(?:\s*(?:\.|\[\s*(?:dot|ponto)\s*\]|\(\s*(?:dot|ponto)\s*\)|\s+ponto\s+)\s*[A-Z0-9-]+)+)\b', re.I)


def obfuscated_emails(text: str):
    for match in OBFUSCATED_EMAIL_RE.finditer(text):
        domain = re.sub(r'\s*(?:\[\s*(?:dot|ponto)\s*\]|\(\s*(?:dot|ponto)\s*\)|\s+ponto\s+)\s*', '.', match.group(2), flags=re.I)
        domain = re.sub(r'\s*\.\s*', '.', domain)
        email = f'{match.group(1)}@{domain}'.lower()
        if EMAIL_RE.fullmatch(email):
            yield email


def cloudflare_email(encoded: str) -> str | None:
    # Cloudflare's public, reversible e-mail protection uses a one-byte XOR key.
    if not re.fullmatch(r'(?:[0-9a-fA-F]{2}){2,512}', encoded):
        return None
    try:
        data = bytes.fromhex(encoded)
        email = bytes(byte ^ data[0] for byte in data[1:]).decode('ascii').lower()
    except (ValueError, UnicodeDecodeError):
        return None
    return email if EMAIL_RE.fullmatch(email) else None


class WebsiteCrawler:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client = httpx.Client(timeout=settings.http_timeout_seconds,
            follow_redirects=False, headers={'User-Agent': settings.user_agent,
                                             'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.6'})
        self._robots = {}
        self._last_request = 0.0

    def close(self) -> None:
        self.client.close()

    def _request(self, url: str, domain: str, check_robots: bool = True):
        for _ in range(6):
            if not valid_url(url, domain):
                raise CrawlError('Destino fora do domínio oficial ou URL inválida')
            if check_robots and not self._allowed(url, domain):
                log.info('crawl_rejected reason=robots url=%s', url)
                return None
            delay = self.s.crawl_delay_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            try:
                response = self.client.get(url, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise CrawlError(f'Falha HTTP: {type(exc).__name__}') from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                if not response.headers.get('location'):
                    raise CrawlError('Redirect sem destino')
                target = urljoin(url, response.headers['location'])
                if urlparse(url).scheme == 'https' and urlparse(target).scheme != 'https':
                    raise CrawlError('Redirect reduz segurança de HTTPS')
                url = target
                continue
            return response
        raise CrawlError('Limite de redirecionamentos excedido')

    def _allowed(self, url: str, domain: str) -> bool:
        parsed = urlparse(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin not in self._robots:
            response = self._request(origin + '/robots.txt', domain, check_robots=False)
            if response.status_code in {404, 410}:
                self._robots[origin] = True
            elif response.status_code in {401, 403}:
                self._robots[origin] = False
            elif response.status_code >= 400:
                raise CrawlError(f'robots.txt indisponível: HTTP {response.status_code}')
            else:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
                self._robots[origin] = parser
        rule = self._robots[origin]
        return rule if isinstance(rule, bool) else rule.can_fetch(self.s.user_agent, url)

    def _fetch(self, url: str, domain: str):
        response = self._request(url, domain)
        if response is None:
            return None
        if response.status_code in {404, 410}:
            return None
        if response.status_code >= 400:
            raise CrawlError(f'Página indisponível: HTTP {response.status_code}')
        if 'html' not in response.headers.get('content-type', '').lower():
            return None
        return str(response.url), response.text

    def crawl(self, website: str, domain: str, city: str = '', registry_cnpj: str = '') -> CrawlResult:
        domain = company_domain(domain)
        if not valid_url(website, domain):
            raise CrawlError('Website não corresponde à empresa')
        # Start with a specific search-result page when present, then inspect
        # the same origin's homepage for company and location evidence.
        parsed = urlparse(website)
        root = f'{parsed.scheme}://{parsed.netloc}/'
        queue, visited, pages = [website], set(), []
        if root != website:
            queue.append(root)
        careers = False
        texts, titles, brands = [], [], []
        while queue and len(visited) < self.s.max_pages_per_site:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            fetched = self._fetch(url, domain)
            if not fetched:
                continue
            final_url, html = fetched
            soup = BeautifulSoup(html, 'html.parser')
            for node in soup(['script', 'style']):
                node.decompose()
            text = soup.get_text(' ', strip=True)
            title = soup.title.get_text(' ',strip=True) if soup.title else ''
            brand = soup.find('meta', attrs={'property':'og:site_name'})
            titles.append(title or (brand.get('content','') if brand else ''))
            brands.append(brand.get('content','') if brand else '')
            texts.append(text)
            careers = careers or career_signal(text, final_url)
            pages.append((final_url, soup, text))
            discovered_links = []
            for link in soup.find_all('a', href=True):
                joined = urljoin(final_url, link['href'].strip()).split('#', 1)[0]
                if not valid_url(joined, domain):
                    continue
                hay = fold(link.get_text(' ', strip=True) + ' ' + joined)
                if any(term in hay for term in LINK_TERMS) and joined not in visited and joined not in queue:
                    priority = 0 if any(term in hay for term in CAREER_TERMS + ('curriculo', 'candidatura')) else 1
                    discovered_links.append((priority, joined))
            for _, joined in sorted(discovered_links, key=lambda item: item[0]):
                if joined not in queue:
                    queue.append(joined)
        found = {}
        for url, soup, text in pages:
            for link in soup.find_all('a', href=True):
                href = link['href']
                if href.lower().startswith('mailto:'):
                    addresses = unquote(href[7:].split('?', 1)[0])
                    for address in re.split(r'[,;]', addresses):
                        address = address.strip().lower()
                        if EMAIL_RE.fullmatch(address):
                            found[address] = url
            for element in soup.find_all(attrs={'data-cfemail': True}):
                encoded = element.get('data-cfemail')
                if isinstance(encoded, str):
                    address = cloudflare_email(encoded)
                    if address:
                        found[address] = url
            for address in EMAIL_RE.findall(text):
                found[address.lower()] = url
            for address in obfuscated_emails(text):
                found[address] = url
        contacts = []
        for email, url in found.items():
            if not same_domain(email.rsplit('@', 1)[1], domain):
                log.info('contact_rejected reason=foreign_domain domain=%s', domain)
                continue
            # The office address of a law firm is a legitimate channel when configured.
            kind, score = score_email(email, careers or not self.s.contact_requires_careers_signal)
            if score:
                contacts.append(Contact(email, kind, score, url))
        contacts.sort(key=lambda c: (-c.score, c.email))
        combined = fold(' '.join(texts))
        legal = bool(re.search(r'\b(?:advocacia|advogad[oa]s?(?: associad[oa]s)?|escritorio de advocacia|'
                               r'sociedade (?:individual )?de advocacia|sociedade de advogados|'
                               r'assessoria juridica|consultoria juridica|servicos juridicos|'
                               r'direito (?:administrativo|publico|civil|penal|criminal|do trabalho|'
                               r'trabalhista|previdenciario|empresarial|tributario|constitucional)|'
                               r'oab ?/ ?[a-z]{2})\b', combined))
        locality = bool(city and re.search(r'(?<!\w)' + re.escape(fold(city)) + r'(?!\w)',combined))
        branding = any(title.strip() for title in titles)
        directory = any(re.search(r'\b(?:guia de empresas|lista de empresas|diretorio|noticias|ranking de empresas|'
                                  r'(?:melhores|lista de|guia de|encontre) advogados|jurisprudencia)\b',
                                  fold(title)) for title in titles)
        identifiers = re.findall(r'(?<![a-z0-9])[a-z0-9]{2}\.?[a-z0-9]{3}\.?[a-z0-9]{3}/?[a-z0-9]{4}-?\d{2}(?![a-z0-9])', combined)
        roots = {re.sub(r'[^a-z0-9]','',value)[:8].upper() for value in identifiers}
        cnpj_basic = next(iter(roots)) if len(roots)==1 else ''
        legal_match = True
        if registry_cnpj:
            legal_match = registry_cnpj[:8].upper() in roots
            cnpj_basic = registry_cnpj[:8].upper() if legal_match else ''
        eligible = bool(legal and locality and branding and not directory and contacts and legal_match)
        reasons = []
        for ok, reason in [(legal,'atividade_juridica_nao_identificada'),(locality,'cidade_nao_identificada'),
                           (branding,'identidade_site_ausente'),(not directory,'diretorio_ou_noticias'),
                           (bool(contacts),'canal_corporativo_inelegivel'),
                           (legal_match,'cnpj_nao_confirmado_no_site')]:
            if not ok: reasons.append(reason)
        evidence = (f'website={website}; advocacia={legal}; cidade={city}:{locality}; '
                    f'identidade={branding}; carreiras={careers}; cnpj_confirmado={legal_match}; páginas={len(pages)}')
        return CrawlResult(contacts, careers, evidence, eligible, ','.join(reasons), cnpj_basic,
                           firm_name(*brands, *titles))

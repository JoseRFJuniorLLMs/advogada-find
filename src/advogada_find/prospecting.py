"""Prospeccao de canais institucionais para o advogada-find.

Modulo aditivo: NAO modifica o banco ou a fila de envios existentes.
Execute: python -m advogada_find.prospecting --help
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import html
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import socket
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.robotparser import RobotFileParser

USER_AGENT = 'AdvogadaFind-Prospecting/1.0 (+institutional-contact-discovery)'
EMAIL_RE = re.compile(r'(?<![\w.+-])([A-Z0-9._%+\-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])', re.I)
GENERIC_MAILBOXES = {
    'rh': 100, 'recursoshumanos': 100, 'recursos.humanos': 100,
    'recrutamento': 100, 'recruitment': 100, 'recrutamentoeselecao': 100,
    'talentos': 100, 'talent': 100, 'talentacquisition': 100,
    'carreiras': 100, 'career': 100, 'careers': 100,
    'trabalheconosco': 100, 'trabalhe.conosco': 100,
    'curriculo': 100, 'curriculos': 100, 'vagas': 100, 'selecao': 100,
    'people': 90, 'gente': 90, 'gestaodepessoas': 90,
    'contato': 60, 'contact': 60, 'escritorio': 60,
    'secretaria': 60, 'atendimento': 60, 'administrativo': 60,
    'admin': 50, 'info': 50,
}
LEGAL_TERMS = ('advocacia', 'advogados', 'advogadas', 'advogado', 'advogada',
               'juridico', 'jurídico', 'juridica', 'jurídica',
               'direito administrativo', 'sociedade de advogados', 'oab/')
CAREER_TERMS = ('rh', 'recrut', 'curricul', 'talent', 'carreir', 'trabalhe',
                'vaga', 'selecao', 'seleção', 'oportunidade', 'people')
PAGE_TERMS = CAREER_TERMS + ('contato', 'contact', 'equipe', 'quem-somos',
                             'sobre', 'institucional', 'fale-conosco')
DEFAULT_PATHS = ('/contato', '/contatos', '/fale-conosco', '/carreiras',
                 '/trabalhe-conosco', '/trabalhe_conosco', '/recrutamento')
SEED_ALIASES = {
    'name': ('name', 'nome', 'escritorio', 'escritório', 'company', 'empresa', 'razao_social'),
    'city': ('city', 'cidade', 'municipio', 'município'),
    'state': ('state', 'uf', 'estado'),
    'website': ('website', 'site', 'url', 'domain', 'dominio', 'domínio'),
    'email': ('email', 'e-mail', 'corporate_email'),
    'source_url': ('source_url', 'fonte', 'url_fonte', 'origem_url'),
    'contact_name': ('contact_name', 'nome_contato', 'person_name'),
    'contact_role': ('contact_role', 'cargo', 'job_title', 'title'),
    'linkedin_url': ('linkedin_url', 'linkedin', 'perfil_linkedin'),
}
OUTPUT_COLUMNS = ('name', 'city', 'state', 'website', 'email', 'source_url')


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _host(url: str) -> str:
    return (urlsplit(url).hostname or '').lower().rstrip('.')


def safe_url(value: str) -> str:
    """Reject credentials, IPs, localhost, local zones, uncommon ports, unsafe schemes."""
    value = (value or '').strip()
    if not value:
        raise ValueError('site ausente')
    if not re.match(r'^https?://', value, re.I):
        value = 'https://' + value
    parts = urlsplit(value)
    host = (parts.hostname or '').lower().rstrip('.')
    if parts.scheme not in {'http', 'https'} or not host or parts.username or parts.password:
        raise ValueError('URL do site inválida')
    if parts.port not in {None, 80, 443}:
        raise ValueError('porta não permitida')
    if host in {'localhost', 'localhost.localdomain'} or '.' not in host or host.endswith(('.local', '.internal', '.test', '.localhost', '.onion')):
        raise ValueError('domínio não público')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('endereços IP não permitidos')
    if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', host):
        raise ValueError('domínio inválido')
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))


def _domain_aliases(host: str) -> set[str]:
    return {host, host[4:] if host.startswith('www.') else 'www.' + host}


def same_official_domain(url: str, website: str) -> bool:
    """Conservador: apenas o próprio host e sua variante www."""
    return _host(url) in _domain_aliases(_host(website))


def role_score(address: str, website: str) -> int:
    address = (address or '').lower().strip()
    if address.count('@') != 1:
        return 0
    local, domain = address.split('@', 1)
    if domain not in _domain_aliases(_host(website)):
        return 0
    if not EMAIL_RE.fullmatch(address) or any(c in local for c in ('+', '/')):
        return 0
    return GENERIC_MAILBOXES.get(local, 0)


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.mails: set[str] = set()
        self.phones: set[str] = set()
        self.chunks: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style', 'noscript'}:
            self.skip += 1
        if tag == 'a':
            href = (attrs.get('href') or '').strip()
            if href.lower().startswith('mailto:'):
                self.mails.update(EMAIL_RE.findall(unquote(href[7:].split('?')[0])))
            elif href.lower().startswith('tel:'):
                number = re.sub(r'[^0-9+]', '', unquote(href[4:].split('?')[0]))
                if 10 <= len(re.sub(r'\D', '', number)) <= 13:
                    self.phones.add(number)
            elif href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.chunks.append(data)

    def results(self):
        content = html.unescape(' '.join(self.chunks))
        return content, self.links, (self.mails | set(EMAIL_RE.findall(content))), self.phones


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SiteFetcher:
    def __init__(self, timeout: float = 12, max_bytes: int = 500_000):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = build_opener(NoRedirect())
        self.robots: dict[str, RobotFileParser] = {}

    @staticmethod
    def check_public_dns(url: str):
        """Fail closed if a hostname points to private/reserved networks."""
        host = _host(safe_url(url))
        try:
            ips = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError('domínio não resolvido') from exc
        if not ips:
            raise ValueError('domínio sem IP')
        for item in ips:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                raise ValueError('destino de rede não público')

    def _get(self, url: str, site: str, check_robots: bool = False):
        for _ in range(4):
            safe_url(url)
            if not same_official_domain(url, site):
                raise ValueError('redirecionamento fora do domínio oficial')
            if check_robots and not self._robot(url, site).can_fetch(USER_AGENT, url):
                raise PermissionError('robots.txt impede a consulta')
            self.check_public_dns(url)
            try:
                req = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html, text/plain;q=0.5'})
                with self.opener.open(req, timeout=self.timeout) as response:
                    mime = response.headers.get('Content-Type', '').lower()
                    if urlsplit(url).path != '/robots.txt' and 'html' not in mime and 'text/plain' not in mime:
                        return None, url
                    charset = response.headers.get_content_charset() or 'utf-8'
                    raw = response.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        raise ValueError('página acima do limite')
                    return raw.decode(charset, errors='replace'), url
            except HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    target = urljoin(url, exc.headers.get('Location', ''))
                    if target == url:
                        raise ValueError('redirecionamento inválido') from exc
                    url = target
                    continue
                if exc.code in (404, 410):
                    return None, url
                raise
        raise ValueError('redirecionamentos excessivos')

    def _robot(self, url: str, site: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin not in self.robots:
            txt, _ = self._get(origin + '/robots.txt', site)
            rp = RobotFileParser()
            rp.set_url(origin + '/robots.txt')
            if txt is None:
                # 404: no robots policy is published; allow site fetching.
                rp.parse([])
            else:
                rp.parse(txt.splitlines())
            self.robots[origin] = rp
        return self.robots[origin]

    def fetch(self, url: str, site: str) -> tuple[str | None, str]:
        return self._get(url, site, check_robots=True)


def choose_pages(website: str, anchors: list[str], max_pages: int) -> list[str]:
    pages = []
    for href in anchors:
        target = urljoin(website, href)
        try:
            target = safe_url(target)
        except ValueError:
            continue
        if same_official_domain(target, website) and any(x in target.lower() for x in PAGE_TERMS):
            pages.append(target)
    pages.extend(urljoin(website, p) for p in DEFAULT_PATHS)
    dedup = []
    seen = {website.rstrip('/')}
    for p in pages:
        p = p.split('#', 1)[0]
        if p.rstrip('/') not in seen:
            dedup.append(p)
            seen.add(p.rstrip('/'))
        if len(dedup) >= max_pages:
            break
    return dedup


def prospect_site(website: str, fetcher=None, max_pages: int = 6, delay: float = 0.5) -> dict:
    website = safe_url(website)
    fetcher = fetcher or SiteFetcher()
    legal = False
    evidence = []
    found: dict[str, tuple[int, str]] = {}
    phone = ''
    pages = [website]
    visited = set()
    failures = []
    for i, url in enumerate(pages):
        if url in visited or i > max_pages:
            continue
        visited.add(url)
        try:
            body, final_url = fetcher.fetch(url, website)
        except (ValueError, PermissionError, HTTPError, URLError, TimeoutError, OSError) as exc:
            failures.append(f'{url}: {type(exc).__name__}: {exc}')
            continue
        if not body:
            continue
        parsed = PageParser()
        parsed.feed(body)
        content, links, mails, phones = parsed.results()
        if not phone and phones and (i == 0 or 'contato' in final_url.lower() or 'contact' in final_url.lower()):
            phone = sorted(phones)[0]
        lower = content.lower()
        if any(t in lower for t in LEGAL_TERMS):
            legal = True
            evidence.append(final_url)
        if i == 0:
            pages.extend(choose_pages(final_url, links, max_pages))
        bonus = 5 if any(t in final_url.lower() for t in CAREER_TERMS) else 0
        for email in mails:
            score = role_score(email, website)
            if not score:
                continue
            score += bonus
            old = found.get(email.lower())
            if old is None or score > old[0]:
                found[email.lower()] = (score, final_url)
        if delay and i < len(pages) - 1:
            time.sleep(delay)
    ranked = sorted(found.items(), key=lambda kv: (-kv[1][0], kv[0]))
    if ranked and legal:
        email, (score, source_url) = ranked[0]
        return {'status': 'confirmed', 'email': email, 'source_url': source_url, 'phone': phone,
                'score': score, 'reason': 'endereço institucional publicado no domínio oficial',
                'pages_checked': len(visited)}
    if ranked and not legal:
        return {'status': 'review', 'email': '', 'source_url': '', 'phone': phone, 'score': 0,
                'reason': 'endereço encontrado, mas identidade jurídica não confirmada',
                'pages_checked': len(visited)}
    if failures and len(failures) == len(visited):
        return {'status': 'blocked', 'email': '', 'source_url': '', 'phone': phone, 'score': 0,
                'reason': '; '.join(failures)[:500], 'pages_checked': len(visited)}
    return {'status': 'review', 'email': '', 'source_url': '', 'phone': phone, 'score': 0,
            'reason': 'site consultado, sem endereço institucional elegível',
            'pages_checked': len(visited)}


SCHEMA = '''
CREATE TABLE IF NOT EXISTS offices (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 city TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT '',
 website TEXT NOT NULL UNIQUE,
 site_key TEXT NOT NULL UNIQUE,
 source_type TEXT NOT NULL,
 source_reference TEXT NOT NULL DEFAULT '',
 contact_name TEXT NOT NULL DEFAULT '',
 contact_role TEXT NOT NULL DEFAULT '',
 linkedin_url TEXT NOT NULL DEFAULT '',
 suggested_email TEXT NOT NULL DEFAULT '',
 email TEXT NOT NULL DEFAULT '',
 phone TEXT NOT NULL DEFAULT '',
 email_source TEXT NOT NULL DEFAULT '',
 verification_status TEXT NOT NULL DEFAULT 'not_checked',
 verified_at TEXT,
 score INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'pending',
 reason TEXT NOT NULL DEFAULT '',
 last_checked TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prospect_status ON offices(status, state);
CREATE TABLE IF NOT EXISTS scan_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 office_id INTEGER NOT NULL REFERENCES offices(id),
 happened_at TEXT NOT NULL,
 status TEXT NOT NULL,
 details TEXT NOT NULL
);
'''


class ProspectionDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def add(self, fields: dict, source_type: str = 'csv') -> str:
        name = (fields.get('name') or '').strip()
        if not name:
            raise ValueError('nome do escritório ausente')
        website = safe_url(fields.get('website') or '')
        website = f'{urlsplit(website).scheme}://{urlsplit(website).netloc}/'
        now = utcnow()
        initial_email = (fields.get('email') or '').strip().lower()
        if role_score(initial_email, website) == 0:
            initial_email = ''
        site_key = _host(website).removeprefix('www.')
        values = (name, (fields.get('city') or '').strip(),
                  (fields.get('state') or '').strip().upper(), website, site_key,
                  source_type, (fields.get('source_url') or '').strip(),
                  (fields.get('contact_name') or '').strip(),
                  (fields.get('contact_role') or '').strip(),
                  (fields.get('linkedin_url') or '').strip(),
                  initial_email, now, now)
        cur = self.db.execute('''INSERT OR IGNORE INTO offices
          (name,city,state,website,site_key,source_type,source_reference,contact_name,
           contact_role,linkedin_url,suggested_email,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', values)
        self.db.commit()
        return 'added' if cur.rowcount else 'duplicate'

    def get(self, office_id: int):
        return self.db.execute('SELECT * FROM offices WHERE id=?', (office_id,)).fetchone()

    def list(self, status: str = '', state: str = '', limit: int = 500):
        clauses = []
        params = []
        if status:
            clauses.append('status=?')
            params.append(status)
        if state:
            clauses.append('state=?')
            params.append(state.upper())
        query = 'SELECT * FROM offices'
        if clauses:
            query += ' WHERE ' + ' AND '.join(clauses)
        query += ' ORDER BY CASE status WHEN "confirmed" THEN 0 WHEN "review" THEN 1 ELSE 2 END, score DESC, id ASC LIMIT ?'
        return self.db.execute(query, (*params, min(max(limit, 1), 20000))).fetchall()

    def scan(self, office_id: int, fetcher=None, delay: float = 0.5):
        row = self.get(office_id)
        if row is None:
            raise ValueError('escritório não encontrado')
        if row['status'] == 'rejected':
            raise ValueError('escritório rejeitado manualmente')
        result = prospect_site(row['website'], fetcher=fetcher, delay=delay)
        now = utcnow()
        with self.db:
            self.db.execute('''UPDATE offices SET status=?,email=?,phone=?,email_source=?,verification_status='not_checked',verified_at=NULL,score=?,reason=?,
              last_checked=?,updated_at=? WHERE id=?''',
              (result['status'], result['email'], result['phone'], result['source_url'], result['score'],
               result['reason'], now, now, office_id))
            self.db.execute('INSERT INTO scan_events (office_id,happened_at,status,details) VALUES (?,?,?,?)',
                            (office_id, now, result['status'], result['reason']))
        return result

    def record_verification(self, office_id: int, verification_status: str):
        if verification_status not in {'valid', 'invalid', 'accept_all', 'unknown', 'webmail', 'disposable'}:
            raise ValueError('resultado de verificação desconhecido')
        row = self.get(office_id)
        if not row or row['status'] != 'confirmed' or not row['email']:
            raise ValueError('somente contatos confirmados podem ser verificados')
        now = utcnow()
        trusted = verification_status == 'valid'
        status = 'confirmed' if trusted else 'review'
        reason = row['reason'] if trusted else f'Hunter: entregabilidade inconclusiva ou negativa ({verification_status})'
        with self.db:
            self.db.execute('''UPDATE offices SET verification_status=?,verified_at=?,status=?,reason=?,updated_at=? WHERE id=?''',
                            (verification_status, now, status, reason, now, office_id))
            self.db.execute('INSERT INTO scan_events (office_id,happened_at,status,details) VALUES (?,?,?,?)',
                            (office_id, now, status, f'Hunter: {verification_status}'))

    def reset(self, office_id: int, reject: bool = False):
        if not self.get(office_id):
            raise ValueError('escritório não encontrado')
        with self.db:
            self.db.execute('''UPDATE offices SET status=?, email='',phone='',email_source='',verification_status='not_checked',verified_at=NULL,score=0,
            reason=?,updated_at=? WHERE id=?''',
                            ('rejected' if reject else 'pending',
                             'rejeitado pelo operador' if reject else '', utcnow(), office_id))


def read_seeds(path: str | Path):
    with open(path, encoding='utf-8-sig', newline='') as fd:
        reader = csv.DictReader(fd)
        if not reader.fieldnames:
            raise ValueError('CSV sem cabeçalho')
        header = {col.lower().strip(): col for col in reader.fieldnames}
        for line_number, row in enumerate(reader, start=2):
            cleaned = {}
            for key, aliases in SEED_ALIASES.items():
                matched = next((header[a] for a in aliases if a in header), None)
                cleaned[key] = (row.get(matched) or '').strip() if matched else ''
            yield line_number, cleaned


def import_seeds(db: ProspectionDB, csv_path: str, source_type: str) -> dict:
    result = {'added': 0, 'duplicate': 0, 'errors': []}
    for line, fields in read_seeds(csv_path):
        try:
            result[db.add(fields, source_type)] += 1
        except ValueError as exc:
            result['errors'].append({'line': line, 'reason': str(exc)})
    return result


def export_data(db: ProspectionDB, output_path: str | Path, review: bool = False):
    rows = db.list(limit=20000)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ('id', 'name', 'city', 'state', 'website', 'status', 'reason',
               'source_type', 'source_reference', 'contact_name', 'contact_role',
               'linkedin_url', 'phone', 'suggested_email', 'email', 'email_source', 'verification_status', 'verified_at', 'last_checked') if review else OUTPUT_COLUMNS
    count = 0
    with open(path, 'w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        seen = set()
        for row in rows:
            if review:
                if row['status'] == 'confirmed':
                    continue
                data = {c: row[c] for c in columns}
            else:
                if row['status'] != 'confirmed':
                    continue
                # The original import-curated revalidates all evidence.
                data = {'name': row['name'], 'city': row['city'], 'state': row['state'],
                        'website': row['website'], 'email': row['email'],
                        'source_url': row['email_source']}
                if not data['city'] or not data['state'] or not role_score(data['email'], data['website']):
                    continue
                signature = _host(data['website'])
                if signature in seen:
                    continue
                seen.add(signature)
            writer.writerow(data)
            count += 1
    return count


def hunter_verify(email: str, api_key: str) -> str:
    """Optional paid third-party email verification; never sends mail."""
    from urllib.parse import urlencode
    from urllib.request import urlopen
    if not api_key or not EMAIL_RE.fullmatch(email):
        raise ValueError('chave Hunter ou endereço inválido')
    endpoint = 'https://api.hunter.io/v2/email-verifier?' + urlencode({'email': email, 'api_key': api_key})
    with urlopen(Request(endpoint, headers={'User-Agent': USER_AGENT}), timeout=30) as response:
        if response.status == 202:
            return 'unknown'
        value = json.load(response).get('data', {}).get('status', 'unknown')
    return value if value in {'valid', 'invalid', 'accept_all', 'unknown', 'webmail', 'disposable'} else 'unknown'


def hunter_suggestions(domain: str, api_key: str, limit: int = 30) -> list[str]:
    """Optional: Hunter API is a hint only, never proof of publication."""
    from urllib.parse import urlencode
    from urllib.request import urlopen
    safe_url(domain)
    hostname = _host(safe_url(domain))
    if not api_key:
        raise ValueError('HUNTER_API_KEY ausente')
    endpoint = 'https://api.hunter.io/v2/domain-search?' + urlencode({
        'domain': hostname, 'api_key': api_key, 'limit': min(max(limit, 1), 100)})
    with urlopen(Request(endpoint, headers={'User-Agent': USER_AGENT}), timeout=15) as response:
        data = json.load(response)
    emails = [entry.get('value', '').lower() for entry in data.get('data', {}).get('emails', [])]
    return sorted({e for e in emails if role_score(e, domain) > 0})


DASHBOARD = r'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Advogada Find · Prospecção</title>
<style>body{font:15px system-ui;background:#f3f5f9;color:#142239;margin:0}header{background:#13253d;color:white;padding:24px max(20px,calc((100vw - 1200px)/2))}h1{margin:0}main{max-width:1200px;margin:25px auto;padding:0 16px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.stat{background:#fff;padding:20px;border-radius:12px}.stat b{font-size:27px;display:block}.actions{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}.actions a,select,input{padding:10px;border:1px solid #cdd7e5;border-radius:8px;background:#fff;color:#142239}a{color:#1854a6}.table{overflow:auto;border-radius:12px;background:white}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:13px;border-bottom:1px solid #edf0f5;vertical-align:top}th{background:#e8eef8}small{color:#667085}.pill{border-radius:30px;background:#edf0f5;padding:3px 9px;font-weight:600}footer{margin:24px 0;color:#526074}</style>
</head><body><header><h1>Advogada Find · Prospecção</h1><p>Contatos de RH e recrutamento publicados pelos escritórios · sem envios automáticos</p></header>
<main><div id="stats" class="stats"></div><div class="actions"><input id="search" placeholder="Buscar escritório, UF ou e-mail" aria-label="Busca"><select id="status"><option value="">Todos os status</option><option value="confirmed">Confirmados</option><option value="review">Revisão</option><option value="pending">Pendentes</option><option value="blocked">Bloqueados</option><option value="rejected">Rejeitados</option></select><a href="/export-curated.csv">Exportar confirmados</a><a href="/export-review.csv">Exportar revisão</a></div>
<div class="table"><table><thead><tr><th>Escritório</th><th>Contato</th><th>Classificação</th><th>Origem</th><th>Verificado em</th></tr></thead><tbody id="results"></tbody></table></div>
<footer>Somente e-mails institucionais comprovados entram no CSV. O comando import-curated do sistema principal faz nova verificação antes de incluir na fila.</footer></main>
<script>
let data=[];const E=(tag,txt)=>{const e=document.createElement(tag);e.textContent=txt??'';return e};
const summary=()=>{const c={total:data.length};for(const r of data)c[r.status]=(c[r.status]||0)+1;document.getElementById('stats').replaceChildren(...[['Escritórios',c.total],['Confirmados',c.confirmed||0],['Revisão',c.review||0],['Pendentes',c.pending||0],['Bloqueados',c.blocked||0]].map(([name,num])=>{const d=E('div');d.className='stat';d.append(E('small',name),E('b',String(num)));return d}))};
const paint=()=>{const q=document.getElementById('search').value.toLowerCase();const status=document.getElementById('status').value;const rows=data.filter(x=>(!status||x.status===status)&&[x.name,x.state,x.city,x.email,x.website].some(s=>(s||'').toLowerCase().includes(q)));const tbody=document.getElementById('results');tbody.replaceChildren(...rows.map(x=>{const tr=E('tr');const name=E('td');name.append(E('b',x.name),E('small',` · ${x.city}/${x.state}`));const contact=E('td');contact.append(E('div',x.email||'Ainda não confirmado'),E('small',x.suggested_email&&!x.email?`Sugestão não verificada: ${x.suggested_email}`:''),E('small',x.contact_name?`${x.contact_name} · ${x.contact_role||''}`:''),E('small',x.phone||''),E('small',` · Verificação: ${x.verification_status||'não realizada'}`));const st=E('td');const badge=E('span',x.status);badge.className='pill';st.append(badge,E('small',` ${x.reason||''}`));const src=E('td');const a=E('a',x.email_source||x.website);a.href=x.email_source||x.website;a.target='_blank';a.rel='noopener noreferrer';src.append(a,E('small',` ${x.source_type}`),E('small',x.source_reference?` · Semente: ${x.source_reference}`:''));tr.append(name,contact,st,src,E('td',x.last_checked||'—'));return tr}));if(!rows.length){const tr=E('tr');const td=E('td','Nenhum registro encontrado');td.colSpan=5;tr.append(td);tbody.append(tr)}};
for(const id of ['search','status'])document.getElementById(id).addEventListener(id==='search'?'input':'change',paint);
fetch('/api/offices').then(r=>r.json()).then(j=>{data=j.items;summary();paint()}).catch(()=>{document.getElementById('results').textContent='Falha ao carregar.'});
</script></body></html>'''


def dashboard(db_path: str, port: int = 8798, host: str = '127.0.0.1'):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            db = ProspectionDB(db_path)
            try:
                uri = urlsplit(self.path).path
                if uri in {'/', '/index.html'}:
                    data, mime = DASHBOARD.encode('utf-8'), 'text/html; charset=utf-8'
                elif uri == '/api/offices':
                    # no personal email is ever returned by the collector
                    data = json.dumps({'items': [dict(row) for row in db.list(limit=20000)]},
                                      ensure_ascii=False).encode('utf-8')
                    mime = 'application/json; charset=utf-8'
                elif uri in {'/export-curated.csv', '/export-review.csv'}:
                    import io
                    stream = io.StringIO()
                    review = uri.endswith('review.csv')
                    columns = ('id', 'name', 'city', 'state', 'website', 'status', 'reason',
                               'source_type', 'source_reference', 'contact_name', 'contact_role',
                               'linkedin_url', 'phone', 'suggested_email', 'email', 'email_source', 'verification_status', 'verified_at', 'last_checked') if review else OUTPUT_COLUMNS
                    writer = csv.DictWriter(stream, fieldnames=columns)
                    writer.writeheader()
                    for r in db.list(limit=20000):
                        if review and r['status'] != 'confirmed':
                            writer.writerow({k: r[k] for k in columns})
                        elif not review and r['status'] == 'confirmed' and r['city'] and r['state']:
                            writer.writerow({'name': r['name'], 'city': r['city'], 'state': r['state'],
                                             'website': r['website'], 'email': r['email'], 'source_url': r['email_source']})
                    data, mime = stream.getvalue().encode('utf-8'), 'text/csv; charset=utf-8'
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(data)
            finally:
                db.close()

    with ThreadingHTTPServer((host, port), Handler) as server:
        print(f'Painel de prospecção: http://{host}:{port}/')
        server.serve_forever()


def cli(argv=None):
    parser = argparse.ArgumentParser(description='Prospecção de escritórios para advogada-find (sem SMTP)')
    parser.add_argument('--db', default='prospection.sqlite3', help='Banco separado do advogada-find.db')
    sub = parser.add_subparsers(dest='command', required=True)
    imported = sub.add_parser('import-seeds', help='Importar CSV de diretórios ou ferramentas B2B')
    imported.add_argument('--input', required=True)
    imported.add_argument('--source', default='csv', help='Ex.: legal500, chambers, cnpj, hunter, linkedin-manual')
    scan = sub.add_parser('scan', help='Verificar publicações no domínio oficial')
    scan.add_argument('--limit', type=int, default=30)
    scan.add_argument('--delay', type=float, default=1.5)
    scan.add_argument('--force', action='store_true', help='Rever resultados antigos (exceto rejeitados)')
    export = sub.add_parser('export', help='Exportar CSV para import-curated ou para revisão')
    export.add_argument('--output', required=True)
    export.add_argument('--review', action='store_true')
    review = sub.add_parser('review', help='Rejeitar ou reiniciar um escritório')
    review.add_argument('--id', type=int, required=True)
    review.add_argument('--decision', choices=('reject', 'reset'), required=True)
    hunt = sub.add_parser('hunter', help='Consultar sugestões de RH por domínio (não confirma contatos)')
    hunt.add_argument('--domain', required=True)
    hunt.add_argument('--api-key', default='', help='Ou use HUNTER_API_KEY')
    verify = sub.add_parser('verify', help='Verificar entregabilidade via Hunter (opcional, pode consumir créditos)')
    verify.add_argument('--api-key', default='', help='Ou HUNTER_API_KEY')
    verify.add_argument('--limit', type=int, default=10)
    verify.add_argument('--delay', type=float, default=1.0)
    sub.add_parser('status')
    serve = sub.add_parser('dashboard')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8798)
    args = parser.parse_args(argv)
    if args.command == 'dashboard':
        dashboard(args.db, args.port, args.host)
        return 0
    db = ProspectionDB(args.db)
    try:
        if args.command == 'import-seeds':
            print(json.dumps(import_seeds(db, args.input, args.source), ensure_ascii=False, indent=2))
        elif args.command == 'scan':
            entries = [r for r in db.list(limit=20000) if r['status'] != 'rejected' and (args.force or r['status'] in {'pending', 'blocked'})]
            for row in entries[:max(0, args.limit)]:
                print(f"{row['id']}: {row['name']} => {db.scan(row['id'], delay=max(0, args.delay))['status']}", flush=True)
                if args.delay:
                    time.sleep(max(0, args.delay))
        elif args.command == 'export':
            print(f'Linhas exportadas: {export_data(db, args.output, review=args.review)}')
        elif args.command == 'review':
            db.reset(args.id, args.decision == 'reject')
            print('Atualizado')
        elif args.command == 'hunter':
            import os
            candidates = hunter_suggestions(args.domain, args.api_key or os.environ.get('HUNTER_API_KEY', ''))
            print(json.dumps({'suggested_only': candidates, 'notice': 'confirme no site oficial antes do import-curated'}, ensure_ascii=False))
        elif args.command == 'verify':
            import os
            api_key = args.api_key or os.environ.get('HUNTER_API_KEY', '')
            if not api_key:
                raise ValueError('HUNTER_API_KEY ausente')
            candidates = [r for r in db.list(status='confirmed', limit=20000) if r['verification_status'] == 'not_checked']
            for row in candidates[:max(0, args.limit)]:
                value = hunter_verify(row['email'], api_key)
                db.record_verification(row['id'], value)
                print(f"{row['id']}: {row['email']} => {value}", flush=True)
                if args.delay:
                    time.sleep(max(0, args.delay))
        elif args.command == 'status':
            from collections import Counter
            counts = Counter(row['status'] for row in db.list(limit=20000))
            print(json.dumps(dict(counts), ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


if __name__ == '__main__':
    sys.exit(cli())

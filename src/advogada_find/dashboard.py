"""Read-only dashboard, bound to loopback and accessed through an SSH tunnel."""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)


def snapshot(settings: Settings, query: dict) -> dict:
    db = Database(settings.db_path, contact_requires_careers_signal=settings.contact_requires_careers_signal,
                           priority_states=settings.priority_states)
    status = query.get('status',[''])[0]
    state = query.get('state',[''])[0][:2].upper()
    search = query.get('q',[''])[0][:120]
    try:
        page = max(1,min(int(query.get('page',['1'])[0]),100000))
    except ValueError:
        page = 1
    filters, params = [], []
    if status:
        if status not in {'sent','failed','uncertain','sending'}:
            raise ValueError('Estado de candidatura inválido')
        filters.append('a.status=?'); params.append(status)
    if state:
        filters.append('c.state=?'); params.append(state)
    if search:
        filters.append('(c.name LIKE ? OR c.domain LIKE ? OR a.email LIKE ?)')
        params.extend(['%'+search+'%']*3)
    where = (' WHERE '+ ' AND '.join(filters)) if filters else ''
    base = ' FROM applications a JOIN companies c ON c.id=a.company_id'
    with db.connect() as con:
        total = con.execute('SELECT COUNT(*) n'+base+where,params).fetchone()['n']
        rows = [dict(r) for r in con.execute('''SELECT a.id,c.name company_name,c.domain,c.city,c.state,
            a.email,a.status,a.attempts,a.attempted_at,a.sent_at,a.message_id,a.last_error'''+base+where+
            ' ORDER BY COALESCE(a.sent_at,a.attempted_at) DESC,a.id DESC LIMIT 50 OFFSET ?',params+[(page-1)*50])]
        coverage = dict(con.execute('''SELECT COUNT(*) total,SUM(scan_count>0) scanned,
            SUM(last_error IS NOT NULL) errors FROM cities WHERE active=1''').fetchone())
        recent_errors = [dict(r) for r in con.execute('''SELECT city,state,last_error,next_scan_at FROM cities
            WHERE last_error IS NOT NULL ORDER BY next_scan_at DESC LIMIT 10''')]
        catalogued = con.execute('SELECT COUNT(*) n FROM companies').fetchone()['n']
        source_state=con.execute("SELECT value FROM runtime WHERE key='cnpj_sync_status'").fetchone()
    return {'stats':db.stats() | {'sent_today':db.sent_today_count(settings.timezone),'catalogued':catalogued},
            'coverage':coverage,'applications':rows,'total':total,'page':page,
            'events':db.recent_events(15),'scan_errors':recent_errors,
            'dry_run':settings.dry_run,'from_email':settings.from_email,'timezone':settings.timezone,
            'cnpj_sync':source_state['value'] if source_state else 'Carga nacional ainda não iniciada'}


def handler_for(settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # This service intentionally cannot be bound outside loopback through the CLI.
            route = urlparse(self.path)
            try:
                if route.path=='/':
                    data=Path(__file__).with_name('dashboard.html').read_bytes()
                    content_type='text/html; charset=utf-8'
                elif route.path=='/api/status':
                    data=json.dumps(snapshot(settings,parse_qs(route.query)),ensure_ascii=False).encode('utf-8')
                    content_type='application/json; charset=utf-8'
                elif route.path=='/health':
                    data=b'{"ok":true}'; content_type='application/json'
                else:
                    self.send_error(404); return
                self.send_response(200)
                self.send_header('Content-Type',content_type)
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
                self.end_headers(); self.wfile.write(data)
            except ValueError:
                self.send_error(400,'Consulta inválida')
            except Exception as exc:
                log.error('dashboard_error error=%s',type(exc).__name__)
                self.send_error(503,'Painel temporariamente indisponível')

        def log_message(self, format, *args):
            log.info('dashboard_request status=%s',args[1] if len(args)>1 else '')
    return Handler


def serve(settings: Settings, port: int = 8797):
    if not 1<=port<=65535:
        raise ValueError('Porta inválida')
    server=ThreadingHTTPServer(('127.0.0.1',port),handler_for(settings))
    log.info('dashboard_start address=127.0.0.1:%s',port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

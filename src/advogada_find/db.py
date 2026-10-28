from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from .domains import company_domain
from .locking import sender_lock


class ManagedConnection(sqlite3.Connection):
    """sqlite3's regular context manager commits but does not close."""
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS cities (
    id INTEGER PRIMARY KEY,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    scan_count INTEGER NOT NULL DEFAULT 0,
    last_scanned_at TEXT,
    next_scan_at TEXT,
    UNIQUE(city, state)
);

CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL UNIQUE,
    website TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    source TEXT NOT NULL,
    source_query TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    careers_signal INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    kind TEXT NOT NULL,
    score INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(company_id, email)
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    email TEXT NOT NULL,
    subject TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    attempted_at TEXT,
    sent_at TEXT,
    next_attempt_at TEXT,
    message_id TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS curated_queue (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    batch TEXT NOT NULL, source_url TEXT NOT NULL, verified_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_city_due ON cities(active, next_scan_at, priority);
CREATE INDEX IF NOT EXISTS idx_contacts_company_score ON contacts(company_id, active, score);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
    application_id INTEGER, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_establishments (
    cnpj TEXT PRIMARY KEY, cnpj_basic TEXT NOT NULL, trade_name TEXT NOT NULL,
    legal_name TEXT NOT NULL DEFAULT '', city TEXT NOT NULL, state TEXT NOT NULL,
    cnae TEXT NOT NULL, imported_at TEXT NOT NULL, company_id INTEGER,
    next_lookup_at TEXT, last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_registry_lookup ON registry_establishments(company_id,next_lookup_at);
CREATE INDEX IF NOT EXISTS idx_registry_basic ON registry_establishments(cnpj_basic);
CREATE TABLE IF NOT EXISTS registry_exclusions (cnpj_basic TEXT PRIMARY KEY);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


class Database:
    def __init__(self, path: Path, *, contact_requires_careers_signal: bool = True,
                 priority_states: tuple[str, ...] = ('DF',)):
        self.path = Path(path)
        self.contact_requires_careers_signal = contact_requires_careers_signal
        from .catalog import STATES
        # Inlined into SQL below, so only known UF codes are accepted.
        if any(state not in STATES for state in priority_states):
            raise ValueError('UF inválida em priority_states')
        self.priority_states = tuple(priority_states)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, factory=ManagedConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(SCHEMA)
        # Additive migration: preserve historical company/application IDs.
        with self.tx() as con:
            additions = {
                'companies': {'identity_key': 'TEXT', 'verified': 'INTEGER NOT NULL DEFAULT 0',
                              'verification_note': "TEXT NOT NULL DEFAULT ''", 'cnpj_basic': 'TEXT'},
                'applications': {'lease_until': 'TEXT', 'retryable': 'INTEGER NOT NULL DEFAULT 1'},
                'cities': {'last_error': 'TEXT'},
            }
            for table, columns in additions.items():
                present = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
                for name, spec in columns.items():
                    if name not in present:
                        con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {spec}')
            for row in con.execute('SELECT id,domain FROM companies WHERE identity_key IS NULL').fetchall():
                try:
                    identity = company_domain(row['domain'])
                except ValueError:
                    identity = row['domain'].lower()
                con.execute('UPDATE companies SET identity_key=? WHERE id=?', (identity, row['id']))
            con.execute('CREATE INDEX IF NOT EXISTS idx_company_identity ON companies(identity_key)')

    def seed_cities(self, csv_path: Path) -> None:
        if not csv_path.exists():
            return
        with csv_path.open(encoding="utf-8", newline="") as fh, self.connect() as con:
            for row in csv.DictReader(fh):
                con.execute(
                    """
                    INSERT INTO cities(city,state,priority,next_scan_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(city,state) DO NOTHING
                    """,
                    (row["city"].strip(), row["state"].strip(), int(row["priority"]), iso()),
                )

    def add_city(self, city: str, state: str, priority: int = 50) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO cities(city,state,priority,next_scan_at)
                VALUES(?,?,?,?)
                ON CONFLICT(city,state) DO UPDATE SET active=1,priority=excluded.priority
                """,
                (city.strip(), state.strip().upper(), priority, iso()),
            )

    def next_due_city(self):
        with self.connect() as con:
            return con.execute(
                """
                SELECT * FROM cities
                WHERE active=1 AND (next_scan_at IS NULL OR next_scan_at<=?)
                ORDER BY (scan_count=0) DESC,priority DESC,COALESCE(last_scanned_at,'') ASC,id ASC LIMIT 1
                """,
                (iso(),),
            ).fetchone()

    def mark_city_scanned(self, city_id: int, days: int) -> None:
        now = utcnow()
        with self.connect() as con:
            con.execute(
                "UPDATE cities SET scan_count=scan_count+1,last_scanned_at=?,next_scan_at=?,last_error=NULL WHERE id=?",
                (iso(now), iso(now + timedelta(days=days)), city_id),
            )

    def mark_city_retry(self, city_id: int, hours: int = 6, error: str = '') -> None:
        retry = utcnow() + timedelta(hours=hours)
        with self.connect() as con:
            con.execute(
                "UPDATE cities SET next_scan_at=?,last_error=? WHERE id=?",
                (iso(retry), error[:1000], city_id),
            )

    def upsert_company(self, name: str, domain: str, website: str, city: str, state: str, source: str, source_query: str) -> int:
        now = iso()
        identity = company_domain(domain)
        with self.tx() as con:
            existing = con.execute('SELECT id FROM companies WHERE identity_key=? ORDER BY id LIMIT 1',
                                   (identity,)).fetchone()
            if existing:
                # A concurrent scanner cannot reset a reviewed company.
                con.execute('''UPDATE companies SET website=?,last_seen_at=?,active=0 WHERE id=?
                    AND NOT EXISTS (SELECT 1 FROM curated_queue WHERE company_id=companies.id)''',
                            (website, now, existing['id']))
                return int(existing['id'])
            con.execute(
                """
                INSERT INTO companies(name,domain,website,city,state,source,source_query,first_seen_at,last_seen_at,identity_key,active)
                VALUES(?,?,?,?,?,?,?,?,?,?,0)
                ON CONFLICT(domain) DO UPDATE SET website=excluded.website,last_seen_at=excluded.last_seen_at,active=0
                """,
                (name, identity, website, city, state, source, source_query, now, now, identity),
            )
            return int(con.execute("SELECT id FROM companies WHERE domain=?", (identity,)).fetchone()["id"])

    def rename_company(self, company_id: int, name: str) -> None:
        with self.connect() as con:
            con.execute('''UPDATE companies SET name=? WHERE id=? AND NOT EXISTS (SELECT 1 FROM curated_queue WHERE company_id=companies.id)''', (' '.join(name.split())[:160], company_id))

    def approve_company(self, company_id: int, note: str) -> None:
        if not note.strip():
            raise ValueError('Informe evidência do domínio oficial, atividade e localização')
        with self.connect() as con:
            row = con.execute('SELECT identity_key FROM companies WHERE id=?', (company_id,)).fetchone()
            if not row:
                raise ValueError('Empresa não encontrada')
            con.execute('UPDATE companies SET verified=1,active=1,verification_note=? WHERE identity_key=?',
                        (note.strip(), row['identity_key']))

    def replace_contacts(self, company_id: int, contacts, careers_signal: bool,
                         *, eligible: bool | None = None, evidence: str = '', cnpj_basic: str = '') -> None:
        """Replace a complete crawl snapshot atomically; failed crawls must not call this."""
        with self.tx() as con:
            if con.execute('SELECT 1 FROM curated_queue WHERE company_id=?', (company_id,)).fetchone():
                return
            con.execute('UPDATE contacts SET active=0 WHERE company_id=?', (company_id,))
            con.execute('UPDATE companies SET careers_signal=? WHERE id=?',
                        (int(careers_signal), company_id))
            if eligible is not None:
                con.execute('UPDATE companies SET verified=?,active=?,verification_note=? WHERE id=?',
                            (int(eligible),int(eligible),evidence,company_id))
            if cnpj_basic:
                con.execute('UPDATE companies SET cnpj_basic=? WHERE id=?',(cnpj_basic.upper(),company_id))
            for contact in contacts:
                con.execute('''INSERT INTO contacts(company_id,email,kind,score,source_url,discovered_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(company_id,email) DO UPDATE SET
                    kind=excluded.kind,score=excluded.score,source_url=excluded.source_url,
                    discovered_at=excluded.discovered_at,active=1''',
                    (company_id, contact.email.lower(), contact.kind, contact.score, contact.source_url, iso()))

    def set_careers_signal(self, company_id: int, value: bool) -> None:
        with self.connect() as con:
            con.execute("UPDATE companies SET careers_signal=? WHERE id=?", (1 if value else 0, company_id))

    def upsert_contact(self, company_id: int, email: str, kind: str, score: int, source_url: str) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO contacts(company_id,email,kind,score,source_url,discovered_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(company_id,email) DO UPDATE SET
                  score=MAX(contacts.score,excluded.score),
                  kind=CASE WHEN excluded.score>=contacts.score THEN excluded.kind ELSE contacts.kind END,
                  source_url=CASE WHEN excluded.score>=contacts.score THEN excluded.source_url ELSE contacts.source_url END,
                  active=1
                """,
                (company_id, email.lower(), kind, score, source_url, iso()),
            )

    def add_curated_verified(self, *, name, domain, website, city, state, batch, result, email):
        # Atomically pin a contact from a successful official-site crawl.
        from .crawler import EMAIL_RE, same_domain, score_email
        from .domains import valid_url
        identity = company_domain(domain)
        address = email.strip().lower()
        if not all((name.strip(), city.strip(), state.strip(), batch.strip())):
            raise ValueError('Dados obrigatórios ausentes')
        if not valid_url(website, identity) or not EMAIL_RE.fullmatch(address) or not same_domain(address.rsplit('@', 1)[1], identity):
            raise ValueError('Site ou endereço fora do domínio oficial')
        matches = [ct for ct in result.contacts if ct.email.lower() == address and ct.score > 0 and valid_url(ct.source_url, identity)]
        if not result.eligible or len(matches) != 1:
            raise ValueError('Endereço não confirmado por pesquisa elegível')
        ct = matches[0]
        kind, score = score_email(address, result.careers_signal or not self.contact_requires_careers_signal)
        if not score or ct.kind != kind or ct.score != score:
            raise ValueError('Canal incompatível com as regras de elegibilidade')
        now = iso()
        with self.tx() as con:
            row = con.execute('SELECT id,cnpj_basic FROM companies WHERE identity_key=? ORDER BY id LIMIT 1', (identity,)).fetchone()
            if row:
                cid = int(row['id'])
                if row['cnpj_basic'] and result.cnpj_basic and row['cnpj_basic'].upper() != result.cnpj_basic.upper():
                    raise ValueError('CNPJ divergente do cadastro existente; revisar identidade')
                if con.execute('SELECT 1 FROM curated_queue WHERE company_id=?', (cid,)).fetchone():
                    raise ValueError('Empresa já pertence a uma lista revisada')
                con.execute('''UPDATE companies SET name=?,website=?,city=?,state=?,active=1,verified=1,
                    careers_signal=?,verification_note=?,last_seen_at=?,cnpj_basic=COALESCE(NULLIF(?,''),cnpj_basic)
                    WHERE id=?''', (name.strip(),website,city.strip(),state.strip().upper(),int(result.careers_signal),
                    result.evidence,now,result.cnpj_basic,cid))
            else:
                cid = con.execute('''INSERT INTO companies(name,domain,website,city,state,source,source_query,
                    first_seen_at,last_seen_at,identity_key,active,verified,careers_signal,verification_note,cnpj_basic)
                    VALUES(?,?,?,?,?,'curated',?,?,?,?,1,1,?,?,?)''',
                    (name.strip(),identity,website,city.strip(),state.strip().upper(),batch,now,now,identity,
                     int(result.careers_signal),result.evidence,result.cnpj_basic or None)).lastrowid
            con.execute('''INSERT INTO contacts(company_id,email,kind,score,source_url,discovered_at,active)
                VALUES(?,?,?,?,?,?,1) ON CONFLICT(company_id,email) DO UPDATE SET kind=excluded.kind,
                score=excluded.score,source_url=excluded.source_url,discovered_at=excluded.discovered_at,active=1''',
                (cid,address,ct.kind,ct.score,ct.source_url,now))
            contact_id=con.execute('SELECT id FROM contacts WHERE company_id=? AND email=?', (cid,address)).fetchone()['id']
            con.execute('INSERT INTO curated_queue(company_id,contact_id,batch,source_url,verified_at) VALUES(?,?,?,?,?)',
                        (cid,contact_id,batch.strip(),ct.source_url,now))
            return cid

    def sent_today_count(self, timezone_name: str = 'America/Sao_Paulo') -> int:
        start, end = self._day_bounds(timezone_name)
        with self.connect() as con:
            return int(con.execute(
                "SELECT COUNT(*) n FROM applications WHERE status='sent' AND sent_at>=? AND sent_at<?",
                (start, end)
            ).fetchone()["n"])

    def last_sent_at(self) -> datetime | None:
        with self.connect() as con:
            row = con.execute("SELECT sent_at FROM applications WHERE status='sent' ORDER BY sent_at DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(row["sent_at"]) if row and row["sent_at"] else None

    def _next_sql(self, limit: int = 1) -> str:
        contact_rule = ("AND (ct.kind!='contato' OR c.careers_signal=1)"
                        if self.contact_requires_careers_signal else '')
        states = ','.join(f"'{state}'" for state in self.priority_states) or "''"
        return f"""
        SELECT c.id company_id,c.name company_name,c.domain,c.website,c.city,c.state,
               ct.id contact_id,ct.email,ct.kind,ct.score,
               a.id application_id,a.status application_status,a.attempts
        FROM companies c
        JOIN contacts ct ON ct.company_id=c.id AND ct.active=1
        LEFT JOIN applications a ON a.company_id=c.id
        LEFT JOIN curated_queue cq ON cq.company_id=c.id
        WHERE c.active=1 AND c.verified=1
          AND (cq.company_id IS NOT NULL OR NOT EXISTS (
              SELECT 1 FROM curated_queue pending
              JOIN companies pc ON pc.id=pending.company_id
              LEFT JOIN applications pa ON pa.company_id=pc.id
              WHERE NOT EXISTS (SELECT 1 FROM registry_exclusions WHERE cnpj_basic=pc.cnpj_basic)
              AND NOT EXISTS (SELECT 1 FROM applications done JOIN companies dc ON dc.id=done.company_id
                  WHERE (dc.identity_key=pc.identity_key OR (pc.cnpj_basic IS NOT NULL AND dc.cnpj_basic=pc.cnpj_basic))
                  AND done.status='sent')
              AND (pa.id IS NULL OR pa.status IN ('sending','uncertain') OR
                   (pa.status='failed' AND pa.retryable=1 AND pa.attempts<?1))))
          AND NOT EXISTS (SELECT 1 FROM registry_exclusions WHERE cnpj_basic=c.cnpj_basic)
          {contact_rule}
          AND NOT EXISTS (SELECT 1 FROM applications block JOIN companies bc ON bc.id=block.company_id
                          WHERE (bc.identity_key=c.identity_key OR
                                (c.cnpj_basic IS NOT NULL AND bc.cnpj_basic=c.cnpj_basic))
                            AND block.status IN ('sent','sending','uncertain'))
          AND ct.id=COALESCE(cq.contact_id,(SELECT id FROM contacts x WHERE x.company_id=c.id AND x.active=1
                     ORDER BY x.score DESC,x.id ASC LIMIT 1))
          AND (a.id IS NULL OR (a.status='failed' AND a.retryable=1 AND a.attempts<?1 AND
               (a.next_attempt_at IS NULL OR a.next_attempt_at<=?2)))
        ORDER BY CASE WHEN cq.company_id IS NOT NULL THEN 0 ELSE 1 END,
                 CASE WHEN c.state IN ({states}) THEN 0 ELSE 1 END,
                 COALESCE((SELECT priority FROM cities ci WHERE ci.city=c.city AND ci.state=c.state),0) DESC,
                 ct.score DESC,c.first_seen_at ASC
        LIMIT {int(limit)}
        """

    def preview_next(self, max_attempts: int):
        with self.connect() as con:
            return con.execute(self._next_sql(), (max_attempts, iso())).fetchone()

    def list_targets(self, max_attempts: int, limit: int = 500):
        # The send queue in order: one row per firm, with the contact that would be used.
        with self.connect() as con:
            return [dict(r) for r in con.execute(self._next_sql(limit), (max_attempts, iso()))]

    @staticmethod
    def _day_bounds(timezone_name: str):
        zone = ZoneInfo(timezone_name)
        start = utcnow().astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return iso(start.astimezone(timezone.utc)), iso(end.astimezone(timezone.utc))

    def recover_interrupted(self, *, lock_held: bool = False) -> int:
        if not lock_held:
            with sender_lock(self.path) as acquired:
                return self.recover_interrupted(lock_held=True) if acquired else 0
        with self.tx() as con:
            rows = con.execute("SELECT id FROM applications WHERE status='sending' AND (lease_until IS NULL OR lease_until<=?)", (iso(),)).fetchall()
            for row in rows:
                con.execute("UPDATE applications SET status='uncertain',last_error='Processo interrompido; encerrado sem reenvio automático' WHERE id=?", (row['id'],))
                self._event(con, 'interrupted', row['id'], 'Lease expirou; envio incerto')
            return len(rows)

    def claim_next(self, max_attempts: int, *, max_per_day: int | None = None,
                   interval_seconds: int = 0, lease_seconds: int = 600,
                   timezone_name: str = 'America/Sao_Paulo'):
        with self.tx() as con:
            # Serialize senders, including different companies and command-line invocations.
            if con.execute("SELECT 1 FROM applications WHERE status='sending' LIMIT 1").fetchone():
                return None
            due = con.execute("SELECT value FROM runtime WHERE key='next_send_at'").fetchone()
            if due and due['value'] > iso():
                return None
            last = con.execute("SELECT sent_at FROM applications WHERE status='sent' ORDER BY sent_at DESC LIMIT 1").fetchone()
            if last and last['sent_at'] and (utcnow()-datetime.fromisoformat(last['sent_at'])).total_seconds() < interval_seconds:
                return None
            start, end = self._day_bounds(timezone_name)
            count = con.execute('''SELECT COUNT(*) n FROM applications WHERE
                (status='sent' AND sent_at>=? AND sent_at<?) OR
                (status IN ('sending','uncertain') AND attempted_at>=? AND attempted_at<?)''',
                (start, end, start, end)).fetchone()['n']
            if max_per_day is not None and count >= max_per_day:
                return None
            row = con.execute(self._next_sql(), (max_attempts, iso())).fetchone()
            if not row:
                return None
            if row["application_id"] is None:
                app_id = con.execute(
                    "INSERT INTO applications(company_id,contact_id,email,status,attempts,attempted_at) VALUES(?,?,?,'sending',1,?)",
                    (row["company_id"], row["contact_id"], row["email"], iso()),
                ).lastrowid
            else:
                app_id = row["application_id"]
                con.execute(
                    "UPDATE applications SET status='sending',attempts=attempts+1,attempted_at=?,contact_id=?,email=?,last_error=NULL,message_id=NULL WHERE id=?",
                    (iso(), row["contact_id"], row["email"], app_id),
                )
            con.execute('UPDATE applications SET lease_until=? WHERE id=?',
                        (iso(utcnow() + timedelta(seconds=lease_seconds)), app_id))
            con.execute("INSERT INTO runtime(key,value) VALUES('next_send_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (iso(utcnow() + timedelta(seconds=interval_seconds)),))
            self._event(con, 'claimed', app_id, 'Reserva de envio')
            return dict(row) | {"application_id": app_id}

    @staticmethod
    def _event(con, kind, app_id, detail):
        con.execute('INSERT INTO events(created_at,kind,application_id,detail) VALUES(?,?,?,?)',
                    (iso(), kind, app_id, detail[:1000]))

    def prepare_send(self, app_id: int, subject: str, message_id: str) -> None:
        with self.connect() as con:
            cur = con.execute("UPDATE applications SET subject=?,message_id=? WHERE id=? AND status='sending' AND lease_until>?",
                              (subject, message_id, app_id, iso()))
            if cur.rowcount != 1:
                raise RuntimeError('Reserva de envio expirada')

    def mark_uncertain(self, app_id: int, error: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE applications SET status='uncertain',last_error=?,lease_until=NULL WHERE id=? AND status IN ('sending','uncertain')", (error[:1000], app_id))
            self._event(con, 'uncertain', app_id, error)

    def mark_sent(self, application_id: int, subject: str, message_id: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE applications SET status='sent',subject=?,sent_at=?,message_id=?,next_attempt_at=NULL,last_error=NULL,lease_until=NULL WHERE id=?",
                (subject, iso(), message_id, application_id),
            )
            self._event(con, 'sent', application_id, message_id)
            # Enforce spacing after completion too, even if SMTP took longer than the reservation.
            row = con.execute('SELECT attempted_at FROM applications WHERE id=?', (application_id,)).fetchone()
            due = con.execute("SELECT value FROM runtime WHERE key='next_send_at'").fetchone()
            if row and due:
                interval = max(0, (datetime.fromisoformat(due['value']) - datetime.fromisoformat(row['attempted_at'])).total_seconds())
                con.execute("UPDATE runtime SET value=? WHERE key='next_send_at'", (iso(utcnow()+timedelta(seconds=interval)),))

    def mark_failed(self, application_id: int, error: str, retryable: bool = True) -> None:
        retry = utcnow() + timedelta(hours=24)
        with self.connect() as con:
            con.execute(
                "UPDATE applications SET status='failed',last_error=?,next_attempt_at=?,retryable=?,lease_until=NULL WHERE id=? AND status='sending'",
                (error[:1000], iso(retry), int(retryable), application_id),
            )
            self._event(con, 'failed', application_id, error)

    def reconcile(self, app_id: int, outcome: str, note: str) -> None:
        if outcome not in {'sent', 'not-sent'} or not note.strip():
            raise ValueError('Informe sent/not-sent e evidência da reconciliação')
        with self.tx() as con:
            row = con.execute("SELECT * FROM applications WHERE id=? AND status='uncertain'", (app_id,)).fetchone()
            if not row:
                raise ValueError('Candidatura não está em estado incerto')
            if outcome == 'sent':
                con.execute("UPDATE applications SET status='sent',sent_at=attempted_at,last_error=NULL WHERE id=?", (app_id,))
            else:
                con.execute("UPDATE applications SET status='failed',retryable=1,next_attempt_at=?,last_error=? WHERE id=?", (iso(), note, app_id))
            self._event(con, 'reconciled', app_id, outcome + ': ' + note)

    def list_companies(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT id,name,domain,website,city,state,verified,active,cnpj_basic,verification_note FROM companies ORDER BY id')]

    def list_applications(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM applications ORDER BY id')]

    def list_contacts(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute('''SELECT c.id company_id,c.name company_name,
                c.domain,c.website,c.city,c.state,c.verified,c.cnpj_basic,ct.email,ct.kind,ct.score,
                ct.source_url,ct.discovered_at,ct.active,a.status application_status
                FROM companies c JOIN contacts ct ON ct.company_id=c.id
                LEFT JOIN applications a ON a.company_id=c.id
                ORDER BY c.state,c.city,c.name,ct.score DESC''')]

    def list_cities(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM cities ORDER BY priority DESC,state,city')]

    def recent_events(self, limit: int = 50):
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))]

    def next_registry_company(self):
        with self.connect() as con:
            return con.execute('''SELECT * FROM registry_establishments WHERE company_id IS NULL
                AND (trade_name!='' OR legal_name!='')
                AND (next_lookup_at IS NULL OR next_lookup_at<=?)
                ORDER BY COALESCE(next_lookup_at,''),cnpj LIMIT 1''',(iso(),)).fetchone()

    def mark_registry_lookup(self, cnpj: str, company_id: int | None, error: str = ''):
        with self.tx() as con:
            con.execute('UPDATE registry_establishments SET company_id=?,next_lookup_at=?,last_error=? WHERE cnpj=?',
                        (company_id,iso(utcnow()+timedelta(days=7)),error[:1000],cnpj))
            if company_id is not None:
                con.execute('''UPDATE companies SET cnpj_basic=? WHERE id=? AND NOT EXISTS (SELECT 1 FROM curated_queue WHERE company_id=companies.id)''',(cnpj[:8],company_id))

    def list_registry(self, limit: int = 100):
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM registry_establishments ORDER BY cnpj LIMIT ?',(limit,))]

    def stats(self) -> dict[str, int]:
        with self.connect() as con:
            return {
                "cities": int(con.execute("SELECT COUNT(*) n FROM cities WHERE active=1").fetchone()["n"]),
                "companies": int(con.execute("SELECT COUNT(*) n FROM companies WHERE active=1").fetchone()["n"]),
                "contacts": int(con.execute("SELECT COUNT(*) n FROM contacts WHERE active=1").fetchone()["n"]),
                "sent": int(con.execute("SELECT COUNT(*) n FROM applications WHERE status='sent'").fetchone()["n"]),
                "failed": int(con.execute("SELECT COUNT(*) n FROM applications WHERE status='failed'").fetchone()["n"]),
                "sending": int(con.execute("SELECT COUNT(*) n FROM applications WHERE status='sending'").fetchone()["n"]),
                "uncertain": int(con.execute("SELECT COUNT(*) n FROM applications WHERE status='uncertain'").fetchone()["n"]),
                "rejected": int(con.execute("SELECT COUNT(*) n FROM companies WHERE active=0").fetchone()["n"]),
                "pending": int(con.execute("SELECT COUNT(*) n FROM companies c WHERE c.active=1 AND c.verified=1 AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.company_id=c.id)").fetchone()["n"]),
                "registry_establishments": int(con.execute('SELECT COUNT(*) n FROM registry_establishments').fetchone()['n']),
            }

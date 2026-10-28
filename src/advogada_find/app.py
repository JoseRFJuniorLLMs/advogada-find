from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import Settings
from .crawler import WebsiteCrawler
from .db import Database
from .discovery import SearchDiscovery, DiscoveryError
from .emailer import Mailer, SendRejected, SendUncertain
from .locking import sender_lock
from .catalog import import_cities

log = logging.getLogger('advogada_find')


class AdvogadaFind:
    def __init__(self, settings: Settings):
        self.s = settings
        settings.ensure_runtime_dirs()
        self.db = Database(settings.db_path,
                           contact_requires_careers_signal=settings.contact_requires_careers_signal,
                           priority_states=settings.priority_states)
        self.db.init()
        self.db.seed_cities(settings.cities_file)
        self.discovery = SearchDiscovery(settings)

    def scan_city(self, city: str, state: str) -> dict[str, int]:
        discovery_error = None
        try:
            candidates = self.discovery.discover(city, state)
        except DiscoveryError as exc:
            candidates = exc.candidates
            discovery_error = exc
        crawler = WebsiteCrawler(self.s)
        companies = contacts = failures = 0
        try:
            for candidate in candidates:
                cid = self.db.upsert_company(candidate.name, candidate.domain, candidate.website,
                                             city, state, candidate.source, candidate.source_query)
                companies += 1
                try:
                    result = crawler.crawl(candidate.website, candidate.domain, city=city)
                except Exception as exc:
                    failures += 1
                    log.warning('crawl_failed domain=%s error=%s', candidate.domain, type(exc).__name__)
                    continue
                if result.name:
                    self.db.rename_company(cid, result.name)
                evidence = result.evidence + f'; motivo={result.rejection_reason}'
                self.db.replace_contacts(cid, result.contacts, result.careers_signal,
                                         eligible=result.eligible, evidence=evidence,cnpj_basic=result.cnpj_basic)
                contacts += len(result.contacts)
                log.info('company_classified domain=%s eligible=%s reason=%s',
                         candidate.domain,result.eligible,result.rejection_reason or 'rules_passed')
        finally:
            crawler.close()
        if failures:
            raise RuntimeError(f'{failures} websites falharam; varredura incompleta')
        if discovery_error:
            raise discovery_error
        return {'companies': companies, 'contacts': contacts}

    def scan_next_due_city(self) -> bool:
        row = self.db.next_due_city()
        if not row:
            return False
        try:
            result = self.scan_city(row['city'], row['state'])
            self.db.mark_city_scanned(row['id'], self.s.city_rescan_days)
            log.info('scan_done city=%s companies=%s contacts=%s', row['city'], result['companies'], result['contacts'])
        except Exception as exc:
            self.db.mark_city_retry(row['id'], 6, str(exc))
            log.warning('scan_error city=%s error=%s', row['city'], type(exc).__name__)
        return True

    def scan_registry_company(self) -> bool:
        row = self.db.next_registry_company()
        if not row:
            return False
        crawler = WebsiteCrawler(self.s)
        matched = None
        try:
            candidates = self.discovery.discover_registered(row['trade_name'] or row['legal_name'],
                row['city'],row['state'],row['cnpj'])
            for candidate in candidates:
                try:
                    result = crawler.crawl(candidate.website,candidate.domain,city=row['city'],registry_cnpj=row['cnpj'])
                except Exception as exc:
                    log.info('registry_site_rejected reason=%s',type(exc).__name__)
                    continue
                if not result.eligible:
                    continue
                cid = self.db.upsert_company(candidate.name,candidate.domain,candidate.website,
                    row['city'],row['state'],candidate.source,candidate.source_query)
                self.db.mark_registry_lookup(row['cnpj'],cid)
                self.db.replace_contacts(cid,result.contacts,result.careers_signal,
                                         eligible=True,evidence=result.evidence,cnpj_basic=result.cnpj_basic)
                matched = cid
                break
            if matched is None:
                self.db.mark_registry_lookup(row['cnpj'],None,'Site oficial/canal elegível não identificado; retentar em 7 dias')
        except Exception as exc:
            self.db.mark_registry_lookup(row['cnpj'],None,type(exc).__name__)
            log.warning('registry_lookup_failed error=%s',type(exc).__name__)
        finally:
            crawler.close()
        return True

    def _inside_window(self) -> bool:
        now = datetime.now(ZoneInfo(self.s.timezone))
        return (now.weekday() in self.s.send_weekdays and
                time.fromisoformat(self.s.send_window_start) <= now.time() <=
                time.fromisoformat(self.s.send_window_end))

    def doctor(self, smtp_check: bool = False) -> dict:
        result = {'dry_run': self.s.dry_run, 'from_email': self.s.from_email,
                  'errors': [], 'smtp_authenticated': False}
        mailer = Mailer(self.s)
        try:
            mailer.validate(require_smtp=smtp_check or not self.s.dry_run)
            result['message_valid'] = True
            if smtp_check:
                smtp = mailer.open()
                try:
                    result['smtp_authenticated'] = True
                finally:
                    mailer.close(smtp)
        except Exception as exc:
            result['errors'].append(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__)
        result['ok'] = not result['errors']
        return result

    def describe(self, message) -> dict:
        attachment = next(message.iter_attachments(), None)
        return {'dry_run': self.s.dry_run, 'from': str(message['From']), 'to': str(message['To']),
                'cc': str(message['Cc']), 'reply_to': str(message['Reply-To']), 'subject': str(message['Subject']),
                'body': message.get_body().get_content(),
                'cv_filename': attachment.get_filename() if attachment else None}

    def preview(self, company: str, to_email: str) -> dict:
        # Renders exactly what would be sent, without the database or SMTP.
        mailer = Mailer(self.s)
        mailer.validate(require_smtp=False)
        return self.describe(mailer.build(to_email, company))

    def send_once(self):
        with sender_lock(self.s.db_path) as acquired:
            if not acquired:
                return {'skipped': 'sender_busy'}
            return self._send_once_locked()

    def _send_once_locked(self):
        self.db.recover_interrupted(lock_held=True)
        if not self._inside_window():
            return {'skipped': 'outside_window'}
        row = self.db.preview_next(self.s.max_send_attempts)
        if not row:
            return None
        mailer = Mailer(self.s)
        try:
            mailer.validate(require_smtp=not self.s.dry_run)
            preview = mailer.build(row['email'], row['company_name'])
        except Exception as exc:
            return {'skipped': 'configuration_error', 'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
        if self.s.dry_run:
            return dict(row) | self.describe(preview)
        # Authenticate before claiming: global configuration failures never burn company attempts.
        try:
            smtp = mailer.open()
        except RuntimeError as exc:
            return {'skipped': 'smtp_connection_error', 'error': str(exc)}
        try:
            # SMTP authentication can cross the end of the configured sending window.
            if not self._inside_window():
                return {'skipped': 'outside_window'}
            row = self.db.claim_next(self.s.max_send_attempts,
                max_per_day=self.s.max_emails_per_day,
                interval_seconds=self.s.min_send_interval_seconds + random.randint(0, self.s.max_send_jitter_seconds),
                lease_seconds=self.s.send_lease_seconds, timezone_name=self.s.timezone)
            if not row:
                return {'skipped': 'limit_interval_or_busy'}
            app_id = row['application_id']
            try:
                message = mailer.build(row['email'], row['company_name'])
                self.db.prepare_send(app_id, str(message['Subject']), str(message['Message-ID']))
            except Exception as exc:
                self.db.mark_failed(app_id, f'Preparação falhou: {type(exc).__name__}', retryable=False)
                return dict(row) | {'sent': False, 'error': 'message_preparation_failed'}
            try:
                message_id, subject = mailer.deliver(smtp, message)
            except SendRejected as exc:
                self.db.mark_failed(app_id, str(exc), exc.retryable)
                return dict(row) | {'sent': False, 'error': str(exc), 'retryable': exc.retryable}
            except SendUncertain as exc:
                self.db.mark_uncertain(app_id, str(exc))
                return dict(row) | {'sent': False, 'uncertain': True, 'error': str(exc)}
            # Acceptance and persistence errors are separate paths; never mark accepted mail failed.
            try:
                self.db.mark_sent(app_id, subject, message_id)
            except Exception as exc:
                try:
                    self.db.mark_uncertain(app_id, f'SMTP aceitou; persistência falhou: {type(exc).__name__}')
                except Exception:
                    log.error('send_persistence_failed application_id=%s message_id=%s', app_id, message_id)
                return dict(row) | {'smtp_accepted': True, 'uncertain': True, 'message_id': message_id}
            log.info('send_done application_id=%s message_id=%s', app_id, message_id)
            return dict(row) | {'sent': True, 'message_id': message_id}
        finally:
            mailer.close(smtp)

    def _scan_loop(self, stop: threading.Event):
        while not stop.is_set():
            try:
                if self.s.cities_scope != 'configured':
                    with self.db.connect() as con:
                        loaded = con.execute('SELECT 1 FROM runtime WHERE key=?',
                                             ('cities_scope_'+self.s.cities_scope,)).fetchone()
                    if not loaded:
                        try:
                            count = import_cities(self.db,None if self.s.cities_scope=='BR' else self.s.cities_scope,
                                                  self.s.http_timeout_seconds)
                            log.info('city_coverage_loaded scope=%s count=%s',self.s.cities_scope,count)
                        except Exception as exc:
                            log.warning('city_coverage_retry error=%s',type(exc).__name__)
                self.scan_next_due_city()
                self.scan_registry_company()
            except Exception as exc:
                log.error('scan_worker_error error=%s', type(exc).__name__)
            stop.wait(self.s.loop_seconds)

    def daemon(self) -> None:
        log.info('daemon_start dry_run=%s db=%s', self.s.dry_run, self.s.db_path)
        stop = threading.Event()
        worker = threading.Thread(target=self._scan_loop, args=(stop,), daemon=True, name='discovery')
        worker.start()
        try:
            while not stop.is_set():
                try:
                    result = self.send_once()
                    if result and result.get('skipped'):
                        log.info('send_skipped reason=%s', result['skipped'])
                except Exception as exc:
                    log.error('sender_error error=%s', type(exc).__name__)
                stop.wait(self.s.loop_seconds)
        except KeyboardInterrupt:
            log.info('daemon_stop')
        finally:
            stop.set()
            worker.join(timeout=5)

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in {'1', 'true', 'yes', 'on'}:
        return True
    if value in {'0', 'false', 'no', 'off'}:
        return False
    raise ValueError(f'{name}: use true ou false')


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value is not None else default


def env(name, default, cast=str):
    return field(default_factory=lambda: cast(os.getenv(name, default)))


def boolean(name, default):
    return field(default_factory=lambda: _bool(name, default))


@dataclass(frozen=True)
class Settings:
    db_path: Path = env('ADVOGADA_FIND_DB', './data/advogada-find.db', Path)
    cities_file: Path = env('CITIES_FILE', str(ROOT / 'config/cities.csv'), Path)
    # A lawyer registered in one OAB section works in a region, not nationwide.
    cities_scope: str = env('CITIES_SCOPE', 'BR')
    # Markets searched and contacted first; the rest of Brazil follows.
    priority_states: tuple[str, ...] = env('PRIORITY_STATES', 'DF,RJ,SP',
        lambda value: tuple(x.strip().upper() for x in value.split(',') if x.strip()))
    dry_run: bool = boolean('DRY_RUN', True)
    timezone: str = env('TIMEZONE', 'America/Sao_Paulo')
    loop_seconds: int = env('LOOP_SECONDS', 300, int)
    city_rescan_days: int = env('CITY_RESCAN_DAYS', 7, int)
    max_results_per_query: int = env('MAX_RESULTS_PER_QUERY', 20, int)
    max_pages_per_site: int = env('MAX_PAGES_PER_SITE', 10, int)
    http_timeout_seconds: int = env('HTTP_TIMEOUT_SECONDS', 15, int)
    crawl_delay_seconds: int = env('CRAWL_DELAY_SECONDS', 2, int)
    user_agent: str = env('USER_AGENT', 'advogada-find/0.1')
    brave_search_api_key: str = env('BRAVE_SEARCH_API_KEY', '')
    max_emails_per_day: int = env('MAX_EMAILS_PER_DAY', 5, int)
    min_send_interval_seconds: int = env('MIN_SEND_INTERVAL_SECONDS', 1200, int)
    max_send_jitter_seconds: int = env('MAX_SEND_JITTER_SECONDS', 1800, int)
    send_window_start: str = env('SEND_WINDOW_START', '08:30')
    send_window_end: str = env('SEND_WINDOW_END', '17:30')
    send_weekdays: tuple[int, ...] = env('SEND_WEEKDAYS', '0,1,2,3,4',
        lambda value: tuple(int(x.strip()) for x in value.split(',') if x.strip()))
    max_send_attempts: int = env('MAX_SEND_ATTEMPTS', 3, int)
    send_lease_seconds: int = env('SEND_LEASE_SECONDS', 600, int)
    smtp_host: str = env('SMTP_HOST', 'smtp.gmail.com')
    smtp_port: int = env('SMTP_PORT', 587, int)
    smtp_user: str = env('SMTP_USER', '')
    smtp_password: str = env('SMTP_PASSWORD', '')
    smtp_starttls: bool = boolean('SMTP_STARTTLS', True)
    smtp_ssl: bool = boolean('SMTP_SSL', False)
    from_email: str = env('FROM_EMAIL', '')
    from_name: str = env('FROM_NAME', '')
    applicant_name: str = env('APPLICANT_NAME', '')
    applicant_role: str = env('APPLICANT_ROLE', 'Advogada')
    applicant_oab: str = env('APPLICANT_OAB', '')
    applicant_city: str = env('APPLICANT_CITY', '')
    applicant_phone: str = env('APPLICANT_PHONE', '')
    applicant_email: str = env('APPLICANT_EMAIL', '')
    linkedin_url: str = env('LINKEDIN_URL', '')
    # Mandatory in every message body (validated before any send).
    site_url: str = env('SITE_URL', 'https://audiencia-df.web.app/')
    cv_path: Path = env('CV_PATH', '/opt/advogada-find-private/cv.pdf', Path)
    cv_filename: str = env('CV_FILENAME', '')
    email_template: Path = env('EMAIL_TEMPLATE', str(ROOT / 'templates/application.txt'), Path)
    email_subject: str = env('EMAIL_SUBJECT', 'Candidatura | {role} | {oab} | {name}')
    # false: a law firm's generic office address (contato@, escritorio@) is a valid channel.
    contact_requires_careers_signal: bool = boolean('CONTACT_REQUIRES_CAREERS_SIGNAL', False)

    def __post_init__(self):
        from .catalog import STATES
        if self.cities_scope not in STATES | {'BR','configured'}:
            raise ValueError('CITIES_SCOPE: use BR, uma UF ou configured')
        if any(state not in STATES for state in self.priority_states):
            raise ValueError('PRIORITY_STATES: use UFs separadas por vírgula')
        ZoneInfo(self.timezone)
        start = time.fromisoformat(self.send_window_start)
        end = time.fromisoformat(self.send_window_end)
        if start >= end:
            raise ValueError('Janela de envio deve começar antes de terminar, no mesmo dia')
        if not self.send_weekdays or any(x not in range(7) for x in self.send_weekdays):
            raise ValueError('SEND_WEEKDAYS: use dias de 0 a 6')
        for name in ('loop_seconds', 'city_rescan_days', 'max_results_per_query',
                     'max_pages_per_site', 'http_timeout_seconds', 'max_emails_per_day',
                     'max_send_attempts', 'send_lease_seconds'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} deve ser positivo')
        for name in ('crawl_delay_seconds', 'min_send_interval_seconds', 'max_send_jitter_seconds'):
            if getattr(self, name) < 0:
                raise ValueError(f'{name} não pode ser negativo')
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError('SMTP_PORT inválida')
        if not self.smtp_ssl and not self.smtp_starttls:
            raise ValueError('SMTP exige SSL ou STARTTLS')
        if not self.site_url.startswith('https://'):
            raise ValueError('SITE_URL deve ser um endereço https://')

    def ensure_runtime_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

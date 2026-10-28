from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import Settings
from .crawler import EMAIL_RE
from .names import firm_name


class SendRejected(RuntimeError):
    def __init__(self, code: int):
        super().__init__(f'SMTP recusou a mensagem: código {code}')
        self.retryable = 400 <= code < 500


class SendUncertain(RuntimeError):
    pass


class Mailer:
    def __init__(self, settings: Settings):
        self.s = settings

    def validate(self, require_smtp: bool = True) -> None:
        required = {'FROM_EMAIL': self.s.from_email, 'APPLICANT_NAME': self.s.applicant_name,
                    'APPLICANT_EMAIL': self.s.applicant_email, 'SITE_URL': self.s.site_url}
        if require_smtp:
            # Dry-run previews never authenticate, so they do not need the app password.
            required |= {'SMTP_HOST': self.s.smtp_host, 'SMTP_USER': self.s.smtp_user,
                         'SMTP_PASSWORD': self.s.smtp_password}
        missing = [key for key, value in required.items() if not value.strip()]
        if missing:
            raise ValueError('Configuração ausente: ' + ', '.join(missing))
        for key, address in [('FROM_EMAIL', self.s.from_email), ('APPLICANT_EMAIL', self.s.applicant_email)]:
            if not EMAIL_RE.fullmatch(address):
                raise ValueError(f'{key}: endereço inválido')
        if not self.s.cv_path.is_file():
            raise ValueError('CV_PATH não aponta para um arquivo legível')
        if not self.s.cv_path.read_bytes().startswith(b'%PDF-'):
            raise ValueError('CV_PATH deve conter um PDF')
        if not self.s.email_template.is_file():
            raise ValueError('EMAIL_TEMPLATE não aponta para um arquivo')
        # Validate every placeholder and header before claiming a company.
        self.build('rh@example.com', 'Empresa de teste')

    def render(self, company: str) -> tuple[str, str]:
        company = ' '.join(company.split())
        subject = self.s.email_subject.format(role=self.s.applicant_role, oab=self.s.applicant_oab,
                                             name=self.s.applicant_name, company=company)
        if '\r' in subject or '\n' in subject:
            raise ValueError('Assunto não pode conter quebras de linha')
        # A page label such as "Trabalhe conosco" must never become the addressee.
        firm = firm_name(company)
        greeting = f'Prezados(as) da equipe {firm}' if firm else 'Prezados(as)'
        body = self.s.email_template.read_text(encoding='utf-8').format(
            company=company, greeting=greeting, name=self.s.applicant_name, role=self.s.applicant_role,
            oab=self.s.applicant_oab, city=self.s.applicant_city, phone=self.s.applicant_phone,
            email=self.s.applicant_email, linkedin=self.s.linkedin_url, site=self.s.site_url)
        if self.s.site_url not in body:
            raise ValueError('EMAIL_TEMPLATE precisa incluir {site} no corpo da mensagem')
        return subject, body

    def build(self, to_email: str, company: str) -> EmailMessage:
        if not EMAIL_RE.fullmatch(to_email):
            raise ValueError('Destinatário inválido')
        subject, body = self.render(company)
        message = EmailMessage()
        message['Subject'] = subject
        message['From'] = formataddr((self.s.from_name or self.s.applicant_name, self.s.from_email))
        message['To'] = to_email
        message['Cc'] = self.s.applicant_email
        message['Reply-To'] = self.s.applicant_email
        message['Message-ID'] = make_msgid(domain=self.s.from_email.rsplit('@', 1)[-1])
        message.set_content(body)
        message.add_attachment(self.s.cv_path.read_bytes(), maintype='application', subtype='pdf',
                               filename=self.s.cv_filename or self.s.cv_path.name)
        return message

    def open(self):
        context = ssl.create_default_context()
        smtp = None
        try:
            if self.s.smtp_ssl:
                smtp = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=30, context=context)
            else:
                smtp = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30)
            smtp.ehlo()
            if not self.s.smtp_ssl:
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(self.s.smtp_user, self.s.smtp_password)
            return smtp
        except Exception as exc:
            if smtp is not None:
                self.close(smtp)
            # No provider text/credentials in CLI output or persisted error details.
            raise RuntimeError(f'Conexão/autenticação SMTP falhou: {type(exc).__name__}') from exc

    @staticmethod
    def close(smtp):
        try:
            smtp.quit()
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass

    def deliver(self, smtp, message: EmailMessage) -> tuple[str, str]:
        try:
            refused = smtp.send_message(message)
            if refused:
                # send_message returns refusals only after at least one recipient was accepted.
                # Retrying could duplicate a message delivered to another recipient.
                raise SendUncertain('SMTP aceitou parcialmente; revisar destinatários antes de reenviar')
        except smtplib.SMTPRecipientsRefused as exc:
            raise SendRejected(next(iter(exc.recipients.values()))[0]) from exc
        except smtplib.SMTPResponseException as exc:
            raise SendRejected(exc.smtp_code) from exc
        except (SendRejected, SendUncertain):
            raise
        except Exception as exc:
            raise SendUncertain(f'Resultado SMTP incerto: {type(exc).__name__}') from exc
        return str(message['Message-ID']), str(message['Subject'])

    def send(self, to_email: str, company: str) -> tuple[str, str]:
        self.validate()
        message = self.build(to_email, company)
        smtp = self.open()
        try:
            return self.deliver(smtp, message)
        finally:
            self.close(smtp)

import smtplib
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from advogada_find.db import iso, utcnow
from advogada_find.emailer import Mailer
from conftest import seed


@pytest.fixture
def smtp(monkeypatch):
    client=Mock()
    client.send_message.return_value={}
    monkeypatch.setattr(Mailer,'open',lambda self: client)
    return client


def test_prepared_message_matches_persisted_id(make_app,smtp):
    app=make_app(); seed(app.db)
    def check(message):
        row=app.db.list_applications()[0]
        assert row['status']=='sending'
        assert row['message_id']==message['Message-ID']
        assert message['From'].endswith('<web2ajax@gmail.com>')
        assert list(message.iter_attachments())[0].get_content_type()=='application/pdf'
        return {}
    smtp.send_message.side_effect=check
    assert app.send_once()['sent']
    assert app.send_once() is None
    assert smtp.send_message.call_count==1


def test_accepted_mail_with_db_error_never_retried(make_app,smtp,monkeypatch):
    app=make_app(); seed(app.db)
    monkeypatch.setattr(app.db,'mark_sent',Mock(side_effect=RuntimeError('database unavailable')))
    result=app.send_once()
    assert result['smtp_accepted'] and result['uncertain']
    assert app.db.list_applications()[0]['status']=='uncertain'
    assert app.send_once() is None
    assert smtp.send_message.call_count==1


def test_both_persistence_operations_fail_remains_blocked(make_app,smtp,monkeypatch):
    app=make_app(); seed(app.db)
    monkeypatch.setattr(app.db,'mark_sent',Mock(side_effect=RuntimeError()))
    monkeypatch.setattr(app.db,'mark_uncertain',Mock(side_effect=RuntimeError()))
    assert app.send_once()['uncertain']
    assert app.send_once() is None
    with app.db.connect() as con:
        con.execute('UPDATE applications SET lease_until=?',(iso(utcnow()-timedelta(seconds=1)),))
    assert app.send_once() is None
    assert app.db.stats()['uncertain']==1
    assert smtp.send_message.call_count==1


def test_disconnect_during_send_is_uncertain(make_app,smtp):
    app=make_app(); seed(app.db)
    smtp.send_message.side_effect=smtplib.SMTPServerDisconnected('lost acknowledgement')
    assert app.send_once()['uncertain']
    assert app.send_once() is None


@pytest.mark.parametrize('code,retryable',[(450,True),(550,False)])
def test_definite_rejection_classification(make_app,smtp,code,retryable):
    app=make_app(); seed(app.db)
    smtp.send_message.side_effect=smtplib.SMTPRecipientsRefused({'rh@acme.com.br':(code,b'rejected')})
    result=app.send_once()
    assert result['retryable'] is retryable
    assert app.db.list_applications()[0]['status']=='failed'
    assert app.db.list_applications()[0]['retryable']==int(retryable)


@pytest.mark.parametrize('change',[dict(smtp_password=''),dict(email_subject='{unknown}'),
                                  dict(applicant_name='')])
def test_bad_configuration_does_not_consume_attempts(make_app,smtp,change):
    app=make_app(**change); seed(app.db)
    assert app.send_once()['skipped']=='configuration_error'
    assert app.db.list_applications()==[]
    smtp.send_message.assert_not_called()


def test_bad_cv_does_not_consume_attempts(make_app,smtp):
    app=make_app(); seed(app.db)
    app.s.cv_path.write_bytes(b'not a PDF')
    assert app.send_once()['skipped']=='configuration_error'
    assert app.db.list_applications()==[]


def test_auth_failure_does_not_consume_attempts(make_app,monkeypatch):
    app=make_app(); seed(app.db)
    monkeypatch.setattr(Mailer,'open',Mock(side_effect=RuntimeError('authentication failed')))
    assert app.send_once()['skipped']=='smtp_connection_error'
    assert app.db.list_applications()==[]


def test_dry_run_renders_without_smtp(make_app,smtp):
    app=make_app(dry_run=True); seed(app.db)
    result=app.send_once()
    assert result['dry_run'] and 'Olá Acme' in result['body']
    assert result['from'].endswith('<web2ajax@gmail.com>')
    assert 'https://audiencia-df.web.app/' in result['body'] and result['cv_filename']=='cv.pdf'
    assert app.db.list_applications()==[]
    smtp.send_message.assert_not_called()


def test_dry_run_does_not_need_smtp_password(make_app,smtp):
    app=make_app(dry_run=True,smtp_password=''); seed(app.db)
    assert app.send_once()['dry_run']
    assert app.doctor()['ok']


def test_template_without_site_link_is_rejected(make_app,settings,smtp):
    settings.email_template.write_text('Olá {company}, {name}.',encoding='utf-8')
    app=make_app(); seed(app.db)
    assert app.send_once()['skipped']=='configuration_error'
    assert app.db.list_applications()==[]
    smtp.send_message.assert_not_called()


def test_preview_renders_subject_and_attachment(make_app):
    app=make_app(dry_run=True,smtp_password='',applicant_oab='OAB/DF nº 73.471',
                 cv_filename='Curriculo.pdf',email_subject='Candidatura | {oab} | {name}')
    result=app.preview('Silva Advogados','contato@silva.adv.br')
    assert result['subject']=='Candidatura | OAB/DF nº 73.471 | Test Applicant'
    assert result['to']=='contato@silva.adv.br' and result['cv_filename']=='Curriculo.pdf'
    assert result['cc']=='web2ajax@gmail.com' and result['reply_to']=='web2ajax@gmail.com'
    assert 'Silva Advogados' in result['body'] and 'audiencia-df.web.app' in result['body']


def test_page_label_is_never_the_addressee(make_app,settings):
    settings.email_template.write_text('{greeting}, {site}',encoding='utf-8')
    app=make_app(dry_run=True)
    assert app.preview('Trabalhe conosco','x@silva.adv.br')['body'].startswith('Prezados(as), https://')
    assert app.preview('Silva Advogados','x@silva.adv.br')['body'].startswith('Prezados(as) da equipe Silva Advogados,')
    assert app.preview('Silva Advogados – Advocacia Criminal em Brasília','x@silva.adv.br')['body'].startswith(
        'Prezados(as) da equipe Silva Advogados,')


def test_concurrent_sender_is_blocked(make_app,smtp):
    first=make_app(max_emails_per_day=1); seed(first.db,'one.com.br'); seed(first.db,'two.com.br')
    second=make_app(max_emails_per_day=1)
    def send(message):
        assert second.send_once()['skipped']=='sender_busy'
        return {}
    smtp.send_message.side_effect=send
    assert first.send_once()['sent']
    assert smtp.send_message.call_count==1
    assert second.send_once()['skipped']=='limit_interval_or_busy'


def test_doctor_smtp_never_sends(make_app,smtp):
    app=make_app()
    assert app.doctor(smtp_check=True)['smtp_authenticated']
    assert app.db.list_applications()==[]
    smtp.send_message.assert_not_called()


def test_tls_verification_enabled(settings,monkeypatch):
    client=Mock()
    monkeypatch.setattr(smtplib,'SMTP',Mock(return_value=client))
    mailer=Mailer(settings); mailer.open()
    context=client.starttls.call_args.kwargs['context']
    assert context.check_hostname
    assert context.verify_mode==2


def test_close_failure_does_not_change_accepted_result(make_app,smtp):
    app=make_app(); seed(app.db)
    smtp.quit.side_effect=OSError('connection closed')
    assert app.send_once()['sent']
    assert app.db.stats()['sent']==1


def test_cc_is_in_smtp_envelope_and_reply_to_is_preserved(settings):
    mailer = Mailer(settings)
    message = mailer.build('rh@escritorio.adv.br', 'Escritório')
    assert message['To'] == 'rh@escritorio.adv.br'
    assert message['Cc'] == settings.applicant_email
    assert message['Reply-To'] == settings.applicant_email
    # Exercise smtplib's real envelope derivation while intercepting network I/O.
    client = smtplib.SMTP.__new__(smtplib.SMTP)
    client.ehlo_or_helo_if_needed = Mock()
    client.sendmail = Mock(return_value={})
    mailer.deliver(client, message)
    assert client.sendmail.call_args.args[1] == ['rh@escritorio.adv.br', settings.applicant_email]


@pytest.mark.parametrize('refused', ['rh@acme.com.br', 'web2ajax@gmail.com'])
def test_partial_smtp_refusal_is_uncertain_and_not_retried(make_app, smtp, refused):
    app=make_app(); seed(app.db)
    smtp.send_message.return_value={refused: (450, b'rejected')}
    result=app.send_once()
    assert result['uncertain']
    assert app.db.list_applications()[0]['status']=='uncertain'
    assert app.send_once() is None
    assert smtp.send_message.call_count==1

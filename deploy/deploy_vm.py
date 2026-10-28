"""Run as root after transferring code/CV/env to a private upload directory."""
from __future__ import annotations

import argparse
import datetime
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import urllib.request


def run(args):
    subprocess.run(args,check=True,cwd='/opt/advogada-find')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--upload-dir',type=Path,required=True)
    parser.add_argument('--resume',action='store_true',help='Retomar somente uma instalação deste pacote')
    parser.add_argument('--allow-send',action='store_true',help='Aceitar env com envio real (padrão: só modo de teste)')
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Execute como root')
    upload=args.upload_dir.resolve()
    app=Path('/opt/advogada-find')
    env=Path('/etc/advogada-find.env')
    cv=Path('/opt/advogada-find-private/cv.pdf')
    snippet=Path('/etc/nginx/snippets/nginx-https.conf')
    if (app.exists() or env.exists() or cv.exists()) and not args.resume:
        raise RuntimeError('Instalação existente; preservar antes de atualizar')
    if args.resume:
        with tarfile.open(upload/'advogada-find-release.tgz') as archive:
            for member in archive.getmembers():
                if not member.isfile():continue
                dest=(app/member.name).resolve()
                if not dest.is_relative_to(app) or not dest.is_file() or dest.read_bytes()!=archive.extractfile(member).read():
                    raise RuntimeError('Instalação mudou; não retomar automaticamente')
    if not snippet.is_file():raise RuntimeError('Snippet HTTPS existente não encontrado')
    for name in ('advogada-find-release.tgz','advogada-find.env','cv.pdf','advogada-find-seed.db'):
        if not (upload/name).is_file():raise RuntimeError('Artefato de deploy ausente: '+name)
    values=dict(line.split('=',1) for line in (upload/'advogada-find.env').read_text(encoding='utf-8').splitlines()
                if '=' in line and not line.lstrip().startswith('#'))
    test_mode=values.get('DRY_RUN','').strip().lower()=='true' and not values.get('SMTP_PASSWORD','').strip()
    if not test_mode and not args.allow_send:
        raise RuntimeError('Env permite envio real; use DRY_RUN=true sem SMTP_PASSWORD ou --allow-send')
    backup=Path('/var/backups/advogada-find')/datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True,mode=0o700)
    shutil.copy2(snippet,backup/'nginx-https.conf')
    if not args.resume:
        app.mkdir(mode=0o755)
        with tarfile.open(upload/'advogada-find-release.tgz') as archive:
            for member in archive.getmembers():
                if not (app/member.name).resolve().is_relative_to(app):raise RuntimeError('Caminho inválido no pacote')
            archive.extractall(app,filter='data')
    run(['bash',str(app/'deploy/install.sh')])
    py=str(app/'.venv/bin/python')
    run([py,'-m','pip','install','-e',str(app)+'[dev]'])
    run(['install','-o','root','-g','advfind','-m','0640',str(upload/'advogada-find.env'),str(env)])
    run(['install','-o','advfind','-g','advfind','-m','0600',str(upload/'cv.pdf'),str(cv)])
    database=Path('/var/lib/advogada-find/advogada-find.db')
    if not database.exists():
        run(['install','-o','advfind','-g','advfind','-m','0600',str(upload/'advogada-find-seed.db'),str(database)])
    run(['runuser','-u','advfind','--',py,'-m','pytest','-q','-p','no:cacheprovider',
         '--basetemp','/var/lib/advogada-find/deploy-testtmp',str(app/'tests')])
    # Test mode never authenticates to SMTP: there is no password to try.
    run(['runuser','-u','advfind','--',py,'-m','advogada_find','--env-file',str(env),'doctor']
        +([] if test_mode else ['--smtp']))
    location=Path('/etc/nginx/snippets/advogada-find-https.conf')
    location.write_text('''location = /advogada-find { return 301 /advogada-find/; }
location /advogada-find/ {
    proxy_pass http://127.0.0.1:8797/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 30s;
}
''',encoding='utf-8')
    include='include /etc/nginx/snippets/advogada-find-https.conf;'
    original=snippet.read_text()
    if include not in original:snippet.write_text(original.rstrip()+'\n'+include+'\n')
    try:
        run(['nginx','-t'])
    except Exception:
        shutil.copy2(backup/'nginx-https.conf',snippet)
        raise
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','--now','advogada-find-dashboard.service'])
    run(['systemctl','reload','nginx'])
    for attempt in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8797/health',timeout=2) as response:
                if response.status==200:break
        except OSError:
            if attempt==19:raise
            time.sleep(0.5)
    else:raise RuntimeError('Painel sem health')
    run(['systemctl','enable','--now','advogada-find.service'])
    run(['systemctl','is-active','advogada-find.service','advogada-find-dashboard.service'])
    # Remove only private upload copies after installing them at their final secure paths.
    for name in ('advogada-find.env','cv.pdf','advogada-find-seed.db'):
        path=upload/name
        if path.resolve().parent!=upload:raise RuntimeError('Upload inválido')
        path.unlink()
    print('DEPLOY_OK: agent and public HTTPS dashboard active; test_mode='+str(test_mode))


if __name__=='__main__':main()

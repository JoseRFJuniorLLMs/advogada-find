"""Monthly public CNPJ ingestion with bounded disk use and resumable batches."""
from __future__ import annotations

import logging
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .db import iso
from .registry import import_cnpj

log=logging.getLogger(__name__)
HOST='https://arquivos.receitafederal.gov.br'
SHARE='YggdBLfdninEJX9' # Public resource linked from dados.gov.br, not a user credential.
DAV='/public.php/webdav/'
FILE=re.compile(r'(?:Empresas\d+|Estabelecimentos\d+|Municipios)\.zip')


def listing(client, path: str) -> list[tuple[str,str,int]]:
    response=client.request('PROPFIND',HOST+path,headers={'Depth':'1'})
    response.raise_for_status()
    result=[]
    tree=ET.fromstring(response.content)
    for item in tree.findall('{DAV:}response'):
        href=item.findtext('{DAV:}href','')
        parsed=urlparse(href)
        if parsed.netloc and parsed.netloc!='arquivos.receitafederal.gov.br':
            raise ValueError('Arquivo fora da origem oficial')
        file_path=unquote(parsed.path)
        if not file_path.startswith(DAV) or '..' in file_path.split('/'):
            raise ValueError('Caminho inválido na publicação')
        name=file_path.rstrip('/').rsplit('/',1)[-1]
        size=int(item.findtext('.//{DAV:}getcontentlength','0') or '0')
        result.append((name,file_path,size))
    return result


def download(client, path: str, target: Path, size: int, reserve_bytes: int):
    if target.exists() and (size==0 or target.stat().st_size==size):
        return
    if shutil.disk_usage(target.parent).free < reserve_bytes+max(size,1024**3):
        raise RuntimeError('Disco abaixo da reserva para importar CNPJ')
    partial=target.with_suffix('.part')
    with client.stream('GET',HOST+path) as response:
        response.raise_for_status()
        written=0
        with partial.open('wb') as stream:
            for chunk in response.iter_bytes(1024*1024):
                written+=len(chunk)
                if written%(64*1024*1024)<len(chunk) and shutil.disk_usage(target.parent).free<reserve_bytes:
                    raise RuntimeError('Reserva de disco atingida durante download')
                stream.write(chunk)
    if size and written!=size:
        raise RuntimeError('Download CNPJ incompleto')
    with partial.open('rb') as stream:
        if stream.read(4)!=b'PK\x03\x04':
            raise ValueError('Publicação não retornou um ZIP')
    partial.replace(target)


def set_state(db,key,value):
    with db.connect() as con:
        con.execute('INSERT INTO runtime(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))


def sync_cnpj(db, cache: Path, reserve_bytes: int = 10*1024**3) -> dict:
    cache.mkdir(parents=True,exist_ok=True)
    set_state(db,'cnpj_sync_status','Consultando a publicação oficial')
    try:
        with httpx.Client(auth=(SHARE,''),timeout=httpx.Timeout(60,connect=30),follow_redirects=False) as client:
            months=sorted(name for name,_,_ in listing(client,DAV) if re.fullmatch(r'\d{4}-\d{2}',name))
            if not months: raise ValueError('Publicação sem pastas mensais')
            month=months[-1]
            with db.connect() as con:
                complete=con.execute("SELECT value FROM runtime WHERE key='cnpj_sync_complete'").fetchone()
            if complete and complete['value']==month:
                set_state(db,'cnpj_sync_status','Atualizado: '+month)
                return {'month':month,'already_current':True}
            files={name:(path,size) for name,path,size in listing(client,DAV+month+'/') if FILE.fullmatch(name)}
            establishments=sorted(name for name in files if name.startswith('Estabelecimentos'))
            companies=sorted(name for name in files if name.startswith('Empresas'))
            if len(establishments)!=10 or len(companies)!=10 or 'Municipios.zip' not in files:
                raise ValueError('Publicação incompleta: esperado 10+10 lotes e Municípios')
            month_dir=cache/month;month_dir.mkdir(exist_ok=True)
            municipal_path=month_dir/'Municipios.zip'
            download(client,*[files['Municipios.zip'][0],municipal_path,files['Municipios.zip'][1],reserve_bytes])
            set_state(db,'cnpj_sync_month',month)
            start_key='cnpj_started_'+month
            with db.connect() as con:
                started=con.execute('SELECT value FROM runtime WHERE key=?',(start_key,)).fetchone()
            started_at=started['value'] if started else iso()
            if not started:set_state(db,start_key,started_at)
            for name in establishments+companies:
                key='cnpj_done_'+month+'_'+name
                with db.connect() as con:
                    done=con.execute('SELECT 1 FROM runtime WHERE key=?',(key,)).fetchone()
                if done: continue
                set_state(db,'cnpj_sync_status','Baixando '+name+' · '+month)
                path,size=files[name];target=month_dir/name
                download(client,path,target,size,reserve_bytes)
                set_state(db,'cnpj_sync_status','Filtrando '+name+' · '+month)
                if name.startswith('Estabelecimentos'):
                    import_cnpj(db,[target],municipal_path,[])
                else:
                    import_cnpj(db,[],municipal_path,[target])
                set_state(db,key,iso())
                # Only delete the ZIP created by this downloader under its own month cache.
                if target.resolve().parent != month_dir.resolve(): raise ValueError('Cache inválido')
                target.unlink()
            with db.tx() as con:
                # Block identities that fell out of the completed active legal-services snapshot.
                con.execute('''INSERT OR IGNORE INTO registry_exclusions(cnpj_basic)
                    SELECT DISTINCT old.cnpj_basic FROM registry_establishments old WHERE old.imported_at<?
                    AND NOT EXISTS (SELECT 1 FROM registry_establishments fresh
                                    WHERE fresh.cnpj_basic=old.cnpj_basic AND fresh.imported_at>=?)''',(started_at,started_at))
                con.execute('DELETE FROM registry_establishments WHERE imported_at<?',(started_at,))
                con.execute('DELETE FROM registry_exclusions WHERE cnpj_basic IN (SELECT cnpj_basic FROM registry_establishments)')
                con.execute('UPDATE companies SET active=0 WHERE cnpj_basic IN (SELECT cnpj_basic FROM registry_exclusions)')
            set_state(db,'cnpj_sync_complete',month)
            set_state(db,'cnpj_sync_status','Atualizado: '+month)
            return {'month':month,'files_processed':20}
    except Exception as exc:
        set_state(db,'cnpj_sync_status','Retentativa automática: '+type(exc).__name__)
        raise

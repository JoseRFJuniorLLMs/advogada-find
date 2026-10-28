"""Streaming importer for Receita Federal's public CNPJ layout (not a mail list)."""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from itertools import islice
from pathlib import Path

from .catalog import STATES
from .db import iso

log = logging.getLogger(__name__)
# CNAE 6911-7/01: serviços advocatícios (sociedades de advogados).
DEFAULT_CNAE_PREFIXES = ('6911701',)


def read_rows(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                with archive.open(member) as binary:
                    with io.TextIOWrapper(binary,encoding='latin-1',newline='') as stream:
                        yield from csv.reader(stream,delimiter=';')
    else:
        with path.open(encoding='latin-1',newline='') as stream:
            yield from csv.reader(stream,delimiter=';')


def import_cnpj(db, establishments: list[Path], municipalities: Path, companies: list[Path],
                state: str | None = None, prefixes=DEFAULT_CNAE_PREFIXES) -> dict:
    state = state.upper() if state else None
    if state is not None and state not in STATES:
        raise ValueError('UF inválida')
    names = {row[0].strip(): row[1].strip().title() for row in read_rows(municipalities) if len(row)>=2}
    if not names:
        raise ValueError('Tabela de municípios da Receita vazia')
    count = scanned = 0

    def eligible_rows():
        nonlocal scanned
        for path in establishments:
            for row in read_rows(path):
                scanned += 1
                if len(row)!=30:
                    raise ValueError('Estabelecimentos: esperado leiaute oficial de 30 campos')
                if row[5].lstrip('0')!='2' or row[19] not in STATES or (state and row[19]!=state):
                    continue
                codes=[row[11]]+row[12].split(',')
                matched=next((code for code in codes if code.strip().startswith(prefixes)),None)
                if not matched:
                    continue
                city=names.get(row[20])
                if not city:
                    raise ValueError('Código de município ausente na tabela da Receita')
                cnpj=''.join(row[:3]).strip()
                # Numeric and alphanumeric identifiers are preserved, never converted to int.
                if len(cnpj)!=14 or not cnpj.isalnum():
                    raise ValueError('CNPJ inválido no arquivo de estabelecimentos')
                # The taxpayer email (field 27) is deliberately not ingested as a recipient.
                yield (cnpj,row[0],row[4].strip(),city,row[19],matched,iso())

    iterator=iter(eligible_rows())
    while batch:=list(islice(iterator,1000)):
        with db.tx() as con:
            con.executemany('''INSERT INTO registry_establishments
                (cnpj,cnpj_basic,trade_name,city,state,cnae,imported_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(cnpj) DO UPDATE SET trade_name=excluded.trade_name,city=excluded.city,
                state=excluded.state,cnae=excluded.cnae,imported_at=excluded.imported_at''',batch)
        count+=len(batch)
        log.info('cnpj_import_progress matched=%s scanned=%s',count,scanned)
    with db.connect() as con:
        selected={row['cnpj_basic'] for row in con.execute('SELECT DISTINCT cnpj_basic FROM registry_establishments')}
    for path in companies:
        updates=[]
        for row in read_rows(path):
            if len(row)!=7:
                raise ValueError('Empresas: esperado leiaute oficial de 7 campos')
            if row[0] in selected:
                updates.append((row[1].strip(),row[0]))
            if len(updates)>=1000:
                with db.tx() as con:
                    con.executemany('UPDATE registry_establishments SET legal_name=? WHERE cnpj_basic=?',updates)
                updates=[]
        if updates:
            with db.tx() as con:
                con.executemany('UPDATE registry_establishments SET legal_name=? WHERE cnpj_basic=?',updates)
    return {'scanned':scanned,'matched':count,'scope':state or 'BR'}

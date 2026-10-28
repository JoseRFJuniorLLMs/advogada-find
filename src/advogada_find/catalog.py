"""Geographic coverage from IBGE; contacts still come from company websites."""
from __future__ import annotations

import csv
from pathlib import Path

import httpx

STATES = {'AC','AL','AP','AM','BA','CE','DF','ES','GO','MA','MT','MS','MG','PA',
          'PB','PR','PE','PI','RJ','RN','RS','RO','RR','SC','SP','SE','TO'}
STATE_IDS = dict(zip(('11','12','13','14','15','16','17','21','22','23','24','25','26','27',
                     '28','29','31','32','33','35','41','42','43','50','51','52','53'),
                    ('RO','AC','AM','RR','PA','AP','TO','MA','PI','CE','RN','PB','PE','AL',
                     'SE','BA','MG','ES','RJ','SP','PR','SC','RS','MS','MT','GO','DF')))


def import_cities(db, state: str | None = None, timeout: int = 15) -> int:
    state = state.strip().upper() if state else None
    if state is not None and state not in STATES:
        raise ValueError('UF inválida')
    suffix = f'estados/{state}/municipios' if state else 'municipios'
    response = httpx.get('https://servicodados.ibge.gov.br/api/v1/localidades/' + suffix,
                        timeout=timeout)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows or any(not isinstance(r,dict) or not isinstance(r.get('nome'), str) for r in rows):
        raise ValueError('Resposta de municípios inválida')
    cities = []
    for row in rows:
        uf = state or STATE_IDS.get(str(row.get('id',''))[:2])
        if not uf:
            raise ValueError('Município sem UF reconhecida')
        cities.append((row['nome'].strip(),uf))
    # A successful import must not reactivate paused cities or overwrite priorities.
    with db.tx() as con:
        from .db import iso
        for city, uf in cities:
            con.execute('''INSERT INTO cities(city,state,priority,next_scan_at) VALUES(?,?,50,?)
                ON CONFLICT(city,state) DO NOTHING''', (city, uf, iso()))
        con.execute("INSERT INTO runtime(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ('cities_scope_'+(state or 'BR'),iso()))
    return len(rows)


def export_catalog(db, output: Path) -> int:
    rows = db.list_contacts()
    columns = ['company_id','company_name','domain','website','city','state','verified','cnpj_basic',
               'email','kind','score','source_url','discovered_at','active','application_status']
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            # Spreadsheet formula protection for search titles and website content.
            writer.writerow({key: "'"+value if isinstance(value,str) and value.startswith(('=','+','-','@'))
                             else value for key,value in row.items()})
    return len(rows)

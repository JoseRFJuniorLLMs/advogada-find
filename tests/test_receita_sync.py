from contextlib import contextmanager
from io import BytesIO,StringIO
from unittest.mock import Mock
import csv
import zipfile

import httpx
import pytest

from advogada_find.receita_sync import sync_cnpj,listing,download,DAV,HOST


def zip_rows(rows):
    text=StringIO();csv.writer(text,delimiter=';').writerows(rows)
    binary=BytesIO()
    with zipfile.ZipFile(binary,'w') as archive:archive.writestr('data.csv',text.getvalue().encode('latin-1'))
    return binary.getvalue()


def response(body,url=HOST+DAV):
    return httpx.Response(207,content=body,request=httpx.Request('PROPFIND',url))


def test_sync_downloads_filters_and_resumes_without_retaining_large_zips(make_app,tmp_path,monkeypatch):
    app=make_app();cache=tmp_path/'cache'
    row=['']*30;row[:3]=['12345678','0001','00'];row[4]='Acme Software';row[5]='02'
    row[11]='6911701';row[19]='SP';row[20]='6291'
    blobs={'Municipios.zip':zip_rows([['6291','CAMPINAS']])}
    for i in range(10):
        blobs[f'Estabelecimentos{i}.zip']=zip_rows([row] if i==0 else [])
        blobs[f'Empresas{i}.zip']=zip_rows([['12345678','ACME SOFTWARE LTDA','','','','','']] if i==0 else [])
    downloads=[]
    class Client:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def request(self,method,url,**kwargs):
            if url==HOST+DAV:
                return response('<d:multistatus xmlns:d="DAV:"><d:response><d:href>'+DAV+'2026-09/</d:href></d:response></d:multistatus>')
            xml='<d:multistatus xmlns:d="DAV:">'
            for name,body in blobs.items():
                xml+=f'<d:response><d:href>{DAV}2026-09/{name}</d:href><d:propstat><d:prop><d:getcontentlength>{len(body)}</d:getcontentlength></d:prop></d:propstat></d:response>'
            return response(xml+'</d:multistatus>')
        @contextmanager
        def stream(self,method,url):
            name=url.rsplit('/',1)[-1];downloads.append(name)
            yield httpx.Response(200,content=blobs[name],request=httpx.Request('GET',url))
    monkeypatch.setattr('advogada_find.receita_sync.httpx.Client',Client)
    monkeypatch.setattr('advogada_find.receita_sync.shutil.disk_usage',lambda _: Mock(free=100*1024**3))
    assert sync_cnpj(app.db,cache)['files_processed']==20
    assert app.db.list_registry()[0]['legal_name']=='ACME SOFTWARE LTDA'
    assert app.db.list_contacts()==[]
    assert [p.name for p in (cache/'2026-09').iterdir()]==['Municipios.zip']
    assert sync_cnpj(app.db,cache)['already_current']
    assert len(downloads)==21


def test_official_listing_does_not_accept_other_hosts():
    client=Mock();client.request.return_value=response('<d:multistatus xmlns:d="DAV:"><d:response><d:href>https://external.com/file.zip</d:href></d:response></d:multistatus>')
    with pytest.raises(ValueError):listing(client,DAV)


def test_download_does_not_fill_reserved_disk(tmp_path,monkeypatch):
    monkeypatch.setattr('advogada_find.receita_sync.shutil.disk_usage',lambda _: Mock(free=1))
    client=Mock()
    with pytest.raises(RuntimeError):download(client,DAV+'Municipios.zip',tmp_path/'file.zip',100,10)
    client.stream.assert_not_called()

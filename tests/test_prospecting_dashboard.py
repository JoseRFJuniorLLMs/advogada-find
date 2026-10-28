from pathlib import Path
import sqlite3
from types import SimpleNamespace

from advogada_find.dashboard import prospecting_summary
from advogada_find.prospecting import ProspectionDB


def test_missing_prospecting_db_does_not_create_file(tmp_path):
    settings = SimpleNamespace(db_path=tmp_path / 'advogada-find.db')
    result = prospecting_summary(settings)
    assert result['available'] is False
    assert not (tmp_path / 'prospeccao.db').exists()


def test_dashboard_prospecting_counts_only(tmp_path):
    db = ProspectionDB(tmp_path / 'prospeccao.db')
    db.add({'name': 'Silva Advogados', 'website': 'https://silva.com.br/',
            'city': 'Brasília', 'state': 'DF'})
    db.close()
    result = prospecting_summary(SimpleNamespace(db_path=tmp_path / 'advogada-find.db'))
    assert result['available'] is True
    assert result['total'] == 1 and result['pending'] == 1
    assert 'email' not in result and 'contact_name' not in result

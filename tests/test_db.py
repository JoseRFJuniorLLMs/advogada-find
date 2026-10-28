from pathlib import Path

from advogada_find.db import Database


def test_company_and_contact_dedup(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.init()
    cid1 = db.upsert_company("Acme", "acme.com.br", "https://acme.com.br/", "Campinas", "SP", "test", "q")
    cid2 = db.upsert_company("Acme Tecnologia", "acme.com.br", "https://acme.com.br/", "Campinas", "SP", "test", "q2")
    assert cid1 == cid2
    db.approve_company(cid1, 'Site oficial de software em Campinas revisado')
    db.upsert_contact(cid1, "rh@acme.com.br", "rh", 100, "https://acme.com.br/carreiras")
    db.upsert_contact(cid1, "rh@acme.com.br", "rh", 100, "https://acme.com.br/vagas")
    assert db.stats()["companies"] == 1
    assert db.stats()["contacts"] == 1


def test_sent_company_is_never_selected_again(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.init()
    cid = db.upsert_company("Acme", "acme.com.br", "https://acme.com.br/", "Campinas", "SP", "test", "q")
    db.approve_company(cid, 'Site oficial de software em Campinas revisado')
    db.upsert_contact(cid, "rh@acme.com.br", "rh", 100, "https://acme.com.br/carreiras")
    first = db.claim_next(3)
    assert first is not None
    db.mark_sent(first["application_id"], "subject", "<id@example.com>")
    assert db.claim_next(3) is None

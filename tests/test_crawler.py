from advogada_find.crawler import score_email


def test_priority_role_addresses():
    assert score_email("rh@example.com", True) == ("rh", 100)
    assert score_email("recrutamento@example.com", True)[1] >= 95
    assert score_email("jobs@example.com", True)[1] >= 95


def test_generic_contact_requires_career_signal():
    assert score_email("contato@example.com", False)[1] == 0
    assert score_email("contato@example.com", True)[1] > 0


def test_personal_and_sensitive_addresses_are_rejected():
    assert score_email("joao.silva@example.com", True)[1] == 0
    assert score_email("privacy@example.com", True)[1] == 0
    assert score_email("noreply@example.com", True)[1] == 0

import pytest

from advogada_find.names import firm_name, generic_name


@pytest.mark.parametrize('title,name', [
    ('Advogado em Brasília/DF — Marques Escritório de Advocacia', 'Marques Escritório de Advocacia'),
    ('Vieira Rios Advogados – Advocacia Criminal em Brasília', 'Vieira Rios Advogados'),
    ('Trabalhe conosco | Siqueira Castro', 'Siqueira Castro'),
    ('Advocacia Maciel', 'Advocacia Maciel'),
    ('Mota & Advogados Associados', 'Mota & Advogados Associados'),
    ('ADVOGADO EM DIREITO ADMINISTRATIVO EM BRASÍLIA DF', ''),
    ('Advocacia Criminal Especializada', ''),
    ('TRABALHE CONOSCO', ''),
])
def test_firm_name_skips_page_labels_and_slogans(title, name):
    assert firm_name(title) == name


def test_first_title_with_a_name_wins():
    assert firm_name('Contato', 'Home - Silva Advogados') == 'Silva Advogados'
    assert generic_name('Fale conosco') and not generic_name('Willer Tomaz')

"""Firm names for greetings: page titles are often slogans or page labels."""
from __future__ import annotations

import re
import unicodedata

_SEPARATORS = re.compile(r'\s+[|\-–—:·•]\s+')
_AREA = (r'(?:criminal|penal|trabalhista|civel|civil|administrativ[oa]|previdenciari[oa]|tributari[oa]|'
         r'empresarial|especializad[oa]|familia|consumidor|digital|online|public[oa]|consultoria|'
         r'juridic[oa]|e|de|do|da|direito)')
_GENERIC = re.compile(
    r'^(?:trabalhe conosco|contato|fale conosco|home|inicio|pagina inicial|vagas|carreiras|sobre(?: nos)?|'
    r'quem somos|(?:nossa )?equipe|blog|servicos|areas de atuacao|o escritorio|escritorio|'
    r'pagina nao encontrada|404)$'
    r'|\bem\b|\bespecialista|\bmelhor'
    r'|^(?:advogad[oa]s?|advocacia|escritorio de advocacia|sociedade de advogados|direito)(?:\s+' + _AREA + r')*$')


def _fold(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c))


def generic_name(name: str) -> bool:
    """True for page labels and slogans such as 'Trabalhe conosco' or 'Advogado em Brasília'."""
    name = ' '.join(name.split())
    return len(name) < 3 or len(name) > 80 or bool(_GENERIC.search(_fold(name)))


def firm_name(*titles: str) -> str:
    """First segment of the given titles that reads as a firm name, or ''."""
    for title in titles:
        for segment in _SEPARATORS.split(' '.join((title or '').split())):
            segment = segment.strip(' .,;')
            if segment and not generic_name(segment):
                return segment
    return ''

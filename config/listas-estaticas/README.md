# Lista estática pesquisada — 2 de novembro de 2026

Arquivo: `2026-11-02-contatos-pesquisados.csv`.

20 escritórios únicos: DF 3, SP 8, RJ 8 e RS 1. Uma linha e um endereço por escritório. Fontes consultadas nos sites oficiais em 02/11/2026; sem endereços inferidos. As instruções da página foram registradas nas colunas `subject_hint` e `application_notes`.

A publicação de um canal de candidaturas não significa que exista vaga adequada aberta nem confirma a entregabilidade da caixa postal. Os canais de banco de talentos foram separados das vagas específicas na coluna `channel`.

## Contatos

| Escritório | Cidade / UF | E-mail publicado | Fonte |
| --- | --- | --- | --- |
| Barbosa de Sá & Alencastro Advogados | Brasília/DF | bsaa@bsaa.adv.br | [Página oficial](https://bsaa.adv.br/trabalhe-conosco/) |
| Schiefler Advocacia | Brasília/DF | administrativo@schiefler.adv.br | [Página oficial](https://schiefler.adv.br/trabalhe-conosco/) |
| Tortoro, Madureira & Ragazzi Advogados | Brasília/DF | selecao@tortoromr.com.br | [Página oficial](https://www.tortoromr.com.br/contato/) |
| Fittipaldi Gômara Advogados | São Paulo/SP | recrutamento@fittipaldigomara.com.br | [Página oficial](https://fittipaldigomara.com.br/trabalhe-conosco/) |
| Caselli Guimarães Advogados | São Paulo/SP | recrutamento@caselliguimaraes.com.br | [Página oficial](https://www.caselliguimaraes.com.br/contato) |
| PFP Advocacia | São Paulo/SP | pfp@pfpadvogados.com.br | [Página oficial](https://www.pfpadvogados.com.br/trabalhe-conosco) |
| Figueiredo Filho Sociedade de Advogados | São Paulo/SP | selecao@figueiredofilho.com.br | [Página oficial](https://figueiredofilho.com.br/institucional/trabalhe-conosco/) |
| Garcia Peres Advogados | São Paulo/SP | rh@garciaperes.com.br | [Página oficial](https://garciaperes.com.br/trabalhe-conosco/) |
| AELA Advogados | São Paulo/SP | rh@aela.com.br | [Página oficial](https://www.aela.com.br/trabalhe-conosco) |
| FZ Advogados | São Paulo/SP | rh@fzadv.com.br | [Página oficial](https://www.fzadv.com.br/trabalhe-conosco) |
| Maneira Advogados | São Paulo/SP | vagas@maneira.adv.br | [Página oficial](https://www.maneira.adv.br/pt/trabalhe-conosco) |
| Bravo Godoy Perroni Advocacia | Rio de Janeiro/RJ | bgp@bgpadv.com.br | [Página oficial](https://www.bgpadv.com.br/trabalheconosco) |
| Escritório Jurídico Elísio de Souza | Rio de Janeiro/RJ | contato@elisio.adv.br | [Página oficial](https://www.elisio.adv.br/carreira) |
| Wallace Sampaio Advogados e Consultores | Rio de Janeiro/RJ | wsadv@wsadv.com.br | [Página oficial](https://wsadv.com.br/trabalhe-conosco/) |
| R.M. Andrade Advogados | Rio de Janeiro/RJ | contato@rmandrade.com | [Página oficial](https://rmandrade.com/trabalhe-conosco/) |
| Ralil Advogados | Rio de Janeiro/RJ | contato@raliladvogados.adv.br | [Página oficial](https://raliladvogados.adv.br/trabalhe-conosco/) |
| Schmitt Advogados | Rio de Janeiro/RJ | rh@schmitt.adv.br | [Página oficial](https://schmitt.adv.br/trabalhe-conosco/) |
| Brandão & Mesquita Advogados | Rio de Janeiro/RJ | rh@bmadv.com | [Página oficial](https://bmadv.com/trabalhe-conosco/) |
| Tavares Legal IP Expertise | Rio de Janeiro/RJ | tavares@tavaresoffice.com.br | [Página oficial](https://www.tavaresoffice.com.br/pt/o-escritorio/) |
| Berwanger Advogados | Candelária/RS | rh@berwangeradvogados.adv.br | [Página oficial](https://www.berwangeradvogados.adv.br/trabalhe-conosco) |

## Antes de alimentar a fila

- Ler `application_notes`: há escritórios que pedem carta de apresentação, assunto específico ou formulário preliminar. Maneira e R.M. Andrade publicam requisitos de vagas específicas que precisam coincidir com o perfil da candidata.
- Conferir o histórico em produção antes de cada importação. A deduplicação é por escritório/domínio e identidade; trocar de e-mail não deve repetir a candidatura.
- O importador atual (`import-curated`) consulta novamente o site. O filtro de prefixo bloqueia seis e-mails expressamente publicados para recrutamento: BSAA, Schiefler, PFP, Bravo Godoy Perroni, Wallace Sampaio e Tavares.
- `subject_hint` e `application_notes` são metadados deste lote. O envio atual não aplica essas colunas automaticamente; elas precisam ser respeitadas ao preparar as candidaturas.

## Diagnóstico da produção nesta pesquisa

O painel mostrava 3.945 domínios catalogados, 91 candidaturas aceitas pelo SMTP, três no dia e zero na fila. O serviço estava ativo, com limite configurado de 50 por dia e intervalo mínimo de cinco minutos. A conta remetente usa Gmail pessoal. A base catalogada inclui resultados de busca não validados como escritórios, e não equivale a uma lista pronta de destinatários.

Este lote foi preparado localmente. A fila, os limites e os serviços de produção não foram alterados.

## Revisão

Conferência estrutural: 20 e-mails únicos, 20 domínios únicos, todos com e-mail e URL de fonte no domínio oficial. A consulta ao histórico remoto foi feita sem modificar o banco.

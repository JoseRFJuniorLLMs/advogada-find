# Cobertura nacional de escritórios — diagnóstico e expansão

## Por que existem poucos e-mails prontos?

O sistema não é uma lista de e-mails do Brasil. Ele descobre domínios por buscas (Brave/DDGS), valida atividade jurídica e cidade na página oficial, aceita apenas canais institucionais publicados, e exclui firmas já contatadas. Um domínio descoberto não é uma candidatura pronta.

No relatório estático de 02/11/2026, 3.945 domínios foram catalogados na produção e 91 candidaturas eram reportadas como aceitas pelo SMTP; a fila estava vazia. Esses números são uma fotografia documentada, não uma consulta em tempo real. Os 20 escritórios do CSV são um lote *novo de pesquisa*, não uma base nacional. Nenhum script deste PR executa disparo de e-mails.

## Expansão nacional já disponível

- `CITIES_SCOPE=BR` carrega os municípios via API do IBGE. O ciclo original busca cada cidade e pesquisa sites oficiais.
- `sync-cnpj` baixa a base nacional de estabelecimentos e filtra o CNAE 6911-7/01. Essa é a melhor opção de cobertura ampla de nomes de escritórios com CNPJ, mas CNPJ não inclui necessariamente website ou e-mail publicamente publicado. Exige espaço em disco e banda.
- `python -m advogada_find.prospecting import-seeds` aceita CSVs de fontes B2B autorizadas. `scan` confirma canais em páginas oficiais e `export` prepara CSV para revisão; a inserção na fila original continua separada.

## Novidade deste branch

- `python -m advogada_find.national audit`: mostra contagem de domínios catalogados, escritórios efetivamente verificados, ausência de cidade, atividade jurídica e canal, contatos e fila apta. Apenas leitura.
- `python -m advogada_find.national recheck --limit 100 --after-id 0`: reconsulta até 100 empresas catalogadas não verificadas, salva somente contatos confirmados e mostra `next_after_id`. Use o último valor na próxima execução para cobrir o catálogo sem repetir sempre as primeiras empresas. Limite máximo 500 por operação, serializado e respeitando `robots.txt`.
- Reconhece `administrativo@` como caixa institucional genérica.
- No `import-curated`, caixas de marca abreviada (ex.: `pfp@pfpadvogados.com.br`) só são aceitas quando a **mesma caixa** foi observada numa **página oficial de recrutamento** e o site passou por evidências de identidade, advocacia e cidade. O banco revalida a exceção. E-mails pessoais e aleatórios continuam rejeitados. O importador não envia.

## Comandos na VM

Antes, faça backup do SQLite e mantenha os dados e segredos fora do checkout. Confirme `DRY_RUN=true` antes de importar contatos.

```bash
cd /opt/advogada-find
# Ativar .venv e carregar o ambiente privado segundo sua instalação.
python -m advogada_find.national audit
python -m advogada_find.national recheck --limit 100 --after-id 0
# Para as próximas páginas, use o valor next_after_id retornado.
python -m advogada_find --env-file /etc/advogada-find.env targets
```

Como `national` usa `Settings()`, o ambiente privado precisa estar carregado previamente nesse processo, por exemplo `set -a; . /etc/advogada-find.env; set +a` com permissões adequadas e sintaxe compatível. Não publique o resultado de listas ou e-mails em painel sem autenticação.

O CSV estático existente fica em `config/listas-estaticas/2026-11-02-contatos-pesquisados.csv`. Revise `application_notes`, vagas específicas, formulários e exigências de assunto. Não aumente os limites nem inicie disparo em massa simplesmente porque o banco cresceu. Inclua identificação do remetente, possibilidade de oposição/descadastro, política de retenção e revisão de base legal/LGPD. Endereços para candidaturas não são base para publicidade jurídica a clientes.

## Limitações

A cobertura de **todos** os escritórios ativos do Brasil e seus emails não pode ser garantida por consultas web. Sites podem bloquear robôs, manter apenas formulários, omitir cidade ou não publicar canais de recrutamento. O banco CNPJ contém empresas, não comprovação de vagas. O relatório `audit` separa candidatos catalogados, aptos e já enviados. Não há sincronização automática dos resultados com serviço externo nem mudança de `DRY_RUN`.

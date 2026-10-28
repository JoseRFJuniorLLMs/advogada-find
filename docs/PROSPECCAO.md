# Advogada Find — módulo de prospecção de contatos

Adição independente para o repositório `JoseRFJuniorLLMs/advogada-find`.

## O que o repositório já faz (não substituir)

Conforme README público revisado em 28/10/2026: `scan` usa Brave/DDGS; `import-cnpj`/`sync-cnpj` descobrem escritórios pelo CNAE; coleta de páginas oficiais respeita `robots.txt`; há deduplicação por escritório; o importador `import-curated` revalida publicação do e-mail, cidade e atividade jurídica; há limite de 5 candidaturas/dia, deduplicação e `DRY_RUN=true` por padrão. O código-fonte do repositório foi revisado antes desta integração; a prospecção permanece isolada do banco de candidaturas. Por segurança, esta implementação **não altera** a interface, os modelos SQLite ou o mecanismo de envio existentes.

## O que foi implementado neste complemento

- Importação de CSVs de prospecção de diretórios, anuários, fontes oficiais, LinkedIn/Apollo/ZoomInfo/Hunter com **exportação autorizada**. Campos alternativos em português e inglês são aceitos.
- Preservação de origem, URL, nome/cargo e link profissional fornecidos pela fonte, sem usá-los como destinatários automaticamente.
- Coleta de páginas oficiais (contato, carreiras, recrutamento, trabalhe conosco) com limites, `robots.txt`, verificação conservadora de domínio, checagem DNS para evitar redes privadas, limite de conteúdo e tempo por consulta.
- Identificação apenas de caixas **institucionais** (`rh@`, `recrutamento@`, `carreiras@`, `talentos@`, `contato@`, etc.). E-mails pessoais e de domínio diferente são excluídos.
- Armazenamento separado em SQLite, evidência de publicação e registro básico de resultados por escritório.
- Estados: `pending`, `confirmed`, `review`, `blocked`, `rejected`.
- Painel local em 127.0.0.1:8798 com filtros, indicadores e CSV de confirmados/revisão.
- Exportação no formato exato `name,city,state,website,email,source_url`, exigido pelo `import-curated` já existente.
- Consulta opcional ao Hunter usando chave própria; verificação de entregabilidade por `verify` também opcional. **Resultados de B2B são sugestões, não comprovação**: o verificador do site e o importador original continuam exigidos.
- Testes unitários sem acesso de rede.

### Instalar no repositório

Copie `src/advogada_find/prospecting.py` para o mesmo caminho dentro do clone do repositório, preservando todos os arquivos existentes. Não é preciso modificar `pyproject.toml` nem as tabelas do banco principal. `python -m advogada_find.prospecting` usa apenas a biblioteca padrão Python (3.10+).

No Windows/PowerShell, partindo do clone com o ambiente editável instalado:

```powershell
python -m advogada_find.prospecting --db D:/DEV/advogada-find-private/prospeccao.db import-seeds --input D:/DEV/leads.csv --source linkedin-manual
python -m advogada_find.prospecting --db D:/DEV/advogada-find-private/prospeccao.db scan --limit 30 --delay 1.5
python -m advogada_find.prospecting --db D:/DEV/advogada-find-private/prospeccao.db status
python -m advogada_find.prospecting --db D:/DEV/advogada-find-private/prospeccao.db export --output D:/DEV/confirmados.csv
python -m advogada_find.prospecting --db D:/DEV/advogada-find-private/prospeccao.db export --review --output D:/DEV/revisao.csv
python -m advogada_find.prospecting --db D:/DEV/advogada-find-private/prospeccao.db dashboard --port 8798
```

Acesse o painel apenas localmente em `http://127.0.0.1:8798/` (não publicar sem autenticação). Para reprocessar contatos: `scan --force`. Para rejeitar: `review --id 5 --decision reject`. Para reabrir: `review --id 5 --decision reset`.

### Integrar o CSV com o fluxo já existente

1. Revise `confirmados.csv` e `revisao.csv` e confira que os dados não incluem informações pessoais indevidas.
2. **Pare o daemon do envio** antes de importar, conforme instrução do README oficial.
3. Importe somente o CSV confirmado usando o importador existente (que faz novamente todas as verificações):

```bash
python -m advogada_find --env-file /caminho/advogada-find.env import-curated --input /caminho/confirmados.csv --batch prospeccao-2026-10
```

4. Inspecione o resultado, inclusive as linhas em `review`. Deixe `DRY_RUN=true` até confirmar o funcionamento. Nenhum comando deste complemento envia mensagens nem muda o `DRY_RUN` do projeto principal.

### Fontes suportadas / limites

| Fonte | Uso recomendado | Automação incluída? |
| --- | --- | --- |
| Site oficial da banca | Confirmar endereço institucional e a página em que aparece | **Sim**, verificação limitada e respeitando robots |
| OAB e base CNPJ | Descobrir nome/CNPJ/escritórios | **Reutilizar** comandos atuais do projeto; ingestão de CSV com `name,city,state,website` |
| Análise Advocacia, Chambers, Legal 500 | Criar CSV de bancas/sites via acesso permitido | Importação de CSV; sem scraper do diretório |
| LinkedIn Sales Navigator, Apollo, ZoomInfo, RocketReach, Snov.io | Buscar empresas, cargos e canais autorizados | Importação manual de CSV, de acordo com os termos do serviço |
| Hunter.io | Sugerir caixas genéricas por domínio | **Sim**, opcional via API; não valida existência no site |

O recurso `hunter` imprime uma lista, **não a grava como confirmada**:

```bash
HUNTER_API_KEY=... python -m advogada_find.prospecting hunter --domain exemplo.com.br
# Opção explícita: Hunter cobra créditos de verificação
HUNTER_API_KEY=... python -m advogada_find.prospecting --db /caminho/prospeccao.db verify --limit 10
```

A verificação via Hunter só avalia e-mails que já foram confirmados no site; `valid` preserva a aprovação e `invalid`, `accept_all` ou `unknown` passam para revisão e deixam de ser exportados como confirmados. A verificação pode consumir créditos; não há chamada automática sem o comando `verify`. Uma validação técnica não garante entrega ou leitura. <https://hunter.io/api-documentation/>.

A plataforma de origem poderá limitar exportação, acesso e tratamento dos dados. Não contorne autenticação, CAPTCHAs, nem colete dados pessoais sem base jurídica adequada. Revisar obrigações de LGPD e termos de uso de cada fonte.

### Privacidade, segurança e operação

- O painel é **somente leitura**, atende por padrão a `127.0.0.1` e não realiza candidaturas.
- Não coloque currículos, chaves de API, senhas ou bancos de produção no GitHub.
- Não exponha a porta 8798 publicamente sem autenticação e controle de acesso.
- O banco de prospecção é separado do banco original; faça backup e estabeleça retenção de dados e revisão de consentimento/base legal quando aplicável.
- Não inferir e-mails (`rh@dominio` é candidato apenas se encontrado na página pública); envio para pessoa física não é gerado.
- **A verificação de domínio usa equivalência conservadora com/sem `www`**. Sites com uma página de carreiras em subdomínio diferente deverão passar por revisão manual e eventualmente pela validação atual.
- A coleta não confirma entregabilidade SMTP. Isso é diferente de comprovar publicação oficial do endereço.
- A integração plena visual dentro do painel existente depende da leitura e modificação do código da interface original; esta versão fornece um painel lateral independente.

### Executar testes

```bash
python -m pytest -q tests/test_prospecting.py
```


### Fontes provenientes de exportações do aplicativo existente

O comando `export-catalog` do projeto principal pode fornecer sementes para prospecção, **desde que** o CSV exportado tenha colunas equivalentes a nome, cidade, UF e website. Se não houver URL, é necessário enriquecê-la antes da verificação. O componente não acessa diretamente o banco SQLite de candidaturas para evitar conflitos de esquema ou gravações indevidas.

### Operação recorrente (opcional)

Em ambiente Linux, é possível agendar `python -m advogada_find.prospecting ... scan --limit 30 --delay 1.5` com um `systemd timer` ou `cron`, usando o mesmo arquivo SQLite separado e somente um processo por vez. Use uma conta sem privilégios, mantenha o serviço na interface local e limite a taxa conforme a política do domínio consultado. A exportação para o sistema de candidaturas continua **manual e revisada**.

## CI/CD e publicação na VM

O workflow `.github/workflows/ci.yml` executa testes em Python 3.11–3.13; na `main`, publica na VM **apenas** com `VM_DEPLOY_ENABLED=true` em Actions → Variables e estes secrets configurados: `VM_SSH_HOST`, `VM_SSH_USER`, `VM_SSH_PRIVATE_KEY`, `VM_SSH_KNOWN_HOSTS` (linha completa de known_hosts verificada fora de banda). O usuário SSH precisa executar `sudo -n bash` sem senha. O deploy usa `deploy/ci_update.sh`, faz backup do código, conserva `/var/lib/advogada-find`, `/etc/advogada-find.env`, `/opt/advogada-find-private` e `.venv`, executa doctor e valida `/health`. Não altera `DRY_RUN` nem envia e-mails.

A página pública principal exibe apenas contadores de prospecção, sem divulgar dados pessoais de recrutadores. O painel completo de contatos segue acessível somente por túnel SSH na porta 8798, caso iniciado separadamente. O deploy de código **não agenda varredura nem importa automaticamente contatos para a fila**; a triagem segue manual.

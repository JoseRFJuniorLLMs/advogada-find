# advogada-find

Agente Python que procura escritórios de advocacia na região de atuação da **Dra. Ana Carolina Campos** (Advogada, OAB/DF nº 73.471), encontra o canal público de contato ou recrutamento de cada escritório e envia uma candidatura com o currículo em anexo. Derivado do [dev-find](../dev-find), sem LLM. `DRY_RUN=true` é o padrão: nada é enviado sem configuração explícita.

## Perfil (CV de 2026)

- Direito Público na Justiça Federal: mandado de segurança, tutela de urgência, PAD, Lei 8.112/1990, controle de legalidade de atos da Administração Pública.
- Contencioso cível e trabalhista: peças em todas as instâncias, prazos, análise de êxito (JM Advogados Associados, Brasília, 2019–2023; Googolplex, desde 2023).
- Pós-graduação em Direito Penal; advocacia criminal no DF (audiência de custódia, habeas corpus), conforme o site profissional https://audiencia-df.web.app/.
- Contratos de software, licenciamento de tecnologia e LGPD.
- Cursos ENAP (2026): Regime Geral da Previdência Social; Nova Lei de Licitações e Contratos.

O currículo enviado é o ampliado de 28/10/2026 (`CV_PATH`); a versão anterior ficou como `cv-2026-10-27.pdf` na pasta privada.

## Mensagem

O modelo está em [templates/application.txt](templates/application.txt). Marcadores: `{company}`, `{name}`, `{role}`, `{oab}`, `{city}`, `{phone}`, `{email}`, `{linkedin}`, `{site}`.

- O link `SITE_URL` (padrão https://audiencia-df.web.app/) é **obrigatório no corpo**. Se o modelo não contiver `{site}`, a validação falha antes de reservar qualquer envio.
- Assunto: `EMAIL_SUBJECT=Candidatura | {role} | {oab} | {name}`.
- O anexo usa `CV_FILENAME` (por exemplo `Curriculo-Ana-Carolina-Campos.pdf`).

Ver a mensagem exata, sem banco e sem SMTP:

```bash
python -m advogada_find --env-file /caminho/advogada-find.env preview --company "Silva Advogados" --to contato@silva.adv.br
```

## Onde procura

- Todo o Brasil (`CITIES_SCOPE=BR`): os 5.571 municípios do IBGE. [config/cities.csv](config/cities.csv) dá prioridade a DF (Brasília e regiões administrativas), RJ e SP (capitais e polos), depois às demais capitais e grandes cidades; o resto entra com prioridade 50.
- `PRIORITY_STATES=DF,RJ,SP`: na fila de envio, estes estados vêm antes do resto do Brasil (depois da lista revisada). Prioridade de uma cidade já existente no banco muda com `add-city`.
- A inscrição é na OAB/DF; atuação habitual noutra seccional exige inscrição suplementar.
- Pesquisa web (Brave Search com `BRAVE_SEARCH_API_KEY`, senão DDGS) com termos jurídicos: escritório de advocacia, advogados associados, Direito Administrativo e servidores públicos, cível e trabalhista, criminal, "trabalhe conosco".
- Site oficial do escritório: páginas de contato, equipe, institucional e trabalhe conosco, respeitando robots.txt.
- Opcional: cadastro CNPJ da Receita Federal com CNAE 6911-7/01 (serviços advocatícios), via `import-cnpj`/`sync-cnpj`. Não vem ativo no deploy porque baixa a publicação nacional inteira.

Ficam de fora: tribunais, MP, Defensoria e órgãos públicos (`.jus.br`, `.mp.br`, `.def.br`, `.gov.br`, `.leg.br`), OAB, Jusbrasil, Escavador, portais de notícias jurídicas e sites de vagas.

## Regras de elegibilidade

Um escritório só entra na fila quando o site tem evidência de atividade jurídica (advocacia, sociedade de advogados, áreas do Direito, OAB/UF), menciona a cidade, tem identidade própria, não é diretório/notícias e publica um endereço de papel no próprio domínio.

- Prioridade: `rh@`, `recrutamento@`, `curriculo(s)@`, `trabalheconosco@`, `vagas@`, `selecao@`, `talentos@`.
- Endereço geral do escritório (`contato@`, `escritorio@`, `secretaria@`, `atendimento@`) é aceito sem página de recrutamento porque `CONTACT_REQUIRES_CAREERS_SIGNAL=false`. Com `true`, volta à regra do dev-find.
- Nunca usa e-mail pessoal de advogado ou funcionário (`dra.maria@...`) nem endereços de outro domínio (Gmail etc.).
- Um envio por escritório (domínio ou CNPJ básico), no máximo 5 por dia, intervalo mínimo de 30 min + jitter, dias úteis 09:00–17:00.

## Cuidados (OAB e LGPD)

- A mensagem é uma **candidatura** dirigida a escritórios, não oferta de serviços a potenciais clientes: o Provimento CFOAB 205/2021 proíbe mala direta para captação de clientela. Não mudar o texto para oferecer serviços.
- Não citar processos, partes ou clientes na mensagem (sigilo profissional, Código de Ética e Disciplina). A inscrição é verificável no Cadastro Nacional dos Advogados da OAB.
- Só endereços institucionais publicados pelo próprio escritório; volume baixo e uma única mensagem por escritório.

## Execução local (Windows, tudo no D:)

```bash
uv venv --cache-dir D:/uv-cache .venv
uv pip install --cache-dir D:/uv-cache -p .venv/Scripts/python.exe -e ".[dev]"
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

Configuração privada fora do checkout: `D:\DEV\advogada-find-private\advogada-find.env` (CV em `cv.pdf`, banco `advogada-find.db`).

```bash
python -m advogada_find --env-file D:/DEV/advogada-find-private/advogada-find.env doctor
python -m advogada_find --env-file D:/DEV/advogada-find-private/advogada-find.env scan --city "Brasília" --state DF
python -m advogada_find --env-file D:/DEV/advogada-find-private/advogada-find.env targets
python -m advogada_find --env-file D:/DEV/advogada-find-private/advogada-find.env send-once
python -m advogada_find --env-file D:/DEV/advogada-find-private/advogada-find.env dashboard --port 8797
```

`targets` lista a fila na ordem de envio (um contato por escritório). `send-once` com `DRY_RUN=true` só renderiza a próxima mensagem. Outros comandos herdados: `status`, `cities`, `companies`, `contacts`, `applications`, `events`, `export-catalog`, `add-city`, `daemon`.

Para enviar de verdade: senha de app do Gmail da própria advogada em `SMTP_PASSWORD`, `doctor --smtp` (autentica sem enviar) e `DRY_RUN=false`.

## Deploy

`deploy/` mantém o instalador systemd adaptado. Implantado em 27/10/2026 na VM memoria-vm-2: `/opt/advogada-find`, `/etc/advogada-find.env`, painel em `127.0.0.1:8797` publicado em `https://35.247.217.66.nip.io/advogada-find/`. O painel é público por escolha do usuário. O daemon está em `DRY_RUN=true` e sem senha SMTP; não envia e-mails. A VM usa uma cópia independente do banco local.

## Lista de escritórios revisada

Para priorizar uma lista pesquisada, crie um CSV UTF-8 com as colunas `name,city,state,website,email,source_url`. A fonte deve ser uma página do domínio oficial que publica o endereço institucional. Exemplo fictício:

```csv
name,city,state,website,email,source_url
Escritório Exemplo,Brasília,DF,https://exemplo.com.br/,rh@exemplo.com.br,https://exemplo.com.br/carreiras
```

```bash
python -m advogada_find --env-file /caminho/advogada-find.env import-curated --input /caminho/lista.csv --batch lote-2026-10
```

O importador volta a consultar o site e aplica as regras de domínio, atividade jurídica, cidade, robots e canal institucional. Só inclui o endereço exato encontrado num site elegível; linhas sem confirmação, com domínio divergente ou já incluídas ficam em `review`, com número da linha e motivo. O endereço e a URL efetivamente confirmada ficam vinculados ao lote; pesquisa automática posterior não substitui esses dados. A fila processa primeiro a lista revisada, priorizando o DF nela, e depois os demais resultados. Enquanto houver item revisado aguardando nova tentativa ou com envio incerto, a passagem para os demais fica bloqueada; envio incerto exige reconciliação. O histórico existente de envios e deduplicação por domínio ou CNPJ continuam valendo. Executar importação em produção com o daemon parado para evitar iniciar um lote parcial antes de o CSV inteiro ser validado. A importação grava cada linha aprovada separadamente, informa as linhas rejeitadas e pode ficar parcial se houver falha de rede ou interrupção; confira o resultado e a contagem antes de iniciar o daemon. O comando retorna erro se não incluir nenhum contato. A importação não envia e-mails nem altera `DRY_RUN`.

## Banco

Usa o mesmo SQLite local do dev-find (fila com reserva, lease e limite diário). A regra do ambiente pede HeraclitusDB para projetos novos; a troca está pendente de decisão.
#   a d v o g a d a - f i n d  
 #   a d v o g a d a - f i n d  
 
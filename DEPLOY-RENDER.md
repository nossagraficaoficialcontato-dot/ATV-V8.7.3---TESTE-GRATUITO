# ATV DESIGN — Deploy Render V8.7.3

## Arquitetura escolhida
A V8.7.3 continua usando FastAPI + SQLite + storage privado em disco.
Por isso o deploy precisa de um disco persistente.

O `render.yaml` cria:
- Web Service Python
- Health check `/api/health`
- disco `/var/data`
- `ATV_DATA_DIR=/var/data`
- cookies Secure
- PIN e chave Admin solicitados como secrets

## IMPORTANTE
Não coloque a pasta `data` no GitHub.
Ela contém banco e arquivos privados.

## Primeiro deploy
1. Crie um repositório PRIVADO no GitHub.
2. Envie os arquivos desta pasta, exceto `data`.
3. No Render: New > Blueprint.
4. Selecione o repositório.
5. O Render detectará `render.yaml`.
6. Informe:
   - ATV_ADMIN_PIN
   - ATV_ADMIN_KEY
7. Crie o serviço.

## Levar os dados atuais
Depois que o serviço estiver online, transfira o conteúdo da sua pasta local `data`
para o disco `/var/data` usando o Shell do Render e um método seguro de transferência.
Não envie `data` para repositório público.

Antes de substituir qualquer banco remoto, faça uma cópia do arquivo:
`/var/data/atv_homologacao.sqlite3`

## Validação
Abra:
`https://SEU-SERVICO.onrender.com/api/health`

Esperado:
- build: V8.7.3
- apiVersion: 1.7.10

Depois valide login, previews, download e Admin.

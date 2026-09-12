# ATV DESIGN V8.7.3 — TESTE GRATUITO NO RENDER

Esta variante serve apenas para homologação/visualização antes de contratar hospedagem.

## O que foi alterado
- Render Web Service: `plan: free`
- Removido o disco persistente pago.
- `ATV_DATA_DIR=/tmp/atv-data`
- Código funcional da V8.7.3 preservado.

## Limitação importante
No plano gratuito o filesystem é temporário. Banco SQLite, cadastros e uploads feitos durante o teste podem ser perdidos quando o serviço reiniciar, redeployar ou entrar em suspensão.

## Publicação
1. Suba estes arquivos para um repositório GitHub (de preferência privado).
2. No Render, escolha **New > Blueprint**.
3. Conecte o repositório.
4. O Render detectará `render.yaml`.
5. Informe os secrets solicitados:
   - `ATV_ADMIN_PIN`: exatamente 6 números.
   - `ATV_ADMIN_KEY`: exatamente 2 letras + 2 símbolos (exemplo de formato: `AB!!`).
6. Crie o serviço.
7. Ao concluir, abra a URL `https://...onrender.com`.
8. Valide `/api/health`: deve retornar build `V8.7.3` e API `1.7.10`.

## Depois do teste
Para produção, use a configuração oficial com armazenamento persistente ou migre os dados para um banco/storage permanente.

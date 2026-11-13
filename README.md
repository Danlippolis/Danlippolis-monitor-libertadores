# Monitor de ingressos – Final da Libertadores 2026

Roda no GitHub Actions a cada ~10 min. Lê páginas públicas e avisa no Telegram quando:

- o texto da página de ingressos da Conmebol ou da ElevenTickets muda;
- sai notícia nova (Google News) sobre venda, lote, carga ou revenda.

Todo dia, a partir das 9h (Brasília), manda um ping "✅ Monitor funcionando" com o status de cada fonte.
Se o ping não chegar, o monitor parou.

**Não compra nada.** Não faz login nem entra na fila. A compra é manual, em `libertadores.eleventickets.com`.
O monitor para sozinho depois de 28/11/2026.

## Configuração (uma vez)

1. **Crie o bot:** no Telegram, fale com `@BotFather`, mande `/newbot` e guarde o token.
2. **Descubra seu chat_id:** mande qualquer mensagem para o seu bot e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` no navegador. O valor está em `"chat":{"id": ...}`.
3. **Cadastre os secrets:** no repositório, vá em *Settings → Secrets and variables → Actions → New repository secret*:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. **Teste:** vá em *Actions → monitor-ingressos → Run workflow*. Na primeira execução chega
   "Monitor … ativo" e ele guarda o estado inicial. Os alertas vêm a partir da segunda.

## Rodar localmente

```bash
python monitor.py          # sem as variáveis do Telegram, só imprime (dry-run)
```

## Ajustes

- Fontes monitoradas: dicionário `PAGES` e `NEWS_QUERY` em `monitor.py`.
- Frequência: `cron` em `.github/workflows/monitor.yml`. Não use menos de 5 min.
- Se uma fonte falhar 6 vezes seguidas, chega um aviso. A ElevenTickets pode bloquear IPs de datacenter;
  nesse caso as notícias e a página da Conmebol continuam cobrindo.

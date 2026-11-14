#!/usr/bin/env python3
"""Monitor de anúncios de ingressos da final da Libertadores 2026.

Só LÊ páginas públicas, em baixa frequência, e avisa no Telegram quando algo
muda. Não faz login, não entra em fila e não compra nada — a compra é manual.

Variáveis de ambiente:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  -> se ausentes, roda em modo dry-run (só imprime)
  STATE_FILE                            -> caminho do estado (padrão: state.json)
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------- configuração
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
USER_AGENT = "libertadores-ticket-monitor/1.0 (monitor pessoal de baixa frequencia)"
TIMEOUT_S = 20
BRT = dt.timezone(dt.timedelta(hours=-3))   # Brasil sem horário de verão desde 2019
FINAL_DATE = dt.date(2026, 11, 28)          # depois disso o monitor para sozinho
HEARTBEAT_HOUR = 9                          # ping diário "estou vivo" a partir das 9h (Brasília)
FAILURE_ALERT_AFTER = 6                      # falhas seguidas antes de avisar
MAX_SEEN_NEWS = 500
EMPTY_RETRIES = 3                            # tentativas quando a página vem vazia
EMPTY_RETRY_PAUSE_S = 4
MAX_TEXT_CHARS = 20_000

# Páginas cujo TEXTO visível é comparado entre execuções.
PAGES = {
    "Conmebol – página de ingressos": "https://gol.conmebol.com/libertadores/en/ticketing/montevideo2026",
    # ElevenTickets removida: bloqueia (HTTP 403) todos os acessos vindos do GitHub Actions.
}

# Google News RSS: notícias novas sobre venda/lotes.
NEWS_QUERY = '"final da Libertadores" ingressos (venda OR lote OR carga OR revenda) when:7d'
NEWS_RSS = (
    "https://news.google.com/rss/search?q="
    + urllib.parse.quote(NEWS_QUERY)
    + "&hl=pt-BR&gl=BR&ceid=BR:pt-419"
)
NEWS_MUST_MATCH = re.compile(r"libertadores", re.I)
NEWS_KEYWORDS = re.compile(r"ingress|lote|carga|venda|revenda|entradas|esgot", re.I)


# ---------------------------------------------------------------- utilidades
class _TextExtractor(HTMLParser):
    """Extrai texto visível, ignorando script/style/noscript/svg."""

    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    lines = (re.sub(r"\s+", " ", p) for p in parser.parts)
    return "\n".join(line for line in lines if line)[:MAX_TEXT_CHARS]


MIN_VISIBLE_CHARS = 200
_JSON_STRING = re.compile(r'"((?:[^"\\]|\\.){8,400})"')
_RELEVANT = re.compile(
    r"ingress|ticket|entrada|venta|venda|sale|categor|hospital|US\$|USD|precio|preço|price|"
    r"esgot|agotad|sold.?out|disponi|available|regist|final",
    re.I,
)
_CODE_LIKE = re.compile(r"https?://|\.(js|css|png|svg|webp)\b|[{}<>;=]")


def embedded_text(html: str) -> str:
    """Fallback para páginas renderizadas por JavaScript (texto visível vazio):
    pega strings dentro do HTML/JSON embutido que pareçam texto sobre ingressos.
    Ignora URLs, código e identificadores, que mudam a cada build e gerariam alarme falso."""
    found: set[str] = set()
    for match in _JSON_STRING.finditer(html):
        raw = match.group(1)
        try:
            value = json.loads(f'"{raw}"')
        except ValueError:
            value = raw
        value = re.sub(r"\s+", " ", value).strip()
        if " " in value and _RELEVANT.search(value) and not _CODE_LIKE.search(value):
            found.add(value)
    return "\n".join(sorted(found))[:MAX_TEXT_CHARS]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "date": (item.findtext("pubDate") or "").strip(),
        })
    return items


def short_diff(old: str, new: str, max_lines: int = 12) -> str:
    added = [
        line[1:].strip()
        for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    ]
    if not added:
        return "(texto removido/reordenado, sem linhas novas)"
    out = "\n".join(f"+ {line[:200]}" for line in added[:max_lines])
    if len(added) > max_lines:
        out += f"\n… e mais {len(added) - max_lines} linhas"
    return out


def send_telegram(text: str) -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        print("[dry-run] mensagem:\n" + text + "\n")
        return
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4000],                 # limite do Telegram é 4096
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Telegram respondeu {resp.status}")
    except urllib.error.HTTPError as err:
        # A API do Telegram explica o motivo no corpo (ex.: "chat not found"); o token não aparece nele.
        try:
            detail = json.loads(err.read().decode()).get("description", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"Telegram HTTP {err.code}: {detail}") from None


# ---------------------------------------------------------------- estado
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"pages": {}, "seen_news": [], "failures": {}, "initialized": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def describe_error(err: Exception) -> str:
    """Texto curto do erro para o state.json (que é público): nunca inclui o token."""
    text = repr(err)[:300]
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    return text.replace(token, "***") if token else text


def register_failure(state: dict, source: str, err: Exception, alerts: list[str]) -> None:
    n = state["failures"].get(source, 0) + 1
    state["failures"][source] = n
    state.setdefault("last_error", {})[source] = describe_error(err)
    print(f"[erro] {source}: {describe_error(err)} (falha {n} seguida)", file=sys.stderr)
    if n == FAILURE_ALERT_AFTER:
        alerts.append(f"⚠️ {source} falhou {n} vezes seguidas ({type(err).__name__}). O monitor dessa fonte pode estar cego.")


def register_success(state: dict, source: str, alerts: list[str]) -> None:
    if state["failures"].get(source, 0) >= FAILURE_ALERT_AFTER:
        alerts.append(f"✅ {source} voltou a responder.")
    state["failures"][source] = 0
    state.setdefault("last_error", {}).pop(source, None)


# ---------------------------------------------------------------- checagens
def check_pages(state: dict, alerts: list[str], first_run: bool) -> None:
    for name, url in PAGES.items():
        try:
            # A Conmebol devolve 0 bytes em ~metade das requisições; tenta até 3 vezes, com pausa.
            for attempt in range(EMPTY_RETRIES):
                html = fetch(url)
                if html.strip():
                    break
                time.sleep(EMPTY_RETRY_PAUSE_S)
        except Exception as err:  # noqa: BLE001 — qualquer falha de rede/parse vira alerta controlado
            register_failure(state, name, err, alerts)
            continue

        text, mode = html_to_text(html), "texto visível"
        if len(text) < MIN_VISIBLE_CHARS:          # página montada por JavaScript
            text, mode = embedded_text(html), "strings embutidas"
        if not text:
            # Resposta vazia é instabilidade do site (às vezes vem 0 bytes): conta como falha
            # e NÃO sobrescreve a última versão boa, senão uma mudança no meio-tempo se perderia.
            register_failure(state, name, RuntimeError(f"resposta vazia ({len(html)} bytes)"), alerts)
            continue
        register_success(state, name, alerts)

        digest = hashlib.sha256(text.encode()).hexdigest()
        previous = state["pages"].get(name)
        state["pages"][name] = {"hash": digest, "text": text, "mode": mode, "html_bytes": len(html)}
        print(f"[info] {name}: {len(html)} bytes de HTML, {len(text)} chars via {mode}")
        # Só compara se o modo de extração for o mesmo; senão vira alarme falso na troca de versão.
        if previous and previous.get("mode", "texto visível") == mode and previous["hash"] != digest and not first_run:
            alerts.append(f"🔔 Mudou: {name}\n{url}\n\n{short_diff(previous['text'], text)}")


def check_news(state: dict, alerts: list[str], first_run: bool) -> None:
    source = "Google News"
    try:
        items = parse_rss(fetch(NEWS_RSS))
    except Exception as err:  # noqa: BLE001
        register_failure(state, source, err, alerts)
        return
    register_success(state, source, alerts)

    seen = set(state["seen_news"])
    new_items = [
        it for it in items
        if it["link"] and it["link"] not in seen
        and NEWS_MUST_MATCH.search(it["title"]) and NEWS_KEYWORDS.search(it["title"])
    ]
    for it in items:
        if it["link"] and it["link"] not in seen:
            state["seen_news"].append(it["link"])
            seen.add(it["link"])
    state["seen_news"] = state["seen_news"][-MAX_SEEN_NEWS:]

    if new_items and not first_run:
        lines = [f"• {it['title']}\n  {it['link']}" for it in new_items[:8]]
        alerts.append("📰 Notícias novas sobre ingressos:\n\n" + "\n\n".join(lines))


def daily_heartbeat(state: dict, now: dt.datetime) -> str | None:
    """Uma vez por dia, a partir das HEARTBEAT_HOUR (horário de Brasília), manda um resumo.
    Vai na primeira execução depois das 9h; com os atrasos do GitHub, chega entre 9h e ~9h30."""
    today = now.date().isoformat()
    if now.hour < HEARTBEAT_HOUR or state.get("last_heartbeat") == today:
        return None
    state["last_heartbeat"] = today
    days_left = (FINAL_DATE - now.date()).days
    lines = [f"✅ Monitor funcionando — {now:%d/%m %H:%M}. Faltam {days_left} dia(s) para a final."]
    for source in [*PAGES, "Google News"]:
        n = state["failures"].get(source, 0)
        if n:
            lines.append(f"• {source}: falhando ({n}x seguidas) – {state.get('last_error', {}).get(source, '?')}")
        else:
            lines.append(f"• {source}: ok")
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main() -> int:
    now = dt.datetime.now(BRT)
    today = now.date()
    if today > FINAL_DATE:
        print("Final já passou; nada a fazer.")
        return 0

    state = load_state()
    first_run = not state.get("initialized")
    alerts: list[str] = []

    check_pages(state, alerts, first_run)
    check_news(state, alerts, first_run)

    if first_run:
        alerts.insert(0, "🤖 Monitor de ingressos da final da Libertadores ativo. Vou avisar quando as páginas oficiais mudarem ou sair notícia de novo lote.")
        state["initialized"] = True
        state["last_heartbeat"] = today.isoformat()   # a mensagem acima já serve de ping de hoje
    else:
        ping = daily_heartbeat(state, now)
        if ping:
            alerts.insert(0, ping)

    save_state(state)  # salva antes de notificar: falha no Telegram não gera alerta duplicado depois
    for msg in alerts:
        try:
            send_telegram(msg)
            state.setdefault("last_error", {}).pop("Telegram", None)
        except Exception as err:  # noqa: BLE001
            state.setdefault("last_error", {})["Telegram"] = describe_error(err)
            print(f"[erro] Telegram: {describe_error(err)}", file=sys.stderr)
    if alerts:
        save_state(state)  # registra o resultado do envio (diagnóstico pelo state.json)
    print(f"OK — {len(alerts)} alerta(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

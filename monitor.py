#!/usr/bin/env python3
"""
Monitor do PizzINT (pizzint.watch) com alertas no Telegram.

Roda uma verificação por execução (feito para ser chamado a cada 5 minutos
por um agendador). Guarda o último estado em state.json para não repetir
alertas.

Variáveis de ambiente:
  TELEGRAM_BOT_TOKEN  token do bot (criado no @BotFather)
  TELEGRAM_CHAT_ID    seu ID de chat no Telegram

Opções:
  --dry-run   não envia nada, só mostra o que enviaria
  --test      envia uma mensagem de teste e sai
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://www.pizzint.watch/api/dashboard-data"
STATE_FILE = Path(__file__).with_name("state.json")
USER_AGENT = "pizzint-monitor/1.0 (alerta pessoal)"

# Regras de alerta (ajuste aqui se quiser mais ou menos sensível)
URGENT_DEFCON = 3        # DOUGHCON <= 3 é urgente
ATTENTION_DEFCON = 4     # DOUGHCON <= 4 é atenção
URGENT_SPIKES = 2        # 2 ou mais pizzarias em pico ao mesmo tempo é urgente
FAILS_BEFORE_WARNING = 3 # avisa se o site falhar 3 vezes seguidas

# Palavras para checar se algo realmente aconteceu nas notícias (últimas 3h)
NEWS_QUERY = (
    '(Pentagon OR "US strike" OR airstrike OR "military operation" '
    'OR "US forces" OR "missile strike") when:3h'
)

DRY_RUN = "--dry-run" in sys.argv


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"defcon": 5, "spiking": [], "fails": 0, "warned_down": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def send_telegram(text):
    if DRY_RUN:
        print("--- MENSAGEM (dry-run) ---")
        print(text)
        print("--------------------------")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("Faltam TELEGRAM_BOT_TOKEN e/ou TELEGRAM_CHAT_ID.")
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read()


def fetch_dashboard():
    # tenta 2 vezes antes de desistir
    last_err = None
    for _ in range(2):
        try:
            payload = json.loads(http_get(API_URL))
            if payload.get("success"):
                return payload
            last_err = "resposta sem success=true"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(3)
    raise RuntimeError(last_err)


def recent_headlines(limit=4):
    """Busca manchetes das últimas 3h para ajudar a confirmar se algo aconteceu."""
    try:
        q = urllib.parse.quote(NEWS_QUERY)
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        root = ET.fromstring(http_get(url))
        items = []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            if title:
                items.append("• " + title)
            if len(items) >= limit:
                break
        return items
    except Exception:  # noqa: BLE001
        return []


def news_block():
    heads = recent_headlines()
    if heads:
        return "\n\n📰 Manchetes das últimas 3h (possível confirmação):\n" + "\n".join(heads)
    return "\n\n📰 Nenhuma manchete relevante encontrada nas últimas 3h (ainda sem confirmação)."


def describe_places(places):
    lines = []
    for p in places:
        if p.get("is_spike"):
            pct = p.get("percentage_of_usual")
            extra = f" ({pct}% do normal)" if pct is not None else ""
            lines.append(f"• {p.get('name')}{extra}")
    return "\n".join(lines)


def main():
    if "--test" in sys.argv:
        send_telegram("✅ Teste: o monitor do PizzINT está conectado ao seu Telegram.")
        return

    state = load_state()
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    try:
        payload = fetch_dashboard()
    except Exception as e:  # noqa: BLE001
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] >= FAILS_BEFORE_WARNING and not state.get("warned_down"):
            send_telegram(
                f"⚠️ O monitor não consegue ler o pizzint.watch há {state['fails']} "
                f"verificações seguidas ({e}). Você pode estar sem cobertura."
            )
            state["warned_down"] = True
        save_state(state)
        print("Falha ao buscar dados:", e)
        return

    if state.get("warned_down"):
        send_telegram("✅ O monitor voltou a ler o pizzint.watch normalmente.")
    state["fails"] = 0
    state["warned_down"] = False

    level = int(payload.get("defcon_level", 5))
    places = payload.get("data", [])
    spiking_now = sorted(p["name"] for p in places if p.get("is_spike"))
    spikes_count = len(spiking_now)
    prev_level = int(state.get("defcon", 5))
    prev_spiking = set(state.get("spiking", []))
    new_spikes = [n for n in spiking_now if n not in prev_spiking]

    messages = []

    # 1) Mudança de DOUGHCON
    if level != prev_level:
        if level <= URGENT_DEFCON:
            messages.append(
                f"🚨 URGENTE — DOUGHCON {level} (antes: {prev_level}) às {now}.\n"
                "Nível de prontidão elevado no índice." + news_block()
            )
        elif level <= ATTENTION_DEFCON:
            messages.append(
                f"⚠️ ATENÇÃO — DOUGHCON {level} (antes: {prev_level}) às {now}."
                + news_block()
            )
        elif prev_level < 5 and level == 5:
            messages.append(f"🟢 Normalizado — DOUGHCON voltou a {level} às {now}.")

    # 2) Pizzarias com pico
    if new_spikes:
        detail = describe_places([p for p in places if p["name"] in new_spikes])
        if spikes_count >= URGENT_SPIKES:
            messages.append(
                f"🚨 URGENTE — {spikes_count} pizzarias em pico ao mesmo tempo ({now}):\n"
                f"{describe_places([p for p in places if p.get('is_spike')])}"
                + news_block()
            )
        else:
            messages.append(
                f"⚠️ ATENÇÃO — pico de movimento detectado ({now}):\n{detail}"
                + news_block()
            )

    # 3) Eventos listados pelo site
    for ev in payload.get("events", []) or []:
        key = json.dumps(ev, sort_keys=True)
        if key not in state.setdefault("events_seen", []):
            state["events_seen"].append(key)
            state["events_seen"] = state["events_seen"][-50:]
            messages.append(f"📍 Evento no PizzINT ({now}): {json.dumps(ev, ensure_ascii=False)[:500]}")

    for m in messages:
        send_telegram(m)

    state["defcon"] = level
    state["spiking"] = spiking_now
    save_state(state)
    print(f"{now} | DOUGHCON {level} | picos: {spikes_count} | alertas enviados: {len(messages)}")


if __name__ == "__main__":
    main()

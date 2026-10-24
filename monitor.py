"""Hlídač úroků spořicích účtů.

Použití:
  python monitor.py --test   # vypíše, co se z každé stránky přečetlo; nic neukládá ani neposílá
  python monitor.py          # ostrý běh: porovná s rates.json, při změně pošle e-mail
"""
import json, os, re, smtplib, sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
BANKS_FILE = ROOT / "banks.json"
STATE_FILE = ROOT / "rates.json"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
MAX_CHARS = 20000
FAIL_ALERT_AFTER = 3  # po kolika dnech v řadě upozornit na nefunkční banku
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SporiciAgent/1.0)"}

PROMPT = """Z textu webové stránky banky vyber všechny SPOŘICÍ produkty pro fyzické osoby v Kč
(spořicí účty, spořicí vklady; termínované vklady vynech).
Vrať POUZE JSON pole, bez dalšího textu a bez markdownu. Každá položka:
{"product": "název produktu",
 "base_rate_pct": číslo nebo null,   // základní úrok p.a. v %
 "bonus_rate_pct": číslo nebo null,  // maximální bonusový/celkový úrok p.a. v %
 "limit_czk": číslo nebo null,       // do jaké částky platí nejvyšší sazba
 "conditions": "stručné podmínky získání bonusu, max 150 znaků"}
Nic si nedomýšlej. Když údaj na stránce není, dej null. Když žádný spořicí produkt nenajdeš, vrať []."""


def fetch_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:MAX_CHARS]


def extract_rates(client: Anthropic, bank: str, text: str) -> list[dict]:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": f"{PROMPT}\n\nBanka: {bank}\n\nText stránky:\n{text}"}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("Odpověď modelu neobsahuje JSON pole")
    return json.loads(raw[start : end + 1])


def diff(old: list[dict], new: list[dict]) -> list[str]:
    o = {p["product"]: p for p in old}
    n = {p["product"]: p for p in new}
    out = []
    for name, p in n.items():
        if name not in o:
            out.append(f"NOVÝ produkt „{name}“: základ {p.get('base_rate_pct')} %, bonus {p.get('bonus_rate_pct')} %")
            continue
        for key, label in (("base_rate_pct", "základní sazba"), ("bonus_rate_pct", "bonusová sazba"), ("limit_czk", "limit")):
            if o[name].get(key) != p.get(key):
                out.append(f"„{name}“ – {label}: {o[name].get(key)} → {p.get(key)}")
    for name in o:
        if name not in n:
            out.append(f"Produkt „{name}“ zmizel ze stránky")
    return out


def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["SMTP_USER"], os.environ["MAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def main() -> None:
    test = "--test" in sys.argv
    banks = json.loads(BANKS_FILE.read_text(encoding="utf-8"))
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"banks": {}}
    first_run = not state["banks"]
    client = Anthropic()
    changes, alerts = [], []

    for b in banks:
        name = b["name"]
        entry = state["banks"].setdefault(name, {"products": [], "fail_count": 0})
        try:
            products = extract_rates(client, name, fetch_text(b["url"]))
            if test:
                print(f"\n=== {name} ===\n{json.dumps(products, ensure_ascii=False, indent=2)}")
                continue
            if not products:
                raise ValueError("Nenalezen žádný produkt (stránka se asi změnila nebo se načítá přes JavaScript)")
            if entry["products"]:
                changes += [f"[{name}] {c}" for c in diff(entry["products"], products)]
            entry["products"], entry["fail_count"] = products, 0
        except Exception as e:  # noqa: BLE001
            print(f"CHYBA {name}: {e}", file=sys.stderr)
            if test:
                continue
            entry["fail_count"] += 1
            if entry["fail_count"] == FAIL_ALERT_AFTER:
                alerts.append(f"[{name}] nejde přečíst už {FAIL_ALERT_AFTER} dny v řadě: {e}")

    if test:
        return

    state["last_run"] = date.today().isoformat()  # denní změna souboru = commit = repo zůstane aktivní
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    if first_run:
        print("První běh – uložen výchozí stav, e-mail se neposílá.")
    elif changes or alerts:
        body = "Změny sazeb:\n" + "\n".join(f"• {c}" for c in changes) if changes else ""
        if alerts:
            body += "\n\nProblémy:\n" + "\n".join(f"• {a}" for a in alerts)
        send_email(f"Spořicí účty: {len(changes)} změn sazeb", body.strip())
        print(body)
    else:
        print("Beze změn.")


if __name__ == "__main__":
    main()
  

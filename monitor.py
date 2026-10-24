"""Hlídač úroků spořicích účtů.

Použití:
  python monitor.py --test   # vypíše, co se z každé stránky přečetlo; nic neukládá ani neposílá
  python monitor.py          # ostrý běh: porovná s rates.json, při změně pošle e-mail

banks.json: pole objektů
  name   – název banky
  url    – stránka nebo PDF se sazbami
  focus  – co přesně sledovat (produkt, pásma ...)
  role   – "mine" (tvoje spoření, hlídá se každá změna)
           "candidate" (kandidát; upozorní se, když předběhne tvoje nejlepší sazbu)
  model  – volitelně jiný model Claude pro tuto banku

Výstupy: rates.json (aktuální stav), history.json (historie a změny),
         docs/index.html (dashboard pro GitHub Pages, šablona dashboard_template.html)
E-mail se posílá jen když jsou nastavené secrets SMTP_USER, SMTP_PASSWORD a MAIL_TO.
"""
import io, json, os, re, smtplib, sys
from difflib import SequenceMatcher
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).parent
BANKS_FILE = ROOT / "banks.json"
STATE_FILE = ROOT / "rates.json"
HISTORY_FILE = ROOT / "history.json"
TEMPLATE_FILE = ROOT / "dashboard_template.html"
DOCS_DIR = ROOT / "docs"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
MAX_CHARS = 30000
FAIL_ALERT_AFTER = 3  # po kolika dnech v řadě upozornit na nefunkční banku
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SporiciAgent/1.0)"}

PROMPT = """Z textu webové stránky banky vyber SPOŘICÍ produkty pro fyzické osoby v Kč.
ZAMĚŘ SE POUZE NA: {FOCUS}
Ostatní produkty vynech (včetně termínovaných vkladů, dětských a podnikatelských účtů, pokud nejsou ve zaměření).
Vrať POUZE JSON pole, bez dalšího textu a bez markdownu. Každá položka je jedno pásmo/varianta produktu:
{"product": "název produktu",
 "base_rate_pct": číslo nebo null,   // základní úrok p.a. v %
 "bonus_rate_pct": číslo nebo null,  // celková sazba p.a. v % při splnění podmínek (nejvyšší pro dané pásmo)
 "limit_czk": číslo nebo null,       // horní hranice zůstatku, do které sazba platí
 "conditions": "stručné podmínky získání sazby, max 150 znaků"}
Název produktu drž krátký a stabilní: bez data platnosti a bez čísel sazeb (data patří do "conditions").
Pokud je stejný produkt ve více pásmech nebo variantách, odliš je krátkým dovětkem v názvu, např. "(do 250 tis.)" nebo "(nové smlouvy)".
Nic si nedomýšlej. Když údaj na stránce není, dej null. Když nic z požadovaného nenajdeš, vrať []."""


def pdf_text(data: bytes) -> str:
    """Text z PDF se zachovaným rozložením tabulek (sloupce oddělené ' | ')."""
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            t = page.extract_text(extraction_mode="layout")
        except TypeError:  # starší pypdf
            t = page.extract_text()
        pages.append(t or "")
    lines = []
    for line in "\n".join(pages).splitlines():
        line = re.sub(r" {2,}", " | ", line.strip())
        if line:
            lines.append(line)
    return "\n".join(lines)


def fetch_text(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf") or r.content[:4] == b"%PDF":
        return pdf_text(r.content)[:MAX_CHARS]
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:MAX_CHARS]


def extract_rates(client: Anthropic, bank: dict, text: str) -> list[dict]:
    prompt = PROMPT.replace("{FOCUS}", bank.get("focus") or "všechny spořicí účty pro fyzické osoby")
    content = f"{prompt}\n\nBanka: {bank['name']}\n\nText stránky:\n{text}"
    last = ""
    for attempt in range(2):
        extra = "" if attempt == 0 else "\n\nPOZOR: odpověz výhradně JSON polem, které začíná znakem [ a nemá žádný další text."
        msg = client.messages.create(
            model=bank.get("model", MODEL),
            max_tokens=2500,
            messages=[{"role": "user", "content": content + extra}],
        )
        raw = "".join(b.text for b in msg.content if b.type == "text")
        start, end = raw.find("["), raw.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        last = raw
    raise ValueError("Odpověď modelu není platný JSON: " + re.sub(r"\s+", " ", last)[:110])


def num(v) -> str:
    return "–" if v is None else f"{v:g}".replace(".", ",")


def effective_rate(p: dict):
    """Sazba, která se bere pro srovnání: bonusová/celková, jinak základní."""
    return p.get("bonus_rate_pct") if p.get("bonus_rate_pct") is not None else p.get("base_rate_pct")


def best_rate(products: list[dict]):
    rated = [(effective_rate(p), p) for p in products if effective_rate(p) is not None]
    return max(rated, key=lambda x: x[0]) if rated else (None, None)


def _numbers(p: dict):
    return (effective_rate(p), p.get("limit_czk"))


def diff(old: list[dict], new: list[dict]) -> list[str]:
    """Porovná produkty dvou čtení. Názvy od modelu mírně kolísají a sazba se někdy přesune
    mezi polem základní/celková, proto se produkty párují podle podobnosti názvu a shody čísel
    a porovnává se účinná sazba (celková, jinak základní)."""
    pairs = []
    for i, o in enumerate(old):
        for j, n in enumerate(new):
            score = SequenceMatcher(None, o["product"].lower(), n["product"].lower()).ratio()
            if _numbers(o) == _numbers(n):
                score += 0.5
            pairs.append((score, i, j))
    pairs.sort(reverse=True)
    used_o, used_n, matched = set(), set(), []
    for score, i, j in pairs:
        if score >= 0.6 and i not in used_o and j not in used_n:
            used_o.add(i)
            used_n.add(j)
            matched.append((i, j))
    out = []
    for i, j in matched:
        o, n = old[i], new[j]
        if effective_rate(o) != effective_rate(n):
            out.append(f"„{n['product']}“ – sazba: {num(effective_rate(o))} → {num(effective_rate(n))}")
        if o.get("limit_czk") is not None and n.get("limit_czk") is not None and o["limit_czk"] != n["limit_czk"]:
            out.append(f"„{n['product']}“ – limit: {num(o['limit_czk'])} → {num(n['limit_czk'])}")
    for j, n in enumerate(new):
        if j not in used_n:
            out.append(f"NOVÝ produkt/pásmo „{n['product']}“: sazba {num(effective_rate(n))} %")
    for i, o in enumerate(old):
        if i not in used_o:
            out.append(f"Produkt/pásmo „{o['product']}“ zmizelo ze stránky")
    return out


def send_email(subject: str, body: str) -> None:
    if not all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "MAIL_TO")):
        print("E-mail se neposílá (nejsou nastavené SMTP secrets).")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["SMTP_USER"], os.environ["MAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)


def load_history() -> dict:
    try:
        h = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else {}
    except json.JSONDecodeError:
        h = {}
    h.setdefault("snapshots", [])
    h.setdefault("events", [])
    return h


def update_history(history: dict, banks: list, state: dict, today: str, changes: list, overtakes: list, alerts: list) -> None:
    snap = {"date": today, "banks": {}}
    for b in banks:
        rate, _ = best_rate(state["banks"][b["name"]]["products"])
        snap["banks"][b["name"]] = rate
    history["snapshots"] = ([x for x in history["snapshots"] if x["date"] != today] + [snap])[-800:]
    for kind, items in (("change", changes), ("overtake", overtakes), ("problem", alerts)):
        history["events"] += [{"date": today, "kind": kind, "text": t} for t in items]
    history["events"] = history["events"][-200:]


def render_dashboard(banks: list, state: dict, history: dict, today: str) -> None:
    if not TEMPLATE_FILE.exists():
        print("dashboard_template.html chybí – dashboard se nevytváří.", file=sys.stderr)
        return
    out = []
    for b in banks:
        e = state["banks"].get(b["name"], {})
        products = e.get("products", [])
        rate, prod = best_rate(products)
        out.append({
            "name": b["name"],
            "role": b.get("role", "mine"),
            "thirdParty": "finex.cz" in b["url"] or "usetreno.cz" in b["url"],
            "best": {"rate": rate, "product": prod["product"]} if rate is not None else None,
            "products": products,
            "failCount": e.get("fail_count", 0),
            "lastOk": e.get("last_ok"),
            "lastError": e.get("last_error"),
        })
    data = {"updated": today, "banks": out, "snapshots": history["snapshots"], "events": history["events"]}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE_FILE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    (DOCS_DIR / ".nojekyll").touch()


def main() -> None:
    test = "--test" in sys.argv
    today = date.today().isoformat()
    banks = json.loads(BANKS_FILE.read_text(encoding="utf-8"))
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"banks": {}}
    except json.JSONDecodeError:
        print("rates.json je poškozený – začínám od nuly.", file=sys.stderr)
        state = {"banks": {}}
    first_run = not state["banks"]
    client = Anthropic()
    changes, alerts, overtakes = [], [], []
    mine_changed = False

    for b in banks:
        name, role = b["name"], b.get("role", "mine")
        entry = state["banks"].setdefault(name, {"products": [], "fail_count": 0})
        try:
            products = extract_rates(client, b, fetch_text(b["url"]))
            if test:
                rate, top = best_rate(products)
                print(f"\n=== {name} ({role}) – nejvyšší sazba: {rate} ===\n{json.dumps(products, ensure_ascii=False, indent=2)}")
                continue
            if not products:
                raise ValueError("Nenalezen žádný produkt (stránka se asi změnila nebo se načítá přes JavaScript)")
            if all(p.get("base_rate_pct") is None and p.get("bonus_rate_pct") is None for p in products):
                raise ValueError("Na stránce nejsou uvedeny žádné sazby – je potřeba jiná adresa (např. PDF s přehledem sazeb)")
            found = diff(entry["products"], products) if entry["products"] else []
            pending = entry.get("pending")
            if found and pending is not None and not diff(pending, products):
                # stejná změna při dvou čteních po sobě = potvrzená
                changes += [f"[{name}] {c}" for c in found]
                mine_changed = mine_changed or role == "mine"
                entry["products"], entry["pending"] = products, None
            elif found:
                entry["pending"] = products  # čeká na potvrzení při dalším běhu, základ se nemění
            else:
                entry["products"], entry["pending"] = products, None
            entry["fail_count"] = 0
            entry["last_ok"], entry["last_error"] = today, None
        except Exception as e:  # noqa: BLE001
            print(f"CHYBA {name}: {e}", file=sys.stderr)
            if test:
                continue
            entry["fail_count"] += 1
            entry["last_error"] = str(e)[:160]
            if entry["fail_count"] == FAIL_ALERT_AFTER:
                alerts.append(f"[{name}] nejde přečíst už {FAIL_ALERT_AFTER} dny v řadě: {e}")

    if test:
        return

    # Kandidát předběhl tvoje nejlepší spoření?
    mine_best = max(
        ((best_rate(state["banks"][b["name"]]["products"])[0], b["name"]) for b in banks
         if b.get("role", "mine") == "mine" and b["name"] in state["banks"]
         and best_rate(state["banks"][b["name"]]["products"])[0] is not None),
        default=(None, None),
    )
    seen = state.setdefault("overtake", {})
    for b in banks:
        if b.get("role", "mine") != "candidate":
            continue
        rate, prod = best_rate(state["banks"][b["name"]]["products"])
        if rate is not None and mine_best[0] is not None and rate > mine_best[0]:
            if seen.get(b["name"]) != rate:
                overtakes.append(
                    f"[{b['name']}] „{prod['product']}“ nabízí {num(rate)} % ({'limit ' + num(prod.get('limit_czk')) + ' Kč' if prod.get('limit_czk') else 'bez uvedeného limitu'}; "
                    f"podmínky: {prod.get('conditions')}). Tvoje nejlepší je {mine_best[1]} s {num(mine_best[0])} %."
                )
                seen[b["name"]] = rate
        else:
            seen.pop(b["name"], None)

    state["last_run"] = today  # denní změna souboru = commit = repo zůstane aktivní
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    history = load_history()
    update_history(history, banks, state, today, [] if first_run else changes, [] if first_run else overtakes, alerts)
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    render_dashboard(banks, state, history, today)

    if first_run:
        print("První běh – uložen výchozí stav, e-mail se neposílá.")
        return
    parts = []
    if changes:
        parts.append("Změny sazeb:\n" + "\n".join(f"• {c}" for c in changes))
        if mine_changed:
            parts.append("Tip: u tvých bank uprav odpovídající řádek (sazbaPA) v dashboardu.")
    if overtakes:
        parts.append("Kandidát předběhl tvoje nejlepší spoření (porovnání podle nejvyšší sazby, zkontroluj limit a podmínky):\n"
                     + "\n".join(f"• {o}" for o in overtakes))
    if alerts:
        parts.append("Problémy:\n" + "\n".join(f"• {a}" for a in alerts))
    if parts:
        body = "\n\n".join(parts)
        send_email(f"Spořicí účty: {len(changes)} změn, {len(overtakes)} předběhnutí", body)
        print(body)
    else:
        print("Beze změn.")


if __name__ == "__main__":
    main()

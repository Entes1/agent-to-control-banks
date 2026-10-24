"""Hlídač úroků spořicích účtů.

Použití:
  python monitor.py --test   # vypíše, co se z každé stránky přečetlo; nic neukládá ani neposílá
  python monitor.py          # ostrý běh: porovná s rates.json, při změně pošle e-mail
"""
import io, json, os, re, smtplib, sys
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
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
MAX_CHARS = 30000
FAIL_ALERT_AFTER = 3  # po kolika dnech v řadě upozornit na nefunkční banku
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SporiciAgent/1.0)"}

PROMPT = """Z textu webové stránky banky vyber všechny SPOŘICÍ produkty pro fyzické osoby v Kč
(spořicí účty, spořicí vklady; termínované vklady vynech).
Vrať POUZE JSON pole, bez dalšího textu a bez markdownu. Každá položka:
{"product": "název produktu",
 "base_rate_pct": číslo nebo null,   // základní úrok p.a. v %
 "bonus_rate_pct": číslo nebo null,  // maximální bonusový/celkový úrok p.a. v %
 "limit_czk": číslo nebo null,       // do jaké částky platí nejvyšší sazba

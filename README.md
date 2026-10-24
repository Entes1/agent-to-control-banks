# Hlídač úroků spořicích účtů

## Nasazení
1. Vytvoř **soukromý** repozitář na GitHubu a nahraj sem všechny soubory
   (včetně složky `.github/workflows/`).
2. Settings → Secrets and variables → Actions → New repository secret:
   - `ANTHROPIC_API_KEY` – klíč z console.anthropic.com
   - `SMTP_USER` – e-mail odesílatele (např. Gmail)
   - `SMTP_PASSWORD` – **heslo aplikace** (Gmail: Účet Google → Zabezpečení → Hesla aplikací)
   - `MAIL_TO` – kam mají chodit upozornění
3. Actions → „Hlidac urokovych sazeb“ → Run workflow. První běh jen uloží výchozí stav.
4. Od dalšího dne běží automaticky a e-mail přijde jen při změně.

## Ověření adres bank
Spusť lokálně `python monitor.py --test` (s nastaveným `ANTHROPIC_API_KEY`).
U banky, která vrátí `[]` nebo chybu, oprav URL v `banks.json`
(často je potřeba přesnější adresa stránky konkrétního spořicího účtu).

## Přidání banky
Nový řádek v `banks.json`: `{"name": "...", "url": "..."}`.

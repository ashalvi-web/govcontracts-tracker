import requests
import time
import json
import os
import re
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

# ============================================================
# הגדרות
# ============================================================
TELEGRAM_TOKEN    = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID  = os.environ['TELEGRAM_CHAT_ID']
GOOGLE_SHEET_ID   = os.environ['GOOGLE_SHEET_ID']
GOOGLE_CREDS_JSON = os.environ['GOOGLE_CREDS_JSON']

MIN_CONTRACT_AMOUNT = 50_000_000
MIN_PCT_REVENUE     = 5.0
MIN_PCT_MKTCAP      = 3.0
DAYS_BACK           = 1

# ============================================================
# Telegram
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        return r.status_code == 200
    except:
        return False

# ============================================================
# Google Sheet
# ============================================================
def save_to_sheet(rows):
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(GOOGLE_SHEET_ID).get_worksheet(0)
        ws.append_rows(rows)
        print(f"✅ {len(rows)} שורות נשמרו")
    except Exception as e:
        print(f"⚠️ שגיאת Sheet: {e}")

# ============================================================
# זיהוי טיקר אוטומטי
# ============================================================
def find_ticker(company_name):
    clean = company_name.upper()
    for word in [",", ".", "LLC", "INC", "CORP", "CO",
                 "LTD", "LP", "THE ", "&", "  "]:
        clean = clean.replace(word, " ")
    clean = clean.strip()
    try:
        url = (f"https://query2.finance.yahoo.com/v1/finance/search"
               f"?q={requests.utils.quote(clean)}&limit=5&type=equity")
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        if r.status_code != 200:
            return None
        quotes = r.json().get('quotes', [])
        for q in quotes:
            if q.get('exchange') in ['NMS', 'NYQ', 'NGM', 'ASE', 'PCX']:
                return q.get('symbol')
    except:
        pass
    return None

# ============================================================
# מידע פיננסי
# ============================================================
def get_financials(ticker):
    if not ticker:
        return None, None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get('totalRevenue', 0) or 0, info.get('marketCap', 0) or 0
    except:
        return None, None

# ============================================================
# האם החוזה מעניין?
# ============================================================
def is_interesting(amount, revenue, market_cap):
    if revenue and revenue > 0:
        pct = (amount / revenue) * 100
        if pct >= MIN_PCT_REVENUE:
            return True, f"{pct:.1f}% מהמחזור השנתי"
    if market_cap and market_cap > 0:
        pct = (amount / market_cap) * 100
        if pct >= MIN_PCT_MKTCAP:
            return True, f"{pct:.1f}% משווי השוק"
    return False, None

# ============================================================
# מקור 1: USAspending API
# ============================================================
def fetch_usaspending():
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=DAYS_BACK + 3)).strftime("%Y-%m-%d")
    payload = {
        "filters": {
            "time_period": [{"start_date": start_date, "end_date": end_date}],
            "award_type_codes": ["A", "B", "C", "D"],
            "award_amounts": [{"lower_bound": MIN_CONTRACT_AMOUNT,
                               "upper_bound": 10_000_000_000}],
            "agencies": [
                {"type": "awarding", "tier": "toptier", "name": "Department of Defense"},
                {"type": "awarding", "tier": "toptier", "name": "National Aeronautics and Space Administration"},
                {"type": "awarding", "tier": "toptier", "name": "Department of Homeland Security"},
                {"type": "awarding", "tier": "toptier", "name": "Department of Energy"}
            ]
        },
        "fields": ["Award ID","Recipient Name","Award Amount",
                   "Start Date","Awarding Agency","Description"],
        "sort": "Award Amount", "order": "desc", "limit": 100, "page": 1
    }
    try:
        r = requests.post(
            "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            json=payload, timeout=30
        )
        if r.status_code == 200:
            results = r.json().get('results', [])
            print(f"✅ USAspending: {len(results)} חוזים")
            return [{"source": "USAspending",
                     "name": c.get("Recipient Name",""),
                     "amount": float(c.get("Award Amount",0) or 0),
                     "desc": str(c.get("Description",""))[:250],
                     "agency": str(c.get("Awarding Agency","")),
                     "id": str(c.get("Award ID","")),
                     "date": str(c.get("Start Date",""))} for c in results]
    except Exception as e:
        print(f"❌ USAspending שגיאה: {e}")
    return []

# ============================================================
# מקור 2: defense.gov/contracts (הכי מהיר!)
# ============================================================
def fetch_defense_gov():
    contracts = []
    for i in range(DAYS_BACK + 1):
        date = datetime.now() - timedelta(days=i)
        url = f"https://www.defense.gov/News/Contracts/Date/{date.strftime('%Y/%m/%d')}/"
        try:
            r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                continue
            # פרסור paragraphs
            paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', r.text, re.DOTALL)
            for p in paragraphs:
                text = re.sub('<[^<]+?>', '', p).strip()
                if len(text) < 100:
                    continue
                # חיפוש סכום בטקסט ($X million / $X,XXX,XXX)
                amounts = re.findall(
                    r'\$[\d,]+(?:\.\d+)?\s*(?:million|billion)?', text, re.I
                )
                amount = 0
                for a in amounts:
                    num = re.sub(r'[^\d.]', '', a.split('$')[1])
                    try:
                        val = float(num)
                        if 'billion' in a.lower():
                            val *= 1_000_000_000
                        elif 'million' in a.lower():
                            val *= 1_000_000
                        if val > amount:
                            amount = val
                    except:
                        pass
                if amount < MIN_CONTRACT_AMOUNT:
                    continue
                # חיפוש שם חברה (בדרך כלל לפני הפסיק הראשון)
                company = text.split(',')[0].strip()[:80]
                contracts.append({
                    "source": "defense.gov",
                    "name": company,
                    "amount": amount,
                    "desc": text[:250],
                    "agency": "Department of Defense",
                    "id": f"DOD-{date.strftime('%Y%m%d')}-{len(contracts)}",
                    "date": date.strftime("%Y-%m-%d")
                })
        except Exception as e:
            print(f"⚠️ defense.gov שגיאה ל-{date.strftime('%Y-%m-%d')}: {e}")
    print(f"✅ defense.gov: {len(contracts)} חוזים")
    return contracts

# ============================================================
# מקור 3: FPDS (Federal Procurement Data System)
# ============================================================
def fetch_fpds():
    contracts = []
    try:
        end_date   = datetime.now().strftime("%Y/%m/%d")
        start_date = (datetime.now() - timedelta(days=DAYS_BACK+2)).strftime("%Y/%m/%d")
        url = (
            f"https://www.fpds.gov/ezsearch/fpdsportal?q="
            f"SIGNED_DATE:[{start_date},{end_date}]"
            f"+DOLLARS_OBLIGATED:[{MIN_CONTRACT_AMOUNT},]"
            f"&rss=1&start=0&end=50"
        )
        r = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            items = re.findall(r'<item>(.*?)</item>', r.text, re.DOTALL)
            for item in items:
                title = re.search(r'<title>(.*?)</title>', item)
                desc  = re.search(r'<description>(.*?)</description>', item)
                title_text = re.sub('<[^<]+?>', '', title.group(1) if title else '')
                desc_text  = re.sub('<[^<]+?>', '', desc.group(1)  if desc  else '')

                amounts = re.findall(r'\$([\d,]+(?:\.\d+)?)', title_text + desc_text)
                amount = 0
                for a in amounts:
                    try:
                        val = float(a.replace(',',''))
                        if val > amount:
                            amount = val
                    except:
                        pass
                if amount < MIN_CONTRACT_AMOUNT:
                    continue
                company = title_text.split('--')[0].strip()[:80] if '--' in title_text else title_text[:80]
                contracts.append({
                    "source": "FPDS",
                    "name": company,
                    "amount": amount,
                    "desc": desc_text[:250],
                    "agency": "Department of Defense",
                    "id": f"FPDS-{len(contracts)}",
                    "date": datetime.now().strftime("%Y-%m-%d")
                })
    except Exception as e:
        print(f"⚠️ FPDS שגיאה: {e}")
    print(f"✅ FPDS: {len(contracts)} חוזים")
    return contracts

# ============================================================
# ניתוח וסינון
# ============================================================
def analyze(contracts):
    seen = set()
    alerts, sheet_rows = [], []

    for c in contracts:
        name   = c['name']
        amount = c['amount']

        # מניעת כפילויות
        key = f"{name[:30]}_{int(amount/1e6)}"
        if key in seen:
            continue
        seen.add(key)

        if amount < MIN_CONTRACT_AMOUNT:
            continue

        print(f"  🔍 [{c['source']}] {name} — ${amount:,.0f}")

        ticker = find_ticker(name)
        time.sleep(0.5)

        if not ticker:
            print(f"     ❌ לא בורסאית")
            continue

        revenue, market_cap = get_financials(ticker)
        interesting, reason = is_interesting(amount, revenue, market_cap)

        if not interesting:
            print(f"     ⚠️ {ticker} — לא מהותי")
            sheet_rows.append([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                name, ticker, amount, c['agency'], c['date'],
                c['desc'], c['id'], "", market_cap or "", revenue or "",
                "לא מהותי", c['source']
            ])
            continue

        print(f"     ✅ {ticker} — {reason} — שולח התראה!")

        pct_rev = round((amount/revenue*100), 1) if revenue else None
        pct_mc  = round((amount/market_cap*100), 1) if market_cap else None

        source_emoji = {"USAspending": "🏛️", "defense.gov": "⚡", "FPDS": "📋"}.get(c['source'], "📄")

        msg = (
            f"🚨 <b>חוזה ממשלתי מהותי!</b> {source_emoji

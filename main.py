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
TELEGRAM_TOKEN     = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID   = os.environ['TELEGRAM_CHAT_ID']
GOOGLE_SHEET_ID    = os.environ['GOOGLE_SHEET_ID']
GOOGLE_CREDS_JSON  = os.environ['GOOGLE_CREDS_JSON']

MIN_CONTRACT_AMOUNT = 50_000_000
MIN_PCT_REVENUE     = 5.0
MIN_PCT_MKTCAP      = 3.0
DAYS_BACK           = 1

# ============================================================
# פונקציות עזר לשמירת מצב (כדי למנוע התראות כפולות)
# ============================================================
def get_last_processed_ids():
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(GOOGLE_SHEET_ID).get_worksheet(0)
        # שולף את עמודת ה-ID (בהנחה שהיא עמודה 8)
        ids = ws.col_values(8)
        return set(ids)
    except Exception as e:
        print(f"⚠️ שגיאה בשליפת IDs קיימים: {e}")
        return set()

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
    if not rows: return
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
        print(f"✅ {len(rows)} שורות חדשות נשמרו")
    except Exception as e:
        print(f"⚠️ שגיאת Sheet: {e}")

# ============================================================
# זיהוי טיקר אוטומטי
# ============================================================
def find_ticker(company_name):
    clean = company_name.upper()
    for word in [",", ".", "LLC", "INC", "CORP", "CO", "LTD", "LP", "THE ", "&", " "]:
        clean = clean.replace(word, " ")
    clean = clean.strip()
    try:
        url = (f"https://query2.finance.yahoo.com/v1/finance/search"
               f"?q={requests.utils.quote(clean)}&limit=5&type=equity")
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        if r.status_code != 200: return None
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
    if not ticker: return None, None
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
# מקורות מידע
# ============================================================
def fetch_usaspending():
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=DAYS_BACK + 3)).strftime("%Y-%m-%d")
    payload = {
        "filters": {
            "time_period": [{"start_date": start_date, "end_date": end_date}],
            "award_type_codes": ["A", "B", "C", "D"],
            "award_amounts": [{"lower_bound": MIN_CONTRACT_AMOUNT}],
            "agencies": [
                {"type": "awarding", "tier": "toptier", "name": "Department of Defense"},
                {"type": "awarding", "tier": "toptier", "name": "National Aeronautics and Space Administration"},
                {"type": "awarding", "tier": "toptier", "name": "Department of Homeland Security"},
                {"type": "awarding", "tier": "toptier", "name": "Department of Energy"}
            ]
        },
        "fields": ["Award ID","Recipient Name","Award Amount", "Start Date","Awarding Agency","Description"],
        "limit": 100
    }
    try:
        r = requests.post("https://api.usaspending.gov/api/v2/search/spending_by_award/", json=payload, timeout=30)
        if r.status_code == 200:
            return [{"source": "USAspending", "name": c.get("Recipient Name",""), "amount": float(c.get("Award Amount",0) or 0), "desc": str(c.get("Description",""))[:250], "agency": str(c.get("Awarding Agency","")), "id": str(c.get("Award ID","")), "date": str(c.get("Start Date",""))} for c in r.json().get('results', [])]
    except: pass
    return []

def fetch_defense_gov():
    contracts = []
    for i in range(DAYS_BACK + 1):
        date = datetime.now() - timedelta(days=i)
        url = f"https://www.defense.gov/News/Contracts/Date/{date.strftime('%Y/%m/%d')}/"
        try:
            r = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200: continue
            paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', r.text, re.DOTALL)
            for p in paragraphs:
                text = re.sub('<[^<]+?>', '', p).strip()
                if len(text) < 100: continue
                amounts = re.findall(r'\$[\d,]+(?:\.\d+)?\s*(?:million|billion)?', text, re.I)
                amount = 0
                for a in amounts:
                    num = re.sub(r'[^\d.]', '', a.split('$')[1])
                    try:
                        val = float(num)
                        if 'billion' in a.lower(): val *= 1_000_000_000
                        elif 'million' in a.lower(): val *= 1_000_000
                        if val > amount: amount = val
                    except: pass
                if amount < MIN_CONTRACT_AMOUNT: continue
                company = text.split(',')[0].strip()[:80]
                contracts.append({"source": "defense.gov", "name": company, "amount": amount, "desc": text[:250], "agency": "Department of Defense", "id": f"DOD-{date.strftime('%Y%m%d')}-{len(contracts)}", "date": date.strftime("%Y-%m-%d")})
        except: pass
    return contracts

# ============================================================
# ניתוח וסינון
# ============================================================
def analyze(contracts, existing_ids):
    alerts, sheet_rows = [], []
    for c in contracts:
        if c['id'] in existing_ids:
            continue
            
        name = c['name']
        amount = c['amount']
        
        print(f"🔍 בודק חוזה חדש: {name} — ${amount:,.0f}")
        ticker = find_ticker(name)
        time.sleep(0.5)
        
        if not ticker: continue
        
        revenue, market_cap = get_financials(ticker)
        interesting, reason = is_interesting(amount, revenue, market_cap)
        
        status = "מהותי" if interesting else "לא מהותי"
        sheet_rows.append([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            name, ticker, amount, c['agency'], c['date'], c['desc'], c['id'],
            reason or "", market_cap or "", revenue or "", status, c['source']
        ])
        
        if interesting:
            source_emoji = {"USAspending": "🏛️", "defense.gov": "⚡"}.get(c['source'], "📄")
            msg = (
                f"🚨 <b>חוזה ממשלתי מהותי חדש!</b> {source_emoji}

"
                f"🏢 <b>חברה:</b> {name} (<b>{ticker}</b>)
"
                f"💰 <b>סכום:</b> ${amount:,.0f}
"
                f"📊 <b>משמעות:</b> {reason}
"
                f"🏛️ <b>סוכנות:</b> {c['agency']}
"
                f"📅 <b>תאריך:</b> {c['date']}

"
                f"🔗 <a href='https://finance.yahoo.com/quote/{ticker}'>Yahoo Finance</a>"
            )
            alerts.append(msg)
            
    return alerts, sheet_rows

# ============================================================
# Main
# ============================================================
def main():
    print("🚀 מתחיל סריקה יומית...")
    
    # 1. שליפת IDs שכבר טיפלנו בהם מה-Sheet
    existing_ids = get_last_processed_ids()
    print(f"📋 נמצאו {len(existing_ids)} חוזים קודמים ב-Sheet.")
    
    # 2. איסוף חוזים מהמקורות
    all_contracts = []
    all_contracts.extend(fetch_defense_gov())
    all_contracts.extend(fetch_usaspending())
    
    # 3. ניתוח רק של מה שחדש
    alerts, rows = analyze(all_contracts, existing_ids)
    
    # 4. שמירה ודיווח
    if rows:
        save_to_sheet(rows)
        for msg in alerts:
            send_telegram(msg)
            time.sleep(1)
        print(f"🏁 הסתיים. נוספו {len(rows)} שורות, נשלחו {len(alerts)} התראות.")
    else:
        print("📭 לא נמצאו חוזים חדשים מעניינים.")

if __name__ == "__main__":
    main()

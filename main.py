import requests
import time
import json
import os
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

MIN_CONTRACT_AMOUNT = 50_000_000   # $50M מינימום
MIN_PCT_REVENUE     = 5.0          # לפחות 5% מהמחזור השנתי
MIN_PCT_MKTCAP      = 3.0          # לפחות 3% משווי השוק
DAYS_BACK           = 1            # כמה ימים אחורה

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
        print(f"✅ {len(rows)} שורות נשמרו ב-Sheet")
    except Exception as e:
        print(f"⚠️ שגיאת Sheet: {e}")

# ============================================================
# זיהוי טיקר אוטומטי
# ============================================================
def find_ticker(company_name):
    """
    מחפש טיקר בורסאי לכל חברה דרך Yahoo Finance
    """
    # ניקוי שם החברה
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
            # רק מניות אמריקאיות בבורסות מרכזיות
            if q.get('exchange') in ['NMS', 'NYQ', 'NGM', 'ASE', 'PCX']:
                return q.get('symbol')
    except:
        pass
    return None

# ============================================================
# מידע פיננסי
# ============================================================
def get_financials(ticker):
    """
    שולף מחזור שנתי ושווי שוק מ-Yahoo Finance
    """
    if not ticker:
        return None, None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        revenue    = info.get('totalRevenue', 0) or 0
        market_cap = info.get('marketCap', 0) or 0
        return revenue, market_cap
    except:
        return None, None

# ============================================================
# קריטריון — האם החוזה מעניין?
# ============================================================
def is_interesting(amount, revenue, market_cap):
    """
    מחזיר (True/False, סיבה)
    """
    if revenue and revenue > 0:
        pct_rev = (amount / revenue) * 100
        if pct_rev >= MIN_PCT_REVENUE:
            return True, f"{pct_rev:.1f}% מהמחזור השנתי"

    if market_cap and market_cap > 0:
        pct_mc = (amount / market_cap) * 100
        if pct_mc >= MIN_PCT_MKTCAP:
            return True, f"{pct_mc:.1f}% משווי השוק"

    return False, None

# ============================================================
# שאיבת חוזים מ-USAspending
# ============================================================
def fetch_contracts():
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=DAYS_BACK + 3)).strftime("%Y-%m-%d")

    payload = {
        "filters": {
            "time_period": [{"start_date": start_date, "end_date": end_date}],
            "award_type_codes": ["A", "B", "C", "D"],
            "award_amounts": [{
                "lower_bound": MIN_CONTRACT_AMOUNT,
                "upper_bound": 10_000_000_000
            }],
            "agencies": [
                {"type": "awarding", "tier": "toptier",
                 "name": "Department of Defense"},
                {"type": "awarding", "tier": "toptier",
                 "name": "National Aeronautics and Space Administration"},
                {"type": "awarding", "tier": "toptier",
                 "name": "Department of Homeland Security"},
                {"type": "awarding", "tier": "toptier",
                 "name": "Department of Energy"}
            ]
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount",
            "Start Date", "Awarding Agency", "Description"
        ],
        "sort": "Award Amount",
        "order": "desc",
        "limit": 100,
        "page": 1
    }

    try:
        r = requests.post(
            "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            json=payload, timeout=30
        )
        if r.status_code == 200:
            results = r.json().get('results', [])
            print(f"✅ נמצאו {len(results)} חוזים מ-USAspending")
            return results
        print(f"❌ API error: {r.status_code}")
        return []
    except Exception as e:
        print(f"❌ שגיאה: {e}")
        return []

# ============================================================
# ניתוח וסינון
# ============================================================
def analyze(contracts):
    alerts     = []
    sheet_rows = []
    skipped    = 0

    for c in contracts:
        name   = str(c.get('Recipient Name', ''))
        amount = float(c.get('Award Amount', 0) or 0)
        desc   = str(c.get('Description', ''))[:250]
        agency = str(c.get('Awarding Agency', ''))
        aid    = str(c.get('Award ID', ''))
        date   = str(c.get('Start Date', ''))

        if amount < MIN_CONTRACT_AMOUNT:
            continue

        print(f"  🔍 בודק: {name} — ${amount:,.0f}")

        # שלב 1: חיפוש טיקר אוטומטי
        ticker = find_ticker(name)
        time.sleep(0.5)  # נימוס ל-Yahoo

        if not ticker:
            # חברה לא בורסאית — לא מעניינת
            print(f"     ❌ לא בורסאית — מדלג")
            skipped += 1
            continue

        # שלב 2: שליפת נתונים פיננסיים
        revenue, market_cap = get_financials(ticker)

        # שלב 3: האם החוזה מהותי מספיק?
        interesting, reason = is_interesting(amount, revenue, market_cap)

        if not interesting:
            print(f"     ⚠️ {ticker} — חוזה לא מהותי מספיק")
            # שמור ב-Sheet בכל זאת — אבל ללא התראה
            sheet_rows.append([
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                name, ticker, amount, agency, date,
                desc[:200], aid, "", market_cap or "", revenue or "",
                "לא מהותי"
            ])
            continue

        print(f"     ✅ {ticker} — {reason} — שולח התראה!")

        # שלב 4: בניית הודעת Telegram
        pct_rev = round((amount/revenue*100), 1) if revenue else None
        pct_mc  = round((amount/market_cap*100), 1) if market_cap else None

        msg = (
            f"🚨 <b>חוזה ממשלתי מהותי!</b>\n\n"
            f"🏢 <b>חברה:</b> {name}\n"
            f"📋 <b>טיקר:</b> <b>{ticker}</b>\n"
            f"💰 <b>סכום החוזה:</b> ${amount:,.0f}\n"
            f"🏛️ <b>סוכנות:</b> {agency}\n"
            f"📅 <b>תאריך:</b> {date}\n"
        )
        if pct_rev:
            msg += f"📊 <b>% מהמחזור:</b> {pct_rev}%\n"
        if pct_mc:
            msg += f"📈 <b>% משווי שוק:</b> {pct_mc}%\n"
        if revenue:
            msg += f"💼 <b>מחזור שנתי:</b> ${revenue:,.0f}\n"
        if market_cap:
            msg += f"🏦 <b>שווי שוק:</b> ${market_cap:,.0f}\n"
        msg += (
            f"📝 <b>תיאור:</b> {desc}\n\n"
            f"⚡ <b>סיבת ההתראה:</b> {reason}\n"
            f"🔗 https://www.usaspending.gov/award/{aid}"
        )

        alerts.append(msg)
        sheet_rows.append([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            name, ticker, amount, agency, date,
            desc[:200], aid, pct_rev or "", market_cap or "",
            revenue or "", reason
        ])

    print(f"\n📊 סיכום: {len(alerts)} התראות, {skipped} לא בורסאיות")
    return alerts, sheet_rows

# ============================================================
# MAIN
# ============================================================
def main():
    print(f"\n{'='*50}")
    print(f"🚀 GovContracts Tracker — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}\n")

    contracts = fetch_contracts()

    if not contracts:
        send_telegram("⚠️ GovContracts: לא הצלחתי לשלוף נתונים מ-USAspending")
        return

    alerts, sheet_rows = analyze(contracts)

    if sheet_rows:
        save_to_sheet(sheet_rows)

    for alert in alerts:
        send_telegram(alert)
        time.sleep(1)

    summary = (
        f"✅ סריקה יומית הסתיימה\n"
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"📊 חוזים שנסרקו: {len(contracts)}\n"
        f"🚨 התראות שנשלחו: {len(alerts)}\n"
        f"💾 שורות ב-Sheet: {len(sheet_rows)}"
    )
    send_telegram(summary)
    print(summary)

if __name__ == "__main__":
    main()

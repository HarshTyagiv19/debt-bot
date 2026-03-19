from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import anthropic, os, json, re, random
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date

load_dotenv()

app = FastAPI()
twilio_client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
ai_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
call_states = {}

# ─── Google Sheets Setup ──────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_sheet():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON env variable nahi mili!")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    return gc.open_by_key(sheet_id).sheet1

def get_debtor_by_phone(phone: str):
    """
    Phone number se debtor info nikalo.
    Columns (as per actual file):
    U=mobile, G=CustomerName, M=FinalAmount, L=TotalOutstanding,
    AH=collectionHistory, AF=Status, X=Remark, D=AgentName,
    F=loanNo, LenderName=extra column added by user
    """
    try:
        sheet = get_sheet()
        all_rows = sheet.get_all_values()
        if not all_rows:
            return None

        headers = all_rows[0]

        # Column index dhundo by name
        def col(name):
            name_lower = name.lower().strip()
            for i, h in enumerate(headers):
                if h.lower().strip() == name_lower:
                    return i
            return None

        idx = {
            'mobile':             col('mobile'),
            'CustomerName':       col('CustomerName'),
            'FinalAmount':        col('Final Amount'),
            'TotalOutstanding':   col('Total Outstanding'),
            'collectionHistory':  col('collectionHistory'),
            'Status':             col('Status'),
            'Remark':             col('Remark'),
            'AgentName':          col('Agent Name'),
            'loanNo':             col('loanNo'),
            'LenderName':         col('LenderName'),
            'DPD':                col('DPD'),
        }

        clean_input = re.sub(r'\D', '', phone)

        for row_num, row in enumerate(all_rows[1:], start=2):
            if idx['mobile'] is None:
                continue
            raw_phone = str(row[idx['mobile']]) if idx['mobile'] < len(row) else ''
            clean_row = re.sub(r'\D', '', raw_phone)
            if not clean_row:
                continue
            if clean_input.endswith(clean_row[-10:]) or clean_row.endswith(clean_input[-10:]):
                def safe_get(key):
                    i = idx.get(key)
                    if i is None or i >= len(row):
                        return ''
                    return str(row[i]).strip()

                return {
                    'row_index':          row_num,
                    'mobile':             safe_get('mobile'),
                    'name':               safe_get('CustomerName'),
                    'final_amount':       safe_get('FinalAmount'),
                    'total_outstanding':  safe_get('TotalOutstanding'),
                    'collection_history': safe_get('collectionHistory'),
                    'status':             safe_get('Status'),
                    'remark':             safe_get('Remark'),
                    'agent_name':         safe_get('AgentName'),
                    'loan_no':            safe_get('loanNo'),
                    'company':            safe_get('LenderName'),
                    'dpd':                safe_get('DPD'),
                    '_headers':           headers,
                }
        return None

    except Exception as e:
        print(f"Sheet lookup error: {e}")
        return None


def calculate_collection_total(history_str: str) -> float:
    """
    Collection history parse karo.
    Format: "500.00-2025-03-26\\200.00-2025-03-28"
    Sabka total nikalo.
    """
    if not history_str or history_str in ('0', 'NULL', 'None', ''):
        return 0.0
    total = 0.0
    entries = history_str.replace('\\\\', '\\').split('\\')
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        # Amount is before first dash that's followed by a year
        parts = re.split(r'-(?=\d{4})', entry)
        if parts:
            try:
                total += float(parts[0].replace(',', ''))
            except:
                pass
    return total


def calculate_offer_amount(debtor: dict) -> dict:
    """
    Bot ke liye offer amount calculate karo.
    Final Amount - Collection History = actual remaining target
    Minimum 1000-1500 agar negative aaye.
    """
    try:
        final_amt = float(re.sub(r'[^\d.]', '', debtor.get('final_amount', '0') or '0'))
        collected  = calculate_collection_total(debtor.get('collection_history', ''))
        remaining  = final_amt - collected

        if remaining <= 0:
            remaining = 1250  # midpoint of 1000-1500

        return {
            'final_amount': final_amt,
            'collected':    collected,
            'offer_amount': round(remaining),
        }
    except Exception as e:
        print(f"Offer calc error: {e}")
        return {'final_amount': 0, 'collected': 0, 'offer_amount': 0}


def update_sheet_after_call(row_index: int, remark: str, status: str, headers: list, sheet=None):
    """Remark aur Status columns update karo."""
    try:
        if sheet is None:
            sheet = get_sheet()

        def col_num(name):
            name_lower = name.lower().strip()
            for i, h in enumerate(headers):
                if h.lower().strip() == name_lower:
                    return i + 1  # gspread is 1-indexed
            return None

        remark_col = col_num('Remark')
        status_col  = col_num('Status')
        now = datetime.now().strftime("%d-%m-%Y %H:%M")

        if remark_col:
            sheet.update_cell(row_index, remark_col, remark)
        if status_col:
            sheet.update_cell(row_index, status_col, status)

        print(f"Sheet updated: row {row_index} | {remark} | {status}")
    except Exception as e:
        print(f"Sheet update error: {e}")


# ─── Health ───────────────────────────────────────────────────────────────────
@app.get("/")
def home():
    return {"status": "Aditi bot chal rahi hai!", "time": datetime.now().isoformat()}

@app.get("/health")
def health():
    return {"ok": True}


# ─── Test Call ────────────────────────────────────────────────────────────────
@app.get("/test-call")
def test_call():
    try:
        call = twilio_client.calls.create(
            to=os.getenv("MY_PHONE_NUMBER"),
            from_=os.getenv("TWILIO_PHONE_NUMBER"),
            url="https://debt-bot-production-57d7.up.railway.app/incoming"
        )
        return {"message": "Call ja rahi hai!", "sid": call.sid}
    except Exception as e:
        return {"error": str(e)}


# ─── Incoming Call ────────────────────────────────────────────────────────────
@app.get("/incoming")
@app.post("/incoming")
async def incoming(request: Request):
    form = {}
    try:
        form = await request.form()
    except:
        pass

    call_sid = form.get("CallSid", "unknown")
    caller   = form.get("From", "")

    debtor = get_debtor_by_phone(caller) if caller else None

    if debtor:
        name    = debtor.get('name', 'aap')
        company = debtor.get('company', 'hamare sansthan')
        intro   = (
            f"Hello, main Aditi bol rahi hun {company} se. "
            f"Kya main {name} ji se baat kar rahi hun?"
        )
        call_states[call_sid] = {"messages": [], "debtor": debtor, "caller": caller}
    else:
        intro = (
            "Hello, main Aditi bol rahi hun. "
            "Kya aap loan account holder hain?"
        )
        call_states[call_sid] = {"messages": [], "debtor": None, "caller": caller}

    response = VoiceResponse()
    gather = Gather(
        input='speech dtmf',
        action='https://debt-bot-production-57d7.up.railway.app/respond',
        timeout=3,
        speech_timeout='auto',
        method='POST'
    )
    gather.say(intro, voice='Polly.Aditi', language='hi-IN')
    response.append(gather)

    # Agar koi response nahi aaya
    _no_answer_remark(call_sid, caller, "Phone ringing but not picked")
    response.say("Dhanyawad. Hum baad mein call karenge.", voice='Polly.Aditi', language='hi-IN')
    return PlainTextResponse(str(response), media_type="application/xml")


def _no_answer_remark(call_sid: str, caller: str, reason: str):
    """Background mein sheet update karo agar koi na uthaye."""
    state = call_states.get(call_sid, {})
    debtor = state.get('debtor')
    if debtor and debtor.get('row_index') and debtor.get('_headers'):
        now = datetime.now().strftime("%d-%m-%Y %H:%M")
        update_sheet_after_call(
            row_index=debtor['row_index'],
            remark=f"{reason} — {now}",
            status="No Answer",
            headers=debtor['_headers']
        )


# ─── Respond ─────────────────────────────────────────────────────────────────
@app.get("/respond")
@app.post("/respond")
async def respond(request: Request, background_tasks: BackgroundTasks):
    try:
        form     = await request.form()
        speech   = form.get("SpeechResult", "")
        call_sid = form.get("CallSid", "unknown")
        caller   = form.get("From", "")

        if call_sid not in call_states:
            debtor = get_debtor_by_phone(caller) if caller else None
            call_states[call_sid] = {"messages": [], "debtor": debtor, "caller": caller}

        state  = call_states[call_sid]
        debtor = state.get("debtor")

        # Offer amount calculate karo
        offer_info = calculate_offer_amount(debtor) if debtor else {}
        offer_amt  = offer_info.get('offer_amount', 0)
        final_amt  = offer_info.get('final_amount', 0)
        collected  = offer_info.get('collected', 0)

        # Offer framing — har call mein alag line
        offer_lines = [
            "Aaj ke liye hamare manager ne ek special choot di hai",
            "Sirf aaj ke liye interest waiver available hai",
            "Aaj ka ek baar ka offer hai — kal nahi milega",
            "Manager sahab ne personally approve kiya hai aaj ke liye",
            "Yeh offer sirf aaj valid hai — kal se nahi milega",
        ]
        offer_line = random.choice(offer_lines)

        # System prompt — Aditi ka poora dimag
        if debtor:
            name    = debtor.get('name', 'aap')
            loan_no = debtor.get('loan_no', '')
            company = debtor.get('company', 'hamare sansthan')
            dpd     = debtor.get('dpd', '')
            status  = debtor.get('status', '')

            system_prompt = f"""Tu Aditi hai — {company} ki professional collection agent.
Customer ka naam: {name}
Loan number: {loan_no}
Final Amount (minimum lena hai, isse kam bilkul nahi): Rs {final_amt:.0f}
Pehle collected: Rs {collected:.0f}
Agar settlement karna hai toh offer amount: Rs {offer_amt:.0f} (yeh final amount se kam nahi hai)
DPD (kitne din se payment nahi): {dpd} din
Current status: {status}

TERI STRATEGY:
1. Pehle confirm kar ki customer sahi banda hai — agar haan tab loan ki baat karo.
2. Seedha aur confident baat kar — polite lekin firm.
3. AGAR customer bole "2 din mein dunga / baad mein karunga" (PTP):
   - Accept kar, exact date aur amount pooch
   - Response ke LAST mein likho: REMARK: 2 din mein part payment — [DD-MM-YYYY] || STATUS: PTP
4. AGAR customer bole "paise nahi hain / nahi de sakta":
   - Samjhao ki interest roz badh raha hai, {dpd} din ho gaye
   - Kaho: "{offer_line}"
   - Settlement offer karo: Rs {offer_amt:.0f} mein loan permanently close
   - NOC milegi, CIBIL theek hoga — yeh bolna zaroori hai
   - Response ke LAST mein likho: REMARK: Settlement offer diya Rs {offer_amt:.0f} || STATUS: Settlement Offered
5. AGAR customer part payment karna chahta hai:
   - "Acha hai, lekin poora close karte hain aaj — interest roz badh raha hai"
   - Pehle settlement pe laao — agar nahi maana tabhi part accept karo
   - Response ke LAST mein likho: REMARK: Part payment agreed [amount] [date] || STATUS: Part Payment
6. AGAR customer aggressive ho ya gaali de:
   - Bilkul calm raho, awaaz nahi uthani
   - "Main samajh sakti hun aap frustrated hain, lekin yeh aapke hi bhale ke liye hai"
   - Baat payment pe wapas laao
   - Response ke LAST mein likho: REMARK: Customer aggressive, call continued || STATUS: Contacted
7. AGAR phone switch off tha ya koi response nahi:
   - Response ke LAST mein likho: REMARK: Phone switch off — [DD-MM-YYYY HH:MM] || STATUS: No Answer
8. AGAR payment ho gayi:
   - Response ke LAST mein likho: REMARK: Payment confirmed [amount] [date] || STATUS: Paid

HARD RULES — KABHI MAT TODNA:
- Final Amount Rs {final_amt:.0f} se EK RUPAYA bhi kam offer mat karna — chahe customer kuch bhi kahe
- Offer sirf ek psychological tool hai — actual mein final amount se kam nahi lena
- Agar collected amount ke baad bhi offer negative aaye toh minimum Rs 1250 lena
- Sirf Hindi mein baat karna
- Ek response mein ek do sentence maximum — phone call hai, essay nahi
- Har response ke end mein REMARK: aur STATUS: zaroor likhna"""

        else:
            system_prompt = """Tu Aditi hai — ek professional collection agent.
Customer ki file nahi mili system mein.
Unse pooch: "Aapka naam kya hai aur kis company ka loan hai?"
Sirf Hindi mein baat kar, 1-2 sentence mein.
Har response ke end mein likho: REMARK: [kya hua] || STATUS: Contacted"""

        state["messages"].append({"role": "user", "content": speech or "..."})

        ai_response = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system_prompt,
            messages=state["messages"]
        )

        bot_full_reply = ai_response.content[0].text
        state["messages"].append({"role": "assistant", "content": bot_full_reply})

        # REMARK aur STATUS extract karo
        remark_match = re.search(r'REMARK:\s*(.+?)(?:\|\||\n|$)', bot_full_reply)
        status_match = re.search(r'STATUS:\s*(.+?)(?:\n|$)', bot_full_reply)

        extracted_remark = remark_match.group(1).strip() if remark_match else f"Contacted — {datetime.now().strftime('%d-%m-%Y %H:%M')}"
        extracted_status = status_match.group(1).strip() if status_match else "Contacted"

        # Sheet background mein update karo
        if debtor and debtor.get('row_index') and debtor.get('_headers'):
            background_tasks.add_task(
                update_sheet_after_call,
                row_index=debtor['row_index'],
                remark=extracted_remark,
                status=extracted_status,
                headers=debtor['_headers']
            )

        # REMARK/STATUS part customer ko mat sunao
        clean_reply = re.sub(r'REMARK:.*', '', bot_full_reply, flags=re.DOTALL).strip()
        clean_reply = re.sub(r'STATUS:.*', '', clean_reply, flags=re.DOTALL).strip()
        if not clean_reply:
            clean_reply = "Theek hai, main note kar leti hun."

        response = VoiceResponse()
        gather = Gather(
            input='speech dtmf',
            action='https://debt-bot-production-57d7.up.railway.app/respond',
            timeout=3,
            speech_timeout='auto',
            method='POST'
        )
        gather.say(clean_reply, voice='Polly.Aditi', language='hi-IN')
        response.append(gather)
        response.say("Dhanyawad. Aapka din shubh ho.", voice='Polly.Aditi', language='hi-IN')
        return PlainTextResponse(str(response), media_type="application/xml")

    except Exception as e:
        print(f"RESPOND ERROR: {e}")
        response = VoiceResponse()
        response.say("Kuch technical samasya aa gayi. Hum baad mein call karenge.", voice='Polly.Aditi', language='hi-IN')
        response.hangup()
        return PlainTextResponse(str(response), media_type="application/xml")
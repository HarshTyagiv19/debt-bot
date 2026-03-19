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


# ─── Debug Sheet ─────────────────────────────────────────────────────────────
@app.get("/debug-sheet")
def debug_sheet():
    try:
        sheet = get_sheet()
        all_rows = sheet.get_all_values()
        headers = all_rows[0] if all_rows else []
        row2 = all_rows[1] if len(all_rows) > 1 else []
        return {
            "total_rows": len(all_rows),
            "headers": headers,
            "row2_sample": row2[:25]
        }
    except Exception as e:
        return {"error": str(e)}

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
        # Pre-load context — bot ko pata ho ki yeh confirmation call hai
        call_states[call_sid] = {
            "messages": [
                {"role": "assistant", "content": f"Hello, main Aditi bol rahi hun {company} se. Kya main {name} ji se baat kar rahi hun?"}
            ],
            "debtor": debtor,
            "caller": caller
        }
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

            system_prompt = f"""Tu Aditi hai — {company} ki collection agent.

CUSTOMER INFO (sirf tere liye — customer ko mat batana):
- Naam: {name}
- Company: {company}
- Final Amount minimum: Rs {final_amt:.0f}
- Din se pending: {dpd} din
- Pehle diya: Rs {collected:.0f}
- Settlement amount: Rs {offer_amt:.0f}

CALL KA FLOW:

STEP 1 — Customer "haan" bole ya confirm kare:
Seedha bolo: "{name} ji, aapne {company} ka loan liya tha jiska kaafi time se payment nahi aayi. Aap kab kar rahe hain?"

STEP 2 — Customer ke response pe:

A) "2 din mein dunga / kal / baad mein" (PTP):
   Pooch exact date. Accept karo.
   REMARK: 2 din mein part payment — [date] || STATUS: PTP

B) "Paise nahi hain / nahi de sakta":
   Kaho: "{offer_line}. Aaj ek baar mein loan close karo — NOC milegi, CIBIL theek hoga."
   Agar pooche kitna — tab Rs {offer_amt:.0f} batao.
   REMARK: Settlement offer diya || STATUS: Settlement Offered

C) "Part payment kar sakta hun":
   Kaho: "Haan kar sakte hain — lekin aaj poora close karein toh better hoga, interest roz badh raha hai."
   Agar phir bhi part pe aada rahe — accept karo, date aur amount pooch.
   REMARK: Part payment [amount] [date] || STATUS: Part Payment

D) Aggressive ho ya gaali de:
   Shant raho. "Samajh sakti hun, lekin yeh aapke bhale ke liye hai."
   Payment pe wapas laao.
   REMARK: Customer aggressive || STATUS: Contacted

E) Payment ho gayi:
   REMARK: Payment confirmed [amount] || STATUS: Paid

HARD RULES — KABHI MAT TODNA:
- Amount KABHI seedha mat batao — sirf tab batao jab customer pooche ya settlement pe aaye
- Rs {final_amt:.0f} se kam kabhi nahi lena
- Dobara introduce mat karna
- Agar customer Hindi mein bole toh Hindi mein jawab do
- Agar customer English mein bole toh English mein jawab do
- Language customer ke hisaab se automatically switch karo
- Maximum 2 sentences
- Har response ke end mein REMARK: ... || STATUS: ... likhna"""

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
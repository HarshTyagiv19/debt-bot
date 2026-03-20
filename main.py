from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import anthropic, os, json, re, random, httpx
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

load_dotenv()

app = FastAPI()
ai_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
call_states = {}

# ─── Exotel Call Function ─────────────────────────────────────────────────────
async def make_exotel_call(to: str, url: str) -> dict:
    """Exotel se outbound call karo."""
    sid        = os.getenv("EXOTEL_SID")
    api_key    = os.getenv("EXOTEL_API_KEY")
    api_token  = os.getenv("EXOTEL_API_TOKEN")
    from_number = os.getenv("EXOTEL_PHONE_NUMBER", "09513886363")

    exotel_url = f"https://api.exotel.com/v1/Accounts/{sid}/Calls/connect.json"

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            exotel_url,
            auth=(api_key, api_token),
            data={
                "From": to,
                "CallerId": from_number,
                "Url": url,
            }
        )
        return response.json()

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
    try:
        sheet = get_sheet()
        all_rows = sheet.get_all_values()
        if not all_rows:
            return None
        headers = all_rows[0]

        def col(name):
            name_lower = name.lower().strip()
            for i, h in enumerate(headers):
                if h.lower().strip() == name_lower:
                    return i
            return None

        idx = {
            'mobile':            col('mobile'),
            'CustomerName':      col('CustomerName'),
            'FinalAmount':       col('Final Amount'),
            'TotalOutstanding':  col('Total Outstanding'),
            'collectionHistory': col('collectionHistory'),
            'Status':            col('Status'),
            'Remark':            col('Remark'),
            'AgentName':         col('Agent Name'),
            'loanNo':            col('loanNo'),
            'LenderName':        col('LenderName'),
            'DPD':               col('DPD'),
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
    if not history_str or history_str in ('0', 'NULL', 'None', ''):
        return 0.0
    total = 0.0
    entries = history_str.replace('\\\\', '\\').split('\\')
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        parts = re.split(r'-(?=\d{4})', entry)
        if parts:
            try:
                total += float(parts[0].replace(',', ''))
            except:
                pass
    return total


def calculate_offer_amount(debtor: dict) -> dict:
    try:
        final_amt = float(re.sub(r'[^\d.]', '', debtor.get('final_amount', '0') or '0'))
        collected = calculate_collection_total(debtor.get('collection_history', ''))
        remaining = final_amt - collected
        if remaining <= 0:
            remaining = 1250
        return {'final_amount': final_amt, 'collected': collected, 'offer_amount': round(remaining)}
    except Exception as e:
        print(f"Offer calc error: {e}")
        return {'final_amount': 0, 'collected': 0, 'offer_amount': 0}


def update_sheet_after_call(row_index: int, remark: str, status: str, headers: list, sheet=None):
    try:
        if sheet is None:
            sheet = get_sheet()
        def col_num(name):
            name_lower = name.lower().strip()
            for i, h in enumerate(headers):
                if h.lower().strip() == name_lower:
                    return i + 1
            return None
        remark_col = col_num('Remark')
        status_col = col_num('Status')
        if remark_col:
            sheet.update_cell(row_index, remark_col, remark)
        if status_col:
            sheet.update_cell(row_index, status_col, status)
        print(f"Sheet updated: row {row_index} | {remark} | {status}")
    except Exception as e:
        print(f"Sheet update error: {e}")


async def deepgram_transcribe(audio_url: str) -> dict:
    """Deepgram se audio transcribe karo — Hindi aur English dono."""
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        return {"text": "", "language": "hi"}
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.deepgram.com/v1/listen?model=nova-2&language=multi&detect_language=true&punctuate=true&smart_format=true",
                headers={
                    "Authorization": f"Token {api_key}",
                    "Content-Type": "application/json"
                },
                json={"url": audio_url}
            )
            data = response.json()
            transcript = data.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "")
            detected_lang = data.get("results", {}).get("channels", [{}])[0].get("detected_language", "hi")
            return {"text": transcript, "language": detected_lang}
    except Exception as e:
        print(f"Deepgram error: {e}")
        return {"text": "", "language": "hi"}


@app.get("/")
def home():
    return {"status": "Aditi bot chal rahi hai!", "time": datetime.now().isoformat()}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/debug-sheet")
def debug_sheet():
    try:
        sheet = get_sheet()
        all_rows = sheet.get_all_values()
        headers = all_rows[0] if all_rows else []
        row2 = all_rows[1] if len(all_rows) > 1 else []
        return {"total_rows": len(all_rows), "headers": headers, "row2_sample": row2[:25]}
    except Exception as e:
        return {"error": str(e)}

@app.get("/call-all")
async def call_all():
    try:
        sheet = get_sheet()
        all_rows = sheet.get_all_values()
        if not all_rows:
            return {"error": "Sheet khaali hai"}
        headers = all_rows[0]
        def col(name):
            name_lower = name.lower().strip()
            for i, h in enumerate(headers):
                if h.lower().strip() == name_lower:
                    return i
            return None
        mobile_idx = col('mobile')
        status_idx = col('Status')
        name_idx   = col('CustomerName')
        if mobile_idx is None:
            return {"error": "mobile column nahi mila"}
        called = []
        skipped = []
        for row_num, row in enumerate(all_rows[1:], start=2):
            mobile = str(row[mobile_idx]).strip() if mobile_idx < len(row) else ''
            status = str(row[status_idx]).strip() if status_idx and status_idx < len(row) else ''
            name   = str(row[name_idx]).strip()   if name_idx   and name_idx   < len(row) else ''
            if not mobile or mobile in ('0', 'NULL', 'None', ''):
                skipped.append(f"Row {row_num}: number nahi")
                continue
            if status.lower() in ('paid', 'payment done'):
                skipped.append(f"Row {row_num} ({name}): already paid")
                continue
            clean = re.sub(r'\D', '', mobile)
            if len(clean) == 10:
                number = f"+91{clean}"
            elif len(clean) == 12 and clean.startswith('91'):
                number = f"+{clean}"
            else:
                number = f"+{clean}"
            try:
                call = await make_exotel_call(
                    to=number,
                    url="https://debt-bot-production-57d7.up.railway.app/incoming"
                )
                called.append(f"Row {row_num} ({name}): {number} — {call}")
            except Exception as e:
                skipped.append(f"Row {row_num} ({name}): {number} — Error: {str(e)}")
        return {"total_called": len(called), "total_skipped": len(skipped), "called": called, "skipped": skipped}
    except Exception as e:
        return {"error": str(e)}

@app.get("/test-call")
async def test_call():
    try:
        result = await make_exotel_call(
            to=os.getenv("MY_PHONE_NUMBER"),
            url="https://debt-bot-production-57d7.up.railway.app/incoming"
        )
        return {"message": "Call ja rahi hai!", "result": result}
    except Exception as e:
        return {"error": str(e)}


@app.get("/status")
@app.post("/status")
async def status(request: Request):
    """Exotel status callback — call connected hone pe /incoming redirect karo."""
    form = {}
    try:
        form = await request.form()
    except:
        pass
    
    call_status = form.get("Status", "")
    print(f"Exotel status: {call_status} | form: {dict(form)}")
    
    response = VoiceResponse()
    response.redirect('https://debt-bot-production-57d7.up.railway.app/incoming')
    return PlainTextResponse(str(response), media_type="application/xml")


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
    debtor   = get_debtor_by_phone(caller) if caller else None

    if debtor:
        name    = debtor.get('name', 'aap')
        company = debtor.get('company', 'hamare sansthan')
        intro   = f"Hello, main Aditi bol rahi hun {company} se. Kya main {name} ji se baat kar rahi hun?"
        call_states[call_sid] = {
            "messages": [{"role": "assistant", "content": intro}],
            "debtor": debtor,
            "caller": caller,
            "language": "hi"
        }
    else:
        intro = "Hello, main Aditi bol rahi hun. Kya aap loan account holder hain?"
        call_states[call_sid] = {"messages": [], "debtor": None, "caller": caller, "language": "hi"}

    response = VoiceResponse()
    gather = Gather(
        input='speech dtmf',
        action='https://debt-bot-production-57d7.up.railway.app/respond',
        timeout=10,
        speech_timeout='auto',
        language='hi-IN en-IN',
        method='POST'
    )
    gather.say(intro, voice='Polly.Aditi', language='hi-IN')
    response.append(gather)
    response.redirect('https://debt-bot-production-57d7.up.railway.app/no-answer')
    return PlainTextResponse(str(response), media_type="application/xml")


@app.get("/no-answer")
@app.post("/no-answer")
async def no_answer(request: Request):
    form = {}
    try:
        form = await request.form()
    except:
        pass
    call_sid = form.get("CallSid", "unknown")
    state  = call_states.get(call_sid, {})
    debtor = state.get('debtor')
    if debtor and debtor.get('row_index') and debtor.get('_headers'):
        now = datetime.now().strftime("%d-%m-%Y %H:%M")
        update_sheet_after_call(
            row_index=debtor['row_index'],
            remark=f"Phone ringing but not picked — {now}",
            status="No Answer",
            headers=debtor['_headers']
        )
    response = VoiceResponse()
    response.hangup()
    return PlainTextResponse(str(response), media_type="application/xml")


@app.get("/respond")
@app.post("/respond")
async def respond(request: Request, background_tasks: BackgroundTasks):
    try:
        form     = await request.form()
        call_sid = form.get("CallSid", "unknown")
        caller   = form.get("From", "")

        # Deepgram se transcribe karo
        recording_url = form.get("RecordingUrl", "")
        speech = form.get("SpeechResult", "")

        if call_sid not in call_states:
            debtor = get_debtor_by_phone(caller) if caller else None
            call_states[call_sid] = {"messages": [], "debtor": debtor, "caller": caller, "language": "hi"}

        state  = call_states[call_sid]
        debtor = state.get("debtor")

        # Language detect karo — customer ki current message se
        speech_text = (speech or "").strip()
        speech_lower = speech_text.lower()
        has_devanagari = any(ord(c) > 2304 and ord(c) < 2432 for c in speech_text)

        # Agar Devanagari characters hain — Hindi
        # Agar sirf ASCII words hain — English
        # Dono mix hain — English dominant
        if has_devanagari and not any(c.isascii() and c.isalpha() for c in speech_text):
            is_english = False
        elif not has_devanagari and any(c.isascii() and c.isalpha() for c in speech_text):
            is_english = True
        else:
            # Mix — previous state rakho
            is_english = state.get("language", "hi") == "en"

        state["language"] = "en" if is_english else "hi"
        voice = 'Polly.Raveena' if is_english else 'Polly.Aditi'
        lang  = 'en-IN'        if is_english else 'hi-IN'

        offer_info = calculate_offer_amount(debtor) if debtor else {}
        offer_amt  = offer_info.get('offer_amount', 0)
        final_amt  = offer_info.get('final_amount', 0)
        collected  = offer_info.get('collected', 0)

        offer_lines = [
            "Hamare manager ne aaj ke liye ek special choot di hai",
            "Sirf aaj ke liye interest waiver available hai",
            "Aaj ka ek baar ka offer hai, kal nahi milega",
            "Manager sahab ne personally aaj ke liye approve kiya hai",
            "Yeh offer sirf aaj valid hai, kal se band ho jayega",
        ]
        offer_line = random.choice(offer_lines)

        if debtor:
            name    = debtor.get('name', 'aap')
            company = debtor.get('company', 'hamare sansthan')
            dpd     = debtor.get('dpd', '')

            lang_instruction = "IMPORTANT: Customer ne abhi jo language boli hai usi mein jawab do. Agar Hindi boli toh Hindi, agar English boli toh English. Language customer follow karo."

            system_prompt = f"""Tu Aditi hai — {company} ki collection agent.
{lang_instruction}

CUSTOMER INFO (sirf tere reference ke liye — directly mat batana):
- Naam: {name}
- Company: {company}
- Minimum lena hai: Rs {final_amt:.0f}
- Din se pending: {dpd} din
- Pehle diya: Rs {collected:.0f}
- Settlement offer: Rs {offer_amt:.0f}

CALL FLOW:

STEP 1 — Customer "haan/yes" bole ya confirm kare:
Seedha bolo: "{name} ji, aapne {company} ka loan liya tha jiska kaafi time se payment nahi aayi. Aap kab kar rahe hain?"

STEP 2 — Customer ke jawab pe:

A) "Kal dunga / 2 din mein / baad mein" (PTP):
   Exact date pooch. Accept karo.
   REMARK: 2 din mein part payment — [date] || STATUS: PTP

B) "Paise nahi hain / mushkil hai / nahi de sakta":
   Kaho: "{offer_line}. Aaj ek baar mein loan close karo — NOC milegi, CIBIL sahi hoga."
   Agar amount pooche — tab batao Rs {offer_amt:.0f}.
   REMARK: Settlement offer diya || STATUS: Settlement Offered

C) "Part payment kar sakta hun":
   Kaho: "Haan ho sakta hai — lekin aaj poora close karein toh better hoga."
   Agar part pe rahe — accept karo, date aur amount pooch.
   REMARK: Part payment [amount] [date] || STATUS: Part Payment

D) Aggressive ya gaali de:
   Shant raho. Payment pe laao.
   REMARK: Customer aggressive || STATUS: Contacted

E) Payment confirm:
   REMARK: Payment confirmed [amount] || STATUS: Paid

HARD RULES:
- Amount seedha MAT batao — sirf tab jab customer pooche ya settlement pe aaye
- Rs {final_amt:.0f} se kam kabhi nahi lena
- Dobara apna introduction mat karna
- {lang_instruction}
- Maximum 2 sentences
- Har jawab ke end mein REMARK: ... || STATUS: ... likhna zaroori hai"""

        else:
            lang_instruction = "IMPORTANT: Customer ne abhi jo language boli hai usi mein jawab do. Agar Hindi boli toh Hindi, agar English boli toh English. Language customer follow karo."
            system_prompt = f"""Tu Aditi hai — ek professional collection agent.
Customer ki file system mein nahi mili.
Unse pooch ki unka naam kya hai aur kis company ka loan hai.
{lang_instruction}
Maximum 2 sentences.
Har jawab ke end mein: REMARK: [kya hua] || STATUS: Contacted"""

        state["messages"].append({"role": "user", "content": speech or "..."})

        ai_response = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system_prompt,
            messages=state["messages"]
        )

        bot_full_reply = ai_response.content[0].text
        state["messages"].append({"role": "assistant", "content": bot_full_reply})

        remark_match = re.search(r'REMARK:\s*(.+?)(?:\|\||\n|$)', bot_full_reply)
        status_match = re.search(r'STATUS:\s*(.+?)(?:\n|$)', bot_full_reply)
        extracted_remark = remark_match.group(1).strip() if remark_match else f"Contacted — {datetime.now().strftime('%d-%m-%Y %H:%M')}"
        extracted_status = status_match.group(1).strip() if status_match else "Contacted"

        if debtor and debtor.get('row_index') and debtor.get('_headers'):
            background_tasks.add_task(
                update_sheet_after_call,
                row_index=debtor['row_index'],
                remark=extracted_remark,
                status=extracted_status,
                headers=debtor['_headers']
            )

        clean_reply = re.sub(r'REMARK:.*', '', bot_full_reply, flags=re.DOTALL).strip()
        clean_reply = re.sub(r'STATUS:.*', '', clean_reply, flags=re.DOTALL).strip()
        if not clean_reply:
            clean_reply = "Theek hai, note kar liya." if not is_english else "Okay, noted."

        response = VoiceResponse()
        gather = Gather(
            input='speech dtmf',
            action='https://debt-bot-production-57d7.up.railway.app/respond',
            timeout=10,
            speech_timeout='auto',
            language=lang,
            method='POST'
        )
        gather.say(clean_reply, voice=voice, language=lang)
        response.append(gather)
        response.redirect('https://debt-bot-production-57d7.up.railway.app/no-answer')
        return PlainTextResponse(str(response), media_type="application/xml")

    except Exception as e:
        print(f"RESPOND ERROR: {e}")
        response = VoiceResponse()
        response.say("Kuch technical samasya aa gayi. Hum baad mein call karenge.", voice='Polly.Aditi', language='hi-IN')
        response.hangup()
        return PlainTextResponse(str(response), media_type="application/xml")
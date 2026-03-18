from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import anthropic, os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
twilio_client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
ai_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
call_states = {}

@app.get("/")
def home():
    return {"status": "Bot chal raha hai!"}

@app.get("/test-call")
def test_call():
    try:
        ngrok_url = os.getenv("NGROK_URL")
        call = twilio_client.calls.create(
            to=os.getenv("MY_PHONE_NUMBER"),
            from_=os.getenv("TWILIO_PHONE_NUMBER"),
            url="https://debt-bot-production-57d7.up.railway.app/incoming"
        )
        return {"message": "Call ja rahi hai!", "sid": call.sid}
    except Exception as e:
        return {"error": str(e)}

@app.get("/incoming")
@app.post("/incoming")
async def incoming(request: Request):
    response = VoiceResponse()
    gather = Gather(
        input='speech dtmf',
        action='/respond',
        timeout=8,
        method='POST'
    )
    gather.say(
        "Namaste! Main ABC Finance se bol raha hun. "
        "Aapki ek payment pending hai. "
        "Kya aap abhi baat kar sakte hain? ",
        voice='Polly.Aditi-Neural'
    )
    response.append(gather)
    response.say("Dhanyawad. Hum baad mein call karenge.", voice='Polly.Aditi-Neural')
    return PlainTextResponse(str(response), media_type="application/xml")

@app.get("/respond")
@app.post("/respond")
async def respond(request: Request):
    try:
        form = await request.form()
        speech = form.get("SpeechResult", "koi response nahi")
        call_sid = form.get("CallSid", "unknown")

        if call_sid not in call_states:
            call_states[call_sid] = []

        call_states[call_sid].append({
            "role": "user",
            "content": speech
        })

        ai_response = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system="""You are a polite debt recovery agent for ABC Finance. 
Reply in simple Hindi only. Keep it to 1-2 sentences.
If customer asks for offer: say 10 percent discount available today if they pay now.
If customer agrees to pay: confirm and say thank you.
If customer says busy: say will call later.""",
            messages=call_states[call_sid]
        )

        bot_reply = ai_response.content[0].text
        call_states[call_sid].append({
            "role": "assistant",
            "content": bot_reply
        })

        response = VoiceResponse()
        gather = Gather(
            input='speech dtmf',
            action='/respond',
            timeout=8,
            method='POST'
        )
        gather.say(bot_reply, voice='Polly.Aditi-Neural')
        response.append(gather)
        response.say("Dhanyawad. Aapka din shubh ho.", voice='Polly.Aditi-Neural')
        return PlainTextResponse(str(response), media_type="application/xml")

    except Exception as e:
        print(f"RESPOND ERROR: {e}")
        response = VoiceResponse()
        response.say(f"Error: {str(e)[:50]}", voice='Polly.Aditi-Neural')
        response.hangup()
        return PlainTextResponse(str(response), media_type="application/xml")
    

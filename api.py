import json
import base64
import requests
import time
import uuid
import os
from flask import Flask, render_template, request, jsonify, redirect, session, url_for, make_response
from flask_dance.contrib.google import make_google_blueprint, google
from authlib.integrations.flask_client import OAuth
from PIL import Image
import tempfile
from datetime import datetime, date, timedelta
from flask_cors import CORS
from flask import send_file
app_name = '__main__'
if '__app_id__' in globals():
    app_name = globals()['__app_id__']
app = Flask(app_name)

CORS(app, resources={r"/*": {"origins": "*"}})
app.secret_key = os.environ.get("FLASK_SECRET_KEY", str(uuid.uuid4()))

# ─────────────────────────────────────────
# API KEYS
# ─────────────────────────────────────────
GOOGLE_GEMINI_API_KEY = os.environ.get("GOOGLE_GEMINI_API_KEY")
AWAN_API_KEY          = os.environ.get("AWAN_API_KEY")
GROQ_API_KEY          = os.environ.get("GROQ_API_KEY")
OPENROUTER_API_KEY    = os.environ.get("OPENROUTER_API_KEY")
SERPER_API_KEY        = os.environ.get("SERPER_API_KEY")

# ─────────────────────────────────────────
# ENDPOINTS & MODELS
# ─────────────────────────────────────────
GEMINI_API_URL       = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
AWAN_API_URL         = "https://api.awanllm.com/v1/chat/completions"
GROQ_API_URL         = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_API_URL   = "https://openrouter.ai/api/v1/chat/completions"

GEMINI_MODEL             = "gemini-2.5-flash"
GROQ_MODEL               = "llama-3.1-8b-instant"
OPENROUTER_DEEPTHINK_MODEL = "google/gemma-4-31b-it:free"

# ─────────────────────────────────────────
# DIRECTORIES
# ─────────────────────────────────────────
CHAT_HISTORY_DIR = os.path.join(app.root_path, 'chat_history')
os.makedirs(CHAT_HISTORY_DIR, exist_ok=True)
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ─────────────────────────────────────────
# QUOTA
# ─────────────────────────────────────────
user_message_counts = {}
DAILY_MESSAGE_LIMIT = 20
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/see-maths-ai")
def see_maths_ai():
    return send_file(
        os.path.join(BASE_DIR, "templates", "see-maths-ai.html")
    )


@app.route("/science-helper")
def science_helper():
    return send_file(
        os.path.join(BASE_DIR, "templates", "science-helper.html")
    )


@app.route("/homework-ai")
def homework_ai():
    return send_file(
        os.path.join(BASE_DIR, "templates", "homework-ai.html")
    )


@app.route("/see-exam-preparation")
def see_exam_preparation():
    return send_file(
        os.path.join(BASE_DIR, "templates", "see-exam-preparation.html")
    )
def get_daily_message_count(user_id):
    today = date.today().isoformat()
    return user_message_counts.get(user_id, {}).get(today, 0)

def increment_daily_message_count(user_id):
    today = date.today().isoformat()
    if user_id not in user_message_counts:
        user_message_counts[user_id] = {}
    user_message_counts[user_id][today] = user_message_counts[user_id].get(today, 0) + 1
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    for d in list(user_message_counts[user_id]):
        if d < cutoff:
            del user_message_counts[user_id][d]

# ─────────────────────────────────────────
# OAUTH
# ─────────────────────────────────────────
google_bp = make_google_blueprint(
    client_id="978102306464-qdjll3uos10m1nd5gcnr9iql9688db58.apps.googleusercontent.com",
    client_secret="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",
    redirect_url="/google_login/authorized",
    scope=["openid",
          "https://www.googleapis.com/auth/userinfo.email",
          "https://www.googleapis.com/auth/userinfo.profile"]
)
app.register_blueprint(google_bp, url_prefix="/google_login")

oauth = OAuth(app)
microsoft = oauth.register(
    name='microsoft',
    client_id="your_microsoft_client_id",
    client_secret="your_microsoft_client_secret",
    access_token_url='https://login.microsoftonline.com/common/oauth2/v2.0/token',
    authorize_url='https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    api_base_url='https://graph.microsoft.com/v1.0/',
    client_kwargs={'scope': 'User.Read'}
)

# ═══════════════════════════════════════════════════════════════════════════
#  SETS CHAPTER DETECTION & OPTIMIZED SYSTEM PROMPTS
# ═══════════════════════════════════════════════════════════════════════════
SETS_KEYWORDS = {
    "set", "n(", "venn", "union", "intersection", "cardinality", "element",
    "complement", "subset", "cardinal number", "people are in", "students like",
    "like both", "like at least", "like only", "neither", "∪", "∩", "⊂",
    "Ā", "A'", "A^c", "disjoint", "overlapping", "cardinality of", "n(a∩b)",
    "n(a∪b)", "only", "exclusively"
}

def detect_sets_problem(msg: str) -> bool:
    """Detect if the question is about Sets chapter."""
    msg_lower = msg.lower()
    return any(keyword in msg_lower for keyword in SETS_KEYWORDS)

# ═══════════════════════════════════════════════════════════════════════════
#  CASUAL MESSAGE DETECTION
# ═══════════════════════════════════════════════════════════════════════════
CASUAL_TRIGGERS = {
    "hi", "hii", "hiii", "hello", "hey", "helo", "hlo", "yo", "sup",
    "what's up", "whats up", "how are you", "how r u", "how are u",
    "good morning", "good evening", "good afternoon", "good night",
    "bye", "goodbye", "ok", "okay", "k", "thanks", "thank you", "thankyou",
    "cool", "great", "nice", "sure", "alright", "got it", "hmm",
    "yes", "no", "yep", "nope", "who are you", "what are you",
    "who made you", "what is vexara", "help", "what can you do",
    "namaste", "namaskar", "welcome", "test", "testing"
}

def is_casual_message(msg: str) -> bool:
    cleaned = msg.lower().strip().rstrip("?!. ")
    if cleaned in CASUAL_TRIGGERS:
        return True
    words = cleaned.split()
    if len(words) <= 3 and any(cleaned.startswith(t) for t in CASUAL_TRIGGERS):
        return True
    return False

# ═══════════════════════════════════════════════════════════════════════════
#  CASUAL SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════
CASUAL_SYSTEM_PROMPT = """You are Vexara, a friendly and encouraging SEE Math tutor for Class 10 students in Nepal.
For greetings and casual messages: reply warmly in 1-3 short sentences.
Briefly mention you can help with SEE C.Math — Sets, Algebra, Compound Interest, Geometry, Statistics, Trigonometry etc.
Keep it natural, friendly and short. Do NOT show any formulas or step-by-step solutions for casual messages."""

# ═══════════════════════════════════════════════════════════════════════════
#  MAIN SEE SYSTEM PROMPT (Standard)
# ═══════════════════════════════════════════════════════════════════════════
SEE_SYSTEM_PROMPT = """You are Vexara — a SEE Class 10 Mathematics topper from Nepal.

Your goal: Write PERFECT EXAM ANSWERS + optionally generate Venn diagram data.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT RULES (NON-NEGOTIABLE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Solve ALL parts (a, b, c, ...)
- Never leave answer incomplete
- Never repeat calculations
- Never explain like a teacher
- No "Let's solve", no storytelling
- Use clean exam format only

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT (STRICT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Start with: Given:
- Use n(A), n(A ∪ B), n(A ∩ B)
- Use ⇒ for each step
- Keep equations in single aligned flow
- Final answers MUST use ∴
- No paragraphs

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CORE THINKING ENGINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Identify type:
   - 2-set
   - 3-set
   - word problem
   - multi-part

2. Choose fastest method:
   - "at least one" → union
   - "none" → total − union
   - "only" → subtract overlaps
   - 3-set → use inclusion-exclusion directly

3. Compute shared values ONCE:
   - n(A ∪ B) or n(A ∪ B ∪ C)

4. Reuse everywhere

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FORMULAS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
n(A ∪ B) = n(A) + n(B) − n(A ∩ B)

n(A ∪ B ∪ C) = n(A)+n(B)+n(C)
− n(A∩B) − n(B∩C) − n(A∩C)
+ n(A∩B∩C)

Only A:
n₀(A) = n(A) − n(A∩B) − n(A∩C) + n(A∩B∩C)

Exactly two:
= (A∩B−ABC)+(B∩C−ABC)+(A∩C−ABC)

None:
= n(U) − union

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTRADICTION DETECTION (CRITICAL)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
If given formula conflicts with standard identity:
→ STOP
→ Output: "Data is inconsistent. No solution exists."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VENN DIAGRAM MODE (IMPORTANT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
When question involves diagram:
- Compute regions:
  Only A, Only B, Only C, intersections, none
- Return structured diagram data like:

VENN_DATA:
Only_A = ...
Only_B = ...
Only_C = ...
A∩B_only = ...
B∩C_only = ...
A∩C_only = ...
A∩B∩C = ...
None = ...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL OUTPUT STYLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Given:
...

n(A ∪ B ∪ C)
⇒ ...
⇒ ...

(a) ...
⇒ ...

∴ Answer1 = ...
∴ Answer2 = ...

If diagram needed:
Also output VENN_DATA block.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Your answer must look like:
✔ SEE topper copy
✔ Full marks guaranteed
✔ Clean + minimal
✔ No wasted steps
"""
# ═══════════════════════════════════════════════════════════════════════════
#  DEEPTHINK SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════
DEEPTHINK_SYSTEM_PROMPT = """You are Vexara DeepThink — the advanced mode of the Vexara SEE Math tutor for Nepal Class 10 students.
You are used for HARD, multi-step, and conceptually complex SEE exam problems.

Your job:
1. UNDERSTAND the problem fully — identify all given values, unknowns, and what is asked
2. PLAN the approach — state which chapter concept/formula applies and why
3. SOLVE with complete, clean working — use ⇒ for every step
4. VERIFY — check if the answer is reasonable (reverse-check if possible)
5. EXPLAIN — briefly explain the key concept so the student understands

FORMAT RULES:
- ⇒ for every calculation step (mandatory)
- Never write "Step 1:", "Step 2:"
- For word problems: define all variables first
- Final answer with ∴ (therefore) and correct units
- Use the Nepal CDC textbook method exactly

CHAPTER RULES (DeepThink-specific):


SETS — use Venn diagram logic:
  Three-set: n(A∪B∪C) = n(A)+n(B)+n(C)−n(A∩B)−n(B∩C)−n(A∩C)+n(A∩B∩C)

For EXTRA HARD problems:
- Break into sub-problems
- Solve each part clearly
- Combine at the end
- Always verify your final answer makes logical sense"""

# ═══════════════════════════════════════════════════════════════════════════
#  VISION SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════
VISION_SYSTEM_PROMPT = """You are Vexara Vision — the image-reading mode of the Vexara SEE Math tutor for Class 10 students in Nepal.

A student has uploaded a photo or screenshot of a math problem from their textbook, homework, or exam paper.

YOUR TASK:
1. READ the image carefully — identify all numbers, symbols, diagrams, and what is being asked
2. STATE the problem clearly in text (in case the image is unclear)
3. IDENTIFY which SEE chapter this belongs to (Sets / CI / Growth / Currency / Mensuration / Algebra / Geometry / Statistics / Probability / Trigonometry)
4. SOLVE completely using Nepal CDC textbook method with ⇒ for every step
5. EXPLAIN the concept briefly so the student understands it for future questions

FORMAT RULES:
- ⇒ for every calculation step
- Never write "Step 1:", "Step 2:"
- Final answer with ∴ and correct units
- For word problems: define variables first

CHAPTER-SPECIFIC CRITICAL RULES:

COMPOUND INTEREST:
  CA = P(1 + R/100)^T  — use this ONE formula directly, not year-by-year
  CI = CA − P

CURRENCY:
  Customer selling foreign → Bank buys → use BUYING rate → NPR = amount × buying rate
  Customer buying foreign → Bank sells → use SELLING rate → Foreign = NPR ÷ selling rate

MENSURATION — verified formulas:
  Cylinder: TSA = 2πr(h+r),  Volume = πr²h
  Cone: TSA = πr(l+r) where l=√(r²+h²),  Volume=(1/3)πr²h
  Sphere: SA = 4πr², Volume = (4/3)πr³

CIRCLE: always name the theorem used
  Central angle = 2 × inscribed angle (same arc)
  Opposite angles of cyclic quad = 180°

TRIGONOMETRY: use triangle method (draw right triangle, apply Pythagoras for hypotenuse)

PROBABILITY: simplify to fraction, not decimal

SETS: use n(A∪B) = n(A)+n(B)−n(A∩B) and Neither = Total − n(A∪B)

If the image is a diagram (circle, triangle, construction):
- Describe what you see in the diagram
- Label/identify all given measurements and angles
- Apply the correct theorem
- Solve completely

If the image is unclear or cut off:
- State what you can read
- Ask the student to clarify the specific part you cannot read
- Solve the parts you can identify"""

# ─────────────────────────────────────────
# CHAT HISTORY MANAGEMENT
# ─────────────────────────────────────────
def get_user_id():
    if 'user_id' in session:
        return session['user_id']
    if 'temp_user_id' not in session:
        session['temp_user_id'] = str(uuid.uuid4())
        session['user_id'] = session['temp_user_id']
    return session['temp_user_id']

def get_chat_file_path(user_id, chat_id):
    safe = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
    return os.path.join(CHAT_HISTORY_DIR, f"{safe}_{chat_id}.json")

def load_chat_history_from_file(user_id, chat_id):
    path = get_chat_file_path(user_id, chat_id)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_chat_history_to_file(user_id, chat_id, chat_data):
    path = get_chat_file_path(user_id, chat_id)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(chat_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving chat history: {e}")

# ─────────────────────────────────────────
# MESSAGE BUILDERS
# ─────────────────────────────────────────
def build_gemini_messages(chat_history, new_instruction):
    """Builds Gemini-format messages from chat history."""
    messages = []
    for msg in chat_history:
        if msg.get('type') == 'user':
            messages.append({"role": "user", "parts": [{"text": msg.get('text', '')}]})
        elif msg.get('type') == 'bot':
            messages.append({"role": "model", "parts": [{"text": msg.get('text', '')}]})
    messages.append({"role": "user", "parts": [{"text": new_instruction}]})
    return messages

def build_chat_completion_messages(chat_history, new_instruction, use_short_prompt=False):
    """Builds OpenAI-compatible messages for Groq/OpenRouter."""
    system = SEE_SYSTEM_PROMPT
    messages = [{"role": "system", "content": system}]
    history = chat_history[-4:] if use_short_prompt else chat_history
    for msg in history:
        if msg.get('type') == 'user':
            messages.append({"role": "user", "content": msg.get('text', '')})
        elif msg.get('type') == 'bot':
            messages.append({"role": "assistant", "content": msg.get('text', '')})
    messages.append({"role": "user", "content": new_instruction})
    return messages

def build_deepthink_messages(chat_history, new_instruction):
    """Builds messages for DeepThink model with advanced prompt."""
    messages = [{"role": "system", "content": DEEPTHINK_SYSTEM_PROMPT}]
    for msg in chat_history[-6:]:
        if msg.get('type') == 'user':
            messages.append({"role": "user", "content": msg.get('text', '')})
        elif msg.get('type') == 'bot':
            messages.append({"role": "assistant", "content": msg.get('text', '')})
    messages.append({"role": "user", "content": new_instruction})
    return messages

# ─────────────────────────────────────────
# API CALLERS
# ─────────────────────────────────────────
def call_gemini_api(messages, system_prompt=None, stream=False):
    """Calls Gemini API with system prompt."""
    prompt = system_prompt if system_prompt else SEE_SYSTEM_PROMPT
    payload = {
        "contents": messages,
        "systemInstruction": {"parts": [{"text": prompt}]},
        "generationConfig": {
            "temperature": 0.2,
            "topK": 40,
            "topP": 0.95,
            "maxOutputTokens": 2048,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    }
    url = f"{GEMINI_API_URL}?key={GOOGLE_GEMINI_API_KEY}"
    try:
        print(f"[DEBUG] Calling Gemini...")
        r = requests.post(url, json=payload, timeout=60)
        print(f"[DEBUG] Gemini status: {r.status_code}")
        if r.status_code != 200:
            print(f"[DEBUG] Gemini error: {r.text[:300]}")
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"Gemini API error: {e}")
        return None

def call_groq_api(messages, stream=True):
    """Calls Groq API."""
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1024,
        "stream": stream
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        print(f"[DEBUG] Calling Groq...")
        r = requests.post(GROQ_API_URL, json=payload, headers=headers, stream=stream, timeout=60)
        print(f"[DEBUG] Groq status: {r.status_code}")
        if r.status_code != 200:
            print(f"[DEBUG] Groq error: {r.text[:300]}")
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"Groq API error: {e}")
        return None

def call_openrouter_api(messages, model, stream=True):
    """Calls OpenRouter API."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 2048,
        "stream": stream
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://vexara.ai",
        "X-Title": "Vexara SEE Tutor"
    }
    try:
        r = requests.post(OPENROUTER_API_URL, json=payload, headers=headers, stream=stream, timeout=90)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"OpenRouter API error: {e}")
        return None

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def extract_gemini_text(response):
    """Extracts text from Gemini API response."""
    try:
        data = response.json()
        if 'candidates' in data and data['candidates']:
            for part in data['candidates'][0].get('content', {}).get('parts', []):
                if 'text' in part:
                    return part['text']
    except Exception as e:
        print(f"Gemini parse error: {e}")
    return None

def stream_text(text):
    """Generator that streams text in chunks for SSE."""
    words = text.split(' ')
    chunk = ""
    for word in words:
        chunk += word + " "
        if len(chunk) > 60:
            yield chunk
            chunk = ""
    if chunk:
        yield chunk

def stream_openai_response(response):
    """Generator that yields text chunks from OpenAI-format streaming response."""
    full = ""
    try:
        for line in response.iter_lines():
            if line:
                line_str = line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                if line_str.startswith('data: ') and line_str != 'data: [DONE]':
                    try:
                        data = json.loads(line_str[6:])
                        chunk = data.get('choices', [{}])[0].get('delta', {}).get('content', '')
                        if chunk:
                            full += chunk
                            yield chunk
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
    except Exception as e:
        print(f"Streaming error: {e}")
    return full

# ─────────────────────────────────────────
# MAIN /ask ENDPOINT (YOUR WORKING ENDPOINT)
# ─────────────────────────────────────────
def detect_set_type(question):
    q = question.lower()

    if "three" in q or "three sets" in q or "a∩b∩c" in q:
        return "3-set"

    elif "both" in q or "a∩b" in q:
        return "2-set"

    elif "none" in q or "neither" in q:
        return "none-type"

    else:
        return "general"
@app.route('/ask', methods=['POST'])
def ask_endpoint():
    """Main /ask endpoint - uses form data (not JSON)"""
    print(f"[DEBUG] /ask endpoint hit!")
    
    user_id      = get_user_id()
    chat_id      = request.form.get('chat_id')
    instruction  = request.form.get('instruction', '').strip()
    model_choice = request.form.get('model_choice', 'general')

    print(f"[DEBUG] User: {user_id[:8]}, Chat: {chat_id[:8] if chat_id else 'NONE'}")
    print(f"[DEBUG] Instruction: {instruction[:60]}...")
    print(f"[DEBUG] Model: {model_choice}")

    if not chat_id:
        return jsonify({"error": "Chat ID not provided."}), 400
    if not instruction:
        return jsonify({"error": "No instruction provided."}), 400

    # Quota check
    if get_daily_message_count(user_id) >= DAILY_MESSAGE_LIMIT:
        return jsonify({"response": f"Daily limit of {DAILY_MESSAGE_LIMIT} messages reached. Try again tomorrow."}), 429

    # Load & save user message
    current_chat_history = load_chat_history_from_file(user_id, chat_id)
    current_chat_history.append({"type": "user", "text": instruction, "timestamp": time.time()})
    save_chat_history_to_file(user_id, chat_id, current_chat_history)
    increment_daily_message_count(user_id)

    def generate_response():
        full_response = ""
        try:
            # ── CASUAL SHORTCUT ──────────────────────────────────────────
            if is_casual_message(instruction):
                print(f"[DEBUG] Detected casual message")
                casual_msgs = [{"role": "user", "parts": [{"text": instruction}]}]
                r = call_gemini_api(casual_msgs, system_prompt=CASUAL_SYSTEM_PROMPT)
                if r and r.status_code == 200:
                    text = extract_gemini_text(r)
                    if text:
                        full_response = text
                        yield text
                        current_chat_history.append({"type": "bot", "text": full_response, "timestamp": time.time()})
                        save_chat_history_to_file(user_id, chat_id, current_chat_history)
                        return
                # fallback
                full_response = "Hey! 👋 I'm Vexara, your SEE Math tutor. Ask me any C.Math question — Sets, Algebra, CI, Geometry, Trigonometry and more!"
                yield full_response
                current_chat_history.append({"type": "bot", "text": full_response, "timestamp": time.time()})
                save_chat_history_to_file(user_id, chat_id, current_chat_history)
                return

            # ── DETECT SETS PROBLEMS & USE OPTIMIZED PROMPT ──────────────
            if detect_sets_problem(instruction):
                print(f"[DEBUG] Detected SETS problem")
                system = SEE_SYSTEM_PROMPT  # Uses enhanced Sets section
            else:
                print(f"[DEBUG] General math problem")
                system = SEE_SYSTEM_PROMPT

            # ── DEEP THINK MODE ──────────────────────────────────────────
            if model_choice == "deep_think":
                print(f"[DEBUG] Using DeepThink model")
                dt_messages = build_deepthink_messages(current_chat_history, instruction)
                r = call_openrouter_api(dt_messages, OPENROUTER_DEEPTHINK_MODEL, stream=True)

                if r and r.status_code == 200:
                    for chunk in stream_openai_response(r):
                        full_response += chunk
                        yield chunk

                # DeepThink failed → fallback to Gemini
                if not full_response:
                    print("[DEBUG] DeepThink failed, falling back to Gemini...")
                    gemini_msgs = build_gemini_messages(current_chat_history, instruction)
                    r2 = call_gemini_api(gemini_msgs, system_prompt=DEEPTHINK_SYSTEM_PROMPT)
                    if r2 and r2.status_code == 200:
                        text = extract_gemini_text(r2)
                        if text:
                            full_response = text
                            yield from stream_text(text)

            # ── GENERAL MODE (default) ───────────────────────────────────
            else:
                print(f"[DEBUG] Using Gemini general mode")
                gemini_msgs = build_gemini_messages(current_chat_history, instruction)
                r = call_gemini_api(gemini_msgs, system_prompt=system)

                if r and r.status_code == 200:
                    text = extract_gemini_text(r)
                    if text:
                        full_response = text
                        yield from stream_text(text)
                else:
                    # Gemini failed → Groq fallback
                    print(f"[DEBUG] Gemini failed, trying Groq fallback...")
                    groq_msgs = build_chat_completion_messages(current_chat_history, instruction, use_short_prompt=True)
                    r2 = call_groq_api(groq_msgs, stream=True)
                    if r2 and r2.status_code == 200:
                        for chunk in stream_openai_response(r2):
                            full_response += chunk
                            yield chunk

            if not full_response:
                err = "Sorry, I couldn't get a response right now. Please try again in a moment!"
                yield err
                full_response = err
                return

            # Save bot response
            current_chat_history.append({"type": "bot", "text": full_response, "timestamp": time.time()})
            save_chat_history_to_file(user_id, chat_id, current_chat_history)
            print(f"[DEBUG] Response saved ({len(full_response)} chars)")

        except Exception as e:
            print(f"[ERROR] Error in /ask: {e}")
            import traceback; traceback.print_exc()
            yield f"Error: {str(e)}"

    return app.response_class(generate_response(), mimetype='text/event-stream')

# ─────────────────────────────────────────
# IMAGE UPLOAD & VISION ENDPOINT
# ─────────────────────────────────────────
@app.route('/upload_image', methods=['POST'])
def upload_image_endpoint():
    user_id = get_user_id()
    chat_id = request.form.get('chat_id')
    caption = request.form.get('caption', '').strip()

    if not chat_id:
        return jsonify({"error": "Chat ID not provided."}), 400
    if 'image' not in request.files:
        return jsonify({"error": "No image file provided."}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400
    if not file.filename.lower().endswith(('png', 'jpg', 'jpeg', 'gif', 'webp')):
        return jsonify({"error": "File must be an image (PNG, JPG, GIF, WebP)."}), 400

    if get_daily_message_count(user_id) >= DAILY_MESSAGE_LIMIT:
        return jsonify({"response": f"Daily limit of {DAILY_MESSAGE_LIMIT} messages reached."}), 429

    try:
        image_data = base64.standard_b64encode(file.read()).decode('utf-8')
    except Exception as e:
        return jsonify({"error": f"Error reading image: {str(e)}"}), 400

    def stream_image_response():
        try:
            current_chat_history = load_chat_history_from_file(user_id, chat_id)

            caption_context = f"\nStudent's note about this problem: {caption}" if caption else ""

            vision_messages = [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"Solve this math problem from my SEE Class 10 textbook/exam paper.{caption_context}"},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": image_data
                            }
                        }
                    ]
                }
            ]

            print(f"[DEBUG] Processing vision image...")
            r = call_gemini_api(vision_messages, system_prompt=VISION_SYSTEM_PROMPT)

            if not r or r.status_code != 200:
                yield f"Sorry, could not process the image. Please try again or type the question instead."
                return

            text = extract_gemini_text(r)
            if not text:
                yield "Sorry, could not extract a solution from the image."
                return

            # Save to history
            user_msg = f"[Image] {caption if caption else 'Math problem image'}"
            current_chat_history.append({"type": "user",  "text": user_msg, "timestamp": time.time()})
            current_chat_history.append({"type": "bot",   "text": text,     "timestamp": time.time()})
            save_chat_history_to_file(user_id, chat_id, current_chat_history)
            increment_daily_message_count(user_id)

            yield from stream_text(text)

        except Exception as e:
            print(f"[ERROR] Vision error: {e}")
            import traceback; traceback.print_exc()
            yield f"Error: {str(e)}"

    return app.response_class(stream_image_response(), mimetype='text/event-stream')

# ─────────────────────────────────────────
# CHAT MANAGEMENT ENDPOINTS
# ─────────────────────────────────────────
@app.route('/start_new_chat', methods=['POST'])
def start_new_chat_endpoint():
    user_id = get_user_id()
    new_chat_id = str(uuid.uuid4())
    save_chat_history_to_file(user_id, new_chat_id, [])
    has_previous = any(
        f.startswith(f"{user_id}_") and f.endswith(".json") and f != f"{user_id}_{new_chat_id}.json"
        for f in os.listdir(CHAT_HISTORY_DIR)
    )
    return jsonify({"status": "success", "chat_id": new_chat_id, "has_previous_chats": has_previous})

@app.route('/clear_all_chats', methods=['POST'])
def clear_all_chats_endpoint():
    user_id = get_user_id()
    try:
        count = sum(
            1 for f in os.listdir(CHAT_HISTORY_DIR)
            if f.startswith(f"{user_id}_") and f.endswith(".json")
            and not os.remove(os.path.join(CHAT_HISTORY_DIR, f))
        )
        return jsonify({"status": "success", "message": f"Cleared {count} chats."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_chat_history_list', methods=['GET'])
def get_chat_history_list():
    user_id = get_user_id()
    files = [f for f in os.listdir(CHAT_HISTORY_DIR)
            if f.startswith(f"{user_id}_") and f.endswith(".json")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(CHAT_HISTORY_DIR, f)), reverse=True)

    summaries = []
    for filename in files:
        chat_id = filename.replace(f"{user_id}_", "").replace(".json", "")
        chat_data = load_chat_history_from_file(user_id, chat_id)
        title = "New Chat"
        if chat_data:
            first = next((m for m in chat_data if m['type'] == 'user' and m['text'].strip()), None)
            if first:
                t = first['text'].split('\n')[0][:30]
                title = t + ("..." if len(first['text'].split('\n')[0]) > 30 else "")
        summaries.append({'id': chat_id, 'title': title})
    return jsonify(summaries)

@app.route('/get_chat_messages/<chat_id>', methods=['GET'])
def get_chat_messages(chat_id):
    user_id = get_user_id()
    return jsonify(load_chat_history_from_file(user_id, chat_id))

# ─────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────
@app.route('/login')
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/guest_login')
def guest_login():
    """Logs in the user as a guest."""
    session.clear()
    temp_id = str(uuid.uuid4())
    session['temp_user_id'] = temp_id
    session['user_id'] = temp_id
    session['is_guest'] = True
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/user_info', methods=['GET'])
def user_info():
    return jsonify({"user_email": session.get('user', None)})

@app.route('/google_login/authorized')
def google_login_authorized():
    if not google.authorized:
        return redirect(url_for("login"))
    try:
        info = google.get("/oauth2/v2/userinfo")
        if info.ok:
            session['user'] = info.json().get("email")
            session['user_id'] = f"google_{info.json().get('id')}"
            return redirect(url_for('index'))
    except Exception as e:
        print(f"Google login error: {e}")
    return redirect(url_for('login'))

# ─────────────────────────────────────────
# MAIN ROUTES
# ─────────────────────────────────────────
@app.route('/')
def index():
    """Main index route - redirect to login if not authenticated."""
    # Check if user is logged in (has user_id in session)
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/debug/test-gemini', methods=['GET'])
def debug_test_gemini():
    msgs = [{"role": "user", "parts": [{"text": "What is 2+2? One sentence."}]}]
    r = call_gemini_api(msgs)
    if not r or r.status_code != 200:
        return jsonify({"error": f"Gemini failed: {r.status_code if r else 'None'}", "key_set": bool(GOOGLE_GEMINI_API_KEY)})
    text = extract_gemini_text(r)
    return jsonify({"success": True, "response": text, "status_code": r.status_code})

if __name__ == '__main__':
    print("[STARTUP] Starting Vexara API on http://0.0.0.0:5000")
    print("[STARTUP] Endpoints: /ask (POST), /upload_image (POST), /start_new_chat (POST)")
    print(f"[STARTUP] Gemini API Key set: {bool(GOOGLE_GEMINI_API_KEY)}")
    app.run(debug=True, host='0.0.0.0', port=5000)
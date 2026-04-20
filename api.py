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

app_name = '__main__'
if '__app_id__' in globals():
    app_name = globals()['__app_id__']
app = Flask(app_name)

CORS(app, resources={r"/*": {"origins": "*"}})

app.secret_key = os.environ.get("FLASK_SECRET_KEY", str(uuid.uuid4()))

# --- API KEYS ---
GOOGLE_GEMINI_API_KEY = os.environ.get("GOOGLE_GEMINI_API_KEY")
AWAN_API_KEY = os.environ.get("AWAN_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")

# --- API Endpoints ---
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
AWAN_API_URL = "https://api.awanllm.com/v1/chat/completions"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# --- Models ---
GEMINI_MODEL = "gemini-2.5-flash"
AWAN_MODEL = "Meta-Llama-3-8B-Instruct"
GROQ_MODEL = "llama-3.1-8b-instant"  # Stable, always available
OPENROUTER_GENERAL_MODEL = "mistralai/mistral-small-3.2-24b-instruct:free"
OPENROUTER_DEEPTHINK_MODEL = "google/gemma-4-31b-it:free"

# Directories
CHAT_HISTORY_DIR = os.path.join(app.root_path, 'chat_history')
os.makedirs(CHAT_HISTORY_DIR, exist_ok=True)
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Quota Tracking ---
user_message_counts = {}
DAILY_MESSAGE_LIMIT = 20

def get_daily_message_count(user_id):
    """Retrieves the message count for the current user and day."""
    today_str = date.today().isoformat()
    if user_id not in user_message_counts:
        user_message_counts[user_id] = {}
    if today_str not in user_message_counts[user_id]:
        user_message_counts[user_id][today_str] = 0
    return user_message_counts[user_id][today_str]

def increment_daily_message_count(user_id):
    """Increments the message count for the current user and day."""
    today_str = date.today().isoformat()
    if user_id not in user_message_counts:
        user_message_counts[user_id] = {}
    if today_str not in user_message_counts[user_id]:
        user_message_counts[user_id][today_str] = 0
    user_message_counts[user_id][today_str] += 1
    one_week_ago = (date.today() - timedelta(days=7)).isoformat()
    for d_str in list(user_message_counts[user_id].keys()):
        if d_str < one_week_ago:
            del user_message_counts[user_id][d_str]

# OAuth configuration
google_bp = make_google_blueprint(
    client_id="978102306464-qdjll3uos10m1nd5gcnr9iql9688db58.apps.googleusercontent.com",
    client_secret="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",
    redirect_url="/google_login/authorized",
    scope=["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"]
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

# --- SEE SYSTEM PROMPT (CRITICAL FOR EXAM-FOCUSED ANSWERS) ---
SEE_SYSTEM_PROMPT = """You are Vexara, an expert Math tutor for Class 10 SEE (Secondary Education Examination) students in Nepal. You teach ALL chapters of the Class 10 Compulsory Mathematics curriculum.

═══════════════════════════════════════════
CORE ANSWER FORMAT RULES
═══════════════════════════════════════════

- ALWAYS use ⇒ for each calculation step
- NEVER write "Step 1:", "Step 2:" etc.
- For word problems: define variables FIRST, then solve
- For follow-up questions ("explain", "why", "what does this mean"): explain in simple Nepali-student-friendly language
- Keep answers SEE exam pattern: clear, structured, no fluff
- Only refuse completely off-topic questions (weather, movies, etc.)

═══════════════════════════════════════════
CHAPTER 1: SETS
═══════════════════════════════════════════

Key concepts: Set notation, types of sets, Venn diagrams, set operations (union ∪, intersection ∩, difference −, complement A')

Cardinality formula: n(A∪B) = n(A) + n(B) − n(A∩B)
For three sets: n(A∪B∪C) = n(A)+n(B)+n(C) − n(A∩B) − n(B∩C) − n(A∩C) + n(A∩B∩C)

Example — In a class of 40 students, 25 like football, 20 like cricket, 10 like both. How many like neither?

Let F = football, C = cricket
n(F) = 25, n(C) = 20, n(F∩C) = 10, Total = 40

⇒ n(F∪C) = 25 + 20 − 10
⇒ n(F∪C) = 35
⇒ Neither = 40 − 35
⇒ Neither = 5 students

Always draw/describe Venn diagram when helpful. For three-set word problems, use the full formula.

═══════════════════════════════════════════
CHAPTER 2: COMPOUND INTEREST
═══════════════════════════════════════════

Key formulas:
- Compound Interest: A = P(1 + R/100)^T
- CI = A − P
- Half-yearly: A = P(1 + R/200)^(2T)
- Quarterly: A = P(1 + R/400)^(4T)

Example — Find compound interest on Rs 10,000 at 10% p.a. for 2 years.

P = 10000, R = 10%, T = 2

⇒ A = 10000 × (1 + 10/100)²
⇒ A = 10000 × (1.1)²
⇒ A = 10000 × 1.21
⇒ A = Rs 12,100

⇒ CI = 12100 − 10000
⇒ CI = Rs 2,100

Always show full formula substitution. State P, R, T clearly first.

═══════════════════════════════════════════
CHAPTER 3: GROWTH AND DEPRECIATION
═══════════════════════════════════════════

Key formulas:
- Population Growth: P_T = P_0 × (1 + R/100)^T
- Depreciation: V_T = V_0 × (1 − R/100)^T

Example — A machine worth Rs 50,000 depreciates at 10% per year. Find its value after 3 years.

V_0 = 50000, R = 10%, T = 3

⇒ V_3 = 50000 × (1 − 10/100)³
⇒ V_3 = 50000 × (0.9)³
⇒ V_3 = 50000 × 0.729
⇒ V_3 = Rs 36,450

Distinguish clearly: growth uses (1 + R/100), depreciation uses (1 − R/100).

═══════════════════════════════════════════
CHAPTER 4: CURRENCY AND EXCHANGE RATE
═══════════════════════════════════════════

Key concepts: Buying rate, selling rate, commission, conversion between currencies

Formula:
- Buying (bank buys foreign): Local = Foreign × Buying Rate
- Selling (bank sells foreign): Local = Foreign × Selling Rate

Example — If 1 USD = NPR 132 (buying) and NPR 133 (selling). Convert USD 500 to NPR (tourist selling USD to bank).

Bank buys USD from tourist → use buying rate

⇒ NPR = 500 × 132
⇒ NPR = Rs 66,000

For commission: Deduct commission% from the received amount.

Always clarify WHO is buying/selling (bank or customer) to pick correct rate.

═══════════════════════════════════════════
CHAPTER 5: AREA AND VOLUME
═══════════════════════════════════════════

Key formulas:

AREA:
- Triangle: ½ × b × h | Heron's: √[s(s-a)(s-b)(s-c)]
- Rectangle: l × b
- Parallelogram: b × h
- Trapezium: ½(a+b) × h
- Circle: πr²  | Semicircle: πr²/2
- Sector: (θ/360) × πr²

SURFACE AREA:
- Cuboid: 2(lb + bh + lh)
- Cylinder: 2πr(r+h) | Curved: 2πrh
- Cone: πr(r+l) where l=slant height | Curved: πrl
- Sphere: 4πr² | Hemisphere: 3πr²

VOLUME:
- Cuboid: l × b × h
- Cylinder: πr²h
- Cone: (1/3)πr²h
- Sphere: (4/3)πr³ | Hemisphere: (2/3)πr³
- Pyramid: (1/3) × base area × height

Always write the formula first, then substitute values, then solve with ⇒.

═══════════════════════════════════════════
CHAPTER 6: SEQUENCE AND SERIES
═══════════════════════════════════════════

ARITHMETIC PROGRESSION (AP):
- nth term: Tn = a + (n−1)d
- Sum: Sn = n/2 × [2a + (n−1)d] or Sn = n/2 × (a + l)

GEOMETRIC PROGRESSION (GP):
- nth term: Tn = ar^(n−1)
- Sum: Sn = a(rⁿ − 1)/(r − 1) for r ≠ 1

Example — Find the 10th term of AP: 3, 7, 11, 15...

a = 3, d = 7−3 = 4, n = 10

⇒ T₁₀ = 3 + (10−1) × 4
⇒ T₁₀ = 3 + 36
⇒ T₁₀ = 39

Always identify a (first term) and d or r before solving.

═══════════════════════════════════════════
CHAPTER 7: QUADRATIC EQUATION
═══════════════════════════════════════════

Methods: Factorization, Completing the Square, Quadratic Formula

Quadratic Formula: x = [−b ± √(b²−4ac)] / 2a

Example — Solve: x² − 5x + 6 = 0 by factorization

⇒ x² − 3x − 2x + 6 = 0
⇒ x(x − 3) − 2(x − 3) = 0
⇒ (x − 2)(x − 3) = 0
⇒ x = 2 or x = 3

For word problems: form equation first, then solve. Always verify answers.
Nature of roots: D = b²−4ac → D>0 real distinct, D=0 real equal, D<0 no real roots.

═══════════════════════════════════════════
CHAPTER 8: ALGEBRAIC FRACTION
═══════════════════════════════════════════

Key skills: Simplification, LCM, addition/subtraction/multiplication/division of fractions

Example — Simplify: (x²−4)/(x²−x−2)

⇒ = (x+2)(x−2) / (x−2)(x+1)
⇒ = (x+2)/(x+1)   [cancel (x−2)]

Always fully factorize numerator and denominator before cancelling. State restrictions (x ≠ 2, x ≠ −1 etc).

═══════════════════════════════════════════
CHAPTER 9: INDICES (EXPONENTS)
═══════════════════════════════════════════

Laws of Indices:
- aᵐ × aⁿ = aᵐ⁺ⁿ
- aᵐ ÷ aⁿ = aᵐ⁻ⁿ
- (aᵐ)ⁿ = aᵐⁿ
- a⁰ = 1
- a⁻ⁿ = 1/aⁿ
- a^(1/n) = ⁿ√a
- (ab)ⁿ = aⁿbⁿ

Example — Simplify: (2³ × 2⁴) ÷ 2⁵

⇒ = 2^(3+4) ÷ 2⁵
⇒ = 2⁷ ÷ 2⁵
⇒ = 2^(7−5)
⇒ = 2² = 4

Always convert to same base before applying laws.

═══════════════════════════════════════════
CHAPTER 10: TRIANGLES AND QUADRILATERALS
═══════════════════════════════════════════

Key theorems:
- Pythagoras: a² + b² = c² (right triangle)
- Angle sum of triangle = 180°
- Exterior angle = sum of two non-adjacent interior angles
- Properties of parallelogram, rhombus, rectangle, square, trapezium
- Similar triangles: AA, SAS, SSS criteria → corresponding sides proportional

Example — In right triangle, legs = 6cm and 8cm. Find hypotenuse.

⇒ c² = 6² + 8²
⇒ c² = 36 + 64
⇒ c² = 100
⇒ c = 10 cm

For similarity problems, always write the ratio of corresponding sides clearly.

═══════════════════════════════════════════
CHAPTER 11: CONSTRUCTION
═══════════════════════════════════════════

Key constructions (describe steps clearly since this is text-based):
- Bisecting an angle / line segment
- Constructing parallel lines
- Constructing triangles given different conditions (SSS, SAS, ASA)
- Constructing similar triangles
- Circumscribed and inscribed circles of triangles

For construction questions: list each step clearly and numbered. Describe compass and ruler movements in detail. If it's a calculation within construction (like finding scale factor), solve with ⇒ format.

═══════════════════════════════════════════
CHAPTER 12: CIRCLE
═══════════════════════════════════════════

Key theorems:
- Angle at centre = 2 × angle at circumference (same arc)
- Angles in same segment are equal
- Angle in semicircle = 90°
- Opposite angles of cyclic quadrilateral = 180°
- Tangent ⊥ radius at point of contact
- Two tangents from external point are equal
- Tangent-chord angle = inscribed angle in alternate segment

Example — O is centre, arc AB subtends 80° at centre. Find angle at circumference.

⇒ Angle at circumference = 80°/2
⇒ Angle at circumference = 40°

Always state which theorem you're using.

═══════════════════════════════════════════
CHAPTER 13: STATISTICS
═══════════════════════════════════════════

Measures of Central Tendency:
- Mean (ungrouped): x̄ = Σx/n
- Mean (grouped): x̄ = Σfx/Σf  or  x̄ = A + Σfd/Σf (step deviation)
- Median (grouped): M = L + [(n/2 − cf)/f] × h
- Mode (grouped): Mo = L + [f₁−f₀ / 2f₁−f₀−f₂] × h

Measures of Dispersion:
- Quartiles Q1, Q2, Q3
- Interquartile range: IQR = Q3 − Q1
- Mean Deviation: MD = Σf|x−x̄| / Σf
- Standard Deviation: σ = √[Σf(x−x̄)²/Σf]

Always show frequency table clearly. Show cumulative frequency for median. State class boundaries carefully.

═══════════════════════════════════════════
CHAPTER 14: PROBABILITY
═══════════════════════════════════════════

Key formulas:
- P(A) = favourable outcomes / total outcomes
- 0 ≤ P(A) ≤ 1
- P(A') = 1 − P(A)
- P(A∪B) = P(A) + P(B) − P(A∩B)
- Independent events: P(A∩B) = P(A) × P(B)
- Mutually exclusive: P(A∩B) = 0

Example — A bag has 3 red and 5 blue balls. Find probability of drawing a red ball.

Total = 3 + 5 = 8
Favourable (red) = 3

⇒ P(red) = 3/8

For combined events (two dice, two cards etc.): list sample space or use multiplication rule. Always simplify final fraction.

═══════════════════════════════════════════
CHAPTER 15: TRIGONOMETRY (BASICS)
═══════════════════════════════════════════

Trigonometric Ratios (Right Triangle):
- sin θ = Opposite/Hypotenuse (P/H)
- cos θ = Adjacent/Hypotenuse (B/H)
- tan θ = Opposite/Adjacent (P/B)
- cosec θ = H/P = 1/sin θ
- sec θ = H/B = 1/cos θ
- cot θ = B/P = 1/tan θ

Standard Values:
| θ    | 0°  | 30°  | 45°       | 60°       | 90° |
|------|-----|------|-----------|-----------|-----|
| sin  | 0   | 1/2  | 1/√2      | √3/2      | 1   |
| cos  | 1   | √3/2 | 1/√2      | 1/2       | 0   |
| tan  | 0   | 1/√3 | 1         | √3        | ∞   |

Key Identities:
- sin²θ + cos²θ = 1
- 1 + tan²θ = sec²θ
- 1 + cot²θ = cosec²θ

Example — If sin θ = 3/5, find cos θ and tan θ.

Using sin²θ + cos²θ = 1:
⇒ (3/5)² + cos²θ = 1
⇒ 9/25 + cos²θ = 1
⇒ cos²θ = 1 − 9/25
⇒ cos²θ = 16/25
⇒ cos θ = 4/5

⇒ tan θ = sin θ / cos θ = (3/5)/(4/5)
⇒ tan θ = 3/4

Heights & Distances:
- Angle of elevation: looking UP from horizontal
- Angle of depression: looking DOWN from horizontal
- Always draw a right triangle, label known sides/angles, then apply trig ratios

Example — A tower is 30m tall. From a point on ground, angle of elevation = 60°. Find distance from base.

Let distance = x

⇒ tan 60° = 30/x
⇒ √3 = 30/x
⇒ x = 30/√3
⇒ x = 30√3/3
⇒ x = 10√3 m

═══════════════════════════════════════════
GENERAL RULES FOR ALL CHAPTERS
═══════════════════════════════════════════

1. Always write the relevant formula before substituting
2. Always use ⇒ for each calculation step
3. Box or clearly state the final answer
4. For word problems: read carefully, identify what is given and what is asked
5. For SEE exam: answers must be clean, complete, and show full working
6. If student makes a mistake, gently correct and show the right approach
7. If student asks to explain a concept: explain simply with a relatable example from Nepal context (rupees, distance in km, population, etc.)
8. Encourage students — math is solvable step by step!"""
# --- CHAT HISTORY MANAGEMENT ---
def get_user_id():
    """Gets a unique user ID. Prefers authenticated user ID."""
    if 'user_id' in session:
        return session['user_id']
    if 'temp_user_id' not in session:
        session['temp_user_id'] = str(uuid.uuid4())
        session['user_id'] = session['temp_user_id']
    return session['temp_user_id']

def get_chat_file_path(user_id, chat_id):
    """Constructs the file path for a specific chat history."""
    safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
    return os.path.join(CHAT_HISTORY_DIR, f"{safe_user_id}_{chat_id}.json")

def load_chat_history_from_file(user_id, chat_id):
    """Loads chat history for a given user and chat ID from a JSON file."""
    file_path = get_chat_file_path(user_id, chat_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"Warning: Could not decode JSON from {file_path}. Starting with empty chat.")
            return []
        except Exception as e:
            print(f"Error loading chat history from {file_path}: {e}")
            return []
    return []

def save_chat_history_to_file(user_id, chat_id, chat_data):
    """Saves chat history for a given user and chat ID to a JSON file."""
    file_path = get_chat_file_path(user_id, chat_id)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(chat_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving chat history to {file_path}: {e}")

# --- HELPER: Build chat context for API ---
def build_gemini_messages(chat_history, new_instruction):
    """
    Builds the message list for Gemini API from chat history.
    Includes system prompt as first message context.
    """
    messages = []
    
    # Convert chat history to Gemini format
    for msg in chat_history:
        if msg.get('type') == 'user':
            messages.append({
                "role": "user",
                "parts": [{"text": msg.get('text', '')}]
            })
        elif msg.get('type') == 'bot':
            messages.append({
                "role": "model",
                "parts": [{"text": msg.get('text', '')}]
            })
    
    # Add the new user instruction
    messages.append({
        "role": "user",
        "parts": [{"text": new_instruction}]
    })
    
    return messages

def build_chat_completion_messages(chat_history, new_instruction):
    """
    Builds message list for OpenAI-compatible APIs (Groq, OpenRouter, Awan).
    """
    messages = [
        {"role": "system", "content": SEE_SYSTEM_PROMPT}
    ]
    
    # Add chat history
    for msg in chat_history:
        if msg.get('type') == 'user':
            messages.append({"role": "user", "content": msg.get('text', '')})
        elif msg.get('type') == 'bot':
            messages.append({"role": "assistant", "content": msg.get('text', '')})
    
    # Add new instruction
    messages.append({"role": "user", "content": new_instruction})
    
    return messages

# --- GEMINI API CALL ---
def call_gemini_api(messages, stream=False):
    """Calls Gemini API (Gemini does NOT support streaming via REST API)."""
    payload = {
        "contents": messages,
        "systemInstruction": {
            "parts": [{"text": SEE_SYSTEM_PROMPT}]
        },
        "generationConfig": {
            "temperature": 0.7,
            "topK": 40,
            "topP": 0.95,
            "maxOutputTokens": 2048,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    }
    
    url = f"{GEMINI_API_URL}?key={GOOGLE_GEMINI_API_KEY}"
    
    try:
        print(f"[DEBUG] Calling Gemini API (non-streaming) with {len(messages)} messages")
        response = requests.post(url, json=payload, timeout=60)
        print(f"[DEBUG] Gemini response status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"[DEBUG] Gemini error response: {response.text[:500]}")
        
        response.raise_for_status()
        return response
    except Exception as e:
        print(f"Gemini API error: {e}")
        print(f"[DEBUG] API Key set: {bool(GOOGLE_GEMINI_API_KEY)}")
        if GOOGLE_GEMINI_API_KEY:
            print(f"[DEBUG] Key preview: {GOOGLE_GEMINI_API_KEY[:20]}...")
        return None

# --- GROQ API CALL ---
def call_groq_api(messages, stream=True):
    """Calls Groq API (fast LLM)."""
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 2048,
        "stream": stream
    }
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        print(f"[DEBUG] Calling Groq with payload keys: {list(payload.keys())}")
        response = requests.post(GROQ_API_URL, json=payload, headers=headers, stream=stream, timeout=60)
        print(f"[DEBUG] Groq status: {response.status_code}")
        if response.status_code != 200:
            print(f"[DEBUG] Groq error response: {response.text[:500]}")
        response.raise_for_status()
        return response
    except Exception as e:
        print(f"Groq API error: {e}")
        return None

# --- OPENROUTER API CALL ---
def call_openrouter_api(messages, model, stream=True):
    """Calls OpenRouter API."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
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
        response = requests.post(OPENROUTER_API_URL, json=payload, headers=headers, stream=stream, timeout=60)
        response.raise_for_status()
        return response
    except Exception as e:
        print(f"OpenRouter API error: {e}")
        return None

# --- MAIN /ask ENDPOINT (IMPROVED WITH SEE CONTEXT) ---
@app.route('/ask', methods=['POST'])
def ask_endpoint():
    """Main Q&A endpoint with SEE-specific prompting."""
    user_id = get_user_id()
    chat_id = request.form.get('chat_id')
    instruction = request.form.get('instruction', '').strip()
    model_choice = request.form.get('model_choice', 'general')
    web_search_enabled = request.form.get('web_search', 'false').lower() == 'true'
    
    if not chat_id:
        return jsonify({"error": "Chat ID not provided."}), 400
    if not instruction:
        return jsonify({"error": "No instruction provided."}), 400
    
    # Check quota
    current_message_count = get_daily_message_count(user_id)
    if current_message_count >= DAILY_MESSAGE_LIMIT:
        return jsonify({"response": f"You have reached your daily message limit of {DAILY_MESSAGE_LIMIT}. Please try again tomorrow."}), 429
    
    # Load chat history
    current_chat_history = load_chat_history_from_file(user_id, chat_id)
    
    # Save user message to history
    current_chat_history.append({"type": "user", "text": instruction, "timestamp": time.time()})
    save_chat_history_to_file(user_id, chat_id, current_chat_history)
    
    # Increment quota
    increment_daily_message_count(user_id)
    
    def generate_response():
        """Generator function for streaming response."""
        try:
            # Build messages for API
            gemini_messages = build_gemini_messages(current_chat_history, instruction)
            completion_messages = build_chat_completion_messages(current_chat_history, instruction)
            
            response = None
            full_response = ""
            
            # Try primary model based on choice
            if model_choice == "deep_think":
                # Use DeepThink model for complex problems
                print(f"Using DeepThink model for: {instruction[:50]}...")
                response = call_openrouter_api(completion_messages, OPENROUTER_DEEPTHINK_MODEL, stream=True)
                
                # Handle streaming for DeepThink
                if response and response.status_code == 200:
                    try:
                        for line in response.iter_lines():
                            if line:
                                line_str = line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                                if line_str.startswith('data: '):
                                    try:
                                        data = json.loads(line_str[6:])
                                        if 'choices' in data and len(data['choices']) > 0:
                                            choice = data['choices'][0]
                                            if 'delta' in choice and 'content' in choice['delta']:
                                                chunk = choice['delta']['content']
                                                full_response += chunk
                                                yield chunk
                                    except (json.JSONDecodeError, KeyError, TypeError):
                                        continue
                    except Exception as e:
                        print(f"DeepThink streaming error: {e}")
                
                # If DeepThink fails, fall back to Gemini (not Groq)
                if not full_response:
                    print("DeepThink failed or rate limited, falling back to Gemini...")
                    response = call_gemini_api(gemini_messages, stream=False)
                    
                    if response and response.status_code == 200:
                        try:
                            data = response.json()
                            if 'candidates' in data and len(data['candidates']) > 0:
                                candidate = data['candidates'][0]
                                if 'content' in candidate and 'parts' in candidate['content']:
                                    for part in candidate['content']['parts']:
                                        if 'text' in part:
                                            full_response = part['text']
                                            # Stream chunks to UI
                                            words = full_response.split(' ')
                                            chunk = ""
                                            for word in words:
                                                chunk += word + " "
                                                if len(chunk) > 50:
                                                    yield chunk
                                                    chunk = ""
                                            if chunk:
                                                yield chunk
                        except Exception as e:
                            print(f"Fallback Gemini error: {e}")
                    
            elif model_choice == "general":
                # Use Gemini for general questions (NO STREAMING - GET FULL RESPONSE)
                print(f"Using Gemini for: {instruction[:50]}...")
                response = call_gemini_api(gemini_messages, stream=False)
                
                if response and response.status_code == 200:
                    try:
                        data = response.json()
                        print(f"[DEBUG] Gemini response received")
                        
                        if 'candidates' in data and len(data['candidates']) > 0:
                            candidate = data['candidates'][0]
                            if 'content' in candidate and 'parts' in candidate['content']:
                                for part in candidate['content']['parts']:
                                    if 'text' in part:
                                        full_response = part['text']
                                        # Stream the response in chunks for UI
                                        # Split by sentences and yield gradually
                                        words = full_response.split(' ')
                                        chunk = ""
                                        for word in words:
                                            chunk += word + " "
                                            if len(chunk) > 50:  # Yield every ~50 chars
                                                yield chunk
                                                chunk = ""
                                        if chunk:
                                            yield chunk
                    except Exception as e:
                        print(f"Gemini JSON parse error: {e}")
                        yield f"Error parsing Gemini response: {str(e)}"
                else:
                    print(f"Gemini API failed with status {response.status_code if response else 'None'}")
                    # Fall back to Groq
                    print("Gemini failed, trying Groq...")
                    response = call_groq_api(completion_messages, stream=True)
                    
                    if response and response.status_code == 200:
                        try:
                            for line in response.iter_lines():
                                if line:
                                    line_str = line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                                    if line_str.startswith('data: '):
                                        try:
                                            data = json.loads(line_str[6:])
                                            if 'choices' in data and len(data['choices']) > 0:
                                                choice = data['choices'][0]
                                                if 'delta' in choice and 'content' in choice['delta']:
                                                    chunk = choice['delta']['content']
                                                    full_response += chunk
                                                    yield chunk
                                        except (json.JSONDecodeError, KeyError, TypeError):
                                            continue
                        except Exception as e:
                            print(f"Groq streaming error: {e}")
            
            if not full_response:
                yield "Error: Could not get a response from AI models. Please try again."
                return
            
            # Save bot response to history
            current_chat_history.append({"type": "bot", "text": full_response, "timestamp": time.time()})
            save_chat_history_to_file(user_id, chat_id, current_chat_history)
            
        except Exception as e:
            print(f"Error in /ask: {e}")
            import traceback
            traceback.print_exc()
            yield f"Error: {str(e)}"
    
    return app.response_class(generate_response(), mimetype='text/event-stream')

# --- OTHER REQUIRED ENDPOINTS (STUB VERSIONS) ---
@app.route('/start_new_chat', methods=['POST'])
def start_new_chat_endpoint():
    """Starts a new chat session."""
    user_id = get_user_id()
    new_chat_id = str(uuid.uuid4())
    save_chat_history_to_file(user_id, new_chat_id, [])
    
    has_previous_chats = False
    for filename in os.listdir(CHAT_HISTORY_DIR):
        if filename.startswith(f"{user_id}_") and filename.endswith(".json") and filename != f"{user_id}_{new_chat_id}.json":
            has_previous_chats = True
            break
    
    return jsonify({"status": "success", "chat_id": new_chat_id, "has_previous_chats": has_previous_chats})

@app.route('/clear_all_chats', methods=['POST'])
def clear_all_chats_endpoint():
    """Deletes all chat history files for the current user."""
    user_id = get_user_id()
    try:
        count = 0
        for filename in os.listdir(CHAT_HISTORY_DIR):
            if filename.startswith(f"{user_id}_") and filename.endswith(".json"):
                os.remove(os.path.join(CHAT_HISTORY_DIR, filename))
                count += 1
        return jsonify({"status": "success", "message": f"Cleared {count} chats."})
    except Exception as e:
        return jsonify({"status": "error", "message": "Failed to clear all chats.", "error": str(e)}), 500

@app.route('/get_chat_history_list', methods=['GET'])
def get_chat_history_list():
    """Returns a list of chat summaries for the current user."""
    user_id = get_user_id()
    chat_summaries = []
    
    user_chat_files = [f for f in os.listdir(CHAT_HISTORY_DIR) if f.startswith(f"{user_id}_") and f.endswith(".json")]
    user_chat_files.sort(key=lambda f: os.path.getmtime(os.path.join(CHAT_HISTORY_DIR, f)), reverse=True)
    
    for filename in user_chat_files:
        chat_id = filename.replace(f"{user_id}_", "").replace(".json", "")
        chat_data = load_chat_history_from_file(user_id, chat_id)
        
        display_title = "New Chat"
        if chat_data:
            first_meaningful_message = next((
                msg for msg in chat_data 
                if msg['type'] == 'user' and msg['text'].strip()
            ), None)
            if first_meaningful_message:
                display_title = first_meaningful_message['text'].split('\n')[0][:30]
                if len(first_meaningful_message['text'].split('\n')[0]) > 30:
                    display_title += "..."
        
        chat_summaries.append({'id': chat_id, 'title': display_title})
    
    return jsonify(chat_summaries)

@app.route('/get_chat_messages/<chat_id>', methods=['GET'])
def get_chat_messages(chat_id):
    """Returns the full chat message history for a given chat ID."""
    user_id = get_user_id()
    chat_data = load_chat_history_from_file(user_id, chat_id)
    return jsonify(chat_data)

@app.route('/login')
def login():
    """Handles user login."""
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
    """Logs out the user."""
    session.clear()
    return redirect(url_for('login'))

@app.route('/user_info', methods=['GET'])
def user_info():
    """Returns basic user information."""
    user_email = session.get('user', None)
    return jsonify({"user_email": user_email})

@app.route('/google_login/authorized')
def google_login_authorized():
    """Handles Google OAuth callback."""
    if not google.authorized:
        return redirect(url_for("login"))
    try:
        user_info = google.get("/oauth2/v2/userinfo")
        if user_info.ok:
            session['user'] = user_info.json().get("email")
            session['user_id'] = f"google_{user_info.json().get('id')}"
            return redirect(url_for('index'))
        else:
            return redirect(url_for('login'))
    except Exception as e:
        print(f"Error during Google login: {e}")
        return redirect(url_for('login'))

@app.route('/')
def index():
    """Main index route."""
    return render_template('index.html')

@app.route('/debug/test-gemini', methods=['GET'])
def debug_test_gemini():
    """Test Gemini API directly for debugging."""
    test_messages = [
        {
            "role": "user",
            "parts": [{"text": "What is 2+2? Answer in one sentence."}]
        }
    ]
    
    response = call_gemini_api(test_messages, stream=False)
    
    if not response or response.status_code != 200:
        return jsonify({
            "error": f"Gemini API failed with status {response.status_code if response else 'No response'}", 
            "key_set": bool(GOOGLE_GEMINI_API_KEY),
            "full_response": response.text if response else "No response"
        })
    
    try:
        data = response.json()
        print(f"[DEBUG] Full Gemini response: {json.dumps(data, indent=2)[:500]}")
        
        if 'candidates' in data and len(data['candidates']) > 0:
            candidate = data['candidates'][0]
            if 'content' in candidate and 'parts' in candidate['content']:
                for part in candidate['content']['parts']:
                    if 'text' in part:
                        return jsonify({
                            "success": True,
                            "response": part['text'],
                            "status_code": response.status_code
                        })
        
        return jsonify({
            "error": "No text found in response",
            "response_structure": str(data)[:200]
        })
    except Exception as e:
        return jsonify({
            "error": f"Error parsing response: {str(e)}",
            "response_text": response.text[:500] if response else "No response"
        })

# --- IMAGE UPLOAD & VISION ENDPOINT ---
@app.route('/upload_image', methods=['POST'])
def upload_image_endpoint():
    """Handle image upload and vision-based math problem solving."""
    user_id = get_user_id()
    chat_id = request.form.get('chat_id')
    caption = request.form.get('caption', '').strip()
    
    if not chat_id:
        return jsonify({"error": "Chat ID not provided."}), 400
    
    # Check if file is in request
    if 'image' not in request.files:
        return jsonify({"error": "No image file provided."}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400
    
    if not file.filename.lower().endswith(('png', 'jpg', 'jpeg', 'gif', 'webp')):
        return jsonify({"error": "File must be an image (PNG, JPG, GIF, WebP)."}), 400
    
    # Check quota
    current_message_count = get_daily_message_count(user_id)
    if current_message_count >= DAILY_MESSAGE_LIMIT:
        return jsonify({"response": f"You have reached your daily message limit of {DAILY_MESSAGE_LIMIT}. Please try again tomorrow."}), 429
    
    # READ FILE IMMEDIATELY (before generator starts)
    try:
        image_data = base64.standard_b64encode(file.read()).decode('utf-8')
    except Exception as e:
        print(f"Error reading file: {e}")
        return jsonify({"error": f"Error reading image file: {str(e)}"}), 400
    
    def stream_image_response():
        """Stream the vision processing response."""
        try:
            # Build the message with image
            current_chat_history = load_chat_history_from_file(user_id, chat_id)
            
            # Create vision prompt
            vision_prompt = f"""You are a math tutor specializing in SEE exam preparation for Class 10 students in Nepal.

A student has uploaded an image of a math problem. Your task is to:
1. Analyze the image and identify the math problem
2. Explain what the problem is asking (in simple terms)
3. Solve it step-by-step
4. Explain the concept behind it
5. Provide the final answer clearly

The student's caption/note about this problem: {caption if caption else 'None provided'}

Follow the same format as you would for text-based questions - make it educational and SEE-exam focused."""
            
            # Call Gemini Vision API
            print(f"[DEBUG] Processing image for math problem solving...")
            
            vision_messages = [
                {
                    "role": "user",
                    "parts": [
                        {"text": vision_prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": image_data
                            }
                        }
                    ]
                }
            ]
            
            # Call Gemini with vision
            vision_response = call_gemini_api(vision_messages, stream=False)
            
            if not vision_response or vision_response.status_code != 200:
                yield f"Error: Could not process image. Status: {vision_response.status_code if vision_response else 'None'}"
                return
            
            try:
                data = vision_response.json()
                
                if 'candidates' in data and len(data['candidates']) > 0:
                    candidate = data['candidates'][0]
                    if 'content' in candidate and 'parts' in candidate['content']:
                        for part in candidate['content']['parts']:
                            if 'text' in part:
                                full_response = part['text']
                                
                                # Save to chat history
                                user_message = f"[Image Upload] {caption if caption else 'Math problem image'}"
                                current_chat_history.append({"type": "user", "text": user_message, "timestamp": time.time()})
                                current_chat_history.append({"type": "bot", "text": full_response, "timestamp": time.time()})
                                save_chat_history_to_file(user_id, chat_id, current_chat_history)
                                
                                # Increment quota
                                increment_daily_message_count(user_id)
                                
                                # Stream the response
                                words = full_response.split(' ')
                                chunk = ""
                                for word in words:
                                    chunk += word + " "
                                    if len(chunk) > 50:
                                        yield chunk
                                        chunk = ""
                                if chunk:
                                    yield chunk
                                return
                
                yield "Error: No text extracted from image analysis."
            except Exception as e:
                print(f"Vision response parse error: {e}")
                yield f"Error parsing vision response: {str(e)}"
        
        except Exception as e:
            print(f"Image processing error: {e}")
            import traceback
            traceback.print_exc()
            yield f"Error: {str(e)}"
    
    return app.response_class(stream_image_response(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

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
from flask import send_from_directory
from flask import send_file
import tempfile
from datetime import datetime, date, timedelta
from flask_cors import CORS

current_dir = os.path.dirname(os.path.abspath(__file__))
template_path = os.path.join(current_dir, 'templates')
static_path = os.path.join(current_dir, 'static')

app_name = '__main__'
if '__app_id__' in globals():
    app_name = globals()['__app_id__']
app = Flask(app_name)

# ✅ Tell Flask where to find templates + static
app = Flask(
    app_name,
    template_folder=template_path,
    static_folder=static_path
)

# ✅ Enable CORS (Allowing frontend calls from any domain for now)
CORS(app, resources={r"/*": {"origins": "*"}})

# Use an environment variable for the secret key for better security
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
GEMINI_MODEL = "gemini-3.1-flash-lite"
AWAN_MODEL = "Meta-Llama-3-8B-Instruct"
GROQ_NORMAL_MODEL = "llama-3.1-8b-instant"  # For normal mode
GROQ_DEEPTHINK_MODEL = "llama-3.3-70b-versatile"  # For solve/deepthink mode
OPENROUTER_GENERAL_MODEL = "mistralai/mistral-small-3.2-24b-instruct:free"
OPENROUTER_DEEPTHINK_MODEL = "google/gemma-4-31b-it:free"

# Directories
CHAT_HISTORY_DIR = os.path.join(app.root_path, 'chat_history')
os.makedirs(CHAT_HISTORY_DIR, exist_ok=True)
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Load SEE Curriculum Knowledge Base ---
def load_see_curriculum():
    """Load the SEE curriculum knowledge base."""
    curriculum_path = os.path.join(app.root_path, 'see_curriculum.json')
    try:
        with open(curriculum_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print("Warning: see_curriculum.json not found. RAG system will not work.")
        return {}
    except json.JSONDecodeError:
        print("Warning: see_curriculum.json is invalid JSON.")
        return {}

SEE_CURRICULUM = load_see_curriculum()

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

# OAuth configuration (keep existing)
google_bp = make_google_blueprint(
    client_id="1032731423015-tis6kpcdvm96uni6e7p5cnek2bepnuu6.apps.googleusercontent.com",
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

# --- PROMPT TEMPLATES ---
NORMAL_SYSTEM_PROMPT = """You are Vexara, a friendly and helpful AI tutor for SEE students in Nepal.

**YOUR PERSONALITY:**
- Be friendly, encouraging, and supportive
- Use simple language that Class 10 students understand
- Keep responses concise and clear
- For greetings (hi, hello, how are you): Respond warmly and briefly
- For general questions: Be helpful but keep it simple
- For non-educational questions: Politely redirect to educational topics

**WHEN TO USE SOLVE MODE:**
If the student asks a math/science problem that requires step-by-step solving, suggest: "This looks like a problem that needs detailed solving. Click the 'Solve' button for a complete step-by-step solution!"

**EXAMPLES:**
- Student: "Hi" → "Hello! 👋 I'm Vexara, your SEE tutor. How can I help you today?"
- Student: "What is photosynthesis?" → Give brief, clear explanation
- Student: "Solve 3x + 5 = 17" → Suggest using Solve mode for detailed steps"""

DEEPTHINK_SYSTEM_PROMPT = """You are Vexara, an expert SEE exam tutor specializing in step-by-step problem solving for Class 10 students in Nepal.

**SOLVING APPROACH:**

1. **IDENTIFY**: First, identify what chapter/topic this problem belongs to
2. **GIVEN**: List all given information clearly
3. **TO FIND**: State what needs to be calculated/found
4. **FORMULA/CONCEPT**: Write the relevant formula or concept
5. **SOLUTION**: Show each step using arrow format (⇒)
6. **FINAL ANSWER**: State clearly with proper units

**FORMAT FOR MATH PROBLEMS:**

**Problem:** [Restate the problem]

**Chapter:** [Name of chapter - Sets, Algebra, Geometry, etc.]
**Marks in SEE:** [Typical marks for this type]

**Given:**
[List what's given]

**To Find:**
[What needs to be found]

**Formula:**
[Write the formula]

**Solution:**
⇒ [Step 1]
⇒ [Step 2]
⇒ [Step 3]
...

**Final Answer:** [Clear answer with units]

**SEE Tip:** [One helpful tip for exam]

**FORMAT FOR SCIENCE PROBLEMS:**

**Problem:** [Restate]

**Chapter:** [Physics/Chemistry/Biology]
**Marks in SEE:** [Typical marks]

**Given:**
[List with units]

**To Find:**
[What to calculate]

**Formula/Concept:**
[Write formula or concept]

**Solution:**
⇒ [Step 1 with units]
⇒ [Step 2 with units]

**Final Answer:** [With proper units]

**Explanation:** [Brief explanation of the concept]

**IMPORTANT RULES:**
- ALWAYS identify the chapter first
- ALWAYS use ⇒ for calculation steps
- ALWAYS include units in science problems
- For geometry: Mention drawing figure
- For word problems: Define variables first
- Keep SEE marking scheme in mind
- For theoretical questions: Give definitions, explanations, and examples"""

# --- RAG SYSTEM: Chapter Detection ---
def detect_chapter(user_question):
    """Detect which chapter/topic the question belongs to using keyword matching."""
    question_lower = user_question.lower()
    
    # Math chapter keywords
    math_chapters = {
        "sets": ["set", "union", "intersection", "venn", "subset", "universal set", "cardinality", "n(a", "n(b", "complement"],
        "arithmetic": ["compound interest", "population growth", "depreciation", "vat", "discount", "profit", "loss", "tax", "ci", "simple interest"],
        "algebra": ["indices", "simultaneous", "equation", "quadratic", "factorize", "factorise", "solve for x", "find x", "solve for y", "linear equation", "x^2", "polynomial"],
        "geometry": ["triangle", "circle", "parallelogram", "angle", "theorem", "prove that", "area of", "perimeter", "construction", "pythagoras", "congruent", "similar"],
        "trigonometry": ["sin", "cos", "tan", "trig", "angle of elevation", "angle of depression", "height and distance", "pythagoras", "theta", "sinθ", "cosθ", "tanθ"],
        "statistics": ["mean", "median", "mode", "quartile", "ogive", "frequency", "cumulative", "data", "graph", "histogram", "bar chart", "pie chart"],
        "probability": ["probability", "card", "dice", "coin", "random", "chance", "outcome", "sample space"]
    }
    
    # Science chapter keywords
    science_chapters = {
        "physics": ["force", "pressure", "energy", "light", "electricity", "heat", "ohm", "voltage", "current", "resistance", "power", "work", "lens", "mirror", "reflection", "refraction", "circuit", "magnet", "wave", "sound"],
        "chemistry": ["chemical", "reaction", "acid", "base", "salt", "metal", "non-metal", "organic", "carbon", "compound", "element", "equation", "balance", "mole", "ph", "gas", "oxygen", "hydrogen", "nitrogen"],
        "biology": ["cell", "tissue", "organ", "plant", "animal", "human", "digestive", "respiratory", "circulatory", "nervous", "reproduction", "genetics", "dna", "photosynthesis", "enzyme", "hormone", "bacteria", "virus"],
        "astronomy_geology": ["earth", "planet", "solar", "sun", "moon", "star", "galaxy", "volcano", "earthquake", "weather", "climate", "greenhouse", "ozone", "atmosphere", "plate tectonic", "natural disaster"]
    }
    
    detected_chapter = None
    detected_subject = None
    max_score = 0
    
    # Check math chapters
    for chapter, keywords in math_chapters.items():
        score = sum(1 for kw in keywords if kw in question_lower)
        if score > max_score:
            max_score = score
            detected_chapter = chapter
            detected_subject = "mathematics"
    
    # Check science chapters
    for chapter, keywords in science_chapters.items():
        score = sum(1 for kw in keywords if kw in question_lower)
        if score > max_score:
            max_score = score
            detected_chapter = chapter
            detected_subject = "science"
    
    return detected_subject, detected_chapter, max_score

def get_chapter_context(subject, chapter):
    """Retrieve relevant chapter context from curriculum knowledge base."""
    if not SEE_CURRICULUM or not subject or not chapter:
        return ""
    
    try:
        chapter_data = SEE_CURRICULUM["subjects"][subject]["chapters"][chapter]
        
        context = f"""
**SEE Chapter Information:**
Subject: {subject.title()}
Chapter: {chapter.replace('_', ' ').title()}
SEE Marks: {chapter_data.get('marks_distribution', 'Varies')}
Key Topics: {', '.join(chapter_data.get('topics', []))}
Common Questions: {', '.join(chapter_data.get('common_questions', []))}

**Solving Approach:**
{chapter_data.get('solving_approach', 'Standard step-by-step method')}

**Key Formulas/Concepts:**
{chr(10).join(f'• {formula}' for formula in chapter_data.get('key_formulas', chapter_data.get('key_concepts', [])))}

**SEE Tips:**
{chapter_data.get('see_tips', 'Show all steps clearly')}
"""
        return context
    except KeyError:
        return ""

def should_use_deepthink(question):
    """Determine if question needs deepthink/solve mode."""
    question_lower = question.lower()
    
    # Keywords that indicate problem-solving needed
    solve_keywords = [
        "solve", "calculate", "find", "prove", "show that", "evaluate",
        "determine", "compute", "what is the value", "simplify", "factorize",
        "factorise", "draw", "construct", "balance", "derive", "if", "then find"
    ]
    
    # Keywords that indicate conceptual questions (no solve mode)
    concept_keywords = [
        "what is", "define", "explain", "describe", "why", "how does",
        "what are", "difference between", "list", "state", "name"
    ]
    
    # If question has numbers or equations, likely needs solving
    has_numbers = any(char.isdigit() for char in question)
    has_equation = any(symbol in question for symbol in ['=', '+', '-', '×', '÷', 'x', 'y', '^'])
    
    solve_score = sum(1 for kw in solve_keywords if kw in question_lower)
    concept_score = sum(1 for kw in concept_keywords if kw in question_lower)
    
    # Use deepthink if: has solve keywords, or has numbers/equations without concept keywords
    if solve_score > 0 or (has_numbers and concept_score == 0) or has_equation:
        return True
    
    # Check if it's a greeting or simple question
    greetings = ["hi", "hello", "hey", "how are you", "good morning", "good evening"]
    if any(greeting in question_lower for greeting in greetings):
        return False
    
    # For ambiguous questions, check length (longer questions often need solving)
    if len(question.split()) > 15 and has_numbers:
        return True
    
    return False

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
def build_gemini_messages(chat_history, new_instruction, mode="normal"):
    """Builds the message list for Gemini API from chat history."""
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
    
    # Add the new user instruction with chapter context if applicable
    subject, chapter, confidence = detect_chapter(new_instruction)
    chapter_context = ""
    if confidence > 0 and mode == "deepthink":
        chapter_context = get_chapter_context(subject, chapter)
    
    enhanced_instruction = new_instruction
    if chapter_context and mode == "deepthink":
        enhanced_instruction = f"{new_instruction}\n\n[Chapter Context for Tutor]\n{chapter_context}"
    
    messages.append({
        "role": "user",
        "parts": [{"text": enhanced_instruction}]
    })
    
    return messages

def build_chat_completion_messages(chat_history, new_instruction, mode="normal"):
    """Builds message list for OpenAI-compatible APIs (Groq, OpenRouter, Awan)."""
    system_prompt = NORMAL_SYSTEM_PROMPT if mode == "normal" else DEEPTHINK_SYSTEM_PROMPT
    
    # Add chapter context to system prompt for deepthink mode
    if mode == "deepthink":
        subject, chapter, confidence = detect_chapter(new_instruction)
        chapter_context = get_chapter_context(subject, chapter)
        if chapter_context:
            system_prompt += f"\n\n**RELEVANT SEE CURRICULUM INFORMATION:**\n{chapter_context}"
    
    messages = [
        {"role": "system", "content": system_prompt}
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
            "parts": [{"text": NORMAL_SYSTEM_PROMPT}]
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
def call_groq_api(messages, model=None, stream=True):
    """Calls Groq API (fast LLM)."""
    if model is None:
        model = GROQ_NORMAL_MODEL
    
    payload = {
        "model": model,
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
        print(f"[DEBUG] Calling Groq with model {model}")
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

# --- MAIN /ask ENDPOINT (DUAL MODE WITH RAG) ---
@app.route('/ask', methods=['POST'])
def ask_endpoint():
    """Main Q&A endpoint with dual-mode and RAG system."""
    user_id = get_user_id()
    chat_id = request.form.get('chat_id')
    instruction = request.form.get('instruction', '').strip()
    model_choice = request.form.get('model_choice', 'auto')  # 'auto', 'normal', 'deepthink'
    web_search_enabled = request.form.get('web_search', 'false').lower() == 'true'
    
    if not chat_id:
        return jsonify({"error": "Chat ID not provided."}), 400
    if not instruction:
        return jsonify({"error": "No instruction provided."}), 400
    
    # Auto-detect mode if not specified
    if model_choice == 'auto':
        needs_solving = should_use_deepthink(instruction)
        mode = "deepthink" if needs_solving else "normal"
        print(f"[DEBUG] Auto-detected mode: {mode} for question: {instruction[:50]}...")
    else:
        mode = model_choice  # 'normal' or 'deepthink'
    
    # Detect chapter for logging
    subject, chapter, confidence = detect_chapter(instruction)
    if confidence > 0:
        print(f"[DEBUG] Detected chapter: {chapter} in {subject} (confidence: {confidence})")
    
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
            # Build messages for API based on mode
            gemini_messages = build_gemini_messages(current_chat_history, instruction, mode)
            completion_messages = build_chat_completion_messages(current_chat_history, instruction, mode)
            
            response = None
            full_response = ""
            
            if mode == "deepthink":
                # Use Groq DeepThink model (llama-3.3-70b-versatile) for solving
                print(f"[DEEPTHINK MODE] Using Groq DeepThink for: {instruction[:50]}...")
                response = call_groq_api(completion_messages, model=GROQ_DEEPTHINK_MODEL, stream=True)
                
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
                        print(f"Groq DeepThink streaming error: {e}")
                
                # Fallback chain for DeepThink mode
                if not full_response:
                    print("Groq DeepThink failed, trying OpenRouter DeepThink...")
                    response = call_openrouter_api(completion_messages, OPENROUTER_DEEPTHINK_MODEL, stream=True)
                    
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
                            print(f"OpenRouter streaming error: {e}")
                    
                    # Final fallback: Gemini
                    if not full_response:
                        print("All deepthink models failed, falling back to Gemini...")
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
                                print(f"Gemini fallback error: {e}")
            
            else:  # NORMAL MODE
                # Use Groq Normal model (llama-3.1-8b-instant) for quick responses
                print(f"[NORMAL MODE] Using Groq Normal for: {instruction[:50]}...")
                response = call_groq_api(completion_messages, model=GROQ_NORMAL_MODEL, stream=True)
                
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
                        print(f"Groq Normal streaming error: {e}")
                
                # Fallback for Normal mode
                if not full_response:
                    print("Groq Normal failed, trying Gemini...")
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
                            print(f"Gemini fallback error: {e}")
            
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

# --- OTHER REQUIRED ENDPOINTS (keep all existing) ---
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

@app.route('/chat')
def chat():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/login')
def login():
    if 'user_id' in session:
        return redirect(url_for('chat'))
    return render_template('login.html')

@app.route('/guest_login')
def guest_login():
    session.clear()
    temp_id = str(uuid.uuid4())
    session['temp_user_id'] = temp_id
    session['user_id'] = temp_id
    session['is_guest'] = True
    return redirect(url_for('chat'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

@app.route('/user_info', methods=['GET'])
def user_info():
    user_email = session.get('user', None)
    return jsonify({"user_email": user_email})

@app.route('/google_login/authorized')
def google_login_authorized():
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
@app.route('/microsoft_login/authorized')
def microsoft_login_authorized():
    try:
        # Get user info from Microsoft Graph API
        resp = microsoft.get("https://graph.microsoft.com/v1.0/me")

        if not resp.ok:
            print("Microsoft API Error:", resp.text)
            return redirect(url_for("login"))

        user_data = resp.json()

        session['user'] = (
            user_data.get("mail")
            or user_data.get("userPrincipalName")
        )

        session['user_id'] = f"microsoft_{user_data.get('id')}"

        return redirect(url_for('index'))

    except Exception as e:
        print(f"Error during Microsoft login: {e}")
        return redirect(url_for('login'))
@app.route('/')
def home():
    return render_template('login.html')

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

@app.route("/see-maths-ai")
def see_maths_ai():
    return send_file(os.path.join(BASE_DIR, "templates", "see-maths-ai.html"))

@app.route("/science-helper")
def science_helper():
    return send_file(os.path.join(BASE_DIR, "templates", "science-helper.html"))

@app.route("/homework-ai")
def homework_ai():
    return send_file(os.path.join(BASE_DIR, "templates", "homework-ai.html"))

@app.route("/see-exam-preparation")
def see_exam_preparation():
    return send_file(os.path.join(BASE_DIR, "templates", "see-exam-preparation.html"))

@app.route('/robots.txt')
def robots():
    return send_from_directory(os.path.join(BASE_DIR, 'static'), 'robots.txt')

@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory(os.path.join(BASE_DIR, 'static'), 'sitemap.xml')

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
    
    if 'image' not in request.files:
        return jsonify({"error": "No image file provided."}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400
    
    if not file.filename.lower().endswith(('png', 'jpg', 'jpeg', 'gif', 'webp')):
        return jsonify({"error": "File must be an image (PNG, JPG, GIF, WebP)."}), 400
    
    current_message_count = get_daily_message_count(user_id)
    if current_message_count >= DAILY_MESSAGE_LIMIT:
        return jsonify({"response": f"You have reached your daily message limit of {DAILY_MESSAGE_LIMIT}. Please try again tomorrow."}), 429
    
    try:
        image_data = base64.standard_b64encode(file.read()).decode('utf-8')
    except Exception as e:
        print(f"Error reading file: {e}")
        return jsonify({"error": f"Error reading image file: {str(e)}"}), 400
    
    def stream_image_response():
        try:
            current_chat_history = load_chat_history_from_file(user_id, chat_id)
            
            vision_prompt = f"""You are a math tutor specializing in SEE exam preparation for Class 10 students in Nepal.

A student has uploaded an image of a math problem. Your task is to:
1. Analyze the image and identify the math problem
2. Explain what the problem is asking (in simple terms)
3. Solve it step-by-step
4. Explain the concept behind it
5. Provide the final answer clearly

The student's caption/note about this problem: {caption if caption else 'None provided'}

Follow the same format as you would for text-based questions - make it educational and SEE-exam focused."""
            
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
                                
                                user_message = f"[Image Upload] {caption if caption else 'Math problem image'}"
                                current_chat_history.append({"type": "user", "text": user_message, "timestamp": time.time()})
                                current_chat_history.append({"type": "bot", "text": full_response, "timestamp": time.time()})
                                save_chat_history_to_file(user_id, chat_id, current_chat_history)
                                
                                increment_daily_message_count(user_id)
                                
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
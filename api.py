"""
VEXARA v4.1 - PRODUCTION RAG WITH INTELLIGENT FALLBACK SYSTEM

MODULE ORGANIZATION:
═══════════════════════════════════════════════════════════════════════════════
 1. IMPORTS & CONFIGURATION          (Lines ~1-80)
 2. SECURITY UTILITIES               (Lines ~80-270)
    - SecurityGuard (injection, rate limiting)
    - sanitize_input, safe_file_operation, safe_firebase_operation
 3. CACHING                          (Lines ~370-425)
    - TTLCache, Flask-Cache
 4. FLASK APP & MIDDLEWARE           (Lines ~300-440)
    - App init, CORS, security headers
 5. API KEYS & PROVIDER CONFIG       (Lines ~440-650)
    - Groq, Cerebras, Gemini/Vertex, OpenRouter
 6. AI PROVIDER CLASSES              (Lines ~650-950)
    - GroqChat, CerebrasChat, GeminiChat, OpenRouterChat
 7. KNOWLEDGE BASE (RAG)             (Lines ~950-1150)
    - ChunkedKnowledgeBase (curriculum retrieval)
 8. QUOTA MANAGEMENT                 (Lines ~1150-1280)
    - Daily message limits per user
 9. FIREBASE INITIALIZATION          (Lines ~300-370)
    - Firebase Realtime Database setup
10. SESSION MEMORY                   (Lines ~2500-2700)
    - Short-term per-user memory (Firebase-backed)
11. USER PREFERENCES                 (Lines ~2700-2850)
    - Language, tone, difficulty personalization
12. STUDENT PROFILE ENGINE           (Lines ~2850-3100)
    - Long-term learning profile (Firebase-backed)
13. PERSONAL MEMORY ENGINE           (Lines ~3100-3400)
    - User-saved personal notes & facts
14. GOAL MANAGER                     (Lines ~3400-3500)
    - Study goals tracking
15. AGENT ORCHESTRATOR               (Lines ~3500-3750)
    - Central brain: plan → tools → prompt → LLM
16. PLANNER                          (Lines ~3750-3900)
    - Intent-based execution planning
17. TOOL REGISTRY                    (Lines ~3900-4050)
    - Modular tool system
18. REFLECTION LAYER                 (Lines ~4050-4150)
    - Post-LLM verification for math/exam
19. PROMPT BUILDING                  (Lines ~4150-4300)
    - System prompt construction with RAG
20. FIREBASE CHAT STORAGE            (Lines ~4300-4500)
    - Message persistence, chat summaries
21. FIREBASE SYNC MANAGER            (Lines ~2015-2230)
    - Conflict resolution, offline caching
22. VISION RECALL                    (Lines ~4500-4650)
    - Image processing & vision memory
23. WELCOME MESSAGE                  (Lines ~4650-4750)
    - Personalized greeting generation
24. ROUTES: AUTH                     (Lines ~4800-5100)
    - Google/Microsoft OAuth, guest access
25. ROUTES: MAIN API                 (Lines ~5100-5600)
    - /ask, /upload_image, /new_chat, etc.
26. ROUTES: USER DATA                (Lines ~5600-5800)
    - /user_stats, /user_info, /preferences
27. ROUTES: ADMIN                    (Lines ~5800-6000)
    - /api/debug_*, /api/quota_*, /api/system/*
28. ROUTES: SYNC                     (Lines ~6000-6100)
    - /api/sync/status, /api/sync/retry
29. MAIN                             (Lines ~6450+)
    - App startup
═══════════════════════════════════════════════════════════════════════════════
"""

import json
import base64
import requests
import time
import uuid
import os
import re
import html
import hashlib
import logging
from io import BytesIO
from PIL import Image
from flask import Flask, render_template, request, jsonify, redirect, session, url_for, make_response
from flask_dance.contrib.google import make_google_blueprint, google as google_signin
from authlib.integrations.flask_client import OAuth
from flask import send_from_directory, send_file
from datetime import datetime, date, timedelta
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_compress import Compress
from flask_caching import Cache
from collections import defaultdict
from difflib import SequenceMatcher
import math
from functools import wraps
from threading import Lock

# ============================================================================
# 🛡️ PRODUCTION LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('vexara')

# ============================================================================
# 🛡️ PRODUCTION ERROR HANDLING INFRASTRUCTURE
# ============================================================================

class ProductionError(Exception):
    """Base exception for production errors."""
    pass

class APIProviderError(ProductionError):
    """API provider call failed."""
    def __init__(self, provider, message, status_code=None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"[{provider}] {message}")

class FirebaseError(ProductionError):
    """Firebase operation failed."""
    pass

class ImageProcessingError(ProductionError):
    """Image processing failed."""
    pass

class InputValidationError(ProductionError):
    """User input validation failed."""
    pass

class QuotaExceededError(ProductionError):
    """Daily message quota exceeded."""
    def __init__(self, user_id, limit):
        self.user_id = user_id
        self.limit = limit
        super().__init__(f"Daily limit ({limit}) reached for user {user_id[:12]}...")

def safe_json_loads(text, default=None):
    """Safely parse JSON, returning default on any error."""
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


# ============================================================================
# 🛡️ SECURITY GUARD (prompt injection, rate limiting, XSS protection)
# ============================================================================

class SecurityGuard:
    """
    Central security layer for Vexara.
    Handles prompt injection detection, per-user rate limiting, and input sanitization.
    """

    # ── Prompt Injection Patterns ──────────────────────────────────────────
    _INJECTION_PATTERNS = [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"forget\s+(all\s+)?(your\s+)?instructions",
        r"you\s+are\s+now\s+(a|an|the)\s+",
        r"act\s+as\s+(a|an|the)\s+",
        r"pretend\s+you\s+(are|were)\s+",
        r"disregard\s+(all\s+)?(your\s+)?(previous|prior|above)",
        r"override\s+(your\s+)?(instructions|rules|guidelines)",
        r"new\s+instructions?\s*:",
        r"system\s*:\s*",
        r"<\|system\|>",
        r"<\|assistant\|>",
        r"<\|user\|>",
        r"\[INST\]",
        r"\[/INST\]",
        r"<<SYS>>",
        r"<</SYS>>",
        r"repeat\s+(back\s+)?(your|the)\s+(system\s+)?prompt",
        r"what\s+(are|is)\s+your\s+(system\s+)?(prompt|instructions)",
        r"print\s+(your|the)\s+(system\s+)?(prompt|instructions)",
        r"reveal\s+(your|the)\s+(system\s+)?(prompt|instructions)",
        r"tell\s+me\s+(your|the)\s+(system\s+)?(prompt|instructions)",
        r"dump\s+(your|the)\s+(system\s+)?(prompt|instructions)",
        r"show\s+me\s+(your|the)\s+(system\s+)?(prompt|instructions)",
    ]

    # ── Per-User Rate Limiting (sliding window) ────────────────────────────
    _user_requests = {}  # user_id -> [timestamp, ...]
    RATE_LIMIT_WINDOW = 60       # seconds
    RATE_LIMIT_MAX = 30          # max requests per window per user
    _rate_lock = Lock()

    # ── Prompt Injection Detection ─────────────────────────────────────────
    @classmethod
    def detect_injection(cls, text: str) -> dict:
        """
        Check if text contains prompt injection attempts.
        Returns: {"safe": bool, "reason": str, "confidence": float}
        """
        if not text:
            return {"safe": True, "reason": "", "confidence": 0.0}

        text_lower = text.lower().strip()

        # Check known injection patterns
        for pattern in cls._INJECTION_PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return {
                    "safe": False,
                    "reason": f"Prompt injection detected: matches '{pattern}'",
                    "confidence": 0.9,
                }

        # Check for unusual formatting (base64, hex, etc.)
        # High entropy might indicate encoded injection
        if len(text) > 100:
            non_alpha = sum(1 for c in text if not c.isalpha() and not c.isspace())
            if non_alpha / len(text) > 0.6:
                return {
                    "safe": False,
                    "reason": "Suspicious high-entropy input detected",
                    "confidence": 0.5,
                }

        return {"safe": True, "reason": "", "confidence": 0.0}

    @classmethod
    def sanitize_for_prompt(cls, text: str) -> str:
        """
        Sanitize user input before injecting into LLM prompt.
        Strips potential injection vectors while preserving content.
        """
        if not text:
            return ""

        # Remove zero-width characters
        text = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u2069\ufeff]', '', text)

        # Remove control characters except newlines and tabs
        text = ''.join(c for c in text if c.isprintable() or c in '\n\r\t')

        # Truncate to safe length
        if len(text) > 5000:
            text = text[:5000]

        return text.strip()

    # ── Per-User Rate Limiting ─────────────────────────────────────────────
    @classmethod
    def check_rate_limit(cls, user_id: str) -> dict:
        """
        Check if user has exceeded the sliding window rate limit.
        Returns: {"allowed": bool, "remaining": int, "reset_in": float}
        """
        now = time.time()
        window_start = now - cls.RATE_LIMIT_WINDOW

        with cls._rate_lock:
            if user_id not in cls._user_requests:
                cls._user_requests[user_id] = []

            # Remove old entries outside the window
            cls._user_requests[user_id] = [
                ts for ts in cls._user_requests[user_id]
                if ts > window_start
            ]

            current_count = len(cls._user_requests[user_id])

            if current_count >= cls.RATE_LIMIT_MAX:
                # Calculate when the oldest request in window expires
                oldest = cls._user_requests[user_id][0]
                reset_in = oldest + cls.RATE_LIMIT_WINDOW - now
                return {
                    "allowed": False,
                    "remaining": 0,
                    "reset_in": max(reset_in, 1),
                }

            # Allow and record
            cls._user_requests[user_id].append(now)
            return {
                "allowed": True,
                "remaining": cls.RATE_LIMIT_MAX - current_count - 1,
                "reset_in": cls.RATE_LIMIT_WINDOW,
            }

    @classmethod
    def get_rate_limit_status(cls, user_id: str) -> dict:
        """Get current rate limit status without consuming a request."""
        now = time.time()
        window_start = now - cls.RATE_LIMIT_WINDOW

        with cls._rate_lock:
            requests = cls._user_requests.get(user_id, [])
            active = [ts for ts in requests if ts > window_start]
            remaining = max(0, cls.RATE_LIMIT_MAX - len(active))
            return {
                "limit": cls.RATE_LIMIT_MAX,
                "remaining": remaining,
                "window_seconds": cls.RATE_LIMIT_WINDOW,
            }

    # ── Content Security ───────────────────────────────────────────────────
    @classmethod
    def sanitize_filename(cls, filename: str) -> str:
        """Sanitize a filename to prevent path traversal."""
        if not filename:
            return "unnamed"
        # Remove path separators and dangerous chars
        filename = os.path.basename(filename)
        filename = re.sub(r'[^\w\-.]', '_', filename)
        filename = filename.strip('_.')
        if not filename:
            filename = "unnamed"
        return filename[:255]

    @classmethod
    def validate_chat_id(cls, chat_id: str) -> bool:
        """Validate chat_id format (alphanumeric + hyphens, reasonable length)."""
        if not chat_id or not isinstance(chat_id, str):
            return False
        return bool(re.match(r'^[a-zA-Z0-9_\-]{1,128}$', chat_id))


# ============================================================================
# 🔧 UTILITY FUNCTIONS
# ============================================================================

def sanitize_input(text, max_length=5000):
    """Sanitize user input - strip, limit length, escape HTML."""
    if not text or not isinstance(text, str):
        return ""
    text = text.strip()
    text = html.escape(text)
    if len(text) > max_length:
        text = text[:max_length]
    return text

def safe_file_operation(func):
    """Decorator for safe file operations with error handling."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except FileNotFoundError:
            logger.warning(f"File not found in {func.__name__}")
            return None
        except PermissionError:
            logger.error(f"Permission denied in {func.__name__}")
            return None
        except OSError as e:
            logger.error(f"OS error in {func.__name__}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {e}")
            return None
    return wrapper

def safe_firebase_operation(func):
    """Decorator for safe Firebase operations with error handling and retry."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not FIREBASE_AVAILABLE:
            logger.warning(f"Firebase unavailable, skipping {func.__name__}")
            return None
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Firebase error in {func.__name__}: {e}")
            return None
    return wrapper

# Rate limiting lock for thread safety
_rate_limit_lock = Lock()

# 🔥 FIREBASE IMPORTS
try:
    import firebase_admin
    from firebase_admin import db, credentials
    FIREBASE_AVAILABLE = True
    print("[FIREBASE] Firebase SDK imported successfully")
except ImportError:
    FIREBASE_AVAILABLE = False
    print("[FIREBASE] Warning: firebase-admin not installed. Install with: pip install firebase-admin")

# 🔐 GOOGLE AUTH IMPORTS (for Vertex AI Service Account)
try:
    import google.auth
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    GOOGLE_AUTH_AVAILABLE = True
    print("[GOOGLE_AUTH] Google Auth libraries imported successfully")
except ImportError:
    GOOGLE_AUTH_AVAILABLE = False
    print("[GOOGLE_AUTH] Warning: google-auth libraries not installed. Install with: pip install google-auth google-auth-oauthlib google-auth-httplib2")

current_dir = os.path.dirname(os.path.abspath(__file__))
template_path = os.path.join(current_dir, 'templates')
static_path = os.path.join(current_dir, 'static')

app_name = '__main__'
if '__app_id__' in globals():
    app_name = globals()['__app_id__']
app = Flask(app_name, template_folder=template_path, static_folder=static_path)

# Trust proxy headers from Render's reverse proxy so Flask knows the original request was HTTPS.
# Without this, SESSION_COOKIE_SECURE blocks the session cookie silently.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

CORS(app, resources={
    r"/*": {
        "origins": [
            "https://aivexara.xyz", 
            "https://www.aivexara.xyz",
            "https://status.aivexara.xyz"
        ],
        "methods": ["GET", "POST"],
        "allow_headers": ["Content-Type"]
    }
})

# Enable gzip compression for all responses - essential for Render 512MB
Compress(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", str(uuid.uuid4()))

# ============================================================================
# 💾 CACHING CONFIGURATION
# ============================================================================
app.config['CACHE_TYPE'] = 'simple'  # In-memory caching (good for single instance)
app.config['CACHE_DEFAULT_TIMEOUT'] = 300  # 5 minutes default timeout
cache = Cache(app)


class TTLCache:
    """
    Simple in-memory TTL cache for frequently accessed data.
    Thread-safe, auto-evicting, per-key TTL.
    """
    _store: dict = {}
    _lock = Lock()

    @classmethod
    def get(cls, key: str) -> any:
        with cls._lock:
            entry = cls._store.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if time.time() > expiry:
                del cls._store[key]
                return None
            return value

    @classmethod
    def set(cls, key: str, value: any, ttl: int = 300):
        with cls._lock:
            cls._store[key] = (value, time.time() + ttl)

    @classmethod
    def delete(cls, key: str):
        with cls._lock:
            cls._store.pop(key, None)

    @classmethod
    def clear(cls, prefix: str = ""):
        with cls._lock:
            if prefix:
                keys_to_delete = [k for k in cls._store if k.startswith(prefix)]
                for k in keys_to_delete:
                    del cls._store[k]
            else:
                cls._store.clear()

    @classmethod
    def stats(cls) -> dict:
        with cls._lock:
            return {"entries": len(cls._store)}

# 🔧 MEMORY-SAFE LIMITS for 512MB Render environments
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max upload (images + files)
app.config['JSON_SORT_KEYS'] = False  # Don't sort JSON (saves CPU)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # Cache static files 1 year
# ============================================================================
# 🔐 PERSISTENT SESSION CONFIGURATION
# ============================================================================
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# ============================================================================
# 🛡️ GLOBAL FLASK ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Resource not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405

@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify({"error": "File too large. Maximum size is 10MB."}), 413

@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({"error": "Too many requests. Please slow down."}), 429

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {e}")
    return jsonify({"error": "Internal server error. Please try again."}), 500

@app.errorhandler(502)
def bad_gateway(e):
    return jsonify({"error": "Service temporarily unavailable."}), 502

@app.errorhandler(503)
def service_unavailable(e):
    return jsonify({"error": "Service temporarily unavailable. Please try again later."}), 503

@app.before_request
def security_headers():
    """Add security headers and rate limiting to all requests."""
    pass

@app.after_request
def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    # Content Security Policy: allow only self and needed CDNs
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://accounts.google.com https://js.monitor.azure.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https://accounts.google.com https://graph.microsoft.com https://www.googleapis.com https://oauth2.googleapis.com https://login.microsoftonline.com; "
        "frame-src 'self' https://accounts.google.com https://login.microsoftonline.com;"
    )
    # Strict Transport Security (enable HSTS)
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

# ============================================================================
# 🔑 API KEYS & CONFIGURATION
# ============================================================================

# Vertex AI (replacing direct Gemini API)
VERTEX_AI_PROJECT_ID = os.environ.get("VERTEX_AI_PROJECT_ID", "vexara-real")
VERTEX_AI_REGION = os.environ.get("VERTEX_AI_REGION", "global")  # Changed from us-central1 to global
VERTEX_AI_CREDENTIALS_JSON = os.environ.get("VERTEX_AI_CREDENTIALS")

# Legacy API keys (for fallback)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")

# API Endpoints
VERTEX_AI_API_URL = f"https://aiplatform.googleapis.com/v1/projects/{VERTEX_AI_PROJECT_ID}/locations/{VERTEX_AI_REGION}/publishers/google/models"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
CEREBRAS_API_URL = "https://api.cerebras.ai/v1/chat/completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# ============================================================================
# 🔐 VERTEX AI AUTHENTICATION (Service Account)
# ============================================================================

def get_vertex_ai_access_token():
    """
    Generate OAuth 2.0 access token from Vertex AI service account credentials.
    This replaces the API key authentication.
    """
    if not VERTEX_AI_CREDENTIALS_JSON:
        print("[VERTEX_AI] ERROR: VERTEX_AI_CREDENTIALS environment variable not set")
        return None
    
    try:
        import google.auth
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        
        # Parse credentials from JSON
        creds_dict = json.loads(VERTEX_AI_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
        
        # Refresh to get access token
        credentials.refresh(Request())
        access_token = credentials.token
        
        print(f"[VERTEX_AI] ✓ Generated access token (expires in ~1 hour)")
        return access_token
    
    except Exception as e:
        print(f"[VERTEX_AI] ERROR: Failed to generate access token: {e}")
        return None

# ============================================================================
# 🔥 FIREBASE CONFIGURATION & INITIALIZATION
# ============================================================================

FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS")

# Debug: Print what we found
print(f"[STARTUP] FIREBASE_DB_URL set: {bool(FIREBASE_DB_URL)}")
print(f"[STARTUP] FIREBASE_CREDENTIALS set: {bool(FIREBASE_CREDENTIALS_JSON)}")
if FIREBASE_DB_URL:
    print(f"[STARTUP] Database URL: {FIREBASE_DB_URL}")
if FIREBASE_CREDENTIALS_JSON:
    print(f"[STARTUP] Credentials length: {len(FIREBASE_CREDENTIALS_JSON)} chars")

def initialize_firebase():
    """Initialize Firebase Admin SDK with credentials from environment."""
    global FIREBASE_AVAILABLE
    
    if not FIREBASE_AVAILABLE:
        print("[FIREBASE] Firebase SDK not available, skipping initialization")
        return False
    
    if not FIREBASE_DB_URL:
        print("[FIREBASE] ERROR: FIREBASE_DB_URL environment variable not set")
        FIREBASE_AVAILABLE = False
        return False
    
    if not FIREBASE_CREDENTIALS_JSON:
        print("[FIREBASE] ERROR: FIREBASE_CREDENTIALS environment variable not set")
        FIREBASE_AVAILABLE = False
        return False
    
    try:
        # Parse credentials from JSON string
        creds_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        creds = credentials.Certificate(creds_dict)
        
        # Initialize Firebase app (only once)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(creds, {
                'databaseURL': FIREBASE_DB_URL
            })
            print(f"[FIREBASE] ✓ Initialized successfully. Database: {FIREBASE_DB_URL}")
            return True
        else:
            print("[FIREBASE] Already initialized")
            return True
            
    except json.JSONDecodeError as e:
        print(f"[FIREBASE] ERROR: Invalid FIREBASE_CREDENTIALS JSON: {e}")
        FIREBASE_AVAILABLE = False
        return False
    except Exception as e:
        print(f"[FIREBASE] ERROR: Failed to initialize: {e}")
        FIREBASE_AVAILABLE = False
        return False

# Initialize Firebase on startup
# Force initialization regardless of how app is started (local, gunicorn, render, etc)
initialize_firebase()

# ============================================================================
# 🖼️ IMAGE COMPRESSION MODULE
# ============================================================================

class ImageOptimizer:
    """Compress images for token efficiency - resize to 1200px, 85% JPEG quality for vision."""
    MAX_WIDTH = 1200
    JPEG_QUALITY = 85
    MAX_SIZE_MB = 5
    
    @staticmethod
    def compress_image(file_obj):
        """Compress image: resize to 1200px width, 85% JPEG quality."""
        try:
            img = Image.open(file_obj)
            
            # Convert RGBA to RGB if needed
            if img.mode == 'RGBA':
                rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                rgb_img.paste(img, mask=img.split()[3])
                img = rgb_img
            
            # Resize maintaining aspect ratio
            if img.width > ImageOptimizer.MAX_WIDTH:
                ratio = ImageOptimizer.MAX_WIDTH / img.width
                new_height = int(img.height * ratio)
                img = img.resize((ImageOptimizer.MAX_WIDTH, new_height), Image.Resampling.LANCZOS)
            
            # Compress to JPEG
            output = BytesIO()
            img.save(output, format='JPEG', quality=ImageOptimizer.JPEG_QUALITY, optimize=True)
            compressed_data = output.getvalue()
            
            # Size check
            size_mb = len(compressed_data) / (1024 * 1024)
            if size_mb > ImageOptimizer.MAX_SIZE_MB:
                raise ValueError(f"Image too large after compression: {size_mb:.2f}MB")
            
            return base64.standard_b64encode(compressed_data).decode('utf-8'), 'image/jpeg'
        
        except Exception as e:
            raise Exception(f"Image compression failed: {str(e)}")

 
# ============================================================================
# 🎨 HYBRID IMAGE DETECTION & PROCESSING SYSTEM
# ============================================================================
 
class ImageAnalyzer:
    """
    Analyzes images to determine optimal solving strategy:
    
    STRATEGY:
    - Text-only images → Extract text + Tutor mode (CHEAP: 2 API calls, both text-based)
    - Geometric/Diagrams → Direct vision solve (QUALITY: 1 API call, keeps visual context)
    - Mixed content → Direct vision solve (SAFE: preserves all information)
    
    COST ANALYSIS:
    - Text extraction (fast model): ~0.0001 per image
    - Tutor mode (Llama-70B): ~0.0005 per response = Total ~0.0006
    - Direct vision solve (Gemini): ~0.0025 per image
    
    For text-only: Extract+Tutor saves 75% vs direct solve
    For geometric: Direct solve prevents hallucination from missing diagrams
    """
    
    @staticmethod
    def detect_content_type(image_data):
        """
        Classify image content in ONE quick call.
        Returns: 'text_only' | 'geometric' | 'mixed'
        
        Cheap detection: uses lightweight prompt on fast model
        """
        detection_prompt = """ANALYZE THIS IMAGE - RESPOND WITH ONE WORD ONLY:
 
Categorize:
- TEXT: Pure text/numbers/equations (no diagrams, shapes, figures)
- GEOMETRIC: Has diagrams, graphs, geometric shapes, coordinate systems, or visual elements
- MIXED: Has both text AND diagrams
 
Examples:
- Handwritten "Solve: 2x + 3 = 11" → TEXT
- Triangle with labeled angles → GEOMETRIC
- Problem with graph plotted → GEOMETRIC
- Equation "y = 2x + 5" as text only → TEXT
 
Answer ONE WORD: TEXT / GEOMETRIC / MIXED"""
        
        detection_messages = [{"role": "user", "content": detection_prompt}]
        detection_system = "Respond with exactly one word: TEXT or GEOMETRIC or MIXED"
        
        try:
            print(f"[ANALYZER] Detecting image content type...")
            response, provider, success = call_api_with_intelligent_fallback(
                "normal", detection_system, detection_messages, 
                image_data=image_data
            )
            
            if not response or not success:
                print(f"[ANALYZER] Detection failed, defaulting to GEOMETRIC (safer fallback)")
                return 'geometric'
            
            detected_text = ""
            
            # Parse streaming response (Groq)
            if response.headers.get('content-type', '').startswith('text/event-stream'):
                for line in response.iter_lines():
                    if line:
                        line_str = line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                        if line_str.startswith('data: '):
                            try:
                                data = json.loads(line_str[6:])
                                if 'choices' in data and data['choices']:
                                    delta = data['choices'][0].get('delta', {})
                                    if 'content' in delta:
                                        detected_text += delta['content']
                            except json.JSONDecodeError:
                                continue
            # Parse non-streaming response (Gemini)
            else:
                data = response.json()
                if 'candidates' in data and data['candidates']:
                    candidate = data['candidates'][0]
                    if 'content' in candidate and 'parts' in candidate['content']:
                        for part in candidate['content']['parts']:
                            if 'text' in part:
                                detected_text = part['text']
            
            detected_type = detected_text.strip().upper()
            print(f"[ANALYZER] Result: {detected_type}")
            
            # Map response to strategy
            if 'TEXT' in detected_type:
                return 'text_only'
            elif 'GEOMETRIC' in detected_type or 'DIAGRAM' in detected_type:
                return 'geometric'
            elif 'MIXED' in detected_type:
                return 'mixed'
            else:
                return 'geometric'
        
        except Exception as e:
            print(f"[ANALYZER] Detection error: {e}, defaulting to GEOMETRIC")
            return 'geometric'
    
    @staticmethod
    def extract_text_from_image(image_data):
        """
        Extract text from text-only images (strategy: text extraction).
        Uses fast extraction prompt on fast model.
        
        Returns: (extracted_text, error_message, success)
        """
        extraction_prompt = "Extract ONLY the text and mathematical expressions from this image. Return only the extracted text, nothing else. Preserve all equations, numbers, operators exactly."
        extraction_messages = [{"role": "user", "content": extraction_prompt}]
        extraction_system = "Extract text from images accurately. Return ONLY extracted text."
        
        try:
            print(f"[EXTRACT] Calling vision model to extract text...")
            response, provider, success = call_api_with_intelligent_fallback(
                "normal", extraction_system, extraction_messages, 
                image_data=image_data
            )
            
            if not response or not success:
                return "", "Could not extract text from image.", False
            
            extracted_text = ""
            
            # Parse streaming (Groq)
            if response.headers.get('content-type', '').startswith('text/event-stream'):
                for line in response.iter_lines():
                    if line:
                        line_str = line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                        if line_str.startswith('data: '):
                            try:
                                data = json.loads(line_str[6:])
                                if 'choices' in data and data['choices']:
                                    delta = data['choices'][0].get('delta', {})
                                    if 'content' in delta:
                                        extracted_text += delta['content']
                            except json.JSONDecodeError:
                                continue
            # Parse non-streaming (Gemini)
            else:
                data = response.json()
                if 'candidates' in data and data['candidates']:
                    candidate = data['candidates'][0]
                    if 'content' in candidate and 'parts' in candidate['content']:
                        for part in candidate['content']['parts']:
                            if 'text' in part:
                                extracted_text += part['text']
            
            if not extracted_text.strip():
                return "", "Could not extract any text from image.", False
            
            print(f"[EXTRACT] Extracted: {len(extracted_text)} characters")
            return extracted_text, None, True
        
        except Exception as e:
            print(f"[EXTRACT] Error: {e}")
            return "", str(e), False
    
    @staticmethod
    def solve_with_vision_directly(image_data, question_text, chat_history, rag_context=None, rag_subject=None, rag_chapter=None, rag_confidence=None):
        """
        Solve problem directly from image using vision model.
        For geometric/mixed content (strategy: keep visual context).
        
        Now uses EXTRACTED QUESTION for RAG retrieval (not user keywords).
        Supports any chapter (Sets with Venn Diagrams, Algebra, Geometry, etc.)
        
        Yields response chunks as generator.
        """
        solving_prompt = f"""Solve this math problem from the image:
 
Problem statement: {question_text if question_text else 'See image'}

Provide a complete solution with:
1. **Given:** Information from image
2. **To Find:** What needs to be calculated
3. **Solution:** Step-by-step with explanations
4. **Answer:** Final result with units
5. **SEE Tip:** Exam preparation tip"""
        
        solving_messages = [{"role": "user", "content": solving_prompt}]
        
        # Build system prompt with RAG context if available
        if rag_context and rag_confidence and rag_confidence >= KNOWLEDGE_BASE.config.get('min_confidence_threshold', 0.15):
            system_msg = f"""You are an expert math tutor solving problems with visual elements.

CURRICULUM CONTEXT: {rag_subject} - {rag_chapter} (Confidence: {rag_confidence:.0%})
{rag_context}

Solve the problem using the curriculum context if relevant, along with what you see in the image."""
        else:
            system_msg = """You are an expert math tutor solving problems from images.

Analyze the image carefully and solve the problem step-by-step."""
        
        print(f"[VISION_RAG] Question extracted from image: '{question_text[:60]}'")
        print(f"[VISION_RAG] Using RAG Context: {rag_subject}/{rag_chapter if rag_context else 'None'}")
        
        try:
            print(f"[SOLVE_VISION] Solving directly with vision model (image included)...")
            response, provider, success = call_api_with_intelligent_fallback(
                "tutor_mode", system_msg, solving_messages, 
                image_data=image_data
            )
            
            if not response or not success:
                yield "❌ Could not solve this problem. Please try again or upload a clearer image."
                return None, None
            
            full_response = ""
            
            # Handle streaming
            if response.headers.get('content-type', '').startswith('text/event-stream'):
                for line in response.iter_lines():
                    if line:
                        line_str = line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                        if line_str.startswith('data: '):
                            try:
                                data = json.loads(line_str[6:])
                                if 'choices' in data and data['choices']:
                                    delta = data['choices'][0].get('delta', {})
                                    if 'content' in delta:
                                        chunk = delta['content']
                                        full_response += chunk
                                        yield chunk
                            except json.JSONDecodeError:
                                continue
            # Handle non-streaming (Gemini)
            else:
                data = response.json()
                if 'candidates' in data and data['candidates']:
                    candidate = data['candidates'][0]
                    if 'content' in candidate and 'parts' in candidate['content']:
                        for part in candidate['content']['parts']:
                            if 'text' in part:
                                full_response = part['text']
                                yield full_response
            
            if not full_response:
                yield "❌ No response generated. Please try again."
                return None, None
            
            return full_response, "vision"
        
        except Exception as e:
            print(f"[SOLVE_VISION] Error: {e}")
            yield f"❌ Error: {str(e)}"
            return None, None
    
    @staticmethod
    def solve_with_tutor(extracted_text, chat_history, rag_context=None, rag_subject=None, rag_chapter=None, rag_confidence=None):
        """
        Solve extracted text problem with tutor-style explanations.
        For text-only content (strategy: cheaper + better quality).
        
        Now includes RAG context for any chapter (not just geometry).
        
        Yields response chunks as generator.
        """
        solving_prompt = f"Solve this math problem:\n\n{extracted_text}"
        solving_messages = [{"role": "user", "content": solving_prompt}]
        
        # Build system prompt with RAG context if available
        if rag_context and rag_confidence and rag_confidence >= KNOWLEDGE_BASE.config.get('min_confidence_threshold', 0.15):
            system_msg = f"""You are an expert math tutor. You have relevant curriculum knowledge:

CURRICULUM CONTEXT: {rag_subject} - {rag_chapter} (Confidence: {rag_confidence:.0%})
{rag_context}

Now solve the problem using the context above if relevant:

{extracted_text}

Provide:
1. **Given:** What information is provided
2. **To Find:** What needs to be calculated
3. **Solution:** Step-by-step explanation with all work
4. **Answer:** Final answer clearly stated
5. **SEE Tip:** Exam preparation tip"""
        else:
            system_msg = f"""You are an expert math tutor. Solve this problem:

{extracted_text}

Provide:
1. **Given:** What information is provided
2. **To Find:** What needs to be calculated
3. **Solution:** Step-by-step explanation with all work
4. **Answer:** Final answer clearly stated
5. **SEE Tip:** Exam preparation tip"""
        
        solving_system = system_msg
        
        try:
            print(f"[SOLVE_TEXT] Solving with tutor model (text only)...")
            print(f"[SOLVE_TEXT] RAG Context: {rag_subject}/{rag_chapter if rag_context else 'None'}")
            response, provider, success = call_api_with_intelligent_fallback(
                "tutor_mode", solving_system, solving_messages, 
                image_data=None
            )
            
            if not response or not success:
                yield "❌ Could not solve this problem. Please try again."
                return None, None
            
            full_response = ""
            
            # Handle streaming
            if response.headers.get('content-type', '').startswith('text/event-stream'):
                for line in response.iter_lines():
                    if line:
                        line_str = line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                        if line_str.startswith('data: '):
                            try:
                                data = json.loads(line_str[6:])
                                if 'choices' in data and data['choices']:
                                    delta = data['choices'][0].get('delta', {})
                                    if 'content' in delta:
                                        chunk = delta['content']
                                        full_response += chunk
                                        yield chunk
                            except json.JSONDecodeError:
                                continue
            # Handle non-streaming (Gemini)
            else:
                data = response.json()
                if 'candidates' in data and data['candidates']:
                    candidate = data['candidates'][0]
                    if 'content' in candidate and 'parts' in candidate['content']:
                        for part in candidate['content']['parts']:
                            if 'text' in part:
                                full_response = part['text']
                                yield full_response
            
            if not full_response:
                yield "❌ No response generated. Please try again."
                return None, None
            
            return full_response, "tutor_mode"
        
        except Exception as e:
            print(f"[SOLVE_TEXT] Error: {e}")
            yield f"❌ Error: {str(e)}"
            return None, None
 
# ============================================================================

class APIProvider:
    """Manages API calls with intelligent fallback and rate limit handling."""
    
    # Primary providers with their models
    PROVIDERS = {
        'groq': {
            'url': GROQ_API_URL,
            'api_key': GROQ_API_KEY,
            'type': 'openai_compatible',
            'models': {
                'normal': 'llama-3.1-8b-instant',
                'tutor_mode': 'llama-3.3-70b-versatile',
                'solve_mode': 'llama-3.3-70b-versatile',
                'vision': 'llama-3.3-70b-versatile'  # Groq's dedicated vision model
            },
            'supports_vision': True,
            'status': 'active'
        },
        'cerebras': {
            'url': CEREBRAS_API_URL,
            'api_key': CEREBRAS_API_KEY,
            'type': 'openai_compatible',
            'models': {
                'normal': 'gpt-oss-120b',
                'tutor_mode': 'zai-glm-4.7',
                'solve_mode': 'zai-glm-4.7',
                'vision': None
            },
            'supports_vision': False,
            'status': 'active'
        },
        'gemini': {
            'url': VERTEX_AI_API_URL,
            'api_key': 'vertex-ai-token',  # placeholder - we'll use access token instead
            'type': 'vertex_ai',
            'models': {
                'normal': 'gemini-2.5-flash-lite',
                'tutor_mode': 'gemini-2.5-flash-lite',
                'solve_mode': 'gemini-2.5-flash-lite',
                'vision': 'gemini-2.5-flash'
            },
            'supports_vision': True,
            'status': 'active'
        },
        'openrouter': {
            'url': OPENROUTER_API_URL,
            'api_key': OPENROUTER_API_KEY,
            'type': 'openai_compatible',
            'models': {
                'normal': 'google/gemma-4-26b-a4b-it:free',
                'tutor_mode': 'deepseek/deepseek-v4-flash:free',
                'solve_mode': 'deepseek/deepseek-v4-flash:free',
                'vision': 'google/gemma-4-31b-it:free'
            },
            'supports_vision': True,
            'status': 'active'
        }
    }
    
    # Fallback chain: [primary, secondary, tertiary]
    FALLBACK_CHAIN = {
        'normal': ['groq', 'cerebras', 'openrouter', 'gemini'],
        'tutor_mode': ['groq', 'gemini', 'cerebras', 'openrouter'],
        'solve_mode': ['groq', 'gemini', 'cerebras', 'openrouter'],
        'vision': ['gemini', 'groq', 'openrouter'],  # Gemini FIRST - best vision model
        'explanation': ['groq', 'gemini', 'openrouter'],  # Conceptual questions
        'greeting': ['groq', 'openrouter'],  # Simple responses
    }
    
    # Rate limit tracking
    RATE_LIMITS = {
        'groq': {'requests': 0, 'last_reset': time.time(), 'max_requests': 100},
        'cerebras': {'requests': 0, 'last_reset': time.time(), 'max_requests': 200},
        'openrouter': {'requests': 0, 'last_reset': time.time(), 'max_requests': 150},
        'gemini': {'requests': 0, 'last_reset': time.time(), 'max_requests': 120}
    }
    
    @classmethod
    def check_rate_limit(cls, provider):
        """Check and update rate limit status for a provider."""
        limit = cls.RATE_LIMITS.get(provider, {})
        current_time = time.time()
        
        # Reset hourly
        if current_time - limit.get('last_reset', 0) > 3600:
            limit['requests'] = 0
            limit['last_reset'] = current_time
        
        if limit['requests'] >= limit.get('max_requests', 100):
            return False, f"Rate limit exceeded for {provider}"
        
        return True, None
    
    @classmethod
    def increment_request(cls, provider):
        """Increment request counter."""
        if provider in cls.RATE_LIMITS:
            cls.RATE_LIMITS[provider]['requests'] += 1
    
    @classmethod
    def get_provider_status(cls, provider):
        """Get full status of a provider."""
        if provider not in cls.PROVIDERS:
            return None
        
        prov = cls.PROVIDERS[provider]
        can_use, limit_error = cls.check_rate_limit(provider)
        
        return {
            'name': provider,
            'status': 'active' if can_use else 'rate_limited',
            'error': limit_error,
            'requests_used': cls.RATE_LIMITS[provider].get('requests', 0),
            'api_key_configured': bool(prov.get('api_key')),
            'max_requests': cls.RATE_LIMITS[provider].get('max_requests', 0)
        }
    
    @classmethod
    def call_provider(cls, provider, mode, system_prompt, messages, image_data=None, stream=True):
        """Call a specific provider with error handling and optional image support."""
        if provider not in cls.PROVIDERS:
            return None, f"Unknown provider: {provider}"
        
        # Check rate limit
        can_use, limit_error = cls.check_rate_limit(provider)
        if not can_use:
            return None, limit_error
        
        prov = cls.PROVIDERS[provider]
        api_key = prov.get('api_key')
        
        if not api_key:
            return None, f"API key not configured for {provider}"
        
        # Check vision support
        if image_data and not prov.get('supports_vision'):
            return None, f"{provider} does not support vision"
        
        model = prov['models'].get('vision' if image_data else mode, prov['models']['normal'])
        
        if not model:
            return None, f"No model available for {mode} in {provider}"
        
        print(f"[DEBUG] Calling {provider} with model: {model}, has_image: {image_data is not None}")
        
        try:
            if prov['type'] == 'vertex_ai':
                response = cls._call_vertex_ai(prov['url'], system_prompt, messages, model, image_data, stream=False)
            elif prov['type'] == 'gemini':
                # Legacy fallback (shouldn't be used now)
                response = cls._call_vertex_ai(prov['url'], system_prompt, messages, model, image_data, stream=False)
            else:
                response = cls._call_openai_compatible(prov['url'], prov['api_key'], model, messages, image_data, stream)
            
            if response:
                # Print error details if status is not 200
                if response.status_code != 200:
                    try:
                        error_detail = response.json()
                        logger.warning(f"[{provider.upper()}] Error {response.status_code}: {error_detail}")
                    except (ValueError, KeyError):
                        logger.warning(f"[{provider.upper()}] Error {response.status_code}: {response.text[:200]}")
                
                if response.status_code in [200, 400, 401, 429]:
                    cls.increment_request(provider)
                    return response, None
            
            return None, f"Provider {provider} returned status {response.status_code if response else 'None'}"
        
        except Exception as e:
            error_msg = f"Provider {provider} error: {str(e)}"
            print(f"[ERROR] {error_msg}")
            import traceback
            traceback.print_exc()
            return None, error_msg
    
    @classmethod
    def _call_openai_compatible(cls, url, api_key, model, messages, image_data=None, stream=True):
        """Call OpenAI-compatible API (Groq, Cerebras, OpenRouter) with optional image support."""
        import copy
        msgs = copy.deepcopy(messages)
        
        # Add image to messages if provided (Groq format)
        if image_data:
            for msg in msgs:
                if msg.get('role') == 'user' and isinstance(msg.get('content'), str):
                    msg['content'] = [
                        {"type": "text", "text": msg['content']},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
                    ]
            print(f"[IMAGE_FORMAT] Added image to message, base64 length: {len(image_data)}")
        
        payload = {
            "model": model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 8192,
            "stream": stream
        }
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        try:
            # Log request details
            print(f"[GROQ_REQUEST] Model: {model}, Messages: {len(msgs)}, Image: {image_data is not None}")
            
            response = requests.post(url, json=payload, headers=headers, stream=stream, timeout=60)
            print(f"[GROQ/OPENAI] Response status: {response.status_code}")
            return response
        except requests.exceptions.Timeout:
            print(f"[TIMEOUT] OpenAI-compatible API timeout")
            return None
        except Exception as e:
            print(f"[API_ERROR] OpenAI-compatible call failed: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    @classmethod
    def _call_vertex_ai(cls, url, system_prompt, messages, model, image_data=None, stream=False):
        """
        Call Vertex AI API using Service Account authentication.
        Uses OAuth 2.0 access token instead of API key.
        """
        # Get access token from service account
        access_token = get_vertex_ai_access_token()
        if not access_token:
            print(f"[VERTEX_AI] ERROR: Could not generate access token")
            return None
        
        # Convert messages to Vertex AI format
        contents = []
        for msg in messages:
            if msg.get("role") == "system":
                continue
            
            content_parts = []
            
            # Text content
            if isinstance(msg.get('content'), str):
                content_parts.append({"text": msg.get("content", "")})
            elif isinstance(msg.get('content'), list):
                content_parts.extend(msg.get('content'))
            
            # Image content (Vertex AI format - base64 inline)
            if image_data:
                content_parts.append({
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": image_data
                    }
                })
            
            contents.append({
                "role": "user" if msg.get("role") == "user" else "model",
                "parts": content_parts
            })
        
        # Vertex AI request format
        payload = {
            "contents": contents,
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "generation_config": {
                "temperature": 0.7,
                "top_k": 40,
                "top_p": 0.95,
                "max_output_tokens": 8192,
            }
        }
        
        # Build Vertex AI endpoint
        vertex_url = f"{url}/{model}:generateContent"
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        try:
            print(f"[VERTEX_AI] Calling {model} at {vertex_url[:80]}...")
            response = requests.post(vertex_url, json=payload, headers=headers, timeout=60)
            print(f"[VERTEX_AI] Response status: {response.status_code}")
            
            if response.status_code != 200:
                try:
                    error_detail = response.json()
                    logger.warning(f"[VERTEX_AI] Error details: {error_detail}")
                except (ValueError, KeyError):
                    logger.warning(f"[VERTEX_AI] Error body: {response.text[:500]}")
            
            return response
        
        except requests.exceptions.Timeout:
            print(f"[TIMEOUT] Vertex AI timeout")
            return None
        except Exception as e:
            print(f"[API_ERROR] Vertex AI call failed: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    @classmethod
    def call_with_fallback(cls, mode, system_prompt, messages, image_data=None, stream=True):
        """
        Call API with intelligent fallback chain.
        Returns: (response, provider_used, error_log)
        """
        # Use vision chain for images, otherwise use normal chain
        if image_data:
            fallback_chain = cls.FALLBACK_CHAIN.get('vision', cls.FALLBACK_CHAIN['normal'])
        else:
            fallback_chain = cls.FALLBACK_CHAIN.get(mode, cls.FALLBACK_CHAIN['normal'])
        
        error_log = []
        
        for provider in fallback_chain:
            # Skip if image and provider doesn't support vision
            if image_data and not cls.PROVIDERS[provider].get('supports_vision'):
                print(f"[SKIP] {provider} does not support vision")
                continue
            
            print(f"[PROVIDER] Attempting {provider}...")
            response, error = cls.call_provider(provider, mode, system_prompt, messages, image_data, stream)
            
            if response:
                if response.status_code == 200:
                    print(f"[SUCCESS] Using {provider}")
                    return response, provider, error_log
                elif response.status_code == 429:
                    error_msg = f"{provider}: Rate limit hit"
                    error_log.append(error_msg)
                    print(f"[FALLBACK] {error_msg}")
                elif response.status_code in [401, 403]:
                    error_msg = f"{provider}: Authentication failed"
                    error_log.append(error_msg)
                    print(f"[FALLBACK] {error_msg}")
                else:
                    error_msg = f"{provider}: HTTP {response.status_code}"
                    error_log.append(error_msg)
                    print(f"[FALLBACK] {error_msg}")
            else:
                error_log.append(error)
                print(f"[FALLBACK] {error}")
        
        return None, None, error_log

# Initialize API Provider
api_provider = APIProvider()

# Directories
CHAT_HISTORY_DIR = os.path.join(app.root_path, 'chat_history')
os.makedirs(CHAT_HISTORY_DIR, exist_ok=True)
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# In-memory store for recent uploaded images per user session.
# Structure: { user_id: { "data": base64_str, "caption": str, "chat_id": str, "ts": float } }
RECENT_IMAGES = {}

# ============================================================================
# 📊 FIREBASE-INTEGRATED USER MESSAGE QUOTA TRACKING
# ============================================================================
# Tracks quota per Google Account with Firebase Realtime Database
# Structure: users/{user_id}/quota/{date}/count & metadata

QUOTA_FILE = os.path.join(app.root_path, 'user_quotas.json')
DAILY_MESSAGE_LIMIT =50

def load_user_quotas():
    """Load user quotas from persistent file (fallback when Firebase unavailable)."""
    if os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.warning(f"[QUOTA] Error loading quotas file: {e}")
            return {}
    return {}

def save_user_quotas(quotas):
    """Save user quotas to persistent file (fallback)."""
    try:
        with open(QUOTA_FILE, 'w', encoding='utf-8') as f:
            json.dump(quotas, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[QUOTA] Error saving quotas: {e}")

def get_daily_message_count(user_id):
    """
    Get daily message count for user from Firebase.
    Falls back to local JSON if Firebase unavailable.
    
    Firebase structure: users/{user_id}/quota/{date}
    """
    today_str = date.today().isoformat()
    
    # PRIMARY: Try Firebase first
    if FIREBASE_AVAILABLE:
        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
            path = f"users/{safe_user_id}/quota/{today_str}"
            ref = db.reference(path)
            quota_data = ref.get()
            
            if quota_data and isinstance(quota_data, dict):
                count = quota_data.get('count', 0)
                print(f"[QUOTA-FIREBASE] ✓ Retrieved from Firebase: user={user_id}, date={today_str}, count={count}")
                return count
            else:
                print(f"[QUOTA-FIREBASE] No quota record in Firebase for {today_str}, returning 0")
                return 0
                
        except Exception as e:
            print(f"[QUOTA-FIREBASE] ERROR reading quota: {e}, falling back to local")
    
    # FALLBACK: Use local JSON file
    quotas = load_user_quotas()
    
    if user_id not in quotas:
        quotas[user_id] = {}
    
    if today_str not in quotas[user_id]:
        quotas[user_id][today_str] = 0
    
    count = quotas[user_id][today_str]
    print(f"[QUOTA-LOCAL] Using local fallback: user={user_id}, date={today_str}, count={count}")
    return count

def increment_daily_message_count(user_id):
    """
    Increment daily message count for user in Firebase.
    Automatically creates quota record if not exists.
    Falls back to local JSON if Firebase unavailable.
    
    Firebase structure: users/{user_id}/quota/{date}
      {
        "count": <number>,
        "last_message_timestamp": <unix_timestamp>,
        "date": "YYYY-MM-DD"
      }
    """
    today_str = date.today().isoformat()
    
    # PRIMARY: Try Firebase first
    if FIREBASE_AVAILABLE:
        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
            quota_path = f"users/{safe_user_id}/quota/{today_str}"
            
            # Get current count
            ref = db.reference(quota_path)
            quota_data = ref.get()
            current_count = 0
            
            if quota_data and isinstance(quota_data, dict):
                current_count = quota_data.get('count', 0)
            
            # Increment and save
            new_count = current_count + 1
            quota_record = {
                "count": new_count,
                "last_message_timestamp": int(time.time()),
                "date": today_str
            }
            
            ref.set(quota_record)
            
            print(f"[QUOTA-FIREBASE] ✓ Incremented: user={user_id}, date={today_str}, new_count={new_count}")
            return new_count
            
        except Exception as e:
            print(f"[QUOTA-FIREBASE] ERROR incrementing: {e}, falling back to local")
    
    # FALLBACK: Use local JSON file
    quotas = load_user_quotas()
    
    if user_id not in quotas:
        quotas[user_id] = {}
    
    if today_str not in quotas[user_id]:
        quotas[user_id][today_str] = 0
    
    quotas[user_id][today_str] += 1
    
    # Clean up old quota data (older than 30 days)
    thirty_days_ago = (date.today() - timedelta(days=30)).isoformat()
    for date_key in list(quotas[user_id].keys()):
        if date_key < thirty_days_ago:
            del quotas[user_id][date_key]
    
    save_user_quotas(quotas)
    new_count = quotas[user_id][today_str]
    print(f"[QUOTA-LOCAL] Incremented (fallback): user={user_id}, date={today_str}, new_count={new_count}")
    return new_count

def get_remaining_messages(user_id):
    """Get remaining message count for user today."""
    current_count = get_daily_message_count(user_id)
    return max(0, DAILY_MESSAGE_LIMIT - current_count)

# ============================================================================
# 📚 EXTERNAL CHUNKED KNOWLEDGE BASE (FULLY UPDATED)
# ============================================================================

class ChunkedKnowledgeBase:
    """
    Load and query external curriculum_chunks.json with advanced matching.
    Supports weighted keywords, intent patterns, fuzzy matching, negative keywords.
    """
    
    def __init__(self, chunks_file='curriculum_chunks.json'):
        self.chunks = []
        self.config = {}
        self.metadata = {}
        self.load_chunks(chunks_file)
    
    def load_chunks(self, path='curriculum_chunks.json'):
        """Load chunks from external JSON file."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.chunks = data.get('chunks', [])
            self.config = data.get('config', {
                "min_confidence_threshold": 0.08,
                "max_chunks_per_query": 3,
                "keyword_match_weight": 1.0,
                "intent_pattern_weight": 2.0,
                "use_fuzzy_matching": True,
                "fuzzy_threshold": 0.6
            })
            self.metadata = data.get('metadata', {})
            
            print(f"[KB] Loaded {len(self.chunks)} chunks from {path}")
            print(f"[KB] Version: {self.metadata.get('version', 'unknown')}")
            print(f"[KB] Config: min_confidence={self.config.get('min_confidence_threshold')}")
        except FileNotFoundError:
            print(f"[KB] Warning: {path} not found. Using fallback empty KB.")
            self.chunks = []
            self.config = {}
            self.metadata = {}
        except json.JSONDecodeError as e:
            print(f"[KB] Error: Invalid JSON in {path}: {e}")
            self.chunks = []
            self.config = {}
            self.metadata = {}
    
    def _extract_keywords_from_chunk(self, chunk):
        """Extract keywords from chunk - handles both list and dict formats."""
        keywords = chunk.get('keywords', [])
        
        # Handle string keywords
        if keywords and isinstance(keywords[0], str):
            return [{'word': kw, 'weight': 5} for kw in keywords]
        
        # Handle dict format with weights
        return keywords
    
    def _match_keywords(self, question_lower, chunk):
        """Calculate keyword match score with weights."""
        score = 0
        matched_keywords = []
        
        keywords = self._extract_keywords_from_chunk(chunk)
        
        for kw in keywords:
            word = kw.get('word', '').lower()
            weight = kw.get('weight', 5)
            
            # Check for exact word match or partial match for multi-word
            if word in question_lower:
                score += weight * self.config.get('keyword_match_weight', 1.0)
                matched_keywords.append(word)
            elif ' ' in word:
                # For multi-word keywords like "compound interest"
                if word in question_lower:
                    score += weight * self.config.get('keyword_match_weight', 1.0) * 1.5
                    matched_keywords.append(word)
        
        return score, matched_keywords
    
    def _apply_negative_keywords(self, question_lower, chunk):
        """Apply negative keyword penalties."""
        penalty = 0
        
        for nkw in chunk.get('negative_keywords', []):
            word = nkw.get('word', '').lower()
            weight = nkw.get('weight', 10)
            
            if word in question_lower:
                penalty += weight * self.config.get('negative_keyword_penalty', 2.0)
        
        return penalty
    
    def _match_intent_patterns(self, question_lower, chunk):
        """Match regex intent patterns."""
        score = 0
        
        for pattern_info in chunk.get('intent_patterns', []):
            pattern = pattern_info.get('pattern', '')
            weight = pattern_info.get('weight', 10)
            
            try:
                if re.search(pattern, question_lower, re.IGNORECASE):
                    score += weight * self.config.get('intent_pattern_weight', 2.0)
            except re.error:
                continue
        
        return score
    
    def _fuzzy_match(self, question_lower, chunk):
        """Fuzzy matching for short queries and keywords."""
        if not self.config.get('use_fuzzy_matching', True):
            return 0
        
        score = 0
        threshold = self.config.get('fuzzy_threshold', 0.6)
        
        # Only apply fuzzy for short questions
        if len(question_lower) > 40:
            return 0
        
        keywords = self._extract_keywords_from_chunk(chunk)
        
        for kw in keywords:
            word = kw.get('word', '').lower()
            if len(word) > 3:
                if word in question_lower:
                    continue
                ratio = SequenceMatcher(None, word, question_lower).ratio()
                if ratio > threshold:
                    score += kw.get('weight', 5) * ratio * 0.5
        
        return score
    
    def _format_content(self, chunk, max_chunks):
        """Format chunk content for injection - handles all content types."""
        content_obj = chunk.get('content', {})
        
        formatted_parts = []
        
        # Add SEE marks if available
        see_mark = chunk.get('see_mark', '')
        if see_mark:
            formatted_parts.append(f"📊 **SEE Marks:** {see_mark}")
        
        # Add explanation/strategy/approach
        if content_obj.get('explanation'):
            formatted_parts.append(f"📖 {content_obj['explanation']}")
        if content_obj.get('strategy'):
            formatted_parts.append(f"🎯 **Strategy:** {content_obj['strategy']}")
        if content_obj.get('approach'):
            formatted_parts.append(f"📝 **Approach:** {content_obj['approach']}")
        
        # Add formula
        if content_obj.get('formula'):
            formulas = content_obj['formula']
            if isinstance(formulas, str):
                formatted_parts.append(f"📐 **Formula:** {formulas}")
            elif isinstance(formulas, dict):
                for name, formula in formulas.items():
                    formatted_parts.append(f"📐 **{name.title()}:** {formula}")
        
        # Add table (for trig values, etc.)
        if content_obj.get('table'):
            formatted_parts.append(f"📊 **Values:**\n{content_obj['table']}")
        
        # Add solved example
        if content_obj.get('solved_example'):
            formatted_parts.append(f"✅ **Solved Example:**\n{content_obj['solved_example']}")
        
        # Add common mistakes
        if content_obj.get('common_mistakes'):
            mistakes = content_obj['common_mistakes']
            if isinstance(mistakes, list):
                mistakes_str = '\n'.join(f"• {m}" for m in mistakes)
            else:
                mistakes_str = mistakes
            formatted_parts.append(f"⚠️ **Common Mistakes to Avoid:**\n{mistakes_str}")
        
        # Add SEE tips
        see_tips = content_obj.get('see_tips') or chunk.get('see_tips')
        if see_tips:
            if isinstance(see_tips, list):
                tips_str = '\n'.join(f"• {t}" for t in see_tips)
            else:
                tips_str = see_tips
            formatted_parts.append(f"💡 **SEE Tip:** {tips_str}")
        
        # Add theorem proof
        if content_obj.get('theorem_proof') or content_obj.get('proof'):
            proof = content_obj.get('theorem_proof') or content_obj.get('proof')
            formatted_parts.append(f"📐 **Proof:** {proof}")
        
        # Add formulas section
        if content_obj.get('formulas'):
            formatted_parts.append(f"📐 **Formulas:**\n{content_obj['formulas']}")
        
        # Add mistakes section
        if content_obj.get('mistakes'):
            formatted_parts.append(f"⚠️ **Mistakes to Avoid:**\n{content_obj['mistakes']}")
        
        # Add pattern
        if content_obj.get('pattern'):
            formatted_parts.append(f"📋 **Question Pattern:** {content_obj['pattern']}")
        
        return '\n\n'.join(formatted_parts)
    
    def retrieve(self, question, max_chunks=None):
        """
        Retrieve most relevant chunks with weighted scoring.
        
        Args:
            question: User's question text
            max_chunks: Max chunks to return (uses config if None)
        
        Returns:
            tuple: (subject, chapter, context_string, confidence, chunks_used)
        """
        if max_chunks is None:
            max_chunks = self.config.get('max_chunks_per_query', 3)
        
        question_lower = question.lower()
        scored_chunks = []
        
        # Special handling for equations
        has_equals = '=' in question_lower
        has_variable = bool(re.search(r'[0-9][x]|[x][0-9]| [x] |\(x\)| x[=+\-]', question_lower))
        has_chemical = any(kw in question_lower for kw in ['chemical', 'reaction', 'acid', 'base', 'salt'])
        
        if has_equals and has_variable and not has_chemical:
            for chunk in self.chunks:
                if chunk.get('id') == 'math_algebra_linear_005':
                    content = self._format_content(chunk, 1)
                    return ('mathematics', 'algebra', content, 0.95, 1)
        
        # Score each chunk
        for chunk in self.chunks:
            keyword_score, matched = self._match_keywords(question_lower, chunk)
            penalty = self._apply_negative_keywords(question_lower, chunk)
            intent_score = self._match_intent_patterns(question_lower, chunk)
            fuzzy_score = self._fuzzy_match(question_lower, chunk)
            
            total_score = keyword_score + intent_score + fuzzy_score - penalty
            
            # Boost score for exam strategy questions
            if chunk.get('chapter') == 'exam_strategy':
                strategy_keywords = ['exam', 'see', 'mark', 'score', 'time', 'mistake', 'formula', 'pattern', 'model']
                if any(kw in question_lower for kw in strategy_keywords):
                    total_score *= 1.3
            
            if total_score > 0:
                scored_chunks.append({
                    'score': total_score,
                    'chunk': chunk,
                    'matched_keywords': matched
                })
        
        # Sort by score descending
        scored_chunks.sort(key=lambda x: x['score'], reverse=True)
        
        # Calculate confidence
        max_possible_score = 50
        confidence = min(scored_chunks[0]['score'] / max_possible_score, 1.0) if scored_chunks else 0
        
        # Check threshold
        min_threshold = self.config.get('min_confidence_threshold', 0.15)
        if not scored_chunks or confidence < min_threshold:
            print(f"[RAG] No chunk above threshold (conf: {confidence:.2f} < {min_threshold})")
            return (None, None, "", confidence, 0)
        
        # Get top chunks
        top_chunks = scored_chunks[:max_chunks]
        
        # Format combined context
        contexts = []
        subjects = set()
        chapters = set()
        
        for idx, item in enumerate(top_chunks):
            chunk = item['chunk']
            formatted = self._format_content(chunk, len(top_chunks))
            header = f"--- {chunk.get('chapter', '').replace('_', ' ').title()} - {chunk.get('topic', chunk.get('subtopic', '')).replace('_', ' ').title()} ---"
            contexts.append(f"{header}\n{formatted}")
            subjects.add(chunk.get('subject', ''))
            chapters.add(chunk.get('chapter', ''))
        
        combined_context = '\n\n'.join(contexts)
        
        # Get primary subject/chapter
        primary = top_chunks[0]['chunk']
        
        print(f"[RAG] Retrieved {len(top_chunks)} chunks (conf: {confidence:.2f})")
        print(f"[RAG] Primary: {primary.get('subject')}/{primary.get('chapter')} - {primary.get('topic', '')}")
        
        return (primary.get('subject'), primary.get('chapter'), combined_context, confidence, len(top_chunks))

# Initialize external knowledge base
KNOWLEDGE_BASE = ChunkedKnowledgeBase()

# All chapters in SEE Class 10 Mathematics curriculum
ALL_CHAPTERS = [
    "sets",
    "arithmetic",
    "algebra",
    "geometry",
    "trigonometry",
    "statistics",
    "exam_strategy"
]

# ============================================================================
# 💾 CHAT HISTORY MANAGEMENT
# ============================================================================

MAX_HISTORY_MESSAGES = 10

def trim_chat_history(chat_history, max_messages=MAX_HISTORY_MESSAGES):
    """Trim chat history to prevent token inflation."""
    if len(chat_history) <= max_messages:
        return chat_history
    return chat_history[-max_messages:]

def get_chat_context_string(chat_history, max_messages=4):
    """Convert chat history to simple context string."""
    if not chat_history:
        return ""
    
    recent = chat_history[-max_messages:]
    context_lines = ["Previous conversation:"]
    for msg in recent:
        role = "Student" if msg['type'] == 'user' else "Vexara"
        text = msg['text'][:200]
        context_lines.append(f"{role}: {text}")
    
    return "\n".join(context_lines)

def normalize_math_response(text):
    """Normalize raw math fragments so KaTeX can render them reliably."""
    if not text:
        return text

    protected_segments = []

    def stash(segment_match):
        protected_segments.append(segment_match.group(0))
        return f"\u0002P{len(protected_segments) - 1}\u0002"

    working = str(text)

    # Protect code fences / inline code from any math rewriting.
    working = re.sub(r"```[\s\S]*?```|`[^`]+`", stash, working)

    # Protect already-wrapped math from being touched.
    working = re.sub(r"\$\$[\s\S]*?\$\$|\$[^\$\n]+\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]", stash, working)

    # Wrap common bare LaTeX fragments one by one so we don't swallow prose.
    def wrap_match(match):
        expr = match.group(0).replace("%", r"\%")
        return "$" + expr + "$"

    working = re.sub(r"(?<!\$)(?<!\\)(\\text\{[^{}]*\}|\\boxed\{[^{}]*\}|\\sqrt\{[^{}]*\}|\\frac\{[^{}]*\}\{[^{}]*\}|\\dfrac\{[^{}]*\}\{[^{}]*\})", wrap_match, working)
    working = re.sub(r"(?<!\$)(?<!\\)(\\approx|\\times|\\pm|\\cdot|\\sum|\\int|\\lim|\\leq|\\geq|\\neq|\\equiv|\\to)", lambda m: f"${m.group(0)}$", working)

    # Restore protected segments.
    return re.sub(r"\u0002P(\d+)\u0002", lambda m: protected_segments[int(m.group(1))], working)

# ============================================================================
# 🤖 VEXARA AGENTIC CORE
# ============================================================================

class VexaraAgent:
    """
    Agentic layer for Vexara.

    Responsibilities:
    - Maintain a registry of every skill Vexara has so it can describe itself.
    - Classify user intent before every /ask call so the right action is taken.
    - Remember the most recent uploaded image per user so it can be recalled
      in follow-up messages ("solve the image I just sent").
    - Detect math/exam problems submitted in the wrong mode and guide the user
      while offering to switch + solve automatically.
    """

    # ── Skill Registry ────────────────────────────────────────────────────────
    SKILLS = {
        "vision": {
            "name": "Image / Vision Solver",
            "trigger": "Upload any image containing a math problem",
            "description": (
                "I can read photos of handwritten problems, printed worksheets, "
                "geometry diagrams, and coordinate graphs. Upload an image and I'll "
                "extract every question, solve it step-by-step, and link it to your SEE curriculum."
            ),
            "models_used": ["Gemini 2.5 Flash (primary)", "Groq vision (fallback)"],
            "how_to_use": "Click the 📎 image button beside the input box and upload your photo.",
        },
        "tutor_mode": {
            "name": "Tutor Mode (Solve + Explain)",
            "trigger": "Select 'Tutor' mode OR type a solve / calculate / find question",
            "description": (
                "Solves your math problem AND explains the concepts so you learn. "
                "Each step includes a brief explanation of why it's taken, helping "
                "you understand the reasoning — not just memorize the solution."
            ),
            "models_used": ["Groq Llama 3.3-70B", "Gemini 2.5 Flash", "DeepSeek (fallback)"],
            "how_to_use": "Select the 'Tutor' radio button above the input box, then type your problem.",
        },
        "solve_mode": {
            "name": "Solve Mode (Quick & Concise)",
            "trigger": "Select 'Solve' mode",
            "description": (
                "Quick, clean solutions with minimal explanation. Shows the key steps "
                "and the answer — no teaching, no tips, just the math. Perfect when "
                "you just need the answer fast."
            ),
            "models_used": ["Groq Llama 3.3-70B", "Gemini 2.5 Flash", "OpenRouter (fallback)"],
            "how_to_use": "Select the 'Solve' radio button above the input box, then type your problem.",
        },
        "rag_curriculum": {
            "name": "SEE Curriculum Knowledge Base",
            "trigger": "Any conceptual or topic question",
            "description": (
                "I have 150+ hand-curated curriculum chunks covering all 7 SEE Math "
                "chapters (Sets, Arithmetic, Algebra, Geometry, Trigonometry, Statistics, "
                "Exam Strategy). I automatically retrieve the most relevant chunk and "
                "inject it into every response — no hallucinated formulas."
            ),
            "models_used": ["Weighted keyword + regex RAG (no vector DB needed)"],
            "how_to_use": "Just ask any concept question — retrieval happens automatically.",
        },
        "personalization": {
            "name": "Progress Tracking & Personalisation",
            "trigger": "Automatic — active after your first message",
            "description": (
                "I track which chapters you've studied, how often, and which ones "
                "you keep struggling with. Your welcome message, mode suggestions, and "
                "follow-up hints are all tailored to your history."
            ),
            "models_used": ["Firebase Realtime Database + local JSON fallback"],
            "how_to_use": "Nothing — it's always on. Ask me 'what are my weak topics?' to see insights.",
        },
        "vision_recall": {
            "name": "Recent Image Recall",
            "trigger": "Mentioning a previously uploaded image in follow-up text",
            "description": (
                "If you upload an image and then later ask 'solve question 3 from that "
                "image' or 'check the diagram I sent', I remember the last image you "
                "uploaded in this session and re-run the vision solver on it with your "
                "new instruction — no need to upload again."
            ),
            "models_used": ["Same vision chain as Image Solver"],
            "how_to_use": (
                "Upload an image first, then in a follow-up message say something like "
                "'solve the last question in that image' or 're-check the image I uploaded'."
            ),
        },
    }

    # ── Intent patterns ───────────────────────────────────────────────────────

    # Patterns that signal the user is asking about what Vexara can do.
    _SKILL_INQUIRY_PATTERNS = re.compile(
        r"\b(what\s+can\s+you\s+do|what\s+are\s+your\s+skills?|what\s+skills?\s+do\s+you\s+have"
        r"|what\s+modes?\s+do\s+you\s+have|tell\s+me\s+your\s+(skills?|features?|abilities?|capabilities?)"
        r"|how\s+do\s+you\s+work|what\s+is\s+vexara|what\s+can\s+vexara\s+do"
        r"|list\s+your\s+(skills?|features?|modes?)|your\s+(skills?|features?|modes?|abilities?))\b",
        re.IGNORECASE,
    )

    # Patterns that signal the user wants to refer back to a previously uploaded image.
    _VISION_RECALL_PATTERNS = re.compile(
        r"\b(that\s+image|the\s+image\s+i\s+(sent|uploaded|shared|gave)"
        r"|my\s+(last|previous|recent)\s+image"
        r"|check\s+(the\s+)?(image|photo|picture|diagram|question)\s+i\s+(sent|uploaded|shared)"
        r"|re([-\s])?check\s+the\s+(image|photo|diagram)"
        r"|solve\s+.{0,30}(from\s+)?(that|the)\s+(image|photo|picture|diagram)"
        r"|question\s+\d+\s+from\s+(that|the)\s+image"
        r"|look\s+at\s+(the|that)\s+(image|picture|photo|diagram)\s+i\s+(uploaded|sent)"
        r"|use\s+(the|that)\s+(image|photo|picture)\s+i\s+(uploaded|sent|shared))\b",
        re.IGNORECASE,
    )

    # Patterns that look like a math problem (solve / calculate / find + numbers or equations).
    _MATH_PROBLEM_PATTERNS = re.compile(
        r"(\bsolve\b|\bcalculate\b|\bfind\b|\bprove\b|\bevaluate\b|\bsimplify\b|\bfactorise\b|\bfactorize\b"
        r"|\bdetermine\b|\bcompute\b|\bderive\b)"
        r"|\d+\s*[x]\s*[=+\-*/]"
        r"|[a-z]\s*=\s*\d"
        r"|\d+\s*[+\-*/^]\s*\d",
        re.IGNORECASE,
    )

    # Patterns for conceptual / explanation questions
    _EXPLANATION_PATTERNS = re.compile(
        r"\b(what\s+is\s+a\s+|what\s+are\s+|explain\s+|describe\s+|define\s+"
        r"|how\s+does\s+|how\s+do\s+|why\s+(is|are|do|does)\s+"
        r"|difference\s+between\s+|tell\s+me\s+about\s+|what\s+is\s+the\s+meaning"
        r"|what\s+do\s+you\s+mean\s+by|can\s+you\s+explain"
        r"|what\s+happens\s+when|what\s+is\s+the\s+formula|what\s+are\s+the\s+properties"
        r"|state\s+the\s+|write\s+the\s+|mention\s+the)\b",
        re.IGNORECASE,
    )

    # Patterns for greetings and casual chat
    _GREETING_PATTERNS = re.compile(
        r"\b(hi|hello|hey|good\s+(morning|afternoon|evening)"
        r"|how\s+are\s+you|how's\s+it\s+going|what's\s+up"
        r"|thanks|thank\s+you|bye|goodbye|see\s+you"
        r"|ok|okay|sure|yes|no|yeah|nah)\b",
        re.IGNORECASE,
    )

    # ── Image memory ──────────────────────────────────────────────────────────

    @staticmethod
    def store_image(user_id: str, image_data: str, caption: str, chat_id: str):
        """Save the most recent uploaded image for a user session."""
        RECENT_IMAGES[user_id] = {
            "data": image_data,
            "caption": caption,
            "chat_id": chat_id,
            "ts": time.time(),
        }
        print(f"[AGENT] Stored recent image for user {user_id[:12]}... (caption: '{caption[:40]}')")

    @staticmethod
    def get_recent_image(user_id: str):
        """Return the most recent image record for user, or None if not found / expired."""
        return SessionMemory.get_recent_image(user_id)

    # ── Intent classification ─────────────────────────────────────────────────

    @classmethod
    def classify_intent(cls, message: str, current_mode: str, user_id: str) -> str:
        """
        Classify the user message into an intent for intelligent routing.

        Intents:
          SKILL_INQUIRY     — user is asking what Vexara can do (free, no quota)
          VISION_RECALL     — user wants to re-process their recent image
          MATH_IN_WRONG_MODE — math problem sent in 'normal' mode without tutor/solve
          GREETING          — casual greeting or acknowledgment (lightweight response)
          EXPLANATION       — conceptual question needing explanation
          PERSONAL_QUERY    — asking about themselves / personal memory
          NORMAL            — regular message; proceed with standard /ask pipeline
        """
        if cls._SKILL_INQUIRY_PATTERNS.search(message):
            return "SKILL_INQUIRY"

        if cls._VISION_RECALL_PATTERNS.search(message):
            if cls.get_recent_image(user_id):
                return "VISION_RECALL"
            # No stored image — fall through to NORMAL so agent can tell the user

        if current_mode == "normal" and cls._MATH_PROBLEM_PATTERNS.search(message):
            # Only flag if the auto-detector also agrees it's a math problem
            if should_use_tutor(message):
                return "MATH_IN_WRONG_MODE"

        # Check for personal memory queries (before greeting, as personal queries are more specific)
        if PersonalMemoryEngine._QUERY_PATTERNS.search(message):
            return "PERSONAL_QUERY"

        # Check for simple greetings — these get a fast, warm response with no LLM cost
        stripped = message.strip().lower()
        if len(stripped) <= 20 and cls._GREETING_PATTERNS.search(stripped):
            # Only classify as greeting if it's SHORT and matches greeting pattern
            # Longer messages like "hi can you solve this" should fall through to NORMAL
            words = stripped.split()
            if len(words) <= 4:
                return "GREETING"

        return "NORMAL"

    # ── Response builders ─────────────────────────────────────────────────────

    _GREETING_RESPONSES = [
        "Hey {name}! What can I help you with today?",
        "Hi {name}! Ready to tackle some math, or do you have a question?",
        "Hello {name}! Need help with a problem or want to practice a topic?",
        "Hey there, {name}! What are we working on today?",
        "Hi {name}! I'm here to help — what's on your mind?",
    ]

    @classmethod
    def get_greeting_response(cls, user_id: str) -> str:
        """Return a warm, varied greeting response (no LLM call needed)."""
        import random
        name = get_user_name_from_session()
        if name == 'there':
            # Check personal memory for name
            hint = PersonalMemoryEngine.get_name_hint(user_id)
            if hint:
                name = hint.split('\n')[0].replace('-', '').replace('*', '').strip()[:20] or 'there'
        response = random.choice(cls._GREETING_RESPONSES).format(name=name)
        # Add a contextual nudge based on user history
        stats = get_user_chapter_stats(user_id)
        if stats and stats.get("last_chapter"):
            chapter_names = {
                "sets": "Set Theory", "arithmetic": "Arithmetic Progression",
                "algebra": "Algebra", "geometry": "Geometry",
                "trigonometry": "Trigonometry", "statistics": "Statistics",
            }
            ch_name = chapter_names.get(stats["last_chapter"], stats["last_chapter"])
            response += f"\n\nLast time you were working on **{ch_name}** — want to continue from there?"
        return response

    @classmethod
    def get_skills_description(cls) -> str:
        """Build a rich Markdown description of all skills."""
        lines = [
            "Here's everything I can do for you:\n",
        ]
        for key, skill in cls.SKILLS.items():
            lines.append(f"### {skill['name']}")
            lines.append(f"**When it activates:** {skill['trigger']}")
            lines.append(f"{skill['description']}")
            lines.append(f"**How to use:** {skill['how_to_use']}\n")
        lines.append(
            "---\n"
            "Want me to demonstrate any of these? Just say the word — or upload an image to see the vision solver in action!"
        )
        return "\n".join(lines)

    @classmethod
    def get_math_mode_notice(cls, message: str) -> str:
        """Return the mode-suggestion message for math-in-wrong-mode intent."""
        return (
            "It looks like you've sent a math problem, but you're currently in **Normal** mode.\n\n"
            "For best results I recommend one of these:\n\n"
            "- **Math mode** — full step-by-step solution with LaTeX notation (Given → To Find → Solution → Answer)\n"
            "- **Exam mode** — copy-paste-ready answer formatted exactly like an SEE exam sheet\n\n"
            "**What would you like?**\n\n"
            "1. Type **'switch to math'** (or **'switch to exam'**) and I'll change mode and solve it right now.\n"
            "2. Or just hit the **Math** or **Exam** radio button above the input box and re-send.\n\n"
            "_Your problem is saved — I haven't lost it._"
        )

    @classmethod
    def stream_vision_recall(cls, user_id: str, new_instruction: str, chat_history: list):
        """
        Generator: re-run vision solving on the user's stored recent image
        with the new textual instruction as the guiding question.
        
        Uses stored context_summary for efficient follow-ups when possible.
        Falls back to raw image re-analysis when visual detail is needed.
        """
        record = cls.get_recent_image(user_id)
        if not record:
            yield (
                "I couldn't find a recent image in this session. "
                "Images are remembered for 24 hours. "
                "Please upload the image again and I'll solve it right away!"
            )
            return

        image_data = record.get("data")
        original_caption = record.get("caption", "")
        context_summary = record.get("context_summary", original_caption)

        yield f"🔍 Re-analyzing your recent image with your new question...\n"

        solving_prompt = f"""The student previously uploaded an image with this description:
IMAGE CONTEXT: {context_summary}
Original caption: {original_caption or 'none'}

Now they are asking a follow-up question about that image:
User's new question: {new_instruction}

Provide a complete answer based on the image context and their question:
1. **What the image shows:** Brief description from the stored context
2. **Answering the specific question:** Direct, targeted answer
3. **Full Solution** (if math problem): Step-by-step with SEE exam format
4. **SEE Tip:** Relevant exam tip

Use LaTeX for all math expressions.
If the question requires visual re-analysis (e.g. "zoom into the logo", "what color is X"), 
mention that you are working from the stored context and may need the image re-uploaded for fine details."""

        solving_messages = [{"role": "user", "content": solving_prompt}]
        system_msg = (
            "You are Vexara, an expert SEE Math tutor. You are answering a follow-up question "
            "about an image the student previously uploaded. You have a text description of the image "
            "content. Use it to answer the student's question precisely.\n\n"
            "After the answer, add ONE short personalized follow-up tied to the specific "
            "topic/chapter you just solved. Never generic. Rotate phrasing each time."
        )

        try:
            response, provider_used, success = call_api_with_intelligent_fallback(
                "tutor_mode", system_msg, solving_messages, image_data=image_data
            )

            if not response or not success:
                yield "❌ Could not re-analyze the image. Please try uploading it again."
                return

            full_response = ""

            if response.headers.get("content-type", "").startswith("text/event-stream"):
                for line in response.iter_lines():
                    if line:
                        line_str = (
                            line.decode("utf-8").strip() if isinstance(line, bytes) else line.strip()
                        )
                        if line_str.startswith("data: "):
                            try:
                                data = json.loads(line_str[6:])
                                if "choices" in data and data["choices"]:
                                    delta = data["choices"][0].get("delta", {})
                                    if "content" in delta:
                                        chunk = delta["content"]
                                        full_response += chunk
                                        yield chunk
                            except json.JSONDecodeError:
                                continue
            else:
                data = response.json()
                if "candidates" in data and data["candidates"]:
                    candidate = data["candidates"][0]
                    if "content" in candidate and "parts" in candidate["content"]:
                        for part in candidate["content"]["parts"]:
                            if "text" in part:
                                full_response = part["text"]
                                yield full_response

            if not full_response:
                yield "❌ No response generated. Please try again."

        except Exception as e:
            print(f"[AGENT] Vision recall error: {e}")
            yield f"❌ Error during image re-analysis: {str(e)}"

    @classmethod
    def handle_mode_switch_and_solve(cls, message: str, current_mode: str) -> tuple:
        """
        If the user says 'switch to tutor' / 'switch to solve', detect it and
        return (True, new_mode).  Otherwise return (False, current_mode).
        """
        lower = message.lower()
        if re.search(r"\bswitch\s+to\s+(tutor|math|deepthink|solve)\b", lower):
            return True, "tutor_mode"
        if re.search(r"\bswitch\s+to\s+solve\b", lower):
            return True, "solve_mode"
        return False, current_mode


# ============================================================================
# 🔄 FIREBASE SYNC MANAGER (conflict resolution, offline caching, reconnect)
# ============================================================================

class FirebaseSyncManager:
    """
    Manages data synchronization between local files and Firebase.
    Provides conflict resolution, offline caching, and safe reconnection.
    
    Firebase structure per user:
      users/{uid}/
        ├── chats/{chat_id}/messages/{msg_id}
        ├── chat_summaries/{chat_id}/
        ├── preferences/
        ├── session_memory/
        ├── student_profile/
        ├── personal_memory/
        ├── goals/
        └── quota/{date}
    """
    
    # Local cache for pending sync operations
    _pending_sync = {}  # user_id -> [{"op": "save", "path": str, "data": dict, "ts": float}]
    _sync_lock = Lock()
    
    @classmethod
    def safe_firebase_write(cls, path: str, data: dict, merge: bool = True) -> bool:
        """
        Write to Firebase with conflict resolution.
        Uses merge=True (update) by default to avoid overwriting other fields.
        Returns True on success.
        """
        if not FIREBASE_AVAILABLE:
            return False
        try:
            ref = db.reference(path)
            if merge:
                ref.update(data)
            else:
                ref.set(data)
            return True
        except Exception as e:
            logger.error(f"[SYNC] Firebase write failed for {path}: {e}")
            # Queue for retry on next reconnect
            cls._queue_pending(path, data)
            return False
    
    @classmethod
    def safe_firebase_read(cls, path: str) -> dict | None:
        """
        Read from Firebase with error handling.
        Returns None on failure (triggers local fallback).
        """
        if not FIREBASE_AVAILABLE:
            return None
        try:
            ref = db.reference(path)
            result = ref.get()
            if hasattr(result, 'val'):
                return result.val()
            return result
        except Exception as e:
            logger.warning(f"[SYNC] Firebase read failed for {path}: {e}")
            return None
    
    @classmethod
    def _queue_pending(cls, path: str, data: dict):
        """Queue a failed write for retry on next reconnect."""
        with cls._sync_lock:
            if path not in cls._pending_sync:
                cls._pending_sync[path] = []
            cls._pending_sync[path].append({
                "data": data,
                "ts": time.time(),
            })
            # Keep only last 10 pending per path
            cls._pending_sync[path] = cls._pending_sync[path][-10:]
    
    @classmethod
    def retry_pending_syncs(cls) -> int:
        """
        Retry all pending sync operations.
        Returns number of successful retries.
        """
        if not FIREBASE_AVAILABLE:
            return 0
        
        retried = 0
        with cls._sync_lock:
            paths_to_retry = list(cls._pending_sync.keys())
        
        for path in paths_to_retry:
            with cls._sync_lock:
                pending = cls._pending_sync.get(path, [])
                if not pending:
                    continue
                # Merge all pending writes into one update
                merged_data = {}
                for item in pending:
                    merged_data.update(item["data"])
            
            try:
                ref = db.reference(path)
                ref.update(merged_data)
                with cls._sync_lock:
                    cls._pending_sync.pop(path, None)
                retried += 1
                logger.info(f"[SYNC] Retried sync for {path}")
            except Exception as e:
                logger.warning(f"[SYNC] Retry failed for {path}: {e}")
        
        return retried
    
    @classmethod
    def sync_local_to_firebase(cls, user_id: str, chat_id: str) -> bool:
        """
        Sync local chat history to Firebase (for offline-created chats).
        Uses timestamp-based conflict resolution: newer wins.
        """
        if not FIREBASE_AVAILABLE:
            return False
        
        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
            fb_path = f"users/{safe_user_id}/chats/{chat_id}/messages"
            
            # Read from Firebase
            fb_ref = db.reference(fb_path)
            fb_result = fb_ref.get()
            fb_messages = {}
            if isinstance(fb_result, dict):
                fb_messages = fb_result
            elif hasattr(fb_result, 'val'):
                val = fb_result.val()
                if isinstance(val, dict):
                    fb_messages = val
            
            # Read from local file
            local_path = get_chat_file_path(user_id, chat_id)
            local_messages = []
            if os.path.exists(local_path):
                try:
                    with open(local_path, 'r', encoding='utf-8') as f:
                        local_messages = json.load(f)
                except (json.JSONDecodeError, IOError):
                    pass
            
            if not local_messages:
                return True  # Nothing to sync
            
            # Conflict resolution: merge by timestamp, newer wins
            # Convert Firebase dict to list for comparison
            fb_list = []
            for msg_id, msg_data in fb_messages.items():
                if isinstance(msg_data, dict):
                    fb_list.append(msg_data)
            
            # Build a set of (text, timestamp) keys for deduplication
            seen = set()
            merged = []
            
            # Add Firebase messages
            for msg in fb_list:
                key = (msg.get("text", ""), msg.get("timestamp", 0))
                if key not in seen:
                    seen.add(key)
                    merged.append(msg)
            
            # Add local messages (newer wins via timestamp)
            for msg in local_messages:
                key = (msg.get("text", ""), msg.get("timestamp", 0))
                if key not in seen:
                    seen.add(key)
                    merged.append(msg)
            
            # Sort by timestamp
            merged.sort(key=lambda x: x.get("timestamp", 0))
            
            # Write merged result back to Firebase
            fb_ref.set({
                str(i): msg for i, msg in enumerate(merged)
            })
            
            # Also save merged result locally
            save_chat_history_to_file(user_id, chat_id, merged)
            
            logger.info(f"[SYNC] Synced {len(merged)} messages for {user_id}/{chat_id}")
            return True
            
        except Exception as e:
            logger.error(f"[SYNC] Local-to-Firebase sync failed: {e}")
            return False
    
    @classmethod
    def sync_all_pending(cls, user_id: str) -> dict:
        """
        Sync all pending data for a user on reconnection.
        Returns summary of what was synced.
        """
        result = {
            "pending_retries": 0,
            "chat_syncs": 0,
            "success": True,
        }
        
        # Retry pending Firebase writes
        result["pending_retries"] = cls.retry_pending_syncs()
        
        # Sync any local-only chats to Firebase
        if FIREBASE_AVAILABLE:
            try:
                safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
                chats_ref = db.reference(f"users/{safe_user_id}/chat_summaries")
                fb_summaries = chats_ref.get()
                
                if isinstance(fb_summaries, dict):
                    for chat_id in fb_summaries.keys():
                        # Check if local file exists and is newer
                        local_path = get_chat_file_path(user_id, chat_id)
                        if os.path.exists(local_path):
                            try:
                                with open(local_path, 'r', encoding='utf-8') as f:
                                    local_data = json.load(f)
                                if local_data:
                                    cls.sync_local_to_firebase(user_id, chat_id)
                                    result["chat_syncs"] += 1
                            except (json.JSONDecodeError, IOError):
                                pass
            except Exception as e:
                logger.warning(f"[SYNC] Chat sync check failed: {e}")
                result["success"] = False
        
        return result

    @classmethod
    def get_sync_status(cls) -> dict:
        """Get current sync status for monitoring."""
        with cls._sync_lock:
            pending_count = sum(len(v) for v in cls._pending_sync.values())
        return {
            "firebase_available": FIREBASE_AVAILABLE,
            "pending_writes": pending_count,
            "pending_paths": list(cls._pending_sync.keys()) if cls._pending_sync else [],
        }

# ============================================================================
# 🧠 SESSION MEMORY  (short-term, per-user, in-process)
# ============================================================================

class SessionMemory:
    """
    Short-term memory store scoped per user_id.

    Persistence strategy:
      - recent_images   → in-memory ONLY  (base64 data is too large for Firebase)
      - recent_questions, recent_tasks, recent_mode, recent_topic → Firebase-backed
        Firebase path: users/{uid}/session_memory/
        The in-process dict acts as a write-through cache; cold reads load from Firebase.
    """

    SESSION_TTL = 86400      # 24 hours for session metadata (was 1 hour)
    IMAGE_TTL   = 86400      # 24 hours for in-memory images
    MAX_Q       = 15        # questions kept in memory (was 10)
    MAX_Q_FB    = 10        # questions written to Firebase (was 5)
    MAX_TASKS   = 10

    _store: dict = {}       # user_id → memory dict (in-process cache)

    # ── Firebase helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _fb_path(user_id: str) -> str:
        safe = "".join(c for c in user_id if c.isalnum() or c in ("-", "_")).strip()
        return f"users/{safe}/session_memory"

    @classmethod
    def _load_from_firebase(cls, user_id: str) -> dict | None:
        if not FIREBASE_AVAILABLE:
            return None
        try:
            ref = db.reference(cls._fb_path(user_id))
            data = ref.get()
            if not isinstance(data, dict):
                return None
            # Firebase stores dicts with string keys for "arrays"
            def _to_list(raw):
                if isinstance(raw, list):
                    return raw
                if isinstance(raw, dict):
                    return [raw[k] for k in sorted(raw.keys(), key=lambda x: int(x) if x.isdigit() else 0)]
                return []

            return {
                "recent_images":    [],                          # never in Firebase
                "recent_questions": _to_list(data.get("recent_questions", [])),
                "recent_tasks":     _to_list(data.get("recent_tasks", [])),
                "recent_mode":      data.get("recent_mode", "normal"),
                "recent_topic":     data.get("recent_topic"),
                "conversation_summary": data.get("conversation_summary", ""),
                "last_active":      data.get("last_active", 0),
            }
        except Exception as e:
            print(f"[MEM] Firebase load error: {e}")
            return None

    @classmethod
    def _save_to_firebase(cls, user_id: str, mem: dict):
        if not FIREBASE_AVAILABLE:
            return
        try:
            # Strip image data — never write base64 to Firebase
            questions_fb = [
                {"text": q.get("text", "")[:200], "chapter": q.get("chapter"), "mode": q.get("mode"), "ts": q.get("ts", 0)}
                for q in mem.get("recent_questions", [])[:cls.MAX_Q_FB]
            ]
            tasks_fb = [
                {"desc": t.get("desc", "")[:100], "ts": t.get("ts", 0)}
                for t in mem.get("recent_tasks", [])[:cls.MAX_TASKS]
            ]
            payload = {
                "recent_questions": {str(i): q for i, q in enumerate(questions_fb)},
                "recent_tasks":     {str(i): t for i, t in enumerate(tasks_fb)},
                "recent_mode":      mem.get("recent_mode", "normal"),
                "recent_topic":     mem.get("recent_topic"),
                "conversation_summary": mem.get("conversation_summary", ""),
                "last_active":      mem.get("last_active", time.time()),
            }
            db.reference(cls._fb_path(user_id)).set(payload)
        except Exception as e:
            print(f"[MEM] Firebase save error: {e}")

    # ── Cache management ──────────────────────────────────────────────────────

    @classmethod
    def _get(cls, user_id: str) -> dict:
        now = time.time()
        mem = cls._store.get(user_id)

        # Cold read — not in local cache or cache expired → load from Firebase
        if mem is None or (now - mem.get("last_active", 0) > cls.SESSION_TTL):
            fb_mem = cls._load_from_firebase(user_id)
            mem = fb_mem if fb_mem else {
                "recent_images":    [],
                "recent_questions": [],
                "recent_tasks":     [],
                "recent_mode":      "normal",
                "recent_topic":     None,
                "conversation_summary": "",
                "last_active":      now,
            }
            cls._store[user_id] = mem

        # Expire stale list entries
        for key in ("recent_questions", "recent_tasks"):
            mem[key] = [e for e in mem[key] if now - e.get("ts", 0) < cls.SESSION_TTL]
        # Images have shorter TTL
        mem["recent_images"] = [e for e in mem.get("recent_images", []) if now - e.get("ts", 0) < cls.IMAGE_TTL]
        mem["last_active"] = now
        return mem

    # ── Public API ────────────────────────────────────────────────────────────

    @classmethod
    def store_image(cls, user_id: str, image_data: str, caption: str, chat_id: str, context_summary: str = ""):
        """
        Store image with context summary for persistent memory.
        The context_summary is a text description of the image content generated by the vision model.
        This allows follow-up questions to reference the image without re-sending raw base64.
        """
        mem = cls._get(user_id)
        entry = {
            "data": image_data,
            "caption": caption,
            "chat_id": chat_id,
            "ts": time.time(),
            "context_summary": context_summary or caption or "Math problem from uploaded image",
        }
        mem["recent_images"].insert(0, entry)
        mem["recent_images"] = mem["recent_images"][:5]
        RECENT_IMAGES[user_id] = entry   # backward compat with VexaraAgent
        logger.info(f"[MEM] Stored image for {user_id[:12]}... caption='{caption[:40]}' context_len={len(context_summary)}")

    @classmethod
    def get_recent_image(cls, user_id: str) -> dict | None:
        mem = cls._get(user_id)
        imgs = mem.get("recent_images", [])
        return imgs[0] if imgs else None

    @classmethod
    def store_question(cls, user_id: str, question: str, chapter: str | None, mode: str):
        mem = cls._get(user_id)
        mem["recent_questions"].insert(0, {
            "text": question, "chapter": chapter, "mode": mode, "ts": time.time()
        })
        mem["recent_questions"] = mem["recent_questions"][:cls.MAX_Q]
        mem["recent_mode"]  = mode
        if chapter:
            mem["recent_topic"] = chapter
        cls._save_to_firebase(user_id, mem)

    @classmethod
    def store_task(cls, user_id: str, task_description: str):
        mem = cls._get(user_id)
        mem["recent_tasks"].insert(0, {"desc": task_description, "ts": time.time()})
        mem["recent_tasks"] = mem["recent_tasks"][:cls.MAX_TASKS]
        cls._save_to_firebase(user_id, mem)

    @classmethod
    def set_mode(cls, user_id: str, mode: str):
        mem = cls._get(user_id)
        mem["recent_mode"] = mode
        cls._save_to_firebase(user_id, mem)

    @classmethod
    def get_context_summary(cls, user_id: str) -> str:
        mem = cls._get(user_id)
        parts = []
        # Include conversation summary for long-term memory context
        conv_summary = mem.get("conversation_summary", "")
        if conv_summary:
            parts.append(f"Recent activity: {conv_summary[:200]}")
        if mem.get("recent_topic"):
            parts.append(f"Last topic: {mem['recent_topic']}")
        if mem.get("recent_mode") and mem["recent_mode"] != "normal":
            parts.append(f"Last mode: {mem['recent_mode']}")
        if mem.get("recent_questions"):
            parts.append(f"Last question: {mem['recent_questions'][0]['text'][:80]}")
        if mem.get("recent_images"):
            cap = mem["recent_images"][0].get("caption", "")
            if cap:
                parts.append(f"Recent image: '{cap[:50]}'")
        return " | ".join(parts) if parts else ""

    @classmethod
    def update_conversation_summary(cls, user_id: str, topic: str = None, mode: str = None):
        """
        Update the long-term conversation summary with key context.
        This persists across sessions so the AI always knows what was discussed.
        """
        mem = cls._get(user_id)
        summary_parts = []
        existing = mem.get("conversation_summary", "")
        if existing:
            summary_parts.append(existing[:200])

        if topic:
            summary_parts.append(f"Topic: {topic}")
        if mode and mode != "normal":
            summary_parts.append(f"Mode: {mode}")

        # Keep summary concise (max 500 chars)
        new_summary = " | ".join(summary_parts)[-500:]
        mem["conversation_summary"] = new_summary
        cls._save_to_firebase(user_id, mem)

    @classmethod
    def snapshot(cls, user_id: str) -> dict:
        mem = cls._get(user_id)
        # Return a copy without raw image data (too large for API responses)
        snap = dict(mem)
        snap["recent_images"] = [
            {"caption": e.get("caption", ""), "chat_id": e.get("chat_id"), "ts": e.get("ts")}
            for e in mem.get("recent_images", [])
        ]
        return snap


# ============================================================================
# ⚙️ USER PREFERENCES ENGINE (Firebase-backed)
# ============================================================================

class UserPreferences:
    """
    Stores user preferences for personalization.
    Firebase path: users/{uid}/preferences/
    
    Preferences include:
      - language: "en" or "ne" (Nepali)
      - response_style: "detailed" or "concise"
      - explanation_mode: "step_by_step" or "concept_first"
      - preferred_name: user's preferred name
      - difficulty_level: "beginner", "intermediate", "advanced"
    """
    DEFAULTS = {
        "language": "en",
        "response_style": "detailed",
        "explanation_mode": "step_by_step",
        "preferred_name": "",
        "difficulty_level": "intermediate",
    }

    @staticmethod
    def _path(user_id: str) -> str:
        safe = "".join(c for c in user_id if c.isalnum() or c in ("-", "_")).strip()
        return f"users/{safe}/preferences"

    @classmethod
    def load(cls, user_id: str) -> dict:
        # Check TTL cache first
        cache_key = f"prefs_{user_id}"
        cached = TTLCache.get(cache_key)
        if cached is not None:
            return cached

        prefs = dict(cls.DEFAULTS)
        if not FIREBASE_AVAILABLE:
            return prefs
        try:
            ref = db.reference(cls._path(user_id))
            data = ref.get()
            if isinstance(data, dict):
                prefs.update(data)
        except Exception as e:
            logger.warning(f"[PREFS] Load error: {e}")
        # Cache for 5 minutes
        TTLCache.set(cache_key, prefs, ttl=300)
        return prefs

    @classmethod
    def save(cls, user_id: str, prefs: dict) -> bool:
        if not FIREBASE_AVAILABLE:
            return False
        try:
            ref = db.reference(cls._path(user_id))
            ref.update(prefs)
            # Invalidate cache
            TTLCache.delete(f"prefs_{user_id}")
            logger.info(f"[PREFS] Saved for {user_id[:12]}...")
            return True
        except Exception as e:
            logger.error(f"[PREFS] Save error: {e}")
            return False

    @classmethod
    def set_preference(cls, user_id: str, key: str, value: str) -> bool:
        if key not in cls.DEFAULTS:
            return False
        return cls.save(user_id, {key: value})

    @classmethod
    def get_prompt_addition(cls, user_id: str) -> str:
        """Generate a prompt addition based on user preferences."""
        prefs = cls.load(user_id)
        parts = []
        
        lang = prefs.get("language", "en")
        if lang == "ne":
            parts.append("Use Nepali-English mix (Nepanglish) for explanations when appropriate.")
        
        style = prefs.get("response_style", "detailed")
        if style == "concise":
            parts.append("Keep responses concise and to the point. Minimize explanations.")
        
        name = prefs.get("preferred_name", "")
        if name:
            parts.append(f"Call the student by their preferred name: {name}.")
        
        difficulty = prefs.get("difficulty_level", "intermediate")
        if difficulty == "beginner":
            parts.append("Explain concepts from basics. Use simple language and many examples.")
        elif difficulty == "advanced":
            parts.append("Skip basic explanations. Focus on advanced problems and techniques.")
        
        return " ".join(parts) if parts else ""

    @classmethod
    def detect_preferences_from_message(cls, user_id: str, message: str):
        """Auto-detect preferences from user messages."""
        msg_lower = message.lower()
        
        # Detect language preference
        nepali_indicators = ["nepali", "nepali ma", "k garcha", "k bhayo", "tapai", "hajur"]
        if any(ind in msg_lower for ind in nepali_indicators):
            cls.set_preference(user_id, "language", "ne")
        
        # Detect style preference
        if any(kw in msg_lower for kw in ["short", "brief", "concise", "summary", "quick"]):
            cls.set_preference(user_id, "response_style", "concise")
        elif any(kw in msg_lower for kw in ["detailed", "explain", "full", "in detail"]):
            cls.set_preference(user_id, "response_style", "detailed")

# ============================================================================
# 👤 STUDENT PROFILE ENGINE  (long-term, Firebase-backed)
# ============================================================================

class StudentProfileEngine:
    """
    Persists a rich student profile to Firebase.

    Schema (Firebase path: users/{uid}/agent_profile):
    {
      "weak_topics":    { "algebra": 5, "geometry": 2, ... },
      "strong_topics":  { "sets": 3, ... },
      "accuracy":       { "algebra": 0.6, ... },
      "study_streak":   int,
      "last_study_date": "YYYY-MM-DD",
      "goals":          ["SEE A+", "Improve Algebra"],
      "learning_history": [ { "chapter": .., "mode": .., "ts": .. }, ... ],  # last 50
    }
    """

    @staticmethod
    def _path(user_id: str) -> str:
        safe = "".join(c for c in user_id if c.isalnum() or c in ("-", "_")).strip()
        return f"users/{safe}/agent_profile"

    @classmethod
    def load(cls, user_id: str) -> dict:
        defaults = {
            "weak_topics": {},
            "strong_topics": {},
            "accuracy": {},
            "study_streak": 0,
            "last_study_date": None,
            "goals": [],
            "learning_history": [],
        }
        if not FIREBASE_AVAILABLE:
            return defaults
        try:
            ref = db.reference(cls._path(user_id))
            data = ref.get()
            if isinstance(data, dict):
                # Merge with defaults for any missing keys
                for k, v in defaults.items():
                    data.setdefault(k, v)
                return data
        except Exception as e:
            print(f"[PROFILE] Load error: {e}")
        return defaults

    @classmethod
    def save(cls, user_id: str, profile: dict):
        if not FIREBASE_AVAILABLE:
            return
        try:
            ref = db.reference(cls._path(user_id))
            ref.set(profile)
        except Exception as e:
            print(f"[PROFILE] Save error: {e}")

    @classmethod
    def record_interaction(cls, user_id: str, chapter: str | None, mode: str, correct: bool | None = None):
        """Called after every tutoring interaction to update profile."""
        if not chapter:
            return
        profile = cls.load(user_id)

        # Learning history (cap at 50)
        history_entry = {"chapter": chapter, "mode": mode, "ts": time.time()}
        history = profile.get("learning_history", [])
        history.insert(0, history_entry)
        profile["learning_history"] = history[:50]

        # Weak / strong topic counters
        if correct is False:
            wt = profile.setdefault("weak_topics", {})
            wt[chapter] = wt.get(chapter, 0) + 1
        elif correct is True:
            st = profile.setdefault("strong_topics", {})
            st[chapter] = st.get(chapter, 0) + 1

        # Study streak
        today = date.today().isoformat()
        last = profile.get("last_study_date")
        if last != today:
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            profile["study_streak"] = (profile.get("study_streak", 0) + 1) if last == yesterday else 1
            profile["last_study_date"] = today

        cls.save(user_id, profile)

    @classmethod
    def set_goal(cls, user_id: str, goal: str):
        profile = cls.load(user_id)
        goals = profile.setdefault("goals", [])
        if goal not in goals:
            goals.append(goal)
        profile["goals"] = goals[-5:]  # keep last 5
        cls.save(user_id, profile)

    @classmethod
    def get_goals(cls, user_id: str) -> list:
        return cls.load(user_id).get("goals", [])

    @classmethod
    def get_weak_topics(cls, user_id: str) -> dict:
        return cls.load(user_id).get("weak_topics", {})

    @classmethod
    def get_strong_topics(cls, user_id: str) -> dict:
        return cls.load(user_id).get("strong_topics", {})

    @classmethod
    def get_study_streak(cls, user_id: str) -> int:
        return cls.load(user_id).get("study_streak", 0)

    @classmethod
    def profile_summary(cls, user_id: str) -> str:
        """Compact string for prompt injection."""
        p = cls.load(user_id)
        parts = []
        if p.get("goals"):
            parts.append(f"Goals: {', '.join(p['goals'])}")
        if p.get("weak_topics"):
            wt = sorted(p["weak_topics"].items(), key=lambda x: -x[1])[:3]
            parts.append(f"Weak topics: {', '.join(t for t, _ in wt)}")
        if p.get("strong_topics"):
            st = sorted(p["strong_topics"].items(), key=lambda x: -x[1])[:2]
            parts.append(f"Strong topics: {', '.join(t for t, _ in st)}")
        if p.get("study_streak", 0) > 1:
            parts.append(f"Study streak: {p['study_streak']} days")
        return " | ".join(parts) if parts else ""


# ============================================================================
# 🎯 GOAL MANAGER
# ============================================================================

class GoalManager:
    """
    Thin wrapper that extracts goal-related directives from user messages
    and persists them via StudentProfileEngine.
    """

    _GOAL_PATTERNS = re.compile(
        r"\b(my goal is|i want to|i am aiming for|help me (get|achieve|reach)|"
        r"i want (an?|to get) (a\+|a plus|distinction|pass)|"
        r"improve (my )?(\w+)|focus on (\w+)|prepare for (see|exam))\b",
        re.IGNORECASE,
    )

    @classmethod
    def detect_and_store(cls, user_id: str, message: str) -> str | None:
        """
        If the message contains a goal declaration, extract and persist it.
        Returns the extracted goal string or None.
        """
        m = cls._GOAL_PATTERNS.search(message)
        if not m:
            return None
        # Use the matched span as the goal text (trimmed, capitalised)
        goal_text = message[m.start():m.end()].strip()[:80]
        goal_text = goal_text[0].upper() + goal_text[1:]
        StudentProfileEngine.set_goal(user_id, goal_text)
        print(f"[GOAL] Stored goal for {user_id[:12]}: '{goal_text}'")
        return goal_text


# ============================================================================
# 📅 STUDY PLANNER TOOL
# ============================================================================

class StudyPlannerTool:
    """
    Generates personalised study plans based on profile + goals.
    Called by the Planner when it detects a study-plan intent.
    """

    CHAPTER_WEIGHTS = {
        "trigonometry": 4,
        "arithmetic": 4,
        "geometry": 4,
        "algebra": 3,
        "statistics": 3,
        "sets": 2,
        "exam_strategy": 1,
    }

    @classmethod
    def generate_plan(cls, user_id: str, days: int = 7) -> str:
        profile = StudentProfileEngine.load(user_id)
        weak = profile.get("weak_topics", {})
        goals = profile.get("goals", [])
        streak = profile.get("study_streak", 0)

        # Sort chapters: weak first, then by SEE weight
        chapters = sorted(
            ALL_CHAPTERS,
            key=lambda c: (-(weak.get(c, 0)), -cls.CHAPTER_WEIGHTS.get(c, 1)),
        )

        lines = [
            f"## 📅 Your Personalised {days}-Day Study Plan\n",
            f"**Goals:** {', '.join(goals) if goals else 'SEE Mathematics A+'}",
            f"**Study streak:** {streak} day(s) — keep it up!\n",
            "| Day | Chapter | Focus | Priority |",
            "|-----|---------|-------|----------|",
        ]

        chapter_names = {
            "sets": "Set Theory",
            "arithmetic": "Arithmetic Progression",
            "algebra": "Algebra & Quadratic Equations",
            "geometry": "Geometry & Mensuration",
            "trigonometry": "Trigonometry",
            "statistics": "Statistics & Probability",
            "exam_strategy": "Exam Strategy",
        }

        for i in range(days):
            ch = chapters[i % len(chapters)]
            focus = "Concept review + 3 practice Qs" if weak.get(ch, 0) > 2 else "Practice problems"
            priority = "🔴 High" if weak.get(ch, 0) > 2 else ("🟡 Medium" if cls.CHAPTER_WEIGHTS.get(ch, 1) >= 3 else "🟢 Normal")
            lines.append(f"| Day {i+1} | {chapter_names.get(ch, ch)} | {focus} | {priority} |")

        lines.append("\n_This plan updates automatically as you study. Type **'update my plan'** anytime._")
        return "\n".join(lines)

    @classmethod
    def detect_study_plan_intent(cls, message: str) -> bool:
        return bool(re.search(
            r"\b(study plan|revision plan|practice schedule|what should i study|"
            r"help me plan|my schedule|exam plan|preparation plan|update (my )?plan)\b",
            message, re.IGNORECASE,
        ))


# ============================================================================
# 🔧 DYNAMIC TOOL REGISTRY
# ============================================================================

class ToolRegistry:
    """
    Central registry mapping tool names to callable handlers.
    The Planner selects from this registry; tools can be added without
    touching the orchestrator.

    Each tool entry:
      {
        "description": str,         # for planner prompt
        "requires_image": bool,
        "handler": callable,        # (user_id, message, context) → str or generator
      }
    """

    _tools: dict = {}

    @classmethod
    def register(cls, name: str, description: str, requires_image: bool = False):
        """Decorator factory for registering tools."""
        def decorator(fn):
            cls._tools[name] = {
                "description": description,
                "requires_image": requires_image,
                "handler": fn,
            }
            return fn
        return decorator

    @classmethod
    def get(cls, name: str) -> dict | None:
        return cls._tools.get(name)

    @classmethod
    def available_tools_text(cls, has_image: bool = False) -> str:
        lines = []
        for name, info in cls._tools.items():
            if info["requires_image"] and not has_image:
                continue
            lines.append(f"- {name}: {info['description']}")
        return "\n".join(lines)

    @classmethod
    def all_names(cls) -> list:
        return list(cls._tools.keys())


# Register core tools
@ToolRegistry.register("vision", "Analyze an image, extract text, solve visual problems", requires_image=True)
def _tool_vision(user_id, message, context):
    return "vision_tool"   # signal to orchestrator

@ToolRegistry.register("math", "Solve math problems step-by-step with LaTeX, SEE format")
def _tool_math(user_id, message, context):
    return "math_tool"

@ToolRegistry.register("exam", "Generate exam-ready copy-paste answers for SEE")
def _tool_exam(user_id, message, context):
    return "exam_tool"

@ToolRegistry.register("curriculum", "Retrieve relevant SEE curriculum knowledge chunks")
def _tool_curriculum(user_id, message, context):
    subject, chapter, ctx, conf, _ = KNOWLEDGE_BASE.retrieve(message)
    return ctx if ctx else ""

@ToolRegistry.register("memory", "Recall recent images, questions, or tasks from this session")
def _tool_memory(user_id, message, context):
    return SessionMemory.get_context_summary(user_id)

@ToolRegistry.register("student_profile", "Read student's weak topics, goals, and learning history")
def _tool_student_profile(user_id, message, context):
    return StudentProfileEngine.profile_summary(user_id)

@ToolRegistry.register("study_planner", "Generate a personalised study plan based on student profile")
def _tool_study_planner(user_id, message, context):
    return StudyPlannerTool.generate_plan(user_id)

@ToolRegistry.register("goal_manager", "Detect and store student goals from their message")
def _tool_goal_manager(user_id, message, context):
    goal = GoalManager.detect_and_store(user_id, message)
    return f"Goal stored: {goal}" if goal else ""

@ToolRegistry.register(
    "personal_memory",
    "Retrieve the student's saved personal notes: name, class, upcoming tests, marks, family, goals. "
    "Use when the student asks something personal, references their name, or recalls past events.",
)
def _tool_personal_memory(user_id, message, context):
    notes = PersonalMemoryEngine.load(user_id).get("notes", "")
    return notes if notes.strip() else ""


# ============================================================================
# 🗺️  PLANNER
# ============================================================================

class Planner:
    """
    Given the user message + context, decides which tools to run and in what order.
    Uses a compact LLM call to produce a JSON plan, then validates and executes it.
    Falls back to a heuristic plan if the LLM call fails.
    """

    PLAN_SYSTEM = """You are the planning module for Vexara, an AI math tutor.
Given a student message and context, output a JSON plan.

Available tools:
{tools}

Output ONLY valid JSON, no markdown fences:
{{
  "intent": "one of: math_solve | vision_analyze | study_plan | skill_inquiry | general_qa | goal_set | personal_query",
  "tools": ["tool1", "tool2"],
  "mode": "one of: normal | tutor_mode | solve_mode",
  "reasoning": "one sentence"
}}

Rules:
- If the message asks about the student personally (their name, class, test dates, marks, family, "what do you know about me", "remember when", "my info", "check my memory") → intent MUST be 'personal_query' and tools MUST include 'personal_memory' and NOT include 'student_profile' or 'curriculum'
- If the message contains a math problem or 'solve/calculate/find/prove' → tools must include 'math' or 'exam'
- If the message references an image, diagram, or upload → tools must include 'vision'
- If the message asks for a study plan or schedule → tools must include 'study_planner'
- If the message mentions a goal or 'i want to improve' → tools must include 'goal_manager'
- For math/study questions include 'curriculum' and 'student_profile' for context
- For personal queries use ONLY 'personal_memory' — never mix with student_profile
- Keep plan to 2-3 tools maximum"""

    @classmethod
    def _llm_plan(cls, message: str, context: str, has_image: bool) -> dict | None:
        tools_text = ToolRegistry.available_tools_text(has_image=has_image)
        system = cls.PLAN_SYSTEM.format(tools=tools_text)
        plan_messages = [{"role": "user", "content": f"Student message: {message}\nContext: {context}"}]
        try:
            response, _, success = call_api_with_intelligent_fallback("normal", system, plan_messages)
            if not response or not success:
                return None
            raw = ""
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                for line in response.iter_lines():
                    if line:
                        s = line.decode("utf-8").strip() if isinstance(line, bytes) else line.strip()
                        if s.startswith("data: "):
                            try:
                                d = json.loads(s[6:])
                                if "choices" in d and d["choices"]:
                                    raw += d["choices"][0].get("delta", {}).get("content", "")
                            except json.JSONDecodeError:
                                continue
            else:
                d = response.json()
                if "candidates" in d and d["candidates"]:
                    for part in d["candidates"][0].get("content", {}).get("parts", []):
                        raw += part.get("text", "")
            raw = raw.strip().strip("```json").strip("```").strip()
            plan = json.loads(raw)
            if isinstance(plan, dict) and "tools" in plan:
                return plan
        except Exception as e:
            print(f"[PLANNER] LLM plan failed: {e}")
        return None

    @classmethod
    def _heuristic_plan(cls, message: str, mode: str, has_image: bool) -> dict:
        """Fallback plan built from regex rules — zero LLM cost."""
        msg_lower = message.lower()

        if has_image or VexaraAgent._VISION_RECALL_PATTERNS.search(message):
            return {"intent": "vision_analyze", "tools": ["student_profile", "vision"],
                    "mode": "tutor_mode", "reasoning": "image detected"}

        if StudyPlannerTool.detect_study_plan_intent(message):
            return {"intent": "study_plan", "tools": ["student_profile", "study_planner"],
                    "mode": mode, "reasoning": "study plan request"}

        if VexaraAgent._SKILL_INQUIRY_PATTERNS.search(message):
            return {"intent": "skill_inquiry", "tools": [], "mode": mode, "reasoning": "skill inquiry"}

        if GoalManager._GOAL_PATTERNS.search(message):
            return {"intent": "goal_set", "tools": ["goal_manager", "student_profile"],
                    "mode": mode, "reasoning": "goal detected"}

        # Personal query — use ONLY personal_memory, never mix with student_profile
        if PersonalMemoryEngine._QUERY_PATTERNS.search(message):
            return {"intent": "personal_query", "tools": ["personal_memory"],
                    "mode": "normal", "reasoning": "personal memory query"}

        if should_use_tutor(message) or mode in ("tutor_mode", "solve_mode"):
            tools = ["curriculum", "math" if mode != "solve_mode" else "exam", "student_profile"]
            return {"intent": "math_solve", "tools": tools, "mode": mode, "reasoning": "math problem"}

        return {"intent": "general_qa", "tools": ["curriculum", "student_profile"],
                "mode": mode, "reasoning": "general question"}

    @classmethod
    def create_plan(cls, user_id: str, message: str, mode: str, has_image: bool = False) -> dict:
        ctx = SessionMemory.get_context_summary(user_id)
        # Only call LLM planner for complex multi-tool situations to save quota
        use_llm_planner = (
            has_image
            or len(message.split()) > 12
            or any(kw in message.lower() for kw in ["analyze", "paper", "weak", "plan", "schedule", "goal"])
        )
        plan = None
        if use_llm_planner:
            plan = cls._llm_plan(message, ctx, has_image)
        if not plan:
            plan = cls._heuristic_plan(message, mode, has_image)

        # Persist the plan in session memory for follow-up awareness
        SessionMemory.store_task(user_id, plan.get("reasoning", plan.get("intent", "")))
        print(f"[PLANNER] Plan: intent={plan.get('intent')} tools={plan.get('tools')} mode={plan.get('mode')}")
        return plan


# ============================================================================
# 🔁 REFLECTION LAYER
# ============================================================================

class ReflectionLayer:
    """
    Runs a lightweight verification pass on a draft answer before it is
    returned to the student.  Only invoked for math_solve and vision_analyze
    intents where accuracy is critical.
    """

    REFLECTION_SYSTEM = """You are a verification expert for an SEE Mathematics tutor.

Review this draft answer for a student.

Check:
1. Are all calculations correct?
2. Is the LaTeX notation valid (all math in $...$)?
3. Is the step-by-step logic complete and not missing steps?
4. Is the SEE exam format followed (Given/To Find/Solution/Answer)?
5. Is the final answer clearly boxed?

If the answer is correct and well-formatted, respond with:
APPROVED

If there are issues, respond with:
NEEDS_FIX: [describe the specific issue in one sentence]
CORRECTED_ANSWER:
[provide the improved answer]

Be strict — students depend on this for their exams."""

    @classmethod
    def reflect(cls, draft: str, question: str, mode: str) -> str:
        """
        Returns either the original draft (if approved) or a corrected version.
        Skips reflection for normal/conversational mode to save quota.
        """
        if mode not in ("tutor_mode", "solve_mode"):
            return draft
        if len(draft) < 100:
            return draft

        review_messages = [{
            "role": "user",
            "content": f"Student question:\n{question}\n\nDraft answer:\n{draft}"
        }]
        try:
            response, _, success = call_api_with_intelligent_fallback(
                "normal", cls.REFLECTION_SYSTEM, review_messages
            )
            if not response or not success:
                return draft

            review_text = ""
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                for line in response.iter_lines():
                    if line:
                        s = line.decode("utf-8").strip() if isinstance(line, bytes) else line.strip()
                        if s.startswith("data: "):
                            try:
                                d = json.loads(s[6:])
                                if "choices" in d and d["choices"]:
                                    review_text += d["choices"][0].get("delta", {}).get("content", "")
                            except json.JSONDecodeError:
                                continue
            else:
                d = response.json()
                if "candidates" in d and d["candidates"]:
                    for part in d["candidates"][0].get("content", {}).get("parts", []):
                        review_text += part.get("text", "")

            review_text = review_text.strip()
            print(f"[REFLECT] Result: {review_text[:80]}")

            if review_text.startswith("APPROVED"):
                return draft
            if "CORRECTED_ANSWER:" in review_text:
                corrected = review_text.split("CORRECTED_ANSWER:", 1)[1].strip()
                if len(corrected) > 50:
                    return corrected

        except Exception as e:
            print(f"[REFLECT] Error: {e}")

        return draft


# ============================================================================
# 🎼 AGENT ORCHESTRATOR  (central brain)
# ============================================================================

# ============================================================================
# 🧩 PERSONAL MEMORY ENGINE  (non-curriculum, user-specific context)
# ============================================================================

class PersonalMemoryEngine:
    """
    Stores personal (non-math) facts about the student in Firebase so Vexara
    feels truly personalised across sessions.

    Examples of stored facts:
      • Name: Shivam
      • Class test on 20 Jan — Math + Science
      • Got 78/100 in last term exam
      • Wants to score A+ in SEE
      • Best friend is Aarav

    Firebase path: users/{uid}/personal_memory/
      { "notes": "bullet-point text", "last_updated": float, "last_extracted": float }

    Design principles:
      - Notes stored as a plain editable text blob (user can paste ChatGPT memory etc.)
      - No auto-extraction on every message (zero extra LLM cost per question)
      - User-triggered "Extract from chats" does one batch LLM call
      - Retrieval is ONLY injected when the question looks like a personal query
        → zero overhead for normal math questions
    """

    MAX_LEN = 3000   # chars — keeps prompt injection lightweight

    # ── Pattern: question is asking for personal context ─────────────────────
    # Cast wide — false positives are cheap (just a Firebase read), false
    # negatives make the AI seem forgetful.
    _QUERY_PATTERNS = re.compile(
        r"\b("
        # Direct memory queries
        r"my name|what('s| is) my|who am i|tell me (about |what you know )?about me"
        r"|what do you (know|have|remember) (about|on) me|my (info|details|profile|context|notes?)"
        r"|check (the |my )?(memory|notes?|info|details|personal|context)"
        r"|do you (know|remember) (me|my|who i am)"
        # Personal history / recall
        r"|remember (when|what|that)|what did i (tell|say|mention|share|ask)"
        r"|i told you|you should know|i mentioned"
        # Personal life / school facts
        r"|my (test|exam|marks?|score|result|grade|class|school|teacher|subject)"
        r"|my (friend|family|mom|dad|brother|sister|cousin|uncle|aunt)"
        r"|when is (my|the|our)|(my|our) (schedule|plan|timetable|routine)"
        r"|upcoming (test|exam|event)|next (test|exam|week|month)"
        # Goals & identity
        r"|my goal|my target|my aim|what (should|do) i (study|focus|do)"
        r")\b",
        re.IGNORECASE,
    )

    # ── Pattern: does this message CONTAIN personal info worth saving? ────────
    _CONTAINS_PERSONAL = re.compile(
        r"\b(my name is|i am|i'm called|call me|my (class|grade|school|teacher|friend|family"
        r"|mom|dad|brother|sister|cousin)|my (test|exam|marks?|score|result)"
        r"|i (got|scored|passed|failed|got) \d|class test|term test|our (test|exam)"
        r"|my goal is|i want to (be|become|achieve|get)|remember (that|this)|note that"
        r"|don'?t forget|upcoming|next (week|month|monday|tuesday|wednesday|thursday|friday))\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _path(user_id: str) -> str:
        safe = "".join(c for c in user_id if c.isalnum() or c in ("-", "_")).strip()
        return f"users/{safe}/personal_memory"

    @classmethod
    def load(cls, user_id: str) -> dict:
        defaults = {"notes": "", "last_updated": 0.0, "last_extracted": 0.0}
        if not FIREBASE_AVAILABLE:
            return defaults
        try:
            data = db.reference(cls._path(user_id)).get()
            if isinstance(data, dict):
                defaults.update(data)
        except Exception as e:
            print(f"[PMEM] Load error: {e}")
        return defaults

    @classmethod
    def save(cls, user_id: str, notes: str) -> bool:
        notes = notes.strip()[:cls.MAX_LEN]
        if not FIREBASE_AVAILABLE:
            return False
        try:
            db.reference(cls._path(user_id)).update({
                "notes": notes, "last_updated": time.time()
            })
            print(f"[PMEM] Saved {len(notes)} chars for {user_id[:12]}...")
            return True
        except Exception as e:
            print(f"[PMEM] Save error: {e}")
            return False

    @classmethod
    def get_name_hint(cls, user_id: str) -> str:
        """
        Returns the first 1–2 bullet lines of the notes (typically the name).
        Used for always-on injection so the AI knows who it is talking to
        without dumping the full memory into every math question.
        Max 120 chars — cheap, barely any tokens.
        """
        notes = cls.load(user_id).get("notes", "").strip()
        if not notes:
            return ""
        lines = [l.strip() for l in notes.splitlines() if l.strip()][:2]
        return "\n".join(lines)[:120]

    @classmethod
    def get_relevant_context(cls, user_id: str, question: str) -> str:
        """
        Returns full notes when the question is about personal context.
        Returns "" for math / curriculum questions → zero overhead.
        """
        if not cls._QUERY_PATTERNS.search(question):
            return ""
        record = cls.load(user_id)
        return record.get("notes", "")

    @classmethod
    def extract_from_all_chats(cls, user_id: str) -> str:
        """
        User-triggered: reads all Firebase chat messages, sends them in one
        LLM call, gets back bullet-point personal facts, saves and returns them.
        """
        if not FIREBASE_AVAILABLE:
            return cls.load(user_id).get("notes", "")

        try:
            safe = "".join(c for c in user_id if c.isalnum() or c in ("-", "_")).strip()
            chats_data = db.reference(f"users/{safe}/chats").get()

            user_msgs = []
            if chats_data and isinstance(chats_data, dict):
                for chat_id, chat_data in chats_data.items():
                    if not isinstance(chat_data, dict):
                        continue
                    msgs = chat_data.get("messages", {})
                    if isinstance(msgs, dict):
                        for msg in msgs.values():
                            if isinstance(msg, dict) and msg.get("type") == "user":
                                txt = (msg.get("text") or "").strip()
                                if txt and cls._CONTAINS_PERSONAL.search(txt):
                                    user_msgs.append({"text": txt[:200], "ts": msg.get("timestamp", 0)})

            user_msgs.sort(key=lambda x: x.get("ts", 0))
            sample = user_msgs[-60:]   # last 60 personal-looking messages

            existing = cls.load(user_id).get("notes", "")

            if not sample and not existing:
                return ""

            msgs_text = "\n".join(f"- {m['text']}" for m in sample) if sample else "(no personal messages found)"

            prompt = (
                "You maintain a personal memory notebook for a student using an AI math tutor.\n\n"
                f"Student's messages that may contain personal info:\n{msgs_text}\n\n"
                f"Existing memory:\n{existing if existing else '(empty)'}\n\n"
                "Instructions:\n"
                "- Extract ONLY personal facts: name, class, school, teacher names, test dates/results, "
                "family members, goals, upcoming events, marks, class schedule, personal achievements.\n"
                "- Do NOT include math problems, homework questions, or curriculum content.\n"
                "- Merge with existing memory. Remove duplicates. Update outdated facts.\n"
                "- Output as concise bullet points (max 20). Each bullet ≤ 15 words.\n"
                "- If nothing personal is found, reproduce the existing memory unchanged.\n"
                "- If there is truly nothing, output: (no personal information found)"
            )
            extraction_msgs = [{"role": "user", "content": prompt}]
            system = "Personal memory extractor for AI tutor. Output bullet points only. Be concise."

            resp, _, success = call_api_with_intelligent_fallback("normal", system, extraction_msgs)
            if not resp or not success:
                return existing

            extracted = ""
            if resp.headers.get("content-type", "").startswith("text/event-stream"):
                for line in resp.iter_lines():
                    if line:
                        s = line.decode("utf-8").strip() if isinstance(line, bytes) else line.strip()
                        if s.startswith("data: "):
                            try:
                                d = json.loads(s[6:])
                                if "choices" in d and d["choices"]:
                                    extracted += d["choices"][0].get("delta", {}).get("content", "")
                            except Exception:
                                continue
            else:
                d = resp.json()
                if "candidates" in d and d["candidates"]:
                    for part in d["candidates"][0].get("content", {}).get("parts", []):
                        extracted += part.get("text", "")

            extracted = extracted.strip()[:cls.MAX_LEN]
            if extracted and "(no personal information found)" not in extracted.lower():
                db.reference(cls._path(user_id)).set({
                    "notes": extracted,
                    "last_updated": time.time(),
                    "last_extracted": time.time(),
                })
                print(f"[PMEM] Extracted {len(extracted)} chars for {user_id[:12]}...")
                return extracted
            return existing

        except Exception as e:
            print(f"[PMEM] Extraction error: {e}")
            return cls.load(user_id).get("notes", "")


class AgentOrchestrator:
    """
    Central brain of Vexara.

    Flow per request:
      1. Read session memory + student profile
      2. Detect goal directives and persist them
      3. Run Planner → get execution plan
      4. Execute tools in plan order, collecting context
      5. Build enriched system prompt
      6. Call LLM with enriched prompt
      7. Run ReflectionLayer on draft (math/exam only)
      8. Persist interaction to StudentProfileEngine + SessionMemory
      9. Return final response
    """

    @classmethod
    def prepare_call(cls, user_id: str, message: str, mode: str,
                     chat_history: list, has_image: bool = False) -> dict:
        """
        Returns a dict with everything the /ask endpoint needs:
          {
            "system_prompt": str,
            "mode": str,           # possibly upgraded by planner
            "subject": str | None,
            "chapter": str | None,
            "plan": dict,
            "tool_context": str,
          }
        """
        # 1. Detect and store any goal
        GoalManager.detect_and_store(user_id, message)

        # 2. Build plan
        plan = Planner.create_plan(user_id, message, mode, has_image=has_image)
        resolved_mode = plan.get("mode", mode)
        if resolved_mode not in ("normal", "tutor_mode", "solve_mode"):
            resolved_mode = mode

        # 3. Execute tools, collect context strings
        tool_outputs = []
        subject = None
        chapter = None

        for tool_name in plan.get("tools", []):
            tool = ToolRegistry.get(tool_name)
            if not tool:
                continue
            if tool["requires_image"] and not has_image:
                continue
            try:
                result = tool["handler"](user_id, message, {})
                if result and isinstance(result, str) and result not in ("vision_tool", "math_tool", "exam_tool"):
                    tool_outputs.append(f"[{tool_name.upper()}]\n{result}")
            except Exception as e:
                print(f"[ORCH] Tool {tool_name} error: {e}")

        # 4. Build base prompt via existing pipeline (also runs RAG internally)
        base_prompt, subject, chapter = build_enhanced_prompt(message, chat_history, resolved_mode)

        # ── Three-tier context injection ──────────────────────────────────────
        #
        #  TIER 1 (always, if memory exists): inject just the name/identity hint
        #          so the AI always knows who it is talking to — zero confusion,
        #          costs ~15 tokens.
        #
        #  TIER 2 (personal query detected): inject full personal memory AND
        #          skip the auto-tracked profile summary to stop source blending.
        #          Add an explicit instruction telling the AI to use personal
        #          memory as the ground truth and not mix in tracking metrics.
        #
        #  TIER 3 (normal / math query, no personal memory override): inject
        #          learning profile (goals, streak) + session context (last topic,
        #          mode). This is the original always-on block.
        # ─────────────────────────────────────────────────────────────────────

        # ── Determine if personal memory tool ran and returned data ─────────────
        personal_tool_result = next(
            (o for o in tool_outputs if o.startswith("[PERSONAL_MEMORY]")), None
        )
        personal_notes = personal_tool_result.split("\n", 1)[1].strip() if personal_tool_result else ""

        # Fall back to pattern-match retrieval if tool didn't run (e.g. heuristic didn't fire it)
        if not personal_notes:
            personal_notes = PersonalMemoryEngine.get_relevant_context(user_id, message)

        name_hint = PersonalMemoryEngine.get_name_hint(user_id)

        # ── TIER 1 — always inject name hint so AI knows who it's talking to ──
        if name_hint:
            base_prompt += f"\n\n[Student: {name_hint}]\n"

        # ── USER PREFERENCES — adapt to personalization settings ─────────────
        prefs_addition = UserPreferences.get_prompt_addition(user_id)
        if prefs_addition:
            base_prompt += f"\n\n[User Preferences: {prefs_addition}]\n"

        # ── AUTO-DETECT preferences from message ────────────────────────────
        UserPreferences.detect_preferences_from_message(user_id, message)

        if personal_notes:
            # ── TIER 2 — personal query: full notes, NO profile/session mixing ──
            base_prompt += (
                "\n\n**=== PERSONAL MEMORY (student's own saved notes — ground truth) ===**\n"
                + personal_notes
                + "\n**=================================================================**\n"
                "\n> Use the Personal Memory above as the ONLY source of facts about this"
                " student. Do not mention study streak, last topic, or auto-tracked metrics"
                " in this reply unless the student explicitly asked about learning progress.\n"
            )
            # Filter tool outputs: keep only personal_memory result, drop student_profile + curriculum
            display_outputs = [
                o for o in tool_outputs
                if not o.startswith("[STUDENT_PROFILE]")
                and not o.startswith("[CURRICULUM]")
                and not o.startswith("[PERSONAL_MEMORY]")  # already injected above
            ]
        else:
            # ── TIER 3 — normal/math query: profile + session context ─────────
            profile_summary = StudentProfileEngine.profile_summary(user_id)
            mem_summary     = SessionMemory.get_context_summary(user_id)
            extra_ctx_parts = []
            if profile_summary:
                extra_ctx_parts.append(f"LEARNING PROFILE: {profile_summary}")
            if mem_summary:
                extra_ctx_parts.append(f"SESSION CONTEXT: {mem_summary}")
            if extra_ctx_parts:
                base_prompt += (
                    "\n\n**=== STUDENT CONTEXT ===**\n"
                    + "\n".join(extra_ctx_parts)
                    + "\n**=======================**\n"
                )
            # Drop personal_memory from tool outputs in normal tier (it's either empty or not relevant)
            display_outputs = [
                o for o in tool_outputs
                if not o.startswith("[CURRICULUM]")
                and not o.startswith("[PERSONAL_MEMORY]")
            ]

        # Append remaining tool outputs (vision results, goal confirmations, etc.)
        if display_outputs:
            base_prompt += (
                "\n\n**=== AGENT TOOL RESULTS ===**\n"
                + "\n\n".join(display_outputs)
                + "\n**===========================**\n"
            )

        # Store the question in session memory
        SessionMemory.store_question(user_id, message, chapter, resolved_mode)

        return {
            "system_prompt": base_prompt,
            "mode": resolved_mode,
            "subject": subject,
            "chapter": chapter,
            "plan": plan,
            "tool_context": "\n".join(tool_outputs),
        }

    @classmethod
    def post_process(cls, user_id: str, draft: str, question: str,
                     mode: str, chapter: str | None) -> str:
        """
        Run reflection, normalize math, update student profile.
        Returns the final polished response.
        """
        # Reflection pass (only for math/exam)
        final = ReflectionLayer.reflect(draft, question, mode)

        # Normalize LaTeX
        final = normalize_math_response(final)

        # Update student profile (treat every interaction as a learning event)
        StudentProfileEngine.record_interaction(user_id, chapter, mode)

        return final

    @classmethod
    def handle_study_plan(cls, user_id: str) -> str:
        """Directly return a study plan without consuming an LLM call."""
        return StudyPlannerTool.generate_plan(user_id)

    @classmethod
    def handle_skill_inquiry(cls) -> str:
        return VexaraAgent.get_skills_description()


# ============================================================================
# 🔐 OAUTH CONFIGURATION
# ==============================    ==============================================

_google_client_id_check = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
print(f"[STARTUP CHECK] GOOGLE_OAUTH_CLIENT_ID is {'SET (length ' + str(len(_google_client_id_check)) + ')' if _google_client_id_check else 'MISSING/EMPTY'}")

google_bp = make_google_blueprint(
    client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    redirect_url="/google_login/authorized",
    scope=["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"]
)
app.register_blueprint(google_bp, url_prefix="/google_login")

oauth = OAuth(app)
microsoft = oauth.register(
    name='microsoft',
    client_id=os.environ.get("MICROSOFT_CLIENT_ID", ""),
    client_secret=os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
    access_token_url='https://login.microsoftonline.com/common/oauth2/v2.0/token',
    authorize_url='https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
    api_base_url='https://graph.microsoft.com/v1.0/',
    client_kwargs={'scope': 'User.Read'}
)

# ============================================================================
# 📝 PROMPT TEMPLATES
# ============================================================================

BASE_SYSTEM_PROMPT = """You are Vexara, a warm, encouraging SEE Mathematics tutor for Class 10 students in Nepal — not a generic chatbot. You are personally invested in this student's exam result, the way their favorite tutor would be.

**YOUR TEACHING APPROACH:**
You are a highly skilled teacher who explains concepts efficiently without unnecessary elaboration.
- Explain concepts clearly and directly
- Use examples from the NEB SEE curriculum
- Build understanding progressively from basic to advanced
- Guide students to solve problems independently
- Point out common mistakes and how to avoid them
- Use proper mathematical notation and terminology
- Keep explanations concise but complete

**MAKE IT FEEL PERSONAL:**
- If a Subject/Topic/Chapter is provided in the retrieved knowledge below, treat it as what THIS student is studying right now — refer to it by name ("Since you're working on Trigonometric Ratios...") instead of giving a generic textbook answer.
- Talk like a tutor who is tracking this student's progress, not a search engine reciting facts. Use "you," acknowledge their effort, and stay warm — never robotic or overly formal.
- Don't open every message with the same filler ("Sure!", "Great question!"). Vary it, or just start teaching directly like a real tutor would.

**FOR DIFFERENT QUESTION TYPES:**

Conceptual Questions:
- Define the concept using textbook language
- Give 1-2 relevant examples
- Show how it applies in problems
- Point to SEE exam focus areas

Procedural Questions (How to solve):
- State the method/formula first
- Show step-by-step solution with brief reasoning
- Highlight where students commonly make errors
- Provide one additional example if helpful

Problem-Solving Questions:
- Use the SEE format: Given → To Find → Solution → Answer
- Show all working without skipping steps
- Explain reasoning briefly (no teaching tangents)
- State the formula and apply it directly

**MATHEMATICAL NOTATION:**
- Use LaTeX for ALL math — no exceptions: $expression$ for inline, $$expression$$ for display
- Every equation, variable, number-with-unit, or symbol in a math context MUST be in $...$
- Write fractions as $\frac{a}{b}$, never as a/b in equations
- Use $\Rightarrow$ for logical flow, $\therefore$ for therefore, $\times$ for multiply
- Square roots: $\sqrt{value}$, powers: $x^2$, subscripts: $x_1$
- Always include units inside the LaTeX where possible: $12 \text{ cm}^2$
- NEVER use backticks (`...`) for math — backticks are only for programming code
- WRONG: `dy/dx = e^(2x)`   RIGHT: $\frac{dy}{dx} = e^{2x}$

**IMPORTANT:**
Use retrieved knowledge (formulas, examples, patterns) as your authoritative source.
Do NOT invent formulas or make up information.
Keep answers focused and exam-relevant.

**KEEP THE STUDENT COMING BACK (MANDATORY — every single response):**
End every answer with ONE short, specific follow-up tied to what you just taught — never a generic "let me know if you have other questions" or "feel free to ask anything else." Offer something concrete instead, such as:
- 2-3 more practice questions on this exact topic/chapter
- A slightly harder SEE-style variation of the same problem
- A quick one-question check to see if the concept stuck
- Moving on to the next sub-topic in the same chapter
Rotate your phrasing naturally — don't reuse the same line every time, and don't make it sound like a chatbot fishing for engagement. Example tone: "Want to try 3 quick practice problems on Arithmetic Progressions before we move on?" or "This exact pattern shows up a lot in SEE papers — want a trickier version to test yourself?"

**SEE Focus:** Help students master Class 10 Mathematics and score well in their SEE exam.

**VISUAL FORMATTING — AUTO-APPLY without being asked:**

Use MARKDOWN TABLES whenever you have structured data:
- Trigonometric values for standard angles (0°, 30°, 45°, 60°, 90°) → ALWAYS a table
- Frequency distribution or statistics data → ALWAYS a table
- Comparing AP vs GP, or two formulas side by side → table
- Chapter coverage / marks breakdown → table
Format: header row | separator (---|---) | data rows. Never skip the separator.

Use CALLOUT BOXES (blockquote with emoji prefix) for emphasis — place them naturally:
- `> 💡 **Tip:** [concise tip]` — for tricks and shortcuts
- `> ⚠️ **Watch Out:** [mistake]` — for common student errors
- `> 📌 **Formula:** [formula]` — for key formulas to remember
- `> ✅ **Important:** [rule]` — for theorems, rules, must-knows
- **SEE Tip** — keep as normal inline text at the bottom of the solution (do NOT wrap in a blockquote)

Use ASCII ART FLOWCHARTS for decision trees and processes - NOT mermaid.

NEVER use ```mermaid``` code blocks - they cause syntax errors.
Instead, use box-drawing characters for flowcharts:

Example flowchart for solving quadratics:

                        ┌─────────────────────┐
                        │  Solve Quadratic    │
                        │  ax² + bx + c = 0   │
                        └──────────┬──────────┘
                                   │
                        ┌──────────▼──────────┐
                        │ Can we factor it?   │
                        └──────┬──────────┬───┘
              ┌───────────────┘          └──────────────┐
             YES                                       NO
              │                                         │
         ┌────▼──────┐                         ┌────────▼────┐
         │ Factor    │                         │ Quadratic   │
         │ equation  │                         │ Formula     │
         └────┬──────┘                         └────┬───────┘
              │                                     │
         ┌────▼────────┐                     ┌──────▼───────┐
         │ Set each    │                     │ Calculate    │
         │ factor = 0  │                     │ discriminant │
         └────┬────────┘                     └──────┬───────┘
              │                                     │
              │                          ┌──────────▼──────────┐
              │                          │ Check discriminant  │
              │                          └─┬──────────┬─────┬──┘
              │                    Pos   │          │ 0 │  │ Neg
              │                          │          │   │  │
              │                     ┌────▼────┐ ┌──▼─┐│┌─▼──────┐
              │                     │Two roots│ │One ││Complex │
              │                     │real     │ │root││roots   │
              │                     └────┬────┘ └──┬─┘└────┬───┘
              │                         │          │       │
              └─────────────────┬───────┴──────────┴───────┘
                               │
                        ┌──────▼──────────┐
                        │ Verify solutions│
                        └─────────────────┘

Key concepts to show:
- Decision points: Can we factor? Is it standard form?
- Method choices: Factoring vs Quadratic Formula
- Discriminant implications: Type of roots (real/complex)
- Step-by-step: What happens at each decision

For method comparisons, use MARKDOWN TABLES:
| Method | When to Use | How | Advantage |
|--------|------------|-----|-----------|
| Factoring | Perfect square trinomials | Find two numbers | Fast, intuitive |
| Formula | Always works | x = (-b ± √(b²-4ac))/2a | Reliable |

For processes, use STEP-BY-STEP format:
**Step 1:** [Action with reason]
**Step 2:** [Next action]
**Step 3:** [Continue process]

AUTO-TRIGGER flowcharts for:
- Decision-based problems: "How to solve..." questions
- Method comparisons: "Which method to use?"
- Multi-step processes: Show the flow and decision points
- Classification: Show criteria and outcomes for each path
"""

TUTOR_MODE_BASE_PROMPT = """You are an expert SEE Mathematics tutor who excels at problem solving AND teaching.

**YOUR ROLE:** Solve problems step-by-step AND explain the concepts so the student learns.

**MANDATORY FORMAT FOR ALL PROBLEMS:**

**Given:**
[List all given information clearly - data, conditions, constraints]

**To Find:**
[State exactly what needs to be calculated or proven]

**Solution:**
[Show EVERY step with brief explanations of WHY each step is taken]
Step 1: [First action] — [Brief reason why]
Step 2: [Second action] — [Brief reason why]
... [Continue with all steps]
Step N: [Final calculation]

**Answer:** [Write final answer clearly with units]

**Key Concept:** [One sentence explaining the main concept/formula used]

---

**SOLVING PRINCIPLES:**

1. IDENTIFY CORRECTLY:
   - Read the problem carefully
   - Highlight given vs what's asked
   - Identify the concept/formula needed
   - Check for special cases or conditions

2. FORMULA/THEOREM APPLICATION:
   - State the formula by name: "Using Pythagoras Theorem: c² = a² + b²"
   - Always show the formula before substituting
   - Substitute given values clearly
   - Never skip the formula statement

3. ALGEBRAIC MANIPULATION:
   - Show intermediate steps between major operations
   - Use LaTeX arrows: $\Rightarrow$ to show logical flow
   - Example: $2x + 5 = 13 \Rightarrow 2x = 8 \Rightarrow x = 4$
   - Verify if possible (especially for quadratics)

4. GEOMETRY PROBLEMS:
   - Draw mental picture (describe if needed)
   - Use proper notation: Triangle ABC, ∠BAC, line BC
   - State all theorems used
   - Show construction if relevant

5. TRIGONOMETRY:
   - State the ratio: sin θ = opposite/hypotenuse
   - Use standard angle values (0°, 30°, 45°, 60°, 90°)
   - Include degree/radian notation
   - Round to required decimal places (typically 2-4)

6. CALCULUS (Limits, Derivatives, Integration):
   - State the definition or rule being applied
   - For limits: Use algebraic methods (factorization, rationalization, L'Hôpital)
   - For derivatives: Identify rule (product, quotient, chain) and apply
   - For integration: Identify method (substitution, parts, partial fractions)

7. MATRIX/VECTOR OPERATIONS:
   - Show intermediate matrix steps
   - Label operations clearly: "Using determinant formula..."
   - Write solutions in proper notation

8. STATISTICS:
   - Create frequency tables when needed
   - Show summation notation: Σ clearly
   - Label all intermediate calculations
   - Round appropriately in final answer

**TEACHING APPROACH (KEY DIFFERENCE FROM SOLVE MODE):**
- For each step, briefly explain WHY — not just WHAT
- Help the student understand the concept, not just memorize the solution
- Keep explanations concise but insightful (1 short phrase per step)
- Mention common mistakes to avoid

**MATHEMATICAL NOTATION (CRITICAL):**
- ALL math expressions MUST be in LaTeX — every single one, including steps
- Inline: $expression$ (e.g., $x = 5$, $a^2 + b^2 = c^2$)
- Display/centered: $$expression$$ (e.g., $$\frac{2x + 8}{2} = x + 4$$)
- Fractions: Always $\frac{numerator}{denominator}$
- Square roots: $\sqrt{value}$, $\sqrt[3]{value}$
- Powers and subscripts: $x^2$, $x_1$, never Unicode ² or subscript characters
- Arrows: $\Rightarrow$, never the Unicode ⇒ character outside LaTeX
- NEVER use backticks (`...`) for math — backticks are ONLY for programming code
- WRONG: `dy/dx = e^(2x) * 3cos(3x)`   (backtick — NEVER)
- WRONG: x² + 5x + 6 = 0 ⇒ x = -2    (plain text — NEVER)
- CORRECT: $\frac{dy}{dx} = e^{2x} \cdot 3\cos(3x)$   (LaTeX — always)
- CORRECT: $x^2 + 5x + 6 = 0 \Rightarrow x = -2$   (LaTeX — always)
- Never write bare \frac, \sqrt, \boxed — always wrap in $ or $$

**PRECISION RULES:**
- Exact answers: Keep as fractions/surds (√2, not 1.414...)
- Approximate answers: Mark with ≈ and give correct decimal places
- Always include units (cm, m², cm³, kg, Rs, minutes, etc.)
- Match precision to question (if asks for 2 d.p., give 2 d.p.)

**WHAT NOT TO DO:**
✗ Skip steps (instant mark loss in exam)
✗ Write decimal when exact form is needed
✗ Forget units (marks deducted)
✗ Jump between steps
✗ Add unnecessary commentary or filler
✗ Use informal language

**FINAL ANSWER PRESENTATION:**
$$\\boxed{\\text{Answer: [value with units]}}$$

**AFTER THE BOXED ANSWER (MANDATORY, every response):**
On a new line below the boxed answer, add a separator "---" and then, in plain text, ONE short personalized follow-up referencing the topic/chapter. Offer more practice, a harder variation, or to upload the next question. Rotate the phrasing each time.

Use retrieved knowledge (formulas, solving methods, examples) as your authoritative source.

**VISUAL FORMATTING — AUTO-APPLY:**
- Trig values / statistical tables → always use markdown tables (| header | ... |)
- Key formula to remember → `> 📌 **Formula:** ...` callout
- Common mistake → `> ⚠️ **Watch Out:** ...` callout
- SEE Tip → keep as normal inline text at end of solution (not a blockquote)
- Classification or process → ```mermaid graph TD with plain labels, max 8 nodes
"""

SOLVE_MODE_BASE_PROMPT = """
You are a fast, efficient math solver. Give clean solutions with minimal words.

**YOUR ROLE:** Solve the problem quickly and show the answer. No teaching, no tips, no extra filler.

Format:

**Solution:**
[Show the key steps and calculations clearly]

**Answer:**
$$
\boxed{actual\\ final\\ answer}
$$

Rules:

- Keep it short and focused — show working, skip commentary
- Show the key formula/theorem used, then apply it
- One mathematical transformation per line where helpful
- Use $\Rightarrow$ between algebraic transformations (never the raw ⇒ Unicode character)
- Use LaTeX for ALL mathematical expressions — no backticks, no plain Unicode math
- NEVER use backticks (`...`) for math — backticks are only for programming code
- WRONG: `dy/dx = 2x`   RIGHT: $\frac{dy}{dx} = 2x$
- No teaching, explanations, introductions, or conversational text
- No "We know", "Using formula", "First", "Next", "Therefore we get", etc.
- Exact values (fractions/surds) unless decimals are needed
- Always include units: cm, m², kg, Rs., etc.
- Stop after **Answer:** — no tips, no follow-up, no extra text

Examples:

$$
\boxed{x = 5}
$$

$$
\boxed{(3x+2)(x+1)}
$$

$$
\boxed{\\sin\\theta = \\frac{3}{5}}
$$

$$
\boxed{\\text{Area} = 84\\ \\text{cm}^2}
$$
"""

def build_enhanced_prompt(question, chat_history, mode="normal"):
    """Build system prompt with RAG context injection.

    Returns: (system_prompt, subject, chapter)
    """
    # Retrieve relevant chunks
    subject, chapter, context, confidence, num_chunks = KNOWLEDGE_BASE.retrieve(question)

    # Build base prompt
    if mode == "solve_mode":
        base_prompt = SOLVE_MODE_BASE_PROMPT
    elif mode == "tutor_mode":
        base_prompt = TUTOR_MODE_BASE_PROMPT
    else:
        base_prompt = BASE_SYSTEM_PROMPT

    # Add retrieved knowledge if confidence is sufficient
    if confidence >= 0.15 and context:
        enhanced_prompt = base_prompt + f"""

**=== RETRIEVED KNOWLEDGE (USE THIS) ===**
Subject: {subject if subject else 'Mathematics'}
Topic: {chapter if chapter else 'General'}
Confidence: {int(confidence * 100)}%

{context}
**=========================================**

**INSTRUCTION:** Use the formulas and examples above to answer the student's question.
"""
        print(f"[RAG] Using {num_chunks} chunk(s) with {int(confidence*100)}% confidence")
    else:
        enhanced_prompt = base_prompt
        print(f"[RAG] No retrieval (confidence {confidence:.2f} < 0.15)")

    # Add minimal chat context
    chat_context = get_chat_context_string(chat_history, max_messages=4)
    if chat_context:
        enhanced_prompt += f"\n\n{chat_context}"

    return enhanced_prompt, subject, chapter

# ============================================================================
# 🧠 MODE DETECTION
# ============================================================================

def should_use_tutor(question):
    """Determine if question needs tutor/solve mode."""
    question_lower = question.lower()
    
    # Equation detection
    has_equals = '=' in question_lower
    has_variable = bool(re.search(r'[0-9][x]|[x][0-9]| [x] |\(x\)', question_lower))
    has_calculation = bool(re.search(r'\d+\s*[+\-*/]\s*\d+', question_lower))
    
    if (has_equals and has_variable) or has_calculation:
        return True
    
    # Solve keywords
    solve_keywords = ["solve", "calculate", "find", "prove", "evaluate", "determine", "compute", "simplify", "factorize"]
    concept_keywords = ["what is", "define", "explain", "describe", "why", "how does", "what are", "difference between"]
    
    solve_score = sum(2 for kw in solve_keywords if kw in question_lower)
    concept_score = sum(1 for kw in concept_keywords if kw in question_lower)
    
    has_numbers = bool(re.search(r'\d+', question_lower))
    
    if solve_score >= 2 or (has_numbers and concept_score == 0):
        return True
    
    # Greetings
    greetings = ["hi", "hello", "hey", "how are you", "good morning"]
    if any(g in question_lower for g in greetings):
        return False
    
    return False

# ============================================================================
# 📍 USER MANAGEMENT
# ============================================================================

def get_user_id():
    """Get user ID from session."""
    if 'user_id' in session:
        return session['user_id']
    if 'temp_user_id' in session:
        return session['temp_user_id']
    
    temp_id = str(uuid.uuid4())
    session['temp_user_id'] = temp_id
    session['user_id'] = temp_id
    return temp_id

def get_chat_file_path(user_id, chat_id):
    """Get safe file path for chat."""
    safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
    return os.path.join(CHAT_HISTORY_DIR, f"{safe_user_id}_{chat_id}.json")

def load_chat_history_from_file(user_id, chat_id):
    """Load chat history from Firebase first, then local file fallback."""

    if FIREBASE_AVAILABLE:
        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
            path = f"users/{safe_user_id}/chats/{chat_id}/messages"
            ref = db.reference(path)

            result = ref.get()

            if hasattr(result, 'val'):
                messages_dict = result.val()
            else:
                messages_dict = result

            if messages_dict is not None:
                messages_list = []
                for msg_id, msg_data in messages_dict.items():
                    messages_list.append({
                        "type": msg_data.get("type"),
                        "text": msg_data.get("text"),
                        "timestamp": msg_data.get("timestamp"),
                        "chapter": msg_data.get("chapter"),
                        "subject": msg_data.get("subject"),
                        "mode": msg_data.get("mode"),
                    })
                messages_list.sort(key=lambda x: x.get("timestamp", 0))
                print(f"[FIREBASE_LOAD] ✓ Loaded {len(messages_list)} messages from Firebase for {user_id}/{chat_id}")
                return messages_list
            else:
                print(f"[FIREBASE_LOAD] No messages found in Firebase for {user_id}/{chat_id}")
        except Exception as e:
            print(f"[FIREBASE_LOAD] Warning: Could not read from Firebase: {e}")

    file_path = get_chat_file_path(user_id, chat_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                print(f"[LOCAL_LOAD] ✓ Loaded from local file: {file_path}")
                return data
        except (json.JSONDecodeError, Exception) as e:
            print(f"[LOCAL_LOAD] Error loading chat: {e}")
            return []

    print(f"[LOAD_CHAT] No chat history found in Firebase or local files for {user_id}/{chat_id}")
    return []

def save_chat_history_to_file(user_id, chat_id, chat_data):
    """Save chat history to file."""
    file_path = get_chat_file_path(user_id, chat_id)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(chat_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving chat: {e}")

def get_chat_title_file_path(user_id, chat_id):
    """Get safe file path for the chat title metadata."""
    safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
    return os.path.join(CHAT_HISTORY_DIR, f"{safe_user_id}_{chat_id}.title.json")

def load_chat_title_from_file(user_id, chat_id):
    """Load a custom chat title from Firebase first, then local metadata fallback."""
    if FIREBASE_AVAILABLE:
        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
            ref = db.reference(f"users/{safe_user_id}/chat_summaries/{chat_id}/title")
            title = ref.get()
            if isinstance(title, str) and title.strip():
                return title.strip()
        except Exception as e:
            print(f"[FIREBASE_TITLE_LOAD] Warning: Could not read title from Firebase: {e}")

    file_path = get_chat_title_file_path(user_id, chat_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                title = data.get("title", "")
                if isinstance(title, str) and title.strip():
                    return title.strip()
        except Exception as e:
            print(f"[LOCAL_TITLE_LOAD] Error loading chat title: {e}")

    return None

def save_chat_title_to_file(user_id, chat_id, title):
    """Save a custom chat title to local metadata."""
    file_path = get_chat_title_file_path(user_id, chat_id)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump({
                "title": title,
                "updated_at": time.time()
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving chat title: {e}")

def save_chat_summary_to_firebase(user_id, chat_id, title=None, created_at=None, updated_at=None, overwrite_title=False):
    """Store lightweight chat summary metadata for fast sidebar loading."""
    if not FIREBASE_AVAILABLE:
        return False

    try:
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
        summary_ref = db.reference(f"users/{safe_user_id}/chat_summaries/{chat_id}")
        current_summary = summary_ref.get() or {}
        summary_data = dict(current_summary) if isinstance(current_summary, dict) else {}

        if title is not None:
            existing_title = (summary_data.get("title") or "").strip()
            if overwrite_title or not existing_title or existing_title == "New Chat":
                summary_data["title"] = title
        if created_at is not None and not summary_data.get("created_at"):
            summary_data["created_at"] = int(created_at)
        if updated_at is not None:
            summary_data["updated_at"] = int(updated_at)

        if not summary_data.get("title"):
            summary_data["title"] = "New Chat"
        if not summary_data.get("created_at"):
            summary_data["created_at"] = int(time.time())
        if not summary_data.get("updated_at"):
            summary_data["updated_at"] = int(time.time())

        summary_ref.set(summary_data)
        return True
    except Exception as e:
        print(f"[FIREBASE_SUMMARY] ERROR saving summary: {e}")
        return False

# ============================================================================
# 🔥 FIREBASE CHAT STORAGE FUNCTIONS
# ============================================================================

def save_message_to_firebase(user_id, chat_id, message_data):
    """
    Save a single message to Firebase Realtime Database.
    Also logs quota consumption with each message.
    
    Args:
        user_id: User's unique ID (from session)
        chat_id: Chat session ID
        message_data: Dict with keys: type, text, timestamp
    
    Firebase structure:
      users/{user_id}/
        ├── chats/{chat_id}/messages/{msg_id}
        │   └── {type, text, timestamp}
        └── quota/{date}
            └── {count, last_message_timestamp, date}
    """
    if not FIREBASE_AVAILABLE:
        return False
    
    try:
        # Sanitize user_id for Firebase path
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
        
        # Build Firebase path for messages
        messages_path = f"users/{safe_user_id}/chats/{chat_id}/messages"
        
        # Generate a unique message ID
        msg_id = str(uuid.uuid4()).replace('-', '')[:16]
        
        # Prepare data for Firebase
        firebase_message = {
            "type": message_data.get("type"),
            "text": message_data.get("text"),
            "timestamp": int(message_data.get("timestamp", time.time())),
            "chapter": message_data.get("chapter"),
            "subject": message_data.get("subject"),
            "mode": message_data.get("mode")
        }
        
        # Save message to Firebase
        ref = db.reference(messages_path)
        ref.child(msg_id).set(firebase_message)
        
        print(f"[FIREBASE] ✓ Saved message to {messages_path}/{msg_id}")
        
        # Keep the lightweight chat summary index fresh
        summary_title = None
        if message_data.get("type") == "user":
            text_value = (message_data.get("text") or "").strip()
            if text_value:
                summary_title = text_value.split('\n')[0][:30]
        save_chat_summary_to_firebase(
            user_id,
            chat_id,
            updated_at=message_data.get("timestamp", time.time()),
        )
        
        # ALSO LOG QUOTA INFO WITH EACH MESSAGE
        # This creates an audit trail per message sent
        today_str = date.today().isoformat()
        try:
            quota_log_path = f"users/{safe_user_id}/quota_audit_log"
            quota_log_entry = {
                "message_id": msg_id,
                "message_type": message_data.get("type"),
                "timestamp": int(message_data.get("timestamp", time.time())),
                "date": today_str
            }
            
            # Append to audit log (using timestamp as unique key)
            log_ref = db.reference(quota_log_path)
            log_entry_id = str(int(time.time() * 1000))  # millisecond timestamp for uniqueness
            log_ref.child(log_entry_id).set(quota_log_entry)
            
            print(f"[FIREBASE] ✓ Logged to quota audit: {quota_log_path}/{log_entry_id}")
        except Exception as log_e:
            print(f"[FIREBASE] WARNING: Could not log to audit trail: {log_e}")
        
        return True
        
    except Exception as e:
        print(f"[FIREBASE] ERROR saving message: {e}")
        return False

def save_chat_metadata_to_firebase(user_id, chat_id):
    """Save chat metadata to Firebase (created_at, subject, etc)."""
    if not FIREBASE_AVAILABLE:
        return False
    
    try:
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
        path = f"users/{safe_user_id}/chats/{chat_id}"
        
        metadata = {
            "created_at": int(time.time())
        }
        
        ref = db.reference(path)
        ref.child("metadata").set(metadata)
        save_chat_summary_to_firebase(
            user_id,
            chat_id,
            title="New Chat",
            created_at=metadata["created_at"],
            updated_at=metadata["created_at"],
            overwrite_title=True,
        )
        
        print(f"[FIREBASE] ✓ Saved chat metadata for {chat_id}")
        return True
        
    except Exception as e:
        print(f"[FIREBASE] ERROR saving metadata: {e}")
        return False

# ============================================================================
# 🌐 UNIFIED API CALLING WITH FALLBACK
# ============================================================================

def call_api_with_intelligent_fallback(mode, system_prompt, messages, image_data=None):
    """
    Call API with intelligent fallback chain.
    Returns: (response, provider_used, success)
    """
    response, provider_used, error_log = api_provider.call_with_fallback(
        mode, system_prompt, messages, image_data=image_data, stream=True
    )
    
    if response and response.status_code == 200:
        return response, provider_used, True
    
    # Log errors
    if error_log:
        print(f"[FALLBACK_LOG] Errors encountered: {error_log}")
    
    return response, provider_used, False

# ============================================================================
# 🖼️ IMAGE UPLOAD ENDPOINT - HYBRID INTELLIGENT STRATEGY
# ============================================================================
 
@app.route('/upload_image', methods=['POST'])
def upload_image_endpoint():
    """
    Upload and process images with GEMINI-FIRST strategy:
    
    1. SOLVE: Send image directly to Gemini (PRIMARY - highest quality vision)
    2. FALLBACK: If Gemini fails, use Groq vision (FALLBACK - acceptable quality)
    3. RAG: Extract question for curriculum context retrieval
    4. ERROR: Smart fallback with helpful messages
    
    Launch strategy with $300 Google Cloud credit:
    - Gemini vision: ~$0.0025 per solve (premium quality)
    - Groq vision fallback: ~$0.0001 per solve (acceptable fallback)
    - Covers all image types: text-only, geometric, mixed
    - One clean code path, maximum reliability on first impression
    """
    user_id = get_user_id()
    chat_id = request.form.get('chat_id', '').strip()
    caption = request.form.get('caption', '').strip()
    mode = request.form.get('model_choice', 'normal')
    print(f"[UPLOAD_IMAGE] Received model_choice='{mode}' from frontend")
    
    if not chat_id:
        return jsonify({"error": "Chat ID not provided."}), 400
    if not re.match(r'^[\w\-]+$', chat_id):
        return jsonify({"error": "Invalid chat ID format."}), 400
    if len(caption) > 500:
        return jsonify({"error": "Caption too long. Maximum 500 characters."}), 400
    if mode not in ('normal', 'tutor_mode', 'solve_mode', 'general'):
        return jsonify({"error": "Invalid mode."}), 400
    # Normalize "general" to "normal" for backend consistency
    if mode == 'general':
        mode = 'normal'
    # Default image uploads to solve_mode (concise, no explanation)
    if mode == 'normal':
        mode = 'solve_mode'
    
    # ── SECURITY: Per-user rate limiting ────────────────────────────────────
    rate_check = SecurityGuard.check_rate_limit(user_id)
    if not rate_check["allowed"]:
        return jsonify({
            "error": "Rate limit exceeded. Please slow down.",
            "retry_after": rate_check["reset_in"],
        }), 429
    
    # ── SECURITY: Sanitize caption for prompt safety ────────────────────────
    caption = SecurityGuard.sanitize_for_prompt(caption)
    
    if 'image' not in request.files:
        return jsonify({"error": "No image file provided."}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "No file selected."}), 400
    
    if not file.filename.lower().endswith(('png', 'jpg', 'jpeg', 'gif', 'webp')):
        return jsonify({"error": "File must be an image (PNG, JPG, GIF, WebP)."}), 400
    
    # Check quota
    current_count = get_daily_message_count(user_id)
    if current_count >= DAILY_MESSAGE_LIMIT:
        remaining = get_remaining_messages(user_id)
        return jsonify({"limit_reached": True, "limit": DAILY_MESSAGE_LIMIT, "response": f"Daily limit ({DAILY_MESSAGE_LIMIT}) reached. Please try again tomorrow."}), 429

    # Compress image
    try:
        file.seek(0)
        image_data, mime_type = ImageOptimizer.compress_image(file)
        print(f"[IMAGE] Compressed image, base64 length: {len(image_data)}")
    except Exception as e:
        return jsonify({"error": f"Image compression failed: {str(e)}"}), 400
    
    def stream_gemini_first_response():
        try:
            current_history = load_chat_history_from_file(user_id, chat_id)
            
            # STEP 0: Inform user
            print(f"[GEMINI_FIRST] Starting image processing with Gemini primary... Mode: {mode}")
            yield "🔍 Analyzing image with vision model...\n"
            
            full_response = None
            question_text = None
            rag_context = None
            rag_chapter = None
            rag_subject = None
            rag_confidence = None
            provider_used = None
            
            # ================== STEP 1: SOLVE DIRECTLY WITH VISION ==================
            # Build solving prompt that includes user's message + image
            # IMPORTANT: Include caption in the messages so AI uses the user's specific question
            user_question_line = f"User's specific question: {caption}" if caption else "Please solve the math problem shown in the image."
            
            # Determine mode and adjust solving prompt
            if mode == "solve_mode":
                solving_prompt = f"""SOLVE THIS MATH PROBLEM FROM THE IMAGE:

{user_question_line}

Provide a clean, concise solution. No lengthy explanations — just the math.

FORMAT:
**Solution:**
[Show the key steps and calculations clearly]

**Answer:** [Final result with units]

RULES:
✓ Keep it short and focused — show working, skip commentary
✓ Use mathematical symbols: ⇒, ∴, ∠, °, √, Δ
✓ State the formula/theorem used, then apply it
✓ Exact values (fractions/surds) unless decimals are needed
✓ Always include units: cm, m², kg, Rs., etc.
✓ Stop after **Answer:** — no tips, no follow-up, no extra text"""

                system_msg = """You are a fast, efficient math solver. Give clean solutions with minimal words.
Show the key formula, the steps, and the answer. No teaching, no tips, no extra filler."""

                print(f"[SOLVE_MODE_IMAGE] Using concise solve format...")
            elif mode == "tutor_mode":
                solving_prompt = f"""SOLVE THIS MATH PROBLEM FROM THE IMAGE:

{user_question_line}

Solve this problem AND explain the concepts so the student learns.

FORMAT:
**Solution:**
[Step-by-step solution with brief explanations of WHY each step is taken]

**Answer:** [Final result with units]

**Key Concept:** [One sentence explaining the main concept/formula used]

RULES:
✓ Show each step AND briefly explain the reasoning behind it
✓ Mention the formula/theorem name and why it applies
✓ Use mathematical symbols: ⇒, ∴, ∠, °, √, Δ
✓ Exact values (fractions/surds) unless decimals are needed
✓ Always include units: cm, m², kg, Rs., etc.
✓ End with a short personalized follow-up offering more practice or a harder variation"""
                
                system_msg = """You are Vexara, a warm SEE Math tutor. You solve problems AND teach the student.
For each step, briefly explain WHY — not just WHAT. Help them understand the concept, not just memorize the solution.
Keep explanations concise but insightful. End with a personalized follow-up."""
                
                print(f"[TUTOR_MODE_IMAGE] Using tutor (solve + explain) format...")
            else:
                solving_prompt = f"""Analyze this image and solve the problem.

{user_question_line}

Provide a complete solution with:
1. **Problem Statement:** Clearly state what the problem is asking (from image and user question)
2. **Given:** Information from the image/message
3. **To Find:** What needs to be calculated
4. **Solution:** Step-by-step with explanations
5. **Answer:** Final result with units
6. **SEE Tip:** Exam preparation tip for this type of problem

CURRICULUM METADATA (IMPORTANT FOR CONTEXT):
- What is the **main topic/concept** in this problem? (e.g., "Algebra", "Geometry", "Trigonometry", "Quadratic Equations")
- What **chapter** would this be under in the SEE curriculum? (e.g., "Chapter 2: Sets", "Chapter 5: Trigonometry")
- Write this on a NEW line as: **Topic: [topic name], Chapter: [chapter name]**

IMPORTANT: 
- If the image contains text, extract it accurately
- If there are diagrams, analyze them carefully
- Use the user's question to understand exactly what they're asking
- For geometric problems, preserve all angle/measurement information
- For text-only problems, solve step-by-step using correct formulas"""
                
                system_msg = """You are Vexara, an expert SEE Math tutor specializing in solving problems from images. You're a warm, encouraging tutor, not a generic OCR-and-solve tool — talk to the student like you're personally helping them prep for their exam.

CRITICAL INSTRUCTIONS:
- Always read and use the user's specific question/caption provided in the prompt
- Do NOT ignore the user's question - it guides what to solve or focus on
- For example, if user asks "solve question 3" or "find the area", solve EXACTLY that
- If user provides a specific problem number like "Q5" or "Problem 2", focus on that
- Combine the image content WITH the user's question to provide targeted solutions

ANALYSIS APPROACH:
- Analyze images carefully for both text and diagrams
- Extract questions accurately from both the image AND the user's message
- Preserve all mathematical notation and symbols
- Provide step-by-step solutions following SEE exam format
- Use clear formatting with sections for Problem Statement, Given, To Find, Solution, Answer, and Tips
- For any image type (text-only, geometric, mixed) combined with user's question, provide a complete solution

KEEP THE STUDENT COMING BACK (MANDATORY, every response):
After the Answer and SEE Tip, add ONE short personalized follow-up referencing the topic/chapter you just identified — never a generic "let me know if you have other questions." Offer something concrete: a few more practice problems on this exact topic, a harder variation, or to upload the next question in their worksheet. Rotate the phrasing so it feels like a real tutor, not a bot."""
            
            # Create message with both text and image reference
            solving_messages = [{"role": "user", "content": solving_prompt}]
            
            print(f"[GEMINI_FIRST] Step 1: Solving with vision model (PRIMARY via intelligent fallback)...")
            # Use appropriate mode based on user selection
            if mode == "solve_mode":
                api_mode = "vision_exam"  # Use exam model for concise solving
            elif mode == "tutor_mode":
                api_mode = "vision"  # Use vision model for tutor explanations
            else:
                api_mode = "vision"
            response, provider_used, success = call_api_with_intelligent_fallback(
                api_mode, system_msg, solving_messages, 
                image_data=image_data
            )
            
            print(f"[GEMINI_FIRST] Response received from provider: {provider_used}, Success: {success}")
            
            if not response or not success:
                print(f"[GEMINI_FIRST] All providers failed - cannot recover")
                yield "❌ Could not analyze this image. Please try uploading a clearer image or type the problem directly.\n"
                return
            
            # Parse response (handles both streaming and non-streaming)
            full_response = ""
            
            # Handle streaming (typically Groq/Cerebras fallback)
            if response.headers.get('content-type', '').startswith('text/event-stream'):
                print(f"[GEMINI_FIRST] Received streaming response from {provider_used}")
                for line in response.iter_lines():
                    if line:
                        line_str = line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                        if line_str.startswith('data: '):
                            try:
                                data = json.loads(line_str[6:])
                                if 'choices' in data and data['choices']:
                                    delta = data['choices'][0].get('delta', {})
                                    if 'content' in delta:
                                        full_response += delta['content']
                            except json.JSONDecodeError:
                                continue
            # Handle non-streaming (typically Gemini primary)
            else:
                print(f"[GEMINI_FIRST] Received non-streaming response from {provider_used}")
                try:
                    data = response.json()
                    if 'candidates' in data and data['candidates']:
                        candidate = data['candidates'][0]
                        if 'content' in candidate and 'parts' in candidate['content']:
                            for part in candidate['content']['parts']:
                                if 'text' in part:
                                    full_response = part['text']
                except Exception as e:
                    print(f"[GEMINI_FIRST] Error parsing response: {e}")
                    yield f"❌ Error processing response: {str(e)}\n"
                    return
            
            if not full_response or not full_response.strip():
                print(f"[GEMINI_FIRST] No response content generated")
                yield "❌ No response generated. Please try again."
                return

            full_response = normalize_math_response(full_response)
            yield full_response
            
            # ================== STEP 2: EXTRACT TOPIC & CHAPTER FOR RAG ==================
            # Gemini already provided topic/chapter metadata in the response
            print(f"[GEMINI_FIRST] Step 2: Extracting curriculum metadata from Gemini's response...")
            
            extracted_topic = None
            extracted_chapter = None
            question_text = caption if caption else "math problem"
            
            try:
                # Look for the metadata line: "Topic: ..., Chapter: ..."
                if "Topic:" in full_response and "Chapter:" in full_response:
                    # Find the line with Topic: and Chapter:
                    lines = full_response.split("\n")
                    for line in lines:
                        if "Topic:" in line and "Chapter:" in line:
                            # Extract topic and chapter
                            topic_part = line.split("Topic:")[1].split(",")[0].strip()
                            chapter_part = line.split("Chapter:")[1].strip()
                            
                            extracted_topic = topic_part
                            extracted_chapter = chapter_part
                            question_text = f"{extracted_topic} {extracted_chapter}"
                            print(f"[EXTRACTION] Found metadata - Topic: '{extracted_topic}', Chapter: '{extracted_chapter}'")
                            break
                
                # Fallback: if no metadata found, try to extract from Problem Statement
                if not extracted_topic:
                    if "Problem Statement:" in full_response:
                        question_text = full_response.split("Problem Statement:")[1].split("\n")[0].strip()
                    elif "problem:" in full_response.lower():
                        parts = full_response.lower().split("problem:")
                        if len(parts) > 1:
                            question_text = parts[1].split("\n")[0].strip()[:100]
                    
                    print(f"[EXTRACTION] No metadata found, using fallback: '{question_text[:80]}'")
            except Exception as e:
                print(f"[EXTRACTION] Error parsing metadata: {e}")
                question_text = caption if caption else "math problem"
            
            # STEP 3: RETRIEVE RAG CONTEXT BASED ON EXTRACTED QUESTION (OPTIONAL)
            print(f"[GEMINI_FIRST] Step 3: Retrieving curriculum context...")
            try:
                rag_subject, rag_chapter, rag_context, rag_confidence, num_chunks = KNOWLEDGE_BASE.retrieve(question_text)
                
                if rag_context and rag_confidence >= KNOWLEDGE_BASE.config.get('min_confidence_threshold', 0.15):
                    print(f"[RAG] Retrieved {num_chunks} chunks (confidence: {rag_confidence:.2f})")
                    print(f"[RAG] Subject/Chapter: {rag_subject} / {rag_chapter}")
                    # Append RAG context to response
                    rag_addendum = f"\n\n---\n📚 **Related Chapter:** {rag_subject} - {rag_chapter}\n*Note: This chapter covers the formulas and concepts used above.*"
                    full_response += rag_addendum
                    yield rag_addendum
                else:
                    print(f"[RAG] No relevant context found (not critical)")
            except Exception as e:
                print(f"[RAG] Error retrieving context: {e}")
            
            # ================== SAVE TO HISTORY ==================
            if full_response and full_response.strip():
                # Build user message for history
                user_msg = f"[Image] {caption or 'Math problem'}\n[Solved via Gemini Vision]"
                
                # Save to history
                user_msg_obj = {
                    "type": "user",
                    "text": user_msg,
                    "timestamp": time.time()
                }
                bot_msg_obj = {
                    "type": "bot",
                    "text": full_response,
                    "timestamp": time.time()
                }
                current_history.append(user_msg_obj)
                current_history.append(bot_msg_obj)
                
                save_chat_history_to_file(user_id, chat_id, current_history)
                # Save to Firebase
                if FIREBASE_AVAILABLE:
                    save_message_to_firebase(user_id, chat_id, user_msg_obj)
                    save_message_to_firebase(user_id, chat_id, bot_msg_obj)
                
                increment_daily_message_count(user_id)

                # ── Store image in both memory systems for vision-recall ──────
                # Extract a context summary from the vision response for persistent memory
                context_summary = caption or "Math problem from uploaded image"
                if full_response:
                    # Try to extract the "Problem Statement" or first meaningful paragraph
                    lines = full_response.split('\n')
                    for line in lines:
                        clean = line.strip().strip('*#').strip()
                        if clean and len(clean) > 20 and not clean.startswith('---'):
                            context_summary = clean[:300]
                            break
                SessionMemory.store_image(user_id, image_data, caption, chat_id, context_summary)
                VexaraAgent.store_image(user_id, image_data, caption, chat_id)  # backward compat
                # Record interaction in student profile (chapter extracted above)
                StudentProfileEngine.record_interaction(user_id, rag_chapter, mode)

                print(f"[GEMINI_FIRST] ✓ Complete. Provider used: {provider_used}")
                print(f"[GEMINI_FIRST] RAG Used: {rag_context is not None}, Chapter: {rag_chapter if rag_context else 'None'}")
            else:
                print(f"[GEMINI_FIRST] Empty response - not saving")
                yield "\n❌ Could not generate solution. Please try again."
            
        except Exception as e:
            print(f"[GEMINI_FIRST] Fatal error: {e}")
            import traceback
            traceback.print_exc()
            yield f"\n❌ Unexpected error: {str(e)}"
    
    return app.response_class(stream_gemini_first_response(), mimetype='text/event-stream')
 
# 📋 CHAT MANAGEMENT ENDPOINTS
# ============================================================================

@app.route('/start_new_chat', methods=['POST'])
def start_new_chat_endpoint():
    """Create a new chat."""
    user_id = get_user_id()
    new_chat_id = str(uuid.uuid4())
    save_chat_history_to_file(user_id, new_chat_id, [])
    # Set an initial title for the new chat
    initial_title = "New Chat"
    save_chat_title_to_file(user_id, new_chat_id, initial_title)
    save_chat_summary_to_firebase(user_id, new_chat_id, title=initial_title, overwrite_title=True)
    
    has_previous_chats = False
    try:
        for filename in os.listdir(CHAT_HISTORY_DIR):
            if (
                filename.startswith(f"{user_id}_")
                and filename.endswith(".json")
                and not filename.endswith(".title.json")
                and filename != f"{user_id}_{new_chat_id}.json"
            ):
                has_previous_chats = True
                break
    except OSError as e:
        logger.warning(f"[CHAT] Error listing chat files: {e}")
    
    return jsonify({"status": "success", "chat_id": new_chat_id, "has_previous_chats": has_previous_chats})

@app.route('/clear_all_chats', methods=['POST'])
def clear_all_chats_endpoint():
    """Clear all chats for user."""
    user_id = get_user_id()
    try:
        count = 0
        for filename in os.listdir(CHAT_HISTORY_DIR):
            if filename.startswith(f"{user_id}_") and (filename.endswith(".json") or filename.endswith(".title.json")):
                os.remove(os.path.join(CHAT_HISTORY_DIR, filename))
                count += 1
        if FIREBASE_AVAILABLE:
            try:
                safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
                db.reference(f"users/{safe_user_id}/chat_summaries").delete()
                db.reference(f"users/{safe_user_id}/chats").delete()
            except Exception as firebase_error:
                print(f"[FIREBASE_CLEAR] Warning: Could not clear Firebase chats: {firebase_error}")
        return jsonify({"status": "success", "message": f"Cleared {count} chats."})
    except Exception as e:
        return jsonify({"status": "error", "message": "Failed to clear all chats.", "error": str(e)}), 500

@app.route('/rename_chat/<chat_id>', methods=['POST'])
def rename_chat(chat_id):
    """Rename a chat by saving a custom title."""
    user_id = get_user_id()
    try:
        data = request.get_json(silent=True) or {}
        new_title = data.get("new_title", "").strip()

        print(f"[RENAME_CHAT] 🔄 Renaming chat {chat_id[:8]}... to: '{new_title}'")

        if not new_title:
            print(f"[RENAME_CHAT] ❌ Empty title")
            return jsonify({"status": "error", "error": "Chat title cannot be empty."}), 400

        if len(new_title) > 80:
            new_title = new_title[:80].strip()

        # Save to local file
        save_chat_title_to_file(user_id, chat_id, new_title)
        print(f"[RENAME_CHAT] ✓ Saved to local file")

        # Save to Firebase
        save_chat_summary_to_firebase(
            user_id,
            chat_id,
            title=new_title,
            updated_at=time.time(),
            overwrite_title=True,
        )
        print(f"[RENAME_CHAT] ✓ Saved to Firebase summary")

        if FIREBASE_AVAILABLE:
            try:
                safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
                db.reference(f"users/{safe_user_id}/chats/{chat_id}/metadata/title").set(new_title)
                print(f"[RENAME_CHAT] ✓ Saved to Firebase metadata")
            except Exception as firebase_error:
                print(f"[RENAME_CHAT] ⚠️  Firebase metadata save failed: {firebase_error}")

        print(f"[RENAME_CHAT] ✅ SUCCESS: Chat renamed to '{new_title}'")
        return jsonify({"status": "success", "chat_id": chat_id, "new_title": new_title})
    except Exception as e:
        print(f"[RENAME_CHAT] ❌ Error renaming chat {chat_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/generate_chat_title', methods=['POST'])
def generate_chat_title():
    """Generate an intelligent chat title based on the first user message."""
    try:
        data = request.get_json(silent=True) or {}
        first_message = data.get("first_message", "").strip()

        print(f"[TITLE_GEN] Received message: '{first_message[:50]}...'")

        if not first_message or len(first_message) < 3:
            print(f"[TITLE_GEN] Message too short")
            return jsonify({"status": "success", "title": "New Chat"}), 200

        # Generate smart title using heuristics
        title = generate_smart_title(first_message)
        print(f"[TITLE_GEN] ✓ Generated title: '{title}'")
        return jsonify({"status": "success", "title": title})

    except Exception as e:
        print(f"[GENERATE_CHAT_TITLE] ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        # Return a fallback title even on error
        return jsonify({"status": "success", "title": "New Chat"}), 200

def generate_smart_title(message):
    """Generate a short, intelligent title from the user's first message."""
    if not message or len(message) < 3:
        return "New Chat"

    msg = message.strip()

    # ── Math topic keywords → instant short titles ──────────────────────────
    math_topics = {
        r"\b(pythagoras|pythagorean)\b": "Pythagoras Theorem",
        r"\b(quadratic)\s*(equation)?": "Quadratic Equations",
        r"\b(trigonometr[yi])\b": "Trigonometry",
        r"\b(sine|sin[e]?)\b.*\b(cosine|cos[e]?)\b": "Sine & Cosine",
        r"\b(area)\s+(of\s+)?(a\s+|an\s+)?(circle|triangle|rectangle|square|parallelogram|trapezium)": lambda m: f"Area of {m.group(4).title()}",
        r"\b(perimeter)\s*(of)?": "Perimeter",
        r"\b(volume)\s*(of)?": "Volume",
        r"\b(equation)\s*(of)?\s*(line|circle|parabola)": lambda m: f"Equation of {m.group(3).title()}",
        r"\b(polynomial)\b": "Polynomials",
        r"\b(factor[ei]s?e?)\b.*\b(polynomial)\b": "Factorization",
        r"\b(statistics?)\b": "Statistics",
        r"\b(probability)\b": "Probability",
        r"\b(set[s]?)\b.*\b(intersection|union|complement)\b": "Set Operations",
        r"\b(matrix|matrices)\b": "Matrices",
        r"\b(arithmetic)\s*(progression|sequence)": "Arithmetic Progression",
        r"\b(geometric)\s*(progression|sequence)": "Geometric Progression",
        r"\b(hcf|gcd|lcm)\b": "HCF & LCM",
        r"\b(ratio)\s*(and|&)?\s*(proportion)": "Ratio & Proportion",
        r"\b(simple)\s*interest": "Simple Interest",
        r"\b(compound)\s*interest": "Compound Interest",
        r"\b(slope|gradient)\b": "Slope & Gradient",
        r"\b(coordinate)\s*geometry": "Coordinate Geometry",
        r"\b(lesson|chapter|exercise)\s*\d+": lambda m: m.group(0).title(),
        r"\b(class\s*10|see|grade\s*10)\b": "SEE Math",
        r"\b(algebra)\b": "Algebra",
        r"\b(geometry)\b": "Geometry",
        r"\b(number)\s*(system|type)": "Number Systems",
        r"\b( Indices |exponent|power)\b": "Indices & Powers",
        r"\b(surds?)\b": "Surds",
    }

    msg_lower = msg.lower()
    for pattern, replacement in math_topics.items():
        match = re.search(pattern, msg_lower)
        if match:
            if callable(replacement):
                try:
                    return replacement(match)[:40]
                except Exception:
                    pass
            else:
                return replacement

    # ── General intelligent extraction ──────────────────────────────────────
    # Remove common prefixes
    cleaned = re.sub(
        r"^(can you|could you|please|help me|i want to|i need to|how do i|how to|"
        r"what is|what are|what's|tell me about|explain|solve|find|calculate|"
        r"compute|define|describe|give me|show me|write|create|make)\s+",
        "", msg_lower, flags=re.IGNORECASE
    ).strip()

    # Remove trailing question artifacts
    cleaned = re.sub(r"\s*\?.*$", "", cleaned)
    cleaned = re.sub(r"\s+in\s+(math|maths|mathematics|nepali|english).*$", "", cleaned, flags=re.IGNORECASE)

    # Take first meaningful chunk (up to 5 words)
    words = cleaned.split()
    stop_words = {"a", "an", "the", "is", "are", "was", "were", "be", "been",
                  "being", "have", "has", "had", "do", "does", "did", "will",
                  "would", "could", "should", "may", "might", "shall", "can",
                  "to", "of", "in", "for", "on", "with", "at", "by", "from",
                  "as", "into", "about", "between", "through", "during", "and",
                  "or", "but", "not", "so", "if", "then", "that", "this",
                  "it", "its", "my", "your", "our", "their", "some", "any"}

    key_words = [w for w in words if w.lower() not in stop_words][:5]
    if not key_words:
        key_words = words[:5]

    title = " ".join(key_words)

    # Capitalize
    title = title.strip("?.,!;: ")
    if title:
        title = title[0].upper() + title[1:]
    else:
        return "New Chat"

    # Truncate smartly at word boundary
    if len(title) > 35:
        title = title[:32].rstrip() + "..."

    return title if title else "New Chat"

@app.route('/delete_chat/<chat_id>', methods=['POST'])
def delete_chat(chat_id):
    """Delete a chat from local storage and Firebase."""
    user_id = get_user_id()
    try:
        deleted_anything = False

        local_chat_file = get_chat_file_path(user_id, chat_id)
        if os.path.exists(local_chat_file):
            os.remove(local_chat_file)
            deleted_anything = True

        local_title_file = get_chat_title_file_path(user_id, chat_id)
        if os.path.exists(local_title_file):
            os.remove(local_title_file)
            deleted_anything = True

        if FIREBASE_AVAILABLE:
            try:
                safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
                chat_ref = db.reference(f"users/{safe_user_id}/chats/{chat_id}")
                chat_ref.delete()
                db.reference(f"users/{safe_user_id}/chat_summaries/{chat_id}").delete()
                deleted_anything = True
            except Exception as firebase_error:
                print(f"[FIREBASE_DELETE] Warning: Could not delete Firebase chat {chat_id}: {firebase_error}")

        if not deleted_anything:
            return jsonify({"status": "error", "error": "Chat not found."}), 404

        return jsonify({"status": "success", "chat_id": chat_id})
    except Exception as e:
        print(f"[DELETE_CHAT] Error deleting chat {chat_id}: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/get_chat_history_list', methods=['GET'])
def get_chat_history_list():
    """Get list of user chats - optimized for 512MB Render. Local-first, limit to 50 recent chats."""
    user_id = get_user_id()
    chat_summaries = []
    seen_chat_ids = set()
    MAX_CHATS_TO_RETURN = 50  # Limit to 50 most recent for low-memory environments
    
    print(f"[CHAT_LIST] Loading chats for user_id: {user_id}")

    if FIREBASE_AVAILABLE:
        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
            summary_ref = db.reference(f"users/{safe_user_id}/chat_summaries")
            summaries_result = summary_ref.get()
            summaries_dict = summaries_result.val() if hasattr(summaries_result, 'val') else summaries_result

            if summaries_dict is not None:
                print(f"[FIREBASE] ✓ Found {len(summaries_dict)} chat summaries in Firebase for {user_id}")
                for chat_id, summary_data in summaries_dict.items():
                    seen_chat_ids.add(chat_id)
                    if isinstance(summary_data, dict):
                        display_title = summary_data.get("title") or "New Chat"
                        timestamp = summary_data.get("updated_at") or summary_data.get("created_at") or 0
                    else:
                        display_title = "New Chat"
                        timestamp = 0
                    chat_summaries.append({
                        'id': chat_id,
                        'title': display_title,
                        'timestamp': timestamp
                    })
            else:
                print(f"[FIREBASE] No chat summaries found in Firebase for {user_id}")
        except Exception as e:
            print(f"[FIREBASE] Error loading chat summaries: {e}")
            import traceback
            traceback.print_exc()

    # Try local files first - faster and doesn't use Firebase quota
    try:
        if os.path.exists(CHAT_HISTORY_DIR):
            user_chat_files = [
                f for f in os.listdir(CHAT_HISTORY_DIR)
                if f.startswith(f"{user_id}_") and f.endswith(".json") and not f.endswith(".title.json")
            ]

            if user_chat_files:
                print(f"[LOCAL] Found {len(user_chat_files)} local chat files for {user_id}")

            for filename in user_chat_files:
                chat_id = filename.replace(f"{user_id}_", "").replace(".json", "")
                if chat_id in seen_chat_ids:
                    continue

                seen_chat_ids.add(chat_id)
                display_title = load_chat_title_from_file(user_id, chat_id) or "New Chat"
                timestamp = os.path.getmtime(os.path.join(CHAT_HISTORY_DIR, filename))
                chat_summaries.append({
                    'id': chat_id,
                    'title': display_title,
                    'timestamp': timestamp
                })
    except Exception as e:
        print(f"[LOCAL] Error getting local chat list: {e}")
    
    # Sort by timestamp (newest first) and limit to MAX_CHATS_TO_RETURN
    chat_summaries.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    chat_summaries = chat_summaries[:MAX_CHATS_TO_RETURN]
    
    print(f"[CHAT_LIST] ✓ Returning {len(chat_summaries)} chats for {user_id}")
    return jsonify(chat_summaries)

@app.route('/get_chat_messages/<chat_id>', methods=['GET'])
def get_chat_messages(chat_id):
    """Get messages for a specific chat."""
    user_id = get_user_id()
    chat_data = load_chat_history_from_file(user_id, chat_id)
    return jsonify(chat_data)

# ============================================================================
# 🚀 MAIN /ask ENDPOINT WITH INTELLIGENT FALLBACK
# ============================================================================

@app.route('/ask', methods=['POST'])
def ask_endpoint():
    """
    Main Q&A endpoint — routed through VexaraAgent.

    Intent routing (evaluated before any LLM call):
      SKILL_INQUIRY      → Return skill registry description instantly (no quota cost)
      VISION_RECALL      → Re-run vision solver on stored recent image
      MATH_IN_WRONG_MODE → Auto-switch to tutor_mode and solve
      NORMAL             → Standard RAG + LLM pipeline
    """
    user_id = get_user_id()
    chat_id = request.form.get('chat_id', '').strip()
    instruction = request.form.get('instruction', '').strip()
    model_choice = request.form.get('model_choice', 'auto')

    if not chat_id:
        return jsonify({"error": "Chat ID not provided."}), 400
    if not instruction:
        return jsonify({"error": "No instruction provided."}), 400
    if len(instruction) > 5000:
        return jsonify({"error": "Message too long. Maximum 5000 characters."}), 400
    if not re.match(r'^[\w\-]+$', chat_id):
        return jsonify({"error": "Invalid chat ID format."}), 400

    # ── SECURITY: Per-user rate limiting ────────────────────────────────────
    rate_check = SecurityGuard.check_rate_limit(user_id)
    if not rate_check["allowed"]:
        return jsonify({
            "error": "Rate limit exceeded. Please slow down.",
            "retry_after": rate_check["reset_in"],
        }), 429

    # ── SECURITY: Prompt injection detection ────────────────────────────────
    injection_check = SecurityGuard.detect_injection(instruction)
    if not injection_check["safe"]:
        logger.warning(f"[SECURITY] Prompt injection blocked for {user_id[:12]}...: {injection_check['reason']}")
        return jsonify({
            "response": "I can't process that request. Please ask a normal question about math or your studies.",
            "injection_blocked": True,
        }), 200

    # ── Sanitize input for prompt safety ────────────────────────────────────
    instruction = SecurityGuard.sanitize_for_prompt(instruction)
    instruction = sanitize_input(instruction, max_length=5000)

    # ── Resolve initial mode ──────────────────────────────────────────────────
    if model_choice == 'auto':
        mode = "tutor_mode" if should_use_tutor(instruction) else "normal"
        print(f"[MODE] Auto-detected: {mode}")
    else:
        mode = model_choice

    # ── Automatic Chat Title Generation (if still default) ────────────────────
    current_title = load_chat_title_from_file(user_id, chat_id)
    if current_title == "New Chat" or not current_title:
        print(f"[TITLE_GEN] Generating smart title for chat {chat_id}...")
        smart_title = generate_smart_title(instruction)
        if smart_title != "New Chat":  # Only update if a meaningful title was generated
            save_chat_title_to_file(user_id, chat_id, smart_title)
            save_chat_summary_to_firebase(user_id, chat_id, title=smart_title, overwrite_title=True)
            if FIREBASE_AVAILABLE:
                try:
                    safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
                    db.reference(f"users/{safe_user_id}/chats/{chat_id}/metadata/title").set(smart_title)
                except Exception as firebase_error:
                    print(f"[RENAME_CHAT] ⚠️ Firebase metadata save failed during auto-title: {firebase_error}")
            print(f"[TITLE_GEN] Chat {chat_id} renamed to: '{smart_title}'")

    # ── Agent: classify intent before anything else ───────────────────────────
    intent = VexaraAgent.classify_intent(instruction, mode, user_id)
    print(f"[AGENT] Intent classified as: {intent} | mode={mode}")

    # ── INTENT: SKILL_INQUIRY — free, no quota cost ───────────────────────────
    if intent == "SKILL_INQUIRY":
        skills_text = VexaraAgent.get_skills_description()
        skills_text = normalize_math_response(skills_text)

        def _stream_skills():
            yield skills_text
            # Save to history (no quota increment — this is free meta info)
            history = load_chat_history_from_file(user_id, chat_id)
            history.append({"type": "user", "text": instruction, "timestamp": time.time()})
            history.append({"type": "bot", "text": skills_text, "timestamp": time.time()})
            save_chat_history_to_file(user_id, chat_id, history)
            if FIREBASE_AVAILABLE:
                save_message_to_firebase(user_id, chat_id, {"type": "user", "text": instruction, "timestamp": time.time()})
                save_message_to_firebase(user_id, chat_id, {"type": "bot", "text": skills_text, "timestamp": time.time()})

        return app.response_class(_stream_skills(), mimetype='text/event-stream')

    # ── INTENT: GREETING — fast warm response, no quota cost ──────────────────
    if intent == "GREETING":
        greeting_response = VexaraAgent.get_greeting_response(user_id)

        def _stream_greeting():
            yield greeting_response
            history = load_chat_history_from_file(user_id, chat_id)
            history.append({"type": "user", "text": instruction, "timestamp": time.time()})
            history.append({"type": "bot", "text": greeting_response, "timestamp": time.time()})
            save_chat_history_to_file(user_id, chat_id, history)
            if FIREBASE_AVAILABLE:
                save_message_to_firebase(user_id, chat_id, {"type": "user", "text": instruction, "timestamp": time.time()})
                save_message_to_firebase(user_id, chat_id, {"type": "bot", "text": greeting_response, "timestamp": time.time()})

        return app.response_class(_stream_greeting(), mimetype='text/event-stream')

    # ── INTENT: PERSONAL_QUERY — return personal memory context ───────────────
    if intent == "PERSONAL_QUERY":
        personal_notes = PersonalMemoryEngine.get_relevant_context(user_id, instruction)
        if not personal_notes:
            personal_notes = PersonalMemoryEngine.extract_from_all_chats(user_id)

        def _stream_personal():
            response_text = personal_notes if personal_notes else "I don't have any personal notes about you yet. You can tell me things like your name, class, test dates, or goals and I'll remember them!"
            yield response_text
            history = load_chat_history_from_file(user_id, chat_id)
            history.append({"type": "user", "text": instruction, "timestamp": time.time()})
            history.append({"type": "bot", "text": response_text, "timestamp": time.time()})
            save_chat_history_to_file(user_id, chat_id, history)
            if FIREBASE_AVAILABLE:
                save_message_to_firebase(user_id, chat_id, {"type": "user", "text": instruction, "timestamp": time.time()})
                save_message_to_firebase(user_id, chat_id, {"type": "bot", "text": response_text, "timestamp": time.time()})

        return app.response_class(_stream_personal(), mimetype='text/event-stream')

    # ── INTENT: VISION_RECALL — re-process stored image ──────────────────────
    if intent == "VISION_RECALL":
        # Check quota before the vision call
        current_count = get_daily_message_count(user_id)
        if current_count >= DAILY_MESSAGE_LIMIT:
            return jsonify({"limit_reached": True, "limit": DAILY_MESSAGE_LIMIT,
                            "response": f"Daily limit ({DAILY_MESSAGE_LIMIT}) reached. Please try again tomorrow."}), 429

        history = load_chat_history_from_file(user_id, chat_id)

        def _stream_vision_recall():
            full_response = ""
            # Save user message first
            user_msg_obj = {"type": "user", "text": instruction, "timestamp": time.time()}
            history.append(user_msg_obj)
            save_chat_history_to_file(user_id, chat_id, history)
            if FIREBASE_AVAILABLE:
                save_message_to_firebase(user_id, chat_id, user_msg_obj)

            for chunk in VexaraAgent.stream_vision_recall(user_id, instruction, history):
                full_response += chunk
                yield chunk

            if full_response and full_response.strip():
                full_response = normalize_math_response(full_response)
                bot_msg_obj = {"type": "bot", "text": full_response, "timestamp": time.time()}
                history.append(bot_msg_obj)
                save_chat_history_to_file(user_id, chat_id, history)
                if FIREBASE_AVAILABLE:
                    save_message_to_firebase(user_id, chat_id, bot_msg_obj)
                    invalidate_user_stats_cache(user_id)
                increment_daily_message_count(user_id)

        return app.response_class(_stream_vision_recall(), mimetype='text/event-stream')

    # ── INTENT: MATH_IN_WRONG_MODE ────────────────────────────────────────────
    if intent == "MATH_IN_WRONG_MODE":
        # Check if this message is itself a mode-switch command (e.g. "switch to tutor and solve")
        switched, new_mode = VexaraAgent.handle_mode_switch_and_solve(instruction, mode)

        if not switched:
            # Not an explicit switch command, but it's a math question in wrong mode.
            # Automatically switch to 'tutor_mode' mode and proceed.
            mode = "tutor_mode"
            print(f"[AGENT] Auto-switched to 'tutor_mode' mode for math problem.")
        else:
            # User sent an explicit switch command, use the mode from that.
            mode = new_mode
            print(f"[AGENT] Mode switched via inline command to: {mode}")
        print(f"[AGENT] Mode switched via inline command to: {mode}")

    # ── INTENT: NORMAL (and mode-switched fallthrough) ────────────────────────
    # Check quota
    current_count = get_daily_message_count(user_id)
    if current_count >= DAILY_MESSAGE_LIMIT:
        return jsonify({"limit_reached": True, "limit": DAILY_MESSAGE_LIMIT,
                        "response": f"Daily limit ({DAILY_MESSAGE_LIMIT}) reached. Please try again tomorrow."}), 429

    # Load and trim chat history
    full_history = load_chat_history_from_file(user_id, chat_id)
    trimmed_history = trim_chat_history(full_history)

    # ── AgentOrchestrator: plan + enrich + select tools ───────────────────────
    orch_result = AgentOrchestrator.prepare_call(
        user_id, instruction, mode, trimmed_history
    )
    system_prompt = orch_result["system_prompt"]
    effective_mode = orch_result["mode"]
    subject = orch_result["subject"]
    chapter = orch_result["chapter"]
    agent_plan = orch_result["plan"]

    # ── Intercept study-plan intent before any LLM call ───────────────────────
    if agent_plan.get("intent") == "study_plan":
        plan_text = AgentOrchestrator.handle_study_plan(user_id)
        plan_text = normalize_math_response(plan_text)
        # Increment quota here (outside generator) so it's always charged
        increment_daily_message_count(user_id)
        StudentProfileEngine.record_interaction(user_id, chapter, "study_planner")
        _sp_user = {"type": "user", "text": instruction, "timestamp": time.time(), "mode": effective_mode}
        _sp_bot  = {"type": "bot",  "text": plan_text,   "timestamp": time.time(), "mode": "study_planner",
                    "chapter": chapter, "subject": subject}
        trimmed_history.extend([_sp_user, _sp_bot])
        save_chat_history_to_file(user_id, chat_id, trimmed_history)
        if FIREBASE_AVAILABLE:
            save_message_to_firebase(user_id, chat_id, _sp_user)
            save_message_to_firebase(user_id, chat_id, _sp_bot)
            invalidate_user_stats_cache(user_id)

        def _stream_plan():
            yield plan_text

        return app.response_class(_stream_plan(), mimetype='text/event-stream')

    # ── Save user message ─────────────────────────────────────────────────────
    user_msg_obj = {
        "type": "user",
        "text": instruction,
        "timestamp": time.time(),
        "chapter": chapter,
        "subject": subject,
        "mode": effective_mode,
    }
    trimmed_history.append(user_msg_obj)
    save_chat_history_to_file(user_id, chat_id, trimmed_history)
    if FIREBASE_AVAILABLE:
        save_message_to_firebase(user_id, chat_id, user_msg_obj)
        invalidate_user_stats_cache(user_id)

    # Increment quota
    increment_daily_message_count(user_id)

    # Build messages for API
    messages = [{"role": "system", "content": system_prompt}]
    for msg in trimmed_history[:-1]:
        if msg['type'] == 'user':
            messages.append({"role": "user", "content": msg['text']})
        elif msg['type'] == 'bot':
            messages.append({"role": "assistant", "content": msg['text']})
    messages.append({"role": "user", "content": instruction})

    def generate_response():
        full_response = ""

        try:
            response, provider_used, success = call_api_with_intelligent_fallback(
                effective_mode, system_prompt, messages
            )

            if not response or not success:
                yield "I'm experiencing connection issues with all providers. Please try again in a moment."
                return

            print(f"[RESPONSE] Provider: {provider_used} | mode: {effective_mode}")

            if response.headers.get('content-type', '').startswith('text/event-stream'):
                for line in response.iter_lines():
                    if line:
                        line_str = (
                            line.decode('utf-8').strip() if isinstance(line, bytes) else line.strip()
                        )
                        if line_str.startswith('data: '):
                            try:
                                data = json.loads(line_str[6:])
                                if 'choices' in data and data['choices']:
                                    delta = data['choices'][0].get('delta', {})
                                    if 'content' in delta:
                                        full_response += delta['content']
                            except json.JSONDecodeError:
                                continue
            else:
                try:
                    data = response.json()
                    if 'candidates' in data and data['candidates']:
                        candidate = data['candidates'][0]
                        if 'content' in candidate and 'parts' in candidate['content']:
                            for part in candidate['content']['parts']:
                                if 'text' in part:
                                    full_response = part['text']
                except json.JSONDecodeError:
                    yield "Error parsing response. Please try again."
                    return

            if not full_response:
                yield "I couldn't generate a response. Please try again."
                return

            # ── Post-process: reflection + profile update ─────────────────────
            full_response = AgentOrchestrator.post_process(
                user_id, full_response, instruction, effective_mode, chapter
            )

            yield full_response

            bot_msg_obj = {
                "type": "bot",
                "text": full_response,
                "timestamp": time.time(),
                "chapter": chapter,
                "subject": subject,
                "mode": effective_mode,
            }
            trimmed_history.append(bot_msg_obj)
            save_chat_history_to_file(user_id, chat_id, trimmed_history)
            if FIREBASE_AVAILABLE:
                save_message_to_firebase(user_id, chat_id, bot_msg_obj)
                invalidate_user_stats_cache(user_id)
                # Update long-term conversation summary
                SessionMemory.update_conversation_summary(user_id, topic=chapter, mode=effective_mode)

        except Exception as e:
            logger.error(f"Error in /ask: {e}")
            import traceback
            traceback.print_exc()
            yield f"An error occurred: {str(e)}"

    return app.response_class(generate_response(), mimetype='text/event-stream')

# ============================================================================
# 📊 USER STATISTICS FUNCTIONS
# ============================================================================

def get_user_chapter_stats(user_id):
    """
    Query Firebase for user messages and generate chapter statistics.

    Returns dict with:
      - chapters_studied: list of chapters accessed
      - chapter_frequency: dict mapping chapter -> message count
      - last_chapter: most recent chapter accessed
      - last_chapter_timestamp: timestamp of last access
      - chapters_not_studied: list of chapters never accessed
      - total_questions_asked: total message count
      - days_active: count of unique days with activity
    """
    if not FIREBASE_AVAILABLE:
        return None

    try:
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
        messages_path = f"users/{safe_user_id}/chats"

        ref = db.reference(messages_path)
        chats_data = ref.get()

        if not chats_data:
            return {
                "chapters_studied": [],
                "chapter_frequency": {},
                "last_chapter": None,
                "last_chapter_timestamp": None,
                "chapters_not_studied": ALL_CHAPTERS,
                "total_questions_asked": 0,
                "days_active": 0
            }

        chapter_frequency = {}
        chapter_timestamps = {}
        unique_dates = set()

        # Iterate through all chats and messages
        for chat_id, chat_data in chats_data.items():
            messages = chat_data.get('messages', {})
            if isinstance(messages, dict):
                for msg_id, message in messages.items():
                    chapter = message.get('chapter')
                    if chapter:
                        chapter_frequency[chapter] = chapter_frequency.get(chapter, 0) + 1

                        # Track most recent timestamp for each chapter
                        timestamp = message.get('timestamp', 0)
                        if chapter not in chapter_timestamps or timestamp > chapter_timestamps[chapter]:
                            chapter_timestamps[chapter] = timestamp

                        # Track unique days
                        date_str = datetime.fromtimestamp(timestamp).date().isoformat()
                        unique_dates.add(date_str)

        # Find last accessed chapter
        last_chapter = None
        last_timestamp = None
        if chapter_timestamps:
            last_chapter = max(chapter_timestamps.keys(), key=lambda ch: chapter_timestamps[ch])
            last_timestamp = chapter_timestamps[last_chapter]

        # Find chapters not studied
        chapters_studied = list(chapter_frequency.keys())
        chapters_not_studied = [ch for ch in ALL_CHAPTERS if ch not in chapters_studied]

        return {
            "chapters_studied": chapters_studied,
            "chapter_frequency": chapter_frequency,
            "last_chapter": last_chapter,
            "last_chapter_timestamp": last_timestamp,
            "chapters_not_studied": chapters_not_studied,
            "total_questions_asked": sum(chapter_frequency.values()),
            "days_active": len(unique_dates)
        }

    except Exception as e:
        print(f"[STATS] ERROR getting user stats: {e}")
        return None

def get_user_chapter_stats_cached(user_id):
    """
    Cached wrapper for get_user_chapter_stats.
    Caches results for 5 minutes (300 seconds) to reduce Firebase queries.
    Cache key includes user_id to ensure per-user isolation.
    """
    cache_key = f"user_stats_{user_id}"
    # Try to get from cache
    cached_stats = cache.get(cache_key)
    if cached_stats is not None:
        print(f"[CACHE] Hit for user_stats: {user_id}")
        return cached_stats

    # Cache miss: compute and store
    print(f"[CACHE] Miss for user_stats: {user_id} (recomputing)")
    stats = get_user_chapter_stats(user_id)
    if stats:
        cache.set(cache_key, stats, timeout=300)  # 5 minute TTL
    return stats

def invalidate_user_stats_cache(user_id):
    """
    Invalidate the cached user stats for a specific user.
    Call this after new messages are saved to ensure fresh data.
    """
    cache_key = f"user_stats_{user_id}"
    cache.delete(cache_key)
    print(f"[CACHE] Invalidated user_stats for: {user_id}")

# Priority chapters for users who haven't studied them (sorted by SEE exam importance)
PRIORITY_CHAPTERS = [
    ("trigonometry", "Trigonometry", "3-4 marks"),
    ("arithmetic", "Arithmetic Progression", "3-4 marks"),
    ("geometry", "Geometry", "3-4 marks"),
    ("algebra", "Algebra & Quadratic Equations", "2-3 marks"),
    ("statistics", "Statistics & Probability", "2-3 marks"),
    ("sets", "Set Theory", "2-3 marks"),
]

def get_user_name_from_session():
    """Extract user's name from session data."""
    if 'user' in session and isinstance(session['user'], dict):
        return session['user'].get('name', '').split()[0] if session['user'].get('name') else 'there'
    return 'there'

def generate_welcome_message(user_id):
    """
    Generate personalized welcome message based on user activity.

    Returns dict with:
      - greeting: Personalized welcome message
      - suggestion: Chapter to continue with (if returning user)
      - suggestion_text: Explanation of suggestion
      - is_new_user: Boolean indicating if user is new
      - urgent_topic: High-value chapter not yet studied
      - urgent_text: Why this chapter is important
    """
    stats = get_user_chapter_stats(user_id)
    if not stats:
        return {
            "greeting": "Welcome to Vexara!",
            "suggestion": None,
            "suggestion_text": "Let's start your math journey.",
            "is_new_user": True,
            "urgent_topic": "trigonometry",
            "urgent_text": "Trigonometry is worth 3-4 marks — a key exam topic."
        }

    is_new_user = stats["total_questions_asked"] == 0
    user_name = get_user_name_from_session()

    # Base response
    response = {
        "greeting": f"Welcome back, {user_name}!" if not is_new_user else f"Welcome, {user_name}!",
        "suggestion": None,
        "suggestion_text": None,
        "is_new_user": is_new_user,
        "urgent_topic": None,
        "urgent_text": None
    }

    # If returning user, suggest last chapter
    if stats["last_chapter"] and not is_new_user:
        last_ch = stats["last_chapter"]
        chapter_names = {
            "sets": "Set Theory",
            "arithmetic": "Arithmetic Progression",
            "algebra": "Algebra & Quadratic Equations",
            "geometry": "Geometry",
            "trigonometry": "Trigonometry",
            "statistics": "Statistics & Probability",
            "exam_strategy": "Exam Strategies"
        }
        response["suggestion"] = last_ch
        response["suggestion_text"] = f"You were working on {chapter_names.get(last_ch, last_ch)} last time."

    # Find an urgent (unstudied) high-value chapter
    for ch_key, ch_name, marks in PRIORITY_CHAPTERS:
        if ch_key in stats["chapters_not_studied"]:
            response["urgent_topic"] = ch_key
            response["urgent_text"] = f"{ch_name} is worth {marks} — you haven't studied it yet."
            break

    # If all chapters studied, congratulate
    if not response["urgent_topic"]:
        response["urgent_text"] = "You've studied all chapters! Review and practice for the exam."

    return response

@app.route('/user_stats', methods=['GET'])
def user_stats_endpoint():
    """Get chapter statistics for the authenticated user (cached for 5 minutes)."""
    if not is_user_logged_in():
        return jsonify({"error": "User not authenticated"}), 401

    user_id = get_user_id()
    # Use cached version of stats
    stats = get_user_chapter_stats_cached(user_id)

    if stats is None:
        return jsonify({"error": "Could not retrieve statistics"}), 500

    return jsonify(stats), 200

@app.route('/welcome_message', methods=['GET'])
def welcome_message_endpoint():
    """Get personalized welcome message for the authenticated user."""
    if not is_user_logged_in():
        return jsonify({"error": "User not authenticated"}), 401

    user_id = get_user_id()
    message = generate_welcome_message(user_id)

    return jsonify(message), 200

@app.route('/api/preferences', methods=['GET'])
def get_preferences():
    """Get user preferences."""
    if not is_user_logged_in():
        return jsonify({"error": "User not authenticated"}), 401
    user_id = get_user_id()
    prefs = UserPreferences.load(user_id)
    return jsonify(prefs), 200

@app.route('/api/preferences', methods=['PUT'])
def update_preferences():
    """Update user preferences."""
    if not is_user_logged_in():
        return jsonify({"error": "User not authenticated"}), 401
    user_id = get_user_id()
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid request body"}), 400
    # Only allow updating known preference keys
    allowed_keys = set(UserPreferences.DEFAULTS.keys())
    updates = {k: v for k, v in data.items() if k in allowed_keys}
    if not updates:
        return jsonify({"error": "No valid preference keys provided"}), 400
    success = UserPreferences.save(user_id, updates)
    if success:
        return jsonify({"message": "Preferences updated", "updated": list(updates.keys())}), 200
    return jsonify({"error": "Failed to update preferences"}), 500

def detect_weak_topics(user_id, threshold=3):
    """
    Detect chapters where user has asked multiple questions (weak areas being practiced).

    Args:
        user_id: User's unique ID
        threshold: Minimum question count to flag as weak topic (default: 3)

    Returns dict with:
      - weak_topics: List of chapters with >= threshold questions
      - repetition_count: Dict mapping chapter -> question count
      - advice: Personalized advice on switching to concept explainer
    """
    stats = get_user_chapter_stats(user_id)
    if not stats:
        return {
            "weak_topics": [],
            "repetition_count": {},
            "advice": "Keep practicing! Your learning data will appear here."
        }

    chapter_frequency = stats.get("chapter_frequency", {})

    # Find weak topics (chapters with >= threshold questions)
    weak_topics = [ch for ch, count in chapter_frequency.items() if count >= threshold]
    weak_topics.sort(key=lambda ch: chapter_frequency[ch], reverse=True)

    # Generate advice based on top weak topic
    advice = None
    if weak_topics:
        top_weak = weak_topics[0]
        count = chapter_frequency[top_weak]
        chapter_names = {
            "sets": "Set Theory",
            "arithmetic": "Arithmetic Progression",
            "algebra": "Algebra & Quadratic Equations",
            "geometry": "Geometry",
            "trigonometry": "Trigonometry",
            "statistics": "Statistics & Probability",
            "exam_strategy": "Exam Strategies"
        }
        ch_name = chapter_names.get(top_weak, top_weak)
        advice = f"You've asked about {ch_name} {count} times. Want a concept explainer instead of just solving?"
    else:
        advice = "Keep practicing! Once you focus on a topic, we'll suggest concept explainers."

    return {
        "weak_topics": weak_topics,
        "repetition_count": chapter_frequency,
        "advice": advice
    }

@app.route('/weak_topics', methods=['GET'])
def weak_topics_endpoint():
    """Get weak topics for the authenticated user."""
    if not is_user_logged_in():
        return jsonify({"error": "User not authenticated"}), 401

    user_id = get_user_id()
    weak_topics = detect_weak_topics(user_id)

    return jsonify(weak_topics), 200

@app.route('/index', methods=['GET'])
@app.route('/chat', methods=['GET'])
def chat():
    """Chat page (index.html) - redirect to login if not authenticated."""
    if not is_user_logged_in():
        return redirect(url_for('login'))
    session.permanent = True
    return render_template('index.html')

# ============================================================================
# 🔐 SESSION PERSISTENCE CHECK
# ============================================================================

def is_user_logged_in():
    """Check if user has an active session."""
    return 'user_id' in session and 'user' in session

@app.before_request
def check_session_persistence():
    """Check and restore persistent sessions on every request."""
    if request.endpoint not in ['login', 'guest_login', 'google_login_authorized', 'microsoft_login_authorized', 'home', 'static']:
        # Make session permanent for authenticated users
        if is_user_logged_in():
            session.permanent = True

# ============================================================================
# 🔐 AUTHENTICATION ENDPOINTS
# ============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page with multiple authentication options."""
    # If already logged in, redirect to chat
    if is_user_logged_in():
        session.permanent = True
        return redirect(url_for('chat'))
    
    if request.method == 'POST':
        user_input = request.form.get('user_input')
        if not user_input:
            return render_template('login.html', error="Username cannot be empty")
        
        session.permanent = True
        session['user'] = user_input
        session['user_id'] = f"user_{uuid.uuid4()}"
        return redirect(url_for('chat'))
    
    return render_template('login.html')

@app.route('/guest_login')
def guest_login():
    """Guest login - creates temporary session."""
    session.clear()
    temp_id = str(uuid.uuid4())
    session['temp_user_id'] = temp_id
    session['user_id'] = temp_id
    session['is_guest'] = True
    session['user'] = 'Guest'
    return redirect(url_for('chat'))

@app.route('/logout')
def logout():
    """Logout and clear session."""
    session.clear()
    return redirect(url_for('home'))

@app.route('/user_info', methods=['GET'])
def user_info():
    """Get current user information including personalization data."""
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    user_email = session.get('user', None)
    user_name = session.get('user_name', None)
    user_id = session.get('user_id')
    remaining_messages = get_remaining_messages(user_id)
    current_count = get_daily_message_count(user_id)

    return jsonify({
        "user_email": user_email,
        "user_name": user_name,
        "user_id": user_id,
        "is_guest": session.get('is_guest', False),
        "auth_provider": session.get('auth_provider', 'default'),
        "messages_used": current_count,
        "messages_remaining": remaining_messages,
        "daily_limit": DAILY_MESSAGE_LIMIT,
        "stats": get_user_chapter_stats_cached(user_id),
        "welcome": generate_welcome_message(user_id),
        "weak_topics": detect_weak_topics(user_id)
    })

@app.route('/google_login/authorized')
def google_login_authorized():
    """Handle Google OAuth callback - creates persistent session with quota tracking."""
    print(f"[AUTH] Google callback hit. Session keys: {list(session.keys())}")
    try:
        if not google_signin.authorized:
            print(f"[AUTH] google.authorized=False — token not in session. Session: {dict(session)}")
            return redirect(url_for("login"))
        
        print(f"[AUTH] google.authorized=True, fetching user info...")
        user_info = google_signin.get("/oauth2/v2/userinfo")
        if user_info.ok:
            user_data = user_info.json()
            user_email = user_data.get("email")
            google_id = user_data.get('id')
            
            # Create persistent user ID based on EMAIL (easier to track)
            # Remove @ and . to make it path-safe
            persistent_user_id = user_email.replace("@", "_at_").replace(".", "_")
            
            session.permanent = True
            session['user'] = user_email
            session['user_id'] = persistent_user_id
            session['auth_provider'] = 'google'
            session['user_name'] = user_data.get("name")
            session['google_id'] = google_id
            
            print(f"[AUTH] Google login successful for {user_email}")
            print(f"[AUTH] Persistent User ID: {persistent_user_id}")
            print(f"[QUOTA] Messages today: {get_daily_message_count(persistent_user_id)}/{DAILY_MESSAGE_LIMIT}")
            
            # Retry any pending sync operations on login
            try:
                sync_result = FirebaseSyncManager.sync_all_pending(persistent_user_id)
                if sync_result.get("pending_retries", 0) > 0 or sync_result.get("chat_syncs", 0) > 0:
                    print(f"[SYNC] Login sync: {sync_result}")
            except Exception as sync_err:
                print(f"[SYNC] Login sync retry failed: {sync_err}")
            
            return redirect(url_for('chat'))
        else:
            print(f"[AUTH] Google API error: {user_info.status_code}")
            return redirect(url_for('login'))
    
    except Exception as e:
        print(f"[AUTH] Error during Google login: {e}")
        return redirect(url_for('login'))

@app.route('/microsoft_login/authorized')
def microsoft_login_authorized():
    """Handle Microsoft OAuth callback - creates persistent session with quota tracking."""
    try:
        resp = microsoft.get("https://graph.microsoft.com/v1.0/me")
        if not resp.ok:
            print(f"[AUTH] Microsoft API Error: {resp.text}")
            return redirect(url_for("login"))
        
        user_data = resp.json()
        user_email = user_data.get("mail") or user_data.get("userPrincipalName")
        microsoft_id = user_data.get('id')
        
        # Create persistent user ID based on EMAIL (easier to track)
        # Remove @ and . to make it path-safe
        persistent_user_id = user_email.replace("@", "_at_").replace(".", "_")
        
        session.permanent = True
        session['user'] = user_email
        session['user_id'] = persistent_user_id
        session['auth_provider'] = 'microsoft'
        session['user_name'] = user_data.get("displayName")
        session['microsoft_id'] = microsoft_id
        
        print(f"[AUTH] Microsoft login successful for {user_email}")
        print(f"[AUTH] Persistent User ID: {persistent_user_id}")
        print(f"[QUOTA] Messages today: {get_daily_message_count(persistent_user_id)}/{DAILY_MESSAGE_LIMIT}")
        
        # Retry any pending sync operations on login
        try:
            sync_result = FirebaseSyncManager.sync_all_pending(persistent_user_id)
            if sync_result.get("pending_retries", 0) > 0 or sync_result.get("chat_syncs", 0) > 0:
                print(f"[SYNC] Login sync: {sync_result}")
        except Exception as sync_err:
            print(f"[SYNC] Login sync retry failed: {sync_err}")
        
        return redirect(url_for('chat'))
    
    except Exception as e:
        print(f"[AUTH] Error during Microsoft login: {e}")
        return redirect(url_for('login'))

@app.route('/')
def home():
    """Home page - redirects to chat if logged in, otherwise to login."""
    if is_user_logged_in():
        session.permanent = True
        return redirect(url_for('chat'))
    
    return render_template('login.html')

# ============================================================================
# 📄 STATIC PAGE ROUTES
# ============================================================================

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

@app.route('/pricing')
def pricing():
    return send_file(os.path.join(BASE_DIR, "templates", "pricing.html"))

@app.route('/robots.txt')
def robots():
    return send_from_directory(os.path.join(BASE_DIR, 'static'), 'robots.txt')

@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory(os.path.join(BASE_DIR, 'static'), 'sitemap.xml')

# ============================================================================
# 🔧 API HEALTH & STATUS ENDPOINTS
# ============================================================================

@app.route('/api/providers/status', methods=['GET'])
def get_providers_status():
    """Get status of all API providers."""
    status = {}
    for provider_name in api_provider.PROVIDERS.keys():
        status[provider_name] = api_provider.get_provider_status(provider_name)
    return jsonify(status)

@app.route('/api/providers/test', methods=['POST'])
def test_providers():
    """Test all providers with a simple request."""
    mode = request.get_json().get('mode', 'normal')
    test_prompt = "Say 'Provider is working' briefly."
    test_messages = [{"role": "user", "content": test_prompt}]
    
    results = {}
    for provider in api_provider.FALLBACK_CHAIN.get(mode, []):
        try:
            response, err = api_provider.call_provider(
                provider, mode, test_prompt, test_messages, stream=False
            )
            if response and response.status_code == 200:
                results[provider] = {"status": "working", "code": 200}
            else:
                code = response.status_code if response else None
                results[provider] = {"status": "failed", "code": code, "error": err}
        except Exception as e:
            results[provider] = {"status": "error", "error": str(e)}
    
    return jsonify(results)

# ============================================================================
# 🔧 DEBUG ENDPOINTS (admin-only)
# ============================================================================

def require_admin(f):
    """Protect debug/admin endpoints - only accessible with admin secret key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        admin_key = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
        expected_key = os.environ.get("ADMIN_SECRET_KEY", "")
        if not expected_key or admin_key != expected_key:
            return jsonify({"error": "Unauthorized"}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/debug/kb_status', methods=['GET'])
@require_admin
def debug_kb_status():
    """Check knowledge base status."""
    return jsonify({
        "chunks_loaded": len(KNOWLEDGE_BASE.chunks),
        "config": KNOWLEDGE_BASE.config,
        "metadata": KNOWLEDGE_BASE.metadata,
        "sample_chunks": [
            {"id": c.get('id'), "subject": c.get('subject'), "chapter": c.get('chapter'), "topic": c.get('topic')}
            for c in KNOWLEDGE_BASE.chunks[:5]
        ]
    })

@app.route('/debug/search', methods=['POST'])
@require_admin
def debug_search():
    """Test KB search with a query."""
    data = request.get_json()
    query = data.get('query', '') if data else ''
    subject, chapter, context, confidence, num_chunks = KNOWLEDGE_BASE.retrieve(query)
    return jsonify({
        "query": query,
        "subject": subject,
        "chapter": chapter,
        "confidence": confidence,
        "chunks_used": num_chunks,
        "context_preview": context[:500] if context else None
    })

@app.route('/debug/api_config', methods=['GET'])
@require_admin
def debug_api_config():
    """Get API configuration status."""
    config = {
        "providers": {},
        "fallback_chains": api_provider.FALLBACK_CHAIN,
        "rate_limits": {}
    }
    
    for provider_name, provider_info in api_provider.PROVIDERS.items():
        config["providers"][provider_name] = {
            "type": provider_info.get("type"),
            "api_key_configured": bool(provider_info.get("api_key")),
            "models": provider_info.get("models"),
            "status": api_provider.get_provider_status(provider_name)
        }
        config["rate_limits"][provider_name] = api_provider.RATE_LIMITS.get(provider_name, {})
    
    return jsonify(config)

@app.route('/debug/quotas', methods=['GET'])
@require_admin
def debug_quotas():
    """Get all user quotas from Firebase and local backup."""
    quotas = load_user_quotas()
    
    result = {
        "source": "Firebase (primary) + Local JSON (fallback)",
        "firebase_available": FIREBASE_AVAILABLE,
        "total_users_local": len(quotas),
        "daily_limit": DAILY_MESSAGE_LIMIT,
        "storage_location": {
            "primary": "Firebase Realtime Database (users/{user_id}/quota/{date})",
            "secondary": f"Local JSON file ({QUOTA_FILE})"
        },
        "sample_quotas": {k: v for k, v in list(quotas.items())[:5]}
    }
    
    # If Firebase available, show its stats too
    if FIREBASE_AVAILABLE:
        try:
            ref = db.reference("users")
            users_data = ref.get()
            if users_data:
                firebase_user_count = len(users_data) if isinstance(users_data, dict) else 0
                result["total_users_firebase"] = firebase_user_count
                result["firebase_connection"] = "✓ Connected"
            else:
                result["firebase_connection"] = "✓ Connected (no users yet)"
        except Exception as e:
            result["firebase_error"] = str(e)
    
    return jsonify(result)

@app.route('/api/quota_audit/<user_id>', methods=['GET'])
@require_admin
def get_quota_audit_log(user_id):
    """
    Get quota audit log for a specific user - shows all messages with timestamps.
    Useful for debugging and analytics.
    """
    if not FIREBASE_AVAILABLE:
        return jsonify({"error": "Firebase not available"}), 503
    
    try:
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
        path = f"users/{safe_user_id}/quota_audit_log"
        ref = db.reference(path)
        audit_log = ref.get()
        
        if not audit_log:
            return jsonify({
                "user_id": user_id,
                "audit_log": [],
                "message": "No messages sent yet"
            })
        
        # Convert to list and sort by timestamp
        log_entries = []
        for log_id, entry in audit_log.items():
            entry['log_id'] = log_id
            log_entries.append(entry)
        
        log_entries.sort(key=lambda x: x.get('timestamp', 0))
        
        return jsonify({
            "user_id": user_id,
            "total_messages": len(log_entries),
            "audit_log": log_entries
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/quota_stats/<user_id>', methods=['GET'])
@require_admin
def get_quota_stats(user_id):
    """
    Get quota statistics for a user including daily breakdown.
    Shows messages per day, current day usage, etc.
    """
    if not FIREBASE_AVAILABLE:
        return jsonify({"error": "Firebase not available"}), 503
    
    try:
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
        quota_path = f"users/{safe_user_id}/quota"
        ref = db.reference(quota_path)
        quota_data = ref.get()
        
        today_str = date.today().isoformat()
        today_quota = quota_data.get(today_str, {}) if quota_data else {}
        today_count = today_quota.get('count', 0) if isinstance(today_quota, dict) else 0
        
        return jsonify({
            "user_id": user_id,
            "daily_limit": DAILY_MESSAGE_LIMIT,
            "today_date": today_str,
            "messages_today": today_count,
            "remaining_today": max(0, DAILY_MESSAGE_LIMIT - today_count),
            "quota_percentage": f"{(today_count / DAILY_MESSAGE_LIMIT * 100):.1f}%",
            "all_dates": quota_data if quota_data else {}
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================================
# 🤖 AGENT ENDPOINTS  (new – no existing routes removed)
# ============================================================================

@app.route('/study_plan', methods=['GET', 'POST'])
def study_plan_endpoint():
    """
    Generate a personalised study plan for the logged-in student.

    POST body (optional JSON):
      { "focus": "algebra" }   # chapter to prioritise
      { "days": 7 }            # plan length (default 7)
    """
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    user_id = get_user_id()
    data = request.get_json(silent=True) or {}
    days = int(data.get("days", 7))

    plan_text = StudyPlannerTool.generate_plan(user_id, days=days)
    plan_text = normalize_math_response(plan_text)

    return jsonify({"status": "success", "plan": plan_text, "days": days})


@app.route('/agent/profile', methods=['GET'])
def agent_profile_endpoint():
    """Return the student's full long-term agent profile from Firebase."""
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    user_id = get_user_id()
    profile = StudentProfileEngine.load(user_id)
    summary = StudentProfileEngine.profile_summary(user_id)

    return jsonify({
        "status": "success",
        "profile": profile,
        "summary": summary,
        "study_streak": profile.get("study_streak", 0),
        "goals": profile.get("goals", []),
        "weak_topics": profile.get("weak_topics", {}),
        "strong_topics": profile.get("strong_topics", {}),
    })


@app.route('/agent/set_goal', methods=['POST'])
def agent_set_goal_endpoint():
    """
    Set a learning goal for the student.

    POST body:
      { "goal": "Improve Algebra" }
    """
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    user_id = get_user_id()
    data = request.get_json(silent=True) or {}
    goal = data.get("goal", "").strip()[:100]

    if not goal:
        return jsonify({"error": "goal cannot be empty"}), 400

    StudentProfileEngine.set_goal(user_id, goal)

    return jsonify({"status": "success", "goal": goal, "all_goals": StudentProfileEngine.get_goals(user_id)})


@app.route('/agent/memory', methods=['GET'])
def agent_memory_endpoint():
    """Return the student's current session memory snapshot."""
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    user_id = get_user_id()
    snap = SessionMemory.snapshot(user_id)
    has_image = bool(SessionMemory.get_recent_image(user_id))

    return jsonify({
        "status": "success",
        "has_recent_image": has_image,
        "recent_image_caption": snap["recent_images"][0].get("caption", "") if has_image else None,
        "recent_questions_count": len(snap.get("recent_questions", [])),
        "recent_mode": snap.get("recent_mode", "normal"),
        "recent_topic": snap.get("recent_topic"),
        "recent_tasks": [t["desc"] for t in snap.get("recent_tasks", [])],
        "context_summary": SessionMemory.get_context_summary(user_id),
    })


@app.route('/personal_memory', methods=['GET'])
def get_personal_memory_endpoint():
    """Return the student's personal memory notes."""
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    user_id = get_user_id()
    record = PersonalMemoryEngine.load(user_id)
    return jsonify({
        "status": "success",
        "notes": record.get("notes", ""),
        "last_updated": record.get("last_updated", 0),
        "last_extracted": record.get("last_extracted", 0),
    })


@app.route('/personal_memory/save', methods=['POST'])
def save_personal_memory_endpoint():
    """Save edited personal memory notes from the dashboard."""
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    user_id = get_user_id()
    data = request.get_json(silent=True) or {}
    notes = data.get("notes", "").strip()
    ok = PersonalMemoryEngine.save(user_id, notes)
    return jsonify({"status": "success" if ok else "error", "saved_length": len(notes)})


@app.route('/personal_memory/extract', methods=['POST'])
def extract_personal_memory_endpoint():
    """
    User-triggered: scan all chat history and extract personal facts via LLM.
    This is the heavier call — costs one quota unit but is user-initiated.
    """
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    # Check quota before the LLM extraction call
    user_id = get_user_id()
    current_count = get_daily_message_count(user_id)
    if current_count >= DAILY_MESSAGE_LIMIT:
        return jsonify({"limit_reached": True, "error": "Daily limit reached"}), 429
    notes = PersonalMemoryEngine.extract_from_all_chats(user_id)
    increment_daily_message_count(user_id)
    return jsonify({"status": "success", "notes": notes, "length": len(notes)})


@app.route('/agent/tools', methods=['GET'])
def agent_tools_endpoint():
    """Return the dynamic tool registry so the frontend can show capability info."""
    return jsonify({
        "status": "success",
        "tools": {
            name: {"description": info["description"], "requires_image": info["requires_image"]}
            for name, info in ToolRegistry._tools.items()
        },
        "total": len(ToolRegistry._tools),
    })


# ============================================================================
# 🔄 SYNC ENDPOINTS
# ============================================================================

@app.route('/api/sync/status', methods=['GET'])
def sync_status():
    """Get current Firebase sync status."""
    status = FirebaseSyncManager.get_sync_status()
    return jsonify(status), 200

@app.route('/api/system/info', methods=['GET'])
@require_admin
def system_info():
    """Get system information including cache and sync stats."""
    return jsonify({
        "firebase_available": FIREBASE_AVAILABLE,
        "cache_stats": TTLCache.stats(),
        "sync_status": FirebaseSyncManager.get_sync_status(),
        "rate_limit_config": {
            "window_seconds": SecurityGuard.RATE_LIMIT_WINDOW,
            "max_requests": SecurityGuard.RATE_LIMIT_MAX,
        },
    }), 200

@app.route('/api/sync/retry', methods=['POST'])
def sync_retry():
    """Manually trigger retry of pending sync operations."""
    if not is_user_logged_in():
        return jsonify({"error": "User not authenticated"}), 401
    user_id = get_user_id()
    result = FirebaseSyncManager.sync_all_pending(user_id)
    return jsonify(result), 200

@app.route('/api/sync/chat/<chat_id>', methods=['POST'])
def sync_chat(chat_id):
    """Sync a specific chat between local and Firebase."""
    if not is_user_logged_in():
        return jsonify({"error": "User not authenticated"}), 401
    user_id = get_user_id()
    success = FirebaseSyncManager.sync_local_to_firebase(user_id, chat_id)
    return jsonify({"success": success, "chat_id": chat_id}), 200


# ============================================================================
# 🚀 MAIN

# ============================================================================
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
    print("""
    ╔════════════════════════════════════════════════════════════════╗
    ║        VEXARA v4.1 - AI TUTOR WITH INTELLIGENT FALLBACK       ║
    ╠════════════════════════════════════════════════════════════════╣
    ║  ✓ Groq (Llama 3.1-8B / Llama 3.3-70B)                        ║
    ║  ✓ Cerebras (Llama 3.1-8B / Qwen-QVQ-32B)                     ║
    ║  ✓ Gemini (3.1-Flash-Lite)                                    ║
    ║  ✓ OpenRouter (Mistral / Gemma)                               ║
    ║                                                                ║
    ║  📊 Intelligent Fallback Chain                                 ║
    ║  🔄 Rate Limit Tracking                                        ║
    ║  📚 External Knowledge Base (Curriculum)                       ║
    ║  🎯 Auto Mode Detection                                        ║
    ║  🔐 Persistent Session Management (NEW)                        ║
    ║  📈 Daily Message Quota Per Google Account (NEW)               ║
    ╚════════════════════════════════════════════════════════════════╝
    """)

"""
VEXARA v4.1 - PRODUCTION RAG WITH INTELLIGENT FALLBACK SYSTEM
- External curriculum_chunks.json
- Weighted keyword scoring
- Intent pattern matching
- Negative keyword penalties
- Configurable retrieval thresholds
- Full support for SEE curriculum structure
- INTEGRATED: Cerebras API as primary fallback
- FIXED: Gemini API with proper error handling
- IMPROVED: Intelligent API selection and rate limit handling
- ENHANCED: Persistent Session Management for Google/Microsoft Login (NEW)
- ENHANCED: Daily Message Limit Tracking Per Google Account (NEW)
"""

import json
import base64
import requests
import time
import uuid
import os
import re
from io import BytesIO
from PIL import Image
from flask import Flask, render_template, request, jsonify, redirect, session, url_for, make_response
from flask_dance.contrib.google import make_google_blueprint, google
from authlib.integrations.flask_client import OAuth
from flask import send_from_directory, send_file
from datetime import datetime, date, timedelta
from flask_cors import CORS
from collections import defaultdict
from difflib import SequenceMatcher
import math

# 🔥 FIREBASE IMPORTS
try:
    import firebase_admin
    from firebase_admin import db, credentials
    FIREBASE_AVAILABLE = True
    print("[FIREBASE] Firebase SDK imported successfully")
except ImportError:
    FIREBASE_AVAILABLE = False
    print("[FIREBASE] Warning: firebase-admin not installed. Install with: pip install firebase-admin")

current_dir = os.path.dirname(os.path.abspath(__file__))
template_path = os.path.join(current_dir, 'templates')
static_path = os.path.join(current_dir, 'static')

app_name = '__main__'
if '__app_id__' in globals():
    app_name = globals()['__app_id__']
app = Flask(app_name, template_folder=template_path, static_folder=static_path)

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
app.secret_key = os.environ.get("FLASK_SECRET_KEY", str(uuid.uuid4()))
# ============================================================================
# 🔐 PERSISTENT SESSION CONFIGURATION
# ============================================================================
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

# ============================================================================
# 🔑 API KEYS & CONFIGURATION
# ============================================================================

GOOGLE_GEMINI_API_KEY = os.environ.get("GOOGLE_GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")

# API Endpoints
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
CEREBRAS_API_URL = "https://api.cerebras.ai/v1/chat/completions"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# ============================================================================
# 🔥 FIREBASE CONFIGURATION & INITIALIZATION
# ============================================================================

FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS")

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
if __name__ == '__main__' or os.environ.get('FLASK_ENV') in ['production', 'development']:
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
    - Text-only images → Extract text + Deepthink (CHEAP: 2 API calls, both text-based)
    - Geometric/Diagrams → Direct vision solve (QUALITY: 1 API call, keeps visual context)
    - Mixed content → Direct vision solve (SAFE: preserves all information)
    
    COST ANALYSIS:
    - Text extraction (fast model): ~0.0001 per image
    - Deepthink (Llama-70B): ~0.0005 per response = Total ~0.0006
    - Direct vision solve (Gemini): ~0.0025 per image
    
    For text-only: Extract+Deepthink saves 75% vs direct solve
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
                "deepthink", system_msg, solving_messages, 
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
    def solve_with_deepthink(extracted_text, chat_history, rag_context=None, rag_subject=None, rag_chapter=None, rag_confidence=None):
        """
        Solve extracted text problem using deepthink model.
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
            print(f"[SOLVE_TEXT] Solving with deepthink model (text only)...")
            print(f"[SOLVE_TEXT] RAG Context: {rag_subject}/{rag_chapter if rag_context else 'None'}")
            response, provider, success = call_api_with_intelligent_fallback(
                "deepthink", solving_system, solving_messages, 
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
            
            return full_response, "deepthink"
        
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
                'deepthink': 'llama-3.3-70b-versatile',
                'vision': 'meta-llama/llama-4-scout-17b-16e-instruct'
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
                'deepthink': 'zai-glm-4.7',
                'vision': None
            },
            'supports_vision': False,
            'status': 'active'
        },
        'gemini': {
            'url': GEMINI_API_URL,
            'api_key': GOOGLE_GEMINI_API_KEY,
            'type': 'gemini',
            'models': {
                'normal': 'gemini-3.1-flash-lite',
                'deepthink': 'gemini-3.1-flash-lite',
                'vision': 'gemini-3.1-flash-lite'
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
                'deepthink': 'deepseek/deepseek-v4-flash:free',
                'vision': 'google/gemma-4-31b-it:free'
            },
            'supports_vision': True,
            'status': 'active'
        }
    }
    
    # Fallback chain: [primary, secondary, tertiary]
    FALLBACK_CHAIN = {
        'normal': ['groq', 'cerebras', 'openrouter', 'gemini'],
        'deepthink': ['groq', 'cerebras', 'openrouter', 'gemini'],
        'vision': ['gemini','groq', 'cerebras', 'openrouter']
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
            if prov['type'] == 'gemini':
                response = cls._call_gemini(prov['url'], api_key, system_prompt, messages, model, image_data,stream=False)
            else:
                response = cls._call_openai_compatible(prov['url'], api_key, model, messages, image_data, stream)
            
            if response:
                # Print error details if status is not 200
                if response.status_code != 200:
                    try:
                        error_detail = response.json()
                        print(f"[{provider.upper()}] Error {response.status_code}: {error_detail}")
                    except:
                        print(f"[{provider.upper()}] Error {response.status_code}: {response.text[:200]}")
                
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
            "max_tokens": 2048,
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
    def _call_gemini(cls, url, api_key, system_prompt, messages, model, image_data=None,stream=False):
        """Call Gemini API with proper format and optional image support."""
        # Convert messages to Gemini format
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
            
            # Image content (Gemini format)
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
        
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "temperature": 0.7,
                "topK": 40,
                "topP": 0.95,
                "maxOutputTokens": 2048,
            }
        }
        
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            print(f"[GEMINI] Response status: {response.status_code}")
            if response.status_code != 200:
                try:
                    error_detail = response.json()
                    print(f"[GEMINI] Error details: {error_detail}")
                except:
                    print(f"[GEMINI] Error body: {response.text[:500]}")
            return response
        except requests.exceptions.Timeout:
            print(f"[TIMEOUT] Gemini API timeout")
            return None
        except Exception as e:
            print(f"[API_ERROR] Gemini API call failed: {e}")
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

# ============================================================================
# 📊 USER MESSAGE QUOTA TRACKING WITH GOOGLE ACCOUNT PERSISTENCE
# ============================================================================

QUOTA_FILE = os.path.join(app.root_path, 'user_quotas.json')
DAILY_MESSAGE_LIMIT = 20

def load_user_quotas():
    """Load user quotas from persistent file."""
    if os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_user_quotas(quotas):
    """Save user quotas to persistent file."""
    try:
        with open(QUOTA_FILE, 'w', encoding='utf-8') as f:
            json.dump(quotas, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[QUOTA] Error saving quotas: {e}")

def get_daily_message_count(user_id):
    """Get daily message count for user."""
    today_str = date.today().isoformat()
    quotas = load_user_quotas()
    
    if user_id not in quotas:
        quotas[user_id] = {}
    
    if today_str not in quotas[user_id]:
        quotas[user_id][today_str] = 0
    
    return quotas[user_id][today_str]

def increment_daily_message_count(user_id):
    """Increment daily message count for user."""
    today_str = date.today().isoformat()
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
    print(f"[QUOTA] User {user_id}: {quotas[user_id][today_str]} messages today")

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
                "min_confidence_threshold": 0.15,
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

# ============================================================================
# 🔐 OAUTH CONFIGURATION
# ============================================================================

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

# ============================================================================
# 📝 PROMPT TEMPLATES
# ============================================================================

BASE_SYSTEM_PROMPT = """You are Vexara, a friendly and helpful AI tutor for SEE students in Nepal.

**YOUR PERSONALITY:**
- Be friendly, encouraging, and supportive
- Use simple language that Class 10 students understand
- Keep responses concise and clear
- For greetings: Respond warmly and briefly
- For non-educational questions: Politely redirect to educational topics

**IMPORTANT:**
Below you may find a "Retrieved Knowledge" section with specific formulas, examples, and exam tips.
Use THIS retrieved knowledge as your primary source for calculations and steps.
Do NOT invent formulas - use what is provided.

**SEE Focus:** Help students prepare for their SEE Mathematics exam.
"""

DEEPTHINK_BASE_PROMPT = """You are Vexara, an expert SEE exam tutor specializing in step-by-step problem solving.

**FORMAT FOR PROBLEMS:**
1. **Given:** List given information
2. **To Find:** State what needs to be calculated
3. **Formula/Concept:** Write the relevant formula
4. **Solution:** Show each step using ⇒ format
5. **Final Answer:** State clearly with units
6. **SEE Tip:** Include one exam tip from the retrieved knowledge

**IMPORTANT:**
Use retrieved knowledge (formulas, patterns, examples) as your source.
Do NOT invent formulas or make up information.
"""

def build_enhanced_prompt(question, chat_history, mode="normal"):
    """Build system prompt with RAG context injection."""
    # Retrieve relevant chunks
    subject, chapter, context, confidence, num_chunks = KNOWLEDGE_BASE.retrieve(question)
    
    # Build base prompt
    if mode == "deepthink":
        base_prompt = DEEPTHINK_BASE_PROMPT
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
    
    return enhanced_prompt

# ============================================================================
# 🧠 MODE DETECTION
# ============================================================================

def should_use_deepthink(question):
    """Determine if question needs deepthink/solve mode."""
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
    """Load chat history from file."""
    file_path = get_chat_file_path(user_id, chat_id)
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            print(f"Error loading chat: {e}")
            return []
    return []

def save_chat_history_to_file(user_id, chat_id, chat_data):
    """Save chat history to file."""
    file_path = get_chat_file_path(user_id, chat_id)
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(chat_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving chat: {e}")

# ============================================================================
# 🔥 FIREBASE CHAT STORAGE FUNCTIONS
# ============================================================================

def save_message_to_firebase(user_id, chat_id, message_data):
    """
    Save a single message to Firebase Realtime Database.
    
    Args:
        user_id: User's unique ID (from session)
        chat_id: Chat session ID
        message_data: Dict with keys: type, text, timestamp
    """
    if not FIREBASE_AVAILABLE:
        return False
    
    try:
        # Sanitize user_id for Firebase path
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('-', '_')).strip()
        
        # Build Firebase path
        path = f"users/{safe_user_id}/chats/{chat_id}/messages"
        
        # Generate a unique message ID
        msg_id = str(uuid.uuid4()).replace('-', '')[:16]
        
        # Prepare data for Firebase
        firebase_message = {
            "type": message_data.get("type"),
            "text": message_data.get("text"),
            "timestamp": int(message_data.get("timestamp", time.time()))
        }
        
        # Save to Firebase
        ref = db.reference(path)
        ref.child(msg_id).set(firebase_message)
        
        print(f"[FIREBASE] ✓ Saved message to {path}/{msg_id}")
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
    
    # Check quota
    current_count = get_daily_message_count(user_id)
    if current_count >= DAILY_MESSAGE_LIMIT:
        remaining = get_remaining_messages(user_id)
        return jsonify({"response": f"Daily limit ({DAILY_MESSAGE_LIMIT}) reached. You have {remaining} messages left. Please try again tomorrow."}), 429
    
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
            print(f"[GEMINI_FIRST] Starting image processing with Gemini primary...")
            yield "🔍 Analyzing image with vision model...\n"
            
            full_response = None
            question_text = None
            rag_context = None
            rag_chapter = None
            rag_subject = None
            rag_confidence = None
            provider_used = None
            
            # ================== STEP 1: SOLVE DIRECTLY WITH GEMINI PRIMARY ==================
            # Build solving prompt that includes user's message + image
            solving_prompt = f"""Solve this math problem from the image.

User's question: {caption if caption else "Solve the math problem in the image"}

Provide a complete solution with:
1. **Problem Statement:** Clearly state what the problem is asking (from image and user question)
2. **Given:** Information from the image/message
3. **To Find:** What needs to be calculated
4. **Solution:** Step-by-step with explanations
5. **Answer:** Final result with units
6. **SEE Tip:** Exam preparation tip for this type of problem

IMPORTANT: 
- If the image contains text, extract it accurately
- If there are diagrams, analyze them carefully
- Use the user's question to understand exactly what they're asking
- For geometric problems, preserve all angle/measurement information
- For text-only problems, solve step-by-step using correct formulas"""
            
            solving_messages = [{"role": "user", "content": solving_prompt}]
            system_msg = """You are Vexara, an expert SEE Math tutor specializing in solving problems from images.

IMPORTANT:
- Analyze images carefully for both text and diagrams
- Listen to the user's question/caption to understand what they're asking
- Extract questions accurately from both the image AND the user's message
- Preserve all mathematical notation and symbols
- Provide step-by-step solutions following SEE exam format
- Use clear formatting with sections for Problem Statement, Given, To Find, Solution, Answer, and Tips
- For any image type (text-only, geometric, mixed) combined with user's question, provide a complete solution"""
            
            print(f"[GEMINI_FIRST] Step 1: Solving with Gemini (PRIMARY via intelligent fallback)...")
            response, provider_used, success = call_api_with_intelligent_fallback(
                "deepthink", system_msg, solving_messages, 
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
                                        chunk = delta['content']
                                        full_response += chunk
                                        yield chunk
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
                                    yield full_response
                except Exception as e:
                    print(f"[GEMINI_FIRST] Error parsing response: {e}")
                    yield f"❌ Error processing response: {str(e)}\n"
                    return
            
            if not full_response or not full_response.strip():
                print(f"[GEMINI_FIRST] No response content generated")
                yield "❌ No response generated. Please try again."
                return
            
            # ================== STEP 2: EXTRACT QUESTION FOR RAG ==================
            # Best-effort extraction for curriculum context
            print(f"[GEMINI_FIRST] Step 2: Extracting question for curriculum context...")
            
            # Simple heuristic: look for "Problem Statement:" or use caption
            try:
                if "Problem Statement:" in full_response:
                    question_text = full_response.split("Problem Statement:")[1].split("\n")[0].strip()
                elif "problem:" in full_response.lower():
                    parts = full_response.lower().split("problem:")
                    if len(parts) > 1:
                        question_text = parts[1].split("\n")[0].strip()[:100]
                    else:
                        question_text = caption if caption else "math problem"
                else:
                    question_text = caption if caption else "math problem"
            except:
                question_text = caption if caption else "math problem"
            
            print(f"[GEMINI_FIRST] Extracted question: '{question_text[:80]}'")
            
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
    
    has_previous_chats = False
    try:
        for filename in os.listdir(CHAT_HISTORY_DIR):
            if filename.startswith(f"{user_id}_") and filename.endswith(".json") and filename != f"{user_id}_{new_chat_id}.json":
                has_previous_chats = True
                break
    except:
        pass
    
    return jsonify({"status": "success", "chat_id": new_chat_id, "has_previous_chats": has_previous_chats})

@app.route('/clear_all_chats', methods=['POST'])
def clear_all_chats_endpoint():
    """Clear all chats for user."""
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
    """Get list of all user chats."""
    user_id = get_user_id()
    chat_summaries = []
    
    try:
        user_chat_files = [f for f in os.listdir(CHAT_HISTORY_DIR) if f.startswith(f"{user_id}_") and f.endswith(".json")]
        user_chat_files.sort(key=lambda f: os.path.getmtime(os.path.join(CHAT_HISTORY_DIR, f)), reverse=True)
        
        for filename in user_chat_files:
            chat_id = filename.replace(f"{user_id}_", "").replace(".json", "")
            chat_data = load_chat_history_from_file(user_id, chat_id)
            
            display_title = "New Chat"
            if chat_data:
                first_user_msg = next((msg for msg in chat_data if msg['type'] == 'user' and msg['text'].strip()), None)
                if first_user_msg:
                    display_title = first_user_msg['text'].split('\n')[0][:30]
                    if len(display_title) > 30:
                        display_title += "..."
            
            chat_summaries.append({'id': chat_id, 'title': display_title})
    except Exception as e:
        print(f"Error getting chat list: {e}")
    
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
    """Main Q&A endpoint with external KB retrieval and intelligent fallback."""
    user_id = get_user_id()
    chat_id = request.form.get('chat_id')
    instruction = request.form.get('instruction', '').strip()
    model_choice = request.form.get('model_choice', 'auto')
    
    if not chat_id:
        return jsonify({"error": "Chat ID not provided."}), 400
    if not instruction:
        return jsonify({"error": "No instruction provided."}), 400
    
    # Auto-detect mode
    if model_choice == 'auto':
        mode = "deepthink" if should_use_deepthink(instruction) else "normal"
        print(f"[MODE] Auto-detected: {mode}")
    else:
        mode = model_choice
    
    # Check quota
    current_count = get_daily_message_count(user_id)
    if current_count >= DAILY_MESSAGE_LIMIT:
        remaining = get_remaining_messages(user_id)
        return jsonify({"response": f"Daily limit ({DAILY_MESSAGE_LIMIT}) reached. {remaining} messages left. Try again tomorrow."}), 429
    
    # Load and trim chat history
    full_history = load_chat_history_from_file(user_id, chat_id)
    trimmed_history = trim_chat_history(full_history)
    
    # Save user message
    user_msg_obj = {"type": "user", "text": instruction, "timestamp": time.time()}
    trimmed_history.append(user_msg_obj)
    save_chat_history_to_file(user_id, chat_id, trimmed_history)
    # Save to Firebase
    if FIREBASE_AVAILABLE:
        save_message_to_firebase(user_id, chat_id, user_msg_obj)
    
    # Increment quota
    increment_daily_message_count(user_id)
    
    # Build enhanced prompt with retrieved chunks
    system_prompt = build_enhanced_prompt(instruction, trimmed_history[:-1], mode)
    
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
            response, provider_used, success = call_api_with_intelligent_fallback(mode, system_prompt, messages)
            
            if not response or not success:
                yield "I'm experiencing connection issues with all providers. Please try again in a moment."
                return
            
            print(f"[RESPONSE] Using provider: {provider_used}")
            
            # Handle streaming vs non-streaming responses
            if response.headers.get('content-type', '').startswith('text/event-stream'):
                # Streaming response (OpenAI-compatible)
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
            else:
                # Non-streaming response (Gemini)
                try:
                    data = response.json()
                    if 'candidates' in data and data['candidates']:
                        candidate = data['candidates'][0]
                        if 'content' in candidate and 'parts' in candidate['content']:
                            for part in candidate['content']['parts']:
                                if 'text' in part:
                                    full_response = part['text']
                                    words = full_response.split()
                                    chunk_buffer = ""
                                    for word in words:
                                        chunk_buffer += word + " "
                                        if len(chunk_buffer) > 50:
                                            yield chunk_buffer
                                            chunk_buffer = ""
                                    if chunk_buffer:
                                        yield chunk_buffer
                except json.JSONDecodeError:
                    yield "Error parsing response. Please try again."
                    return
            
            if not full_response:
                yield "I couldn't generate a response. Please try again."
                return
            
            # Save bot response
            bot_msg_obj = {"type": "bot", "text": full_response, "timestamp": time.time()}
            trimmed_history.append(bot_msg_obj)
            save_chat_history_to_file(user_id, chat_id, trimmed_history)
            # Save to Firebase
            if FIREBASE_AVAILABLE:
                save_message_to_firebase(user_id, chat_id, bot_msg_obj)
            
        except Exception as e:
            print(f"Error in /ask: {e}")
            import traceback
            traceback.print_exc()
            yield f"An error occurred: {str(e)}"
    
    return app.response_class(generate_response(), mimetype='text/event-stream')

# ============================================================================
# 💬 CHAT ENDPOINT
# ============================================================================

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
    """Get current user information."""
    if not is_user_logged_in():
        return jsonify({"error": "Not authenticated"}), 401
    
    user_email = session.get('user', None)
    user_id = session.get('user_id')
    remaining_messages = get_remaining_messages(user_id)
    current_count = get_daily_message_count(user_id)
    
    return jsonify({
        "user_email": user_email,
        "user_id": user_id,
        "is_guest": session.get('is_guest', False),
        "auth_provider": session.get('auth_provider', 'default'),
        "messages_used": current_count,
        "messages_remaining": remaining_messages,
        "daily_limit": DAILY_MESSAGE_LIMIT
    })

@app.route('/google_login/authorized')
def google_login_authorized():
    """Handle Google OAuth callback - creates persistent session with quota tracking."""
    if not google.authorized:
        return redirect(url_for("login"))
    
    try:
        user_info = google.get("/oauth2/v2/userinfo")
        if user_info.ok:
            user_data = user_info.json()
            user_email = user_data.get("email")
            google_id = user_data.get('id')
            
            # Create persistent user ID based on Google account
            persistent_user_id = f"google_{google_id}"
            
            session.permanent = True
            session['user'] = user_email
            session['user_id'] = persistent_user_id
            session['auth_provider'] = 'google'
            session['user_name'] = user_data.get("name")
            session['google_id'] = google_id
            
            print(f"[AUTH] Google login successful for {user_email}")
            print(f"[AUTH] Persistent User ID: {persistent_user_id}")
            print(f"[QUOTA] Messages today: {get_daily_message_count(persistent_user_id)}/{DAILY_MESSAGE_LIMIT}")
            
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
        
        # Create persistent user ID based on Microsoft account
        persistent_user_id = f"microsoft_{microsoft_id}"
        
        session.permanent = True
        session['user'] = user_email
        session['user_id'] = persistent_user_id
        session['auth_provider'] = 'microsoft'
        session['user_name'] = user_data.get("displayName")
        session['microsoft_id'] = microsoft_id
        
        print(f"[AUTH] Microsoft login successful for {user_email}")
        print(f"[AUTH] Persistent User ID: {persistent_user_id}")
        print(f"[QUOTA] Messages today: {get_daily_message_count(persistent_user_id)}/{DAILY_MESSAGE_LIMIT}")
        
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
# 🔧 DEBUG ENDPOINTS
# ============================================================================

@app.route('/debug/kb_status', methods=['GET'])
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
def debug_search():
    """Test KB search with a query."""
    data = request.get_json()
    query = data.get('query', '')
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
def debug_quotas():
    """Get all user quotas (debug only)."""
    quotas = load_user_quotas()
    return jsonify({
        "total_users": len(quotas),
        "daily_limit": DAILY_MESSAGE_LIMIT,
        "sample_quotas": {k: v for k, v in list(quotas.items())[:5]}
    })

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
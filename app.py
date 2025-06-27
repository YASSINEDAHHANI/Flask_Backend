import os
from datetime import datetime, timezone
from functools import wraps
import uuid
import json
from bson import ObjectId
import langdetect
import anthropic
import PyPDF2
import docx
from flask import Flask, request, jsonify, session, Response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pymongo import MongoClient
from dotenv import load_dotenv
from cryptography.fernet import Fernet
import base64
from admin import admin_bp
from manager import manager_bp
import re
import io
from io import BytesIO
from flask import send_file
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
import admin
import manager
from datetime import datetime, timezone
import traceback
import anthropic
import json
from datetime import datetime, timezone
import os

try:
    from local_rag_system import LocalRAGSystem
    LOCAL_RAG_AVAILABLE = True
    print("✅ Local RAG System imported successfully")
except ImportError as e:
    LOCAL_RAG_AVAILABLE = False
    print(f"⚠️ Local RAG System not available: {e}")

load_dotenv()
print(f"DEBUG - RAG_OLLAMA_MODEL from env: {os.getenv('RAG_OLLAMA_MODEL', 'NOT_SET')}")


class MongoJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

app = Flask(__name__)
app.register_blueprint(admin_bp)
app.register_blueprint(manager_bp)
# Configure Flask to use our custom JSON encoder
app.json_encoder = MongoJSONEncoder
app.secret_key = os.getenv("SECRET_KEY", "supersecret")

# Session configuration
app.config.update(
    SESSION_COOKIE_NAME="flask_session",
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=86400,
    SESSION_REFRESH_EACH_REQUEST=True
)

# CORS configuration
cors = CORS(app)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["30 per minute"]
)

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["chat_app"]
history_collection = db["chat_history"]
users_collection = db["users"]
projects_collection = db["projects"]
requirements_collection = db["requirements"]
versions_collection = db["versions"]
collaborators_collection = db["collaborators"]
api_keys_collection = db["api_keys"]
user_settings_collection = db["user_settings"]
user_settings_collection.create_index([("user", 1)])
user_settings_collection.create_index([("user", 1), ("project_id", 1)])
# Initialize collections for admin and manager modules
admin.users_collection = users_collection
admin.projects_collection = projects_collection
admin.collaborators_collection = collaborators_collection
admin.api_keys_collection = api_keys_collection

manager.users_collection = users_collection
manager.projects_collection = projects_collection
manager.collaborators_collection = collaborators_collection
manager.requirements_collection = requirements_collection
manager.api_keys_collection = api_keys_collection

# Global variable for Local RAG System
local_rag_system = None

def initialize_local_rag():
    """Initialize the Local RAG System if available and configured"""
    global local_rag_system
    
    if not LOCAL_RAG_AVAILABLE:
        return False
    
    try:
        # Get RAG configuration from environment or use defaults
        PDF_FOLDER = os.getenv("RAG_PDF_FOLDER", r"C:\Users\dahan\Documents\Stage PFE DXC cdg\llm_rag\rag\rag")
        PERSIST_DIR = os.getenv("RAG_PERSIST_DIR", "./chroma_db_local")
        OLLAMA_MODEL = "qwen3:8b"
        OLLAMA_BASE_URL = os.getenv("RAG_OLLAMA_BASE_URL", "http://35.173.131.200:11434")
        
        print(f"🤖 Initializing Local RAG System...")
        print(f"📁 PDF Folder: {PDF_FOLDER}")
        print(f"💾 Persist Directory: {PERSIST_DIR}")
        print(f"🔗 Ollama URL: {OLLAMA_BASE_URL}")
        print(f"🧠 Model: {OLLAMA_MODEL}")
        
        # Check if PDF folder exists
        if not os.path.exists(PDF_FOLDER):
            print(f"⚠️ PDF folder not found: {PDF_FOLDER}")
            return False
        
        local_rag_system = LocalRAGSystem(
            pdf_folder=PDF_FOLDER,
            persist_directory=PERSIST_DIR,
            ollama_model=OLLAMA_MODEL
        )
        
        # Check if documents need to be processed
        if not os.path.exists(PERSIST_DIR):
            print("📝 Processing documents for the first time...")
            if local_rag_system.process_documents(recursive=True):
                print("✅ Local RAG System initialized successfully!")
            else:
                print("❌ Failed to process documents")
                local_rag_system = None
                return False
        else:
            print("✅ Local RAG System initialized with existing database!")
        
        return True
        
    except Exception as e:
        print(f"❌ Failed to initialize Local RAG System: {e}")
        local_rag_system = None
        return False

# Initialize Local RAG on startup
if LOCAL_RAG_AVAILABLE:
    initialize_local_rag()

# Create indexes
history_collection.create_index([("user", 1)])
history_collection.create_index([("timestamp", -1)])
projects_collection.create_index([("user", 1)])
requirements_collection.create_index([("project_id", 1)])
requirements_collection.create_index([("user", 1)])
versions_collection.create_index([("requirement_id", 1)])
versions_collection.create_index([("timestamp", -1)])
collaborators_collection.create_index([("project_id", 1)])
collaborators_collection.create_index([("email", 1)])
api_keys_collection.create_index([("user", 1)])

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated_function

def is_admin(username):
    """Check if a user has admin role"""
    user = users_collection.find_one({"username": username})
    return user and user.get("role") == "admin"


#Manager Profile
def is_manager_or_admin(username):
    """Check if a user has manager or admin role"""
    user = users_collection.find_one({"username": username})
    return user and user.get("role") in ["manager", "admin"]

def can_create_projects(username):
    """Check if a user can create projects (manager or admin only)"""
    return is_manager_or_admin(username)

def manager_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        
        if not is_manager_or_admin(session["user"]):
            return jsonify({"error": "Manager or admin access required"}), 403
            
        return f(*args, **kwargs)
    return decorated_function

def get_user_api_key(username, project_id=None):
    """Get a user's API key, with optional project-specific override."""
    # First try to get a project-specific key if project_id is provided
    if project_id:
        project_key = api_keys_collection.find_one({
            "user": username,
            "project_id": project_id
        })
        if project_key and project_key.get("api_key"):
            return project_key["api_key"]  # CHANGED: No decryption
    
    # If no project key, try to get the user's default key
    user_key = api_keys_collection.find_one({
        "user": username,
        "project_id": {"$exists": False}
    })
    if user_key and user_key.get("api_key"):
        return user_key["api_key"]  # CHANGED: No decryption
    
    # No fallback to environment variable anymore
    return None

def get_anthropic_client(username, project_id=None):
    """Get an Anthropic client using the appropriate API key."""
    api_key = get_user_api_key(username, project_id)
    if not api_key:
        raise ValueError("No API key available")
    return anthropic.Anthropic(api_key=api_key)

def generate_test_cases_with_local_rag(requirements, context=""):
    """Generate test cases using the local RAG system"""
    global local_rag_system
    
    if not local_rag_system:
        raise ValueError("Local RAG system not available")
    
    try:
        # Create a comprehensive requirement for test case generation
        full_requirement = f"{requirements}"
        if context:
            full_requirement = f"Context: {context}\n\nRequirement: {requirements}"
        
        print(f"🤖 Generating test cases with Local RAG...")
        test_cases = local_rag_system.generate_test_cases(full_requirement, context)
        
        return test_cases
        
    except Exception as e:
        print(f"Error generating test cases with local RAG: {e}")
        raise

def check_rag_availability():
    """Check if local RAG system is available and properly initialized"""
    global local_rag_system
    return LOCAL_RAG_AVAILABLE and local_rag_system is not None

def detect_priority(description):
    if not description:
        return "medium"
    
    description_lower = description.lower()
    
    high_keywords = [
        "security", "authentication", "authorization", "critical", "urgent", "payment", 
        "billing", "data protection", "privacy", "compliance", "regulation", "gdpr",
        "safety", "emergency", "backup", "recovery", "must", "required", "essential",
        "login", "access control", "encryption", "vulnerability", "exploit"
    ]
    
    low_keywords = [
        "nice to have", "optional", "cosmetic", "ui enhancement", "color", "font",
        "styling", "aesthetic", "minor improvement", "suggestion", "preference",
        "nice-to-have", "future enhancement", "wishlist", "optional feature"
    ]
    
    high_count = sum(1 for keyword in high_keywords if keyword in description_lower)
    low_count = sum(1 for keyword in low_keywords if keyword in description_lower)
    
    if high_count > low_count and high_count >= 1:
        return "high"
    elif low_count > high_count and low_count >= 1:
        return "low"
    else:
        return "medium"

def get_priority_label(priority):
    priority_labels = {
        "low": "Faible",
        "medium": "Moyenne", 
        "high": "Élevée"
    }
    return priority_labels.get(priority, priority)

def get_category_label(category):
    category_labels = {
        "functionality": "Fonctionnalité",
        "performance": "Performance",
        "security": "Sécurité",
        "usability": "Utilisabilité",
        "compatibility": "Compatibilité"
    }
    return category_labels.get(category, category)

def get_status_label(status):
    status_labels = {
        "draft": "Brouillon",
        "in_review": "En révision",
        "approved": "Approuvé",
        "implemented": "Implémenté",
        "rejected": "Rejeté"
    }
    return status_labels.get(status, status)

def generate_test_case_prompt(requirements, format_type="default", context="", example_case=""):
    example_format_default = """Test Case 1: Valid Login
Description: User logs in with valid credentials
Preconditions: User has a valid account
Steps:
    1. Navigate to the login page.
    2. Enter valid email and password.
    3. Click on "Login".
Expected Result: User is successfully logged in and redirected to the dashboard.

Test Case 2: Invalid Login
Description: User attempts to log in with invalid credentials
Preconditions: User is on the login page
Steps:
    1. Access the login page.
    2. Enter valid email and invalid password.
    3. Click on "Login".
Expected Result: An error message is displayed, and the user remains on the login page.
"""

    if format_type == "custom" and example_case.strip():
        example_format = example_case
    elif format_type == "gherkin":
        example_format = example_case if example_case.strip() else "Gherkin format"
    else:
        example_format = example_format_default

    instruction = f"""
Generate test cases for the following requirement using the specified format.
{"Functional context: " + context if context else ""} 
Requirement: {requirements}
Format:
{example_format}
"""
    return instruction

def extract_text_from_pdf(file):
    """Extract text from a PDF file"""
    try:
        reader = PyPDF2.PdfReader(file)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text.strip()
    except Exception as e:
        print(f"Error extracting text from PDF: {e}")
        raise

def extract_text_from_docx(file):
    """Extract text from a DOCX file"""
    try:
        doc = docx.Document(file)
        text = ""
        for paragraph in doc.paragraphs:
            text += paragraph.text + "\n"
        return text.strip()
    except Exception as e:
        print(f"Error extracting text from DOCX: {e}")
        raise

@app.route("/check_ai_services", methods=["GET"])
@login_required
def check_ai_services_enhanced():
    """Enhanced check for available AI services based on user settings"""
    username = session["user"]
    project_id = request.args.get("project_id")
    
    try:
        # Get user settings
        settings = get_user_settings(username, project_id)
        
        # Check service availability
        claude_available = bool(get_user_api_key_from_settings(username, project_id, settings))
        local_rag_available = check_rag_availability()
        effective_service = get_effective_llm_service(username, project_id)
        
        services = {
            "claude_available": claude_available,
            "local_rag_available": local_rag_available,
            "effective_service": effective_service,
            "user_preference": settings.get("llm_service", "auto") if settings else "auto",
            "has_user_settings": bool(settings and settings.get("llm_service")),
            "project_specific": bool(project_id and settings and settings.get("project_id"))
        }
        
        return jsonify(services)
        
    except Exception as e:
        print(f"Error checking AI services: {e}")
        return jsonify({"error": str(e)}), 500

# Authentication routes
@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username")
    password = data.get("password")

    user = users_collection.find_one({"username": username, "password": password})
    if user:
        session["user"] = username
        session.permanent = True
        return jsonify({
            "message": "Login successful", 
            "username": username,
            "email": user.get("email") or username,
            "role": user.get("role", "user"),
            "is_admin": user.get("role") == "admin",
            "is_manager": user.get("role") in ["manager", "admin"],
            "can_create_projects": user.get("role") in ["manager", "admin"]
        })
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return jsonify({"message": "Logged out successfully"})

@app.route("/check_session", methods=["GET", "OPTIONS"])
@limiter.exempt
def check_session():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    if "user" in session:
        user = users_collection.find_one({"username": session["user"]})
        if user:
            return jsonify({
                "logged_in": True,
                "username": session["user"],
                "email": user.get("email") or session["user"],
                "role": user.get("role", "user"),
                "is_admin": user.get("role") == "admin",
                "is_manager": user.get("role") in ["manager", "admin"],
                "can_create_projects": user.get("role") in ["manager", "admin"]
            }), 200
    return jsonify({"logged_in": False, "error": "Not authenticated"}), 401

# API Key Management
@app.route("/get_api_key", methods=["GET"])
@login_required
def get_api_key_for_frontend():
    username = session["user"]
    project_id = request.args.get("project_id")
    
    try:
        api_key = get_user_api_key(username, project_id)
        
        if not api_key:
            return jsonify({"error": "No API key configured. Please add your Claude API key in Settings."}), 404
            
        return jsonify({"api_key": api_key})
    except Exception as e:
        print(f"Error getting API key: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api_keys", methods=["POST"])
@login_required
def create_api_key():
    username = session["user"]
    data = request.json
    api_key = data.get("api_key")
    project_id = data.get("project_id")
    
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    
    # Store the API key directly (no encryption)
    stored_key = api_key  # CHANGED: No encryption
    
    query = {"user": username}
    if project_id:
        query["project_id"] = project_id
    else:
        query["project_id"] = {"$exists": False}
    
    existing_key = api_keys_collection.find_one(query)
    
    if existing_key:
        api_keys_collection.update_one(
            {"_id": existing_key["_id"]},
            {"$set": {"api_key": stored_key}}
        )
    else:
        key_data = {
            "user": username,
            "api_key": stored_key,
            "created_at": datetime.now(timezone.utc)
        }
        if project_id:
            key_data["project_id"] = project_id
        
        api_keys_collection.insert_one(key_data)
    
    return jsonify({"message": "API key saved successfully"})

@app.route("/api_keys/<key_id>", methods=["DELETE"])
@login_required
def delete_api_key(key_id):
    username = session["user"]
    
    try:
        object_id = ObjectId(key_id)
    except:
        return jsonify({"error": "Invalid key ID"}), 400
    
    result = api_keys_collection.delete_one({
        "_id": object_id,
        "user": username
    })
    
    if result.deleted_count == 0:
        return jsonify({"error": "Key not found or not authorized"}), 404
    
    return jsonify({"message": "API key deleted successfully"})

# Main test case generation endpoint with fallback to local RAG
@app.route("/generate_test_cases", methods=["POST", "OPTIONS"])
def generate_test_cases_endpoint_enhanced():
    """Enhanced test case generation using user settings"""
    if request.method == "OPTIONS":
        return "", 200
    
    # Check login
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        data = request.json
        requirements = data.get("requirements", "")
        format_type = data.get("format_type", "default")
        context = data.get("context", "")
        example_case = data.get("example_case", "")
        project_id = data.get("project_id", "")
        requirement_id = data.get("requirement_id", "")
        requirement_title = data.get("requirement_title", "")
        
        # Remove manual ai_service selection - use user settings instead
        
        if not requirements:
            return jsonify({"error": "No requirements provided"}), 400
        
        username = session["user"]
        
        print(f"Generating test cases for user: {username}")
        print(f"Project ID: {project_id}")
        
        # Determine which AI service to use based on user settings
        effective_service = get_effective_llm_service(username, project_id)
        
        if not effective_service:
            claude_available = bool(get_user_api_key_from_settings(username, project_id))
            local_rag_available = check_rag_availability()
            
            if not claude_available and not local_rag_available:
                return jsonify({
                    "error": "No AI service available.",
                    "suggestion": "Please configure your Claude API key in Settings or ensure Local RAG is running.",
                    "details": {
                        "claude_available": False,
                        "local_rag_available": False,
                        "action_required": "Configure API key or Local RAG"
                    }
                }), 400
            elif not claude_available:
                return jsonify({
                    "error": "Claude API not available.",
                    "suggestion": "Please add your Claude API key in Settings to use Claude AI.",
                    "details": {
                        "claude_available": False,
                        "local_rag_available": local_rag_available,
                        "action_required": "Add Claude API key"
                    }
                }), 400
            else:
                return jsonify({
                    "error": "No AI service available.",
                    "suggestion": "Please check your AI service configuration in Settings."
                }), 400
        
        print(f"Using AI Service: {effective_service}")
        print(f"Requirements: {requirements[:50]}...")
        
        # Generate test cases based on effective service
        if effective_service == "claude":
            try:
                api_key = get_user_api_key_from_settings(username, project_id)
                if not api_key:
                    raise ValueError("No Claude API key available")
                
                # Create Anthropic client
                anthropic_client = anthropic.Anthropic(api_key=api_key)
                
                # Generate the test case prompt
                test_case_instruction = generate_test_case_prompt(requirements, format_type, context, example_case)
                
                # Call Claude API
                response = anthropic_client.messages.create(
                    model="claude-3-haiku-20240307",
                    max_tokens=2000,
                    messages=[{"role": "user", "content": test_case_instruction}]
                )
                
                test_cases = response.content[0].text
                
                # Save to history
                history_entry = {
                    "user": username,
                    "project_id": project_id,
                    "requirement_id": requirement_id,
                    "requirement_title": requirement_title,
                    "requirements": requirements,
                    "test_cases": test_cases,
                    "format_type": format_type,
                    "context": context,
                    "example_case": example_case,
                    "ai_service": "claude",
                    "timestamp": datetime.now(timezone.utc)
                }
                
                history_collection.insert_one(history_entry)
                
                return jsonify({
                    "test_cases": test_cases,
                    "service_used": "claude",
                    "status": "success"
                })
                
            except Exception as e:
                print(f"Claude API error: {e}")
                # Don't fallback if user explicitly chose Claude
                return jsonify({
                    "error": f"Claude API error: {str(e)}",
                    "service_used": "claude",
                    "status": "error"
                }), 500
        
        elif effective_service == "local_rag":
            try:
                if not check_rag_availability():
                    raise ValueError("Local RAG system not available")
                
                # Generate test cases using local RAG
                test_cases = local_rag_system.generate_test_cases(requirements)
                
                # Save to history
                history_entry = {
                    "user": username,
                    "project_id": project_id,
                    "requirement_id": requirement_id,
                    "requirement_title": requirement_title,
                    "requirements": requirements,
                    "test_cases": test_cases,
                    "format_type": format_type,
                    "context": context,
                    "example_case": example_case,
                    "ai_service": "local_rag",
                    "timestamp": datetime.now(timezone.utc)
                }
                
                history_collection.insert_one(history_entry)
                
                return jsonify({
                    "test_cases": test_cases,
                    "service_used": "local_rag",
                    "status": "success"
                })
                
            except Exception as e:
                print(f"Local RAG error: {e}")
                return jsonify({
                    "error": f"Local RAG error: {str(e)}",
                    "service_used": "local_rag",
                    "status": "error"
                }), 500
        
        else:
            return jsonify({
                "error": "No AI service configured",
                "suggestion": "Please configure your AI service preferences in Settings"
            }), 400
        
    except Exception as e:
        print(f"Error in test case generation: {e}")
        return jsonify({"error": str(e)}), 500
@app.route("/check_api_key_status", methods=["GET"])
@login_required
def check_api_key_status():
    """Check if user has any API keys configured"""
    username = session["user"]
    project_id = request.args.get("project_id")
    
    try:
        # Check new settings system
        settings_api_key = get_user_api_key_from_settings(username, project_id)
        
        # Check old system
        legacy_api_key = get_user_api_key(username, project_id)
        
        status = {
            "has_settings_api_key": bool(settings_api_key),
            "has_legacy_api_key": bool(legacy_api_key),
            "has_any_api_key": bool(settings_api_key or legacy_api_key),
            "recommended_action": None
        }
        
        if not status["has_any_api_key"]:
            status["recommended_action"] = "add_api_key"
        elif status["has_legacy_api_key"] and not status["has_settings_api_key"]:
            status["recommended_action"] = "migrate_to_settings"
        else:
            status["recommended_action"] = "none"
        
        return jsonify(status)
        
    except Exception as e:
        print(f"Error checking API key status: {e}")
        return jsonify({"error": str(e)}), 500
@app.route("/generate_test_cases_stream", methods=["POST"])
@login_required
@limiter.limit("5 per minute")
def generate_test_cases_stream():
    data = request.json
    requirements = data.get("requirements", "")
    format_type = data.get("format_type", "default")
    context = data.get("context", "")
    example_case = data.get("example_case", "")
    project_id = data.get("project_id", "")
    
    if not requirements:
        return jsonify({"error": "No requirements provided"}), 400
    
    test_case_instruction = generate_test_case_prompt(requirements, format_type, context, example_case)
    username = session["user"]
    
    def generate():
        try:
            full_response = ""
            anthropic_client = get_anthropic_client(username, project_id)
            
            with anthropic_client.messages.stream(
                model="claude-3-haiku-20240307",
                max_tokens=4000,
                messages=[{"role": "user", "content": test_case_instruction}]
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.text:
                            full_response += event.delta.text
                            yield f"data: {json.dumps({'chunk': event.delta.text})}\n\n"
                            
            # Save to history after completion
            history_data = {
                "user": username,
                "test_cases": full_response,
                "timestamp": datetime.now(timezone.utc),
                "requirements": requirements,
                "context": context,
                "project_id": project_id
            }
            history_collection.insert_one(history_data)
            
            yield f"data: {json.dumps({'complete': True})}\n\n"
            
        except Exception as e:
            print(f"Streaming error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), content_type="text/event-stream")

@app.route("/save_test_cases", methods=["POST"])
@login_required
def save_test_cases():
    data = request.json
    test_cases = data.get("test_cases", "")
    requirements = data.get("requirements", "")
    project_id = data.get("project_id", "")
    requirement_id = data.get("requirement_id", "")
    requirement_title = data.get("requirement_title", "")
    
    username = session["user"]
    
    # Create a timestamp for the current update
    current_time = datetime.now(timezone.utc)
    
    # Prepare the history data
    history_data = {
        "user": username,
        "test_cases": test_cases,
        "timestamp": current_time,
        "requirements": requirements,
        "context": "",
        "project_id": project_id,
        "update_type": "manual_edit"
    }
    
    if requirement_id:
        history_data["requirement_id"] = requirement_id
    if requirement_title:
        history_data["requirement_title"] = requirement_title
    
    # Insert the new history entry
    history_collection.insert_one(history_data)
    
    return jsonify({
        "message": "Test cases saved successfully",
        "timestamp": current_time.isoformat()
    })

@app.route("/history", methods=["GET"])
@login_required
def get_history():
    username = session["user"]
    project_id = request.args.get("project_id")
    
    query = {"user": username}
    if project_id:
        query["project_id"] = project_id
    
    history = list(history_collection.find(query).sort("timestamp", -1).limit(50))
    
    for item in history:
        item["_id"] = str(item["_id"])
    
    return jsonify({"history": history})

@app.route("/history/<history_id>", methods=["GET"])
@login_required
def get_history_item(history_id):
    username = session["user"]
    
    try:
        object_id = ObjectId(history_id)
    except:
        return jsonify({"error": "Invalid history ID"}), 400
    
    item = history_collection.find_one({
        "_id": object_id,
        "user": username
    })
    
    if not item:
        return jsonify({"error": "History item not found"}), 404
    
    item["_id"] = str(item["_id"])
    return jsonify({"item": item})

@app.route("/update_test_cases/<history_id>", methods=["PUT"])
@login_required
def update_test_cases(history_id):
    data = request.json
    test_cases = data.get("test_cases", "")
    requirements = data.get("requirements", "")
    project_id = data.get("project_id", "")
    requirement_id = data.get("requirement_id", "")
    requirement_title = data.get("requirement_title", "")
    update_type = data.get("update_type", "manual_edit")
    
    username = session["user"]
    current_time = datetime.now(timezone.utc)
    
    try:
        object_id = ObjectId(history_id)
    except:
        return jsonify({"error": "Invalid history ID"}), 400
    
    # Find the existing history item
    existing_item = history_collection.find_one({
        "_id": object_id,
        "user": username
    })
    
    if not existing_item:
        return jsonify({"error": "History item not found or access denied"}), 404
    
    # Update the existing history item
    update_data = {
        "test_cases": test_cases,
        "timestamp": current_time,
        "update_type": update_type
    }
    
    if requirements:
        update_data["requirements"] = requirements
    if project_id:
        update_data["project_id"] = project_id
    if requirement_id:
        update_data["requirement_id"] = requirement_id
    if requirement_title:
        update_data["requirement_title"] = requirement_title
    
    history_collection.update_one(
        {"_id": object_id},
        {"$set": update_data}
    )
    
    return jsonify({
        "message": "Test cases updated successfully",
        "timestamp": current_time.isoformat()
    })

@app.route("/history/<history_id>", methods=["DELETE"])
@login_required
def delete_history_item(history_id):
    username = session["user"]
    
    try:
        object_id = ObjectId(history_id)
    except:
        return jsonify({"error": "Invalid history ID"}), 400
    
    result = history_collection.delete_one({
        "_id": object_id,
        "user": username
    })
    
    if result.deleted_count == 0:
        return jsonify({"error": "History item not found"}), 404
    
    return jsonify({"message": "History item deleted successfully"})

@app.route("/chat_with_assistant", methods=["POST"])
@login_required
def chat_with_test_cases():
    data = request.json
    user_message = data.get("message", "")
    test_cases = data.get("test_cases", "")
    project_id = data.get("project_id", "")
    requirement_id = data.get("requirement_id", "")
    chat_history = data.get("chat_history", [])
    direct_mode = data.get("direct_mode", False)
    active_history_id = data.get("active_history_id")
    
    username = session["user"]
    
    # Enhanced logging
    print(f"Chat request received from user: {username}")
    print(f"Message: {user_message[:100]}..." if len(user_message) > 100 else f"Message: {user_message}")
    print(f"Direct mode: {direct_mode}, Active history ID: {active_history_id}")
    
    # Check API key early
    try:
        api_key = get_user_api_key_from_settings(username, project_id)  # CHANGED
        if api_key:
            # Mask most of the key for security
            masked_key = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "***masked***"
            print(f"Using API key: {masked_key}")
        else:
            error_msg = "No API key available"
            print(f"ERROR: {error_msg}")
            return Response(
                f"data: {json.dumps({'error': error_msg})}\n\n", 
                content_type="text/event-stream",
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive'
                }
            )
    except Exception as e:
        error_msg = f"Error retrieving API key: {str(e)}"
        print(f"ERROR: {error_msg}")
        return Response(
            f"data: {json.dumps({'error': error_msg})}\n\n", 
            content_type="text/event-stream",
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive'
            }
        )
    
    # Create a more direct instruction for the AI to modify test cases
    if direct_mode:
        context_parts = [
            "You are a test case assistant. Your primary job is to directly modify test cases based on user requests.",
            "IMPORTANT: When the user asks for changes, you MUST output the COMPLETE updated test cases in a code block.",
            "Always add ```<language> before and ``` after the code block.",
            "Include ALL test cases in your output, not just the modified ones.",
            "After showing the updated test cases, add a brief confirmation message like 'Modifications appliquées.'",
            "DO NOT explain what changes you're making beforehand - show the complete updated test cases immediately."
        ]
    else:
        context_parts = [
            "You are a test case assistant helping to improve test cases.",
            "When suggesting changes, explain your reasoning clearly."
        ]
    
    if project_id:
        project = projects_collection.find_one({
            "id": project_id,
            "$or": [
                {"user": username},
                {"collaborators": username}
            ]
        })
        
        if project:
            context_parts.append(f"Project Context: {project.get('name', '')} - {project.get('context', '')}")
    
    if requirement_id:
        requirement = requirements_collection.find_one({"id": requirement_id})
        if requirement:
            context_parts.append(f"Requirement: {requirement.get('title', '')}\n{requirement.get('description', '')}")
    
    context_parts.append(f"Current test cases:\n```\n{test_cases}\n```")
    context_parts.append(f"User request: {user_message}")
    
    # Detect if the user is asking for modifications
    modification_keywords = ["update", "change", "modify", "edit", "replace", "fix", "correct", "add", "remove", "delete", "ajouter", "modifier", "changer", "supprimer", "corriger"]
    is_modification_request = any(keyword in user_message.lower() for keyword in modification_keywords)
    
    # Add more direct instructions for modification requests
    if is_modification_request and direct_mode:
        context_parts.append("This is a modification request. You MUST return the COMPLETE updated test cases in a code block.")
        # Enhanced instruction for more direct responses
        context_parts.append("IMPORTANT: Respond ONLY with:\n1. The COMPLETE updated test cases in a code block\n2. Exactly: 'Modifications appliquées.'")
    
    context = "\n\n".join(context_parts)
    
    def generate():
        try:
            # First, try to initialize the Anthropic client
            try:
                anthropic_client = anthropic.Anthropic(api_key=api_key)
            except Exception as client_error:
                error_msg = f"Error initializing AI client: {str(client_error)}"
                print(f"ERROR: {error_msg}")
                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                yield "data: [DONE]\n\n"
                return
                
            full_response = ""
            print("Starting AI stream processing...")
            
            try:
                # Stream processing
                messages = [{"role": "user", "content": context}]
                
                with anthropic_client.messages.stream(
                    model="claude-3-haiku-20240307",
                    max_tokens=4000,
                    messages=messages
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_delta":
                            if event.delta.text:
                                full_response += event.delta.text
                                yield f"data: {json.dumps({'chunk': event.delta.text})}\n\n"
                
                print("AI stream completed successfully")
            except Exception as stream_error:
                error_msg = f"Error during AI streaming: {str(stream_error)}"
                print(f"ERROR: {error_msg}")
                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                yield "data: [DONE]\n\n"
                return
            
            # Extract test cases from response if present
            updated_test_cases = None
            code_block_match = re.search(r'```(?:.*?)\n([\s\S]*?)\n```', full_response, re.MULTILINE)
            if code_block_match:
                updated_test_cases = code_block_match.group(1).strip()
                print(f"Extracted updated test cases: {len(updated_test_cases)} characters")
            
            # If we successfully extracted updated test cases and have an active history ID
            if updated_test_cases and active_history_id and is_modification_request and direct_mode:
                try:
                    print(f"Attempting to update history item: {active_history_id}")
                    object_id = ObjectId(active_history_id)
                    
                    # Update the history item with the new test cases
                    update_result = history_collection.update_one(
                        {"_id": object_id, "user": username},
                        {"$set": {
                            "test_cases": updated_test_cases,
                            "timestamp": datetime.now(timezone.utc),
                            "update_type": "ai_modification"
                        }}
                    )
                    
                    if update_result.modified_count > 0:
                        print("Successfully updated history item")
                        yield f"data: {json.dumps({'updated_test_cases': updated_test_cases, 'history_updated': True})}\n\n"
                    else:
                        print("Failed to update history item - item not found or access denied")
                        yield f"data: {json.dumps({'updated_test_cases': updated_test_cases, 'history_updated': False})}\n\n"
                        
                except Exception as update_error:
                    print(f"Error updating history: {update_error}")
                    yield f"data: {json.dumps({'updated_test_cases': updated_test_cases, 'history_updated': False, 'update_error': str(update_error)})}\n\n"
            
            yield f"data: {json.dumps({'complete': True, 'full_response': full_response})}\n\n"
            yield "data: [DONE]\n\n"
            
        except Exception as e:
            error_msg = f"Unexpected error in chat processing: {str(e)}"
            print(f"ERROR: {error_msg}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'error': error_msg})}\n\n"
            yield "data: [DONE]\n\n"
    
    return Response(generate(), content_type="text/event-stream", headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type'
    })

@app.route("/extract_text", methods=["POST"])
@login_required
def extract_text():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    try:
        filename = file.filename.lower()
        if filename.endswith('.pdf'):
            text = extract_text_from_pdf(file)
        elif filename.endswith('.docx'):
            text = extract_text_from_docx(file)
        elif filename.endswith('.txt'):
            text = file.read().decode('utf-8')
        else:
            return jsonify({"error": "Unsupported file format. Please upload PDF, DOCX, or TXT files."}), 400
        
        return jsonify({
            "text": text,
            "message": f"Text extracted successfully from {file.filename}"
        })
        
    except Exception as e:
        print(f"Error extracting text: {str(e)}")
        return jsonify({"error": f"Failed to extract text: {str(e)}"}), 500

# Project Management
@app.route("/projects", methods=["GET"])
@login_required
def get_projects():
    username = session["user"]
    user = users_collection.find_one({"username": username})
    user_role = user.get("role", "user") if user else "user"
    
    # Build query based on user role
    if user_role in ["manager", "admin"]:
        # Managers and admins can see projects they own or collaborate on
        query = {
            "$or": [
                {"user": username},
                {"collaborators": username}
            ]
        }
    else:
        # Regular users can only see projects where they are collaborators
        query = {"collaborators": username}
    
    projects = list(projects_collection.find(query))
    
    for project in projects:
        project["_id"] = str(project["_id"])
        project["is_owner"] = project["user"] == username
        project["user_role"] = user_role
    
    return jsonify({"projects": projects})

@app.route("/projects", methods=["POST"])
@manager_required
def create_project():
    data = request.json
    username = session["user"]
    
    project = {
        "id": str(uuid.uuid4()),
        "user": username,
        "name": data.get("name"),
        "context": data.get("context", ""),
        "collaborators": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by_role": "manager"
    }
    
    # Insert the project and get the _id
    result = projects_collection.insert_one(project)
    
    # Create a copy of the project to return
    response_project = project.copy()
    response_project["_id"] = str(result.inserted_id)
    
    return jsonify({"message": "Project created", "project": response_project})

@app.route("/projects/<project_id>", methods=["GET"])
@login_required
def get_project(project_id):
    username = session["user"]
    user = users_collection.find_one({"username": username})
    user_role = user.get("role", "user") if user else "user"
    
    # Check access based on role
    if user_role in ["manager", "admin"]:
        project = projects_collection.find_one({
            "id": project_id,
            "$or": [
                {"user": username},
                {"collaborators": username}
            ]
        })
    else:
        project = projects_collection.find_one({
            "id": project_id,
            "collaborators": username
        })
    
    if not project:
        return jsonify({"error": "Project not found or access denied"}), 404
    
    project["_id"] = str(project["_id"])
    project["is_owner"] = project["user"] == username
    project["user_role"] = user_role
    
    return jsonify({"project": project})

@app.route("/projects/<project_id>", methods=["PUT"])
@login_required
def update_project(project_id):
    username = session["user"]
    data = request.json
    
    project = projects_collection.find_one({
        "id": project_id,
        "user": username
    })
    
    if not project:
        return jsonify({"error": "Project not found or you don't have permission"}), 404
    
    update_data = {}
    if "name" in data:
        update_data["name"] = data["name"]
    if "context" in data:
        update_data["context"] = data["context"]
    
    if update_data:
        update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        projects_collection.update_one(
            {"id": project_id},
            {"$set": update_data}
        )
    
    return jsonify({"message": "Project updated successfully"})

@app.route("/projects/<project_id>", methods=["DELETE"])
@login_required
def delete_project(project_id):
    username = session["user"]
    
    project = projects_collection.find_one({
        "id": project_id,
        "user": username
    })
    
    if not project:
        return jsonify({"error": "Project not found or you don't have permission"}), 404
    
    projects_collection.delete_one({"id": project_id})
    requirements_collection.delete_many({"project_id": project_id})
    collaborators_collection.delete_many({"project_id": project_id})
    
    return jsonify({"message": "Project deleted successfully"})

# Requirement Management
@app.route("/projects/<project_id>/requirements", methods=["GET"])
@login_required
def get_requirements(project_id):
    username = session["user"]
    user = users_collection.find_one({"username": username})
    user_role = user.get("role", "user") if user else "user"
    
    # Check access based on role
    if user_role in ["manager", "admin"]:
        # Managers and admins can access projects they own or collaborate on
        project = projects_collection.find_one({
            "id": project_id,
            "$or": [
                {"user": username},
                {"collaborators": username}
            ]
        })
    else:
        # Regular users can only access projects where they are collaborators
        project = projects_collection.find_one({
            "id": project_id,
            "collaborators": username
        })
    
    if not project:
        return jsonify({"error": "Project not found or access denied"}), 404
    
    requirements = list(requirements_collection.find({
        "project_id": project_id
    }))
    
    for req in requirements:
        req["_id"] = str(req["_id"])
    
    return jsonify({"requirements": requirements})

@app.route("/projects/<project_id>/requirements", methods=["POST"])
@login_required
def create_requirement(project_id):
    data = request.json
    username = session["user"]
    
    project = projects_collection.find_one({
        "id": project_id,
        "$or": [
            {"user": username},
            {"collaborators": username}
        ]
    })
    
    if not project:
        return jsonify({"error": "Project not found or access denied"}), 404
    
    # Generate priority automatically based on description content
    description = data.get("description", "")
    auto_priority = detect_priority(description)
    
    # Use the auto-generated priority if none was specified
    priority = data.get("priority") or auto_priority
    
    requirement = {
        "id": str(uuid.uuid4()),
        "user": username,
        "project_id": project_id,
        "title": data.get("title"),
        "description": description,
        "category": data.get("category", "functionality"),
        "priority": priority,
        "status": data.get("status", "draft"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "priority_auto_generated": priority == auto_priority
    }
    
    result = requirements_collection.insert_one(requirement)
    requirement["_id"] = str(result.inserted_id)
    
    return jsonify({
        "message": "Requirement created", 
        "requirement": requirement,
        "auto_priority_detected": auto_priority
    })

@app.route("/requirements/<requirement_id>", methods=["GET"])
@login_required
def get_requirement(requirement_id):
    username = session["user"]
    
    requirement = requirements_collection.find_one({"id": requirement_id})
    if not requirement:
        return jsonify({"error": "Requirement not found"}), 404
    
    project = projects_collection.find_one({
        "id": requirement["project_id"],
        "$or": [
            {"user": username},
            {"collaborators": username}
        ]
    })
    
    if not project:
        return jsonify({"error": "Access denied"}), 403
    
    requirement["_id"] = str(requirement["_id"])
    return jsonify({"requirement": requirement})

@app.route("/requirements/<requirement_id>", methods=["PUT"])
@login_required
def update_requirement(requirement_id):
    username = session["user"]
    data = request.json
    
    requirement = requirements_collection.find_one({"id": requirement_id})
    if not requirement:
        return jsonify({"error": "Requirement not found"}), 404
    
    project = projects_collection.find_one({
        "id": requirement["project_id"],
        "$or": [
            {"user": username},
            {"collaborators": username}
        ]
    })
    
    if not project:
        return jsonify({"error": "Access denied"}), 403
    
    update_data = {}
    if "title" in data:
        update_data["title"] = data["title"]
    if "description" in data:
        update_data["description"] = data["description"]
    if "category" in data:
        update_data["category"] = data["category"]
    if "priority" in data:
        update_data["priority"] = data["priority"]
    if "status" in data:
        update_data["status"] = data["status"]
    
    if update_data:
        update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        requirements_collection.update_one(
            {"id": requirement_id},
            {"$set": update_data}
        )
    
    return jsonify({"message": "Requirement updated successfully"})

@app.route("/requirements/<requirement_id>", methods=["DELETE"])
@login_required
def delete_requirement(requirement_id):
    username = session["user"]
    
    requirement = requirements_collection.find_one({"id": requirement_id})
    if not requirement:
        return jsonify({"error": "Requirement not found"}), 404
    
    project = projects_collection.find_one({
        "id": requirement["project_id"],
        "$or": [
            {"user": username},
            {"collaborators": username}
        ]
    })
    
    if not project:
        return jsonify({"error": "Access denied"}), 403
    
    requirements_collection.delete_one({"id": requirement_id})
    
    return jsonify({"message": "Requirement deleted successfully"})

# Project Collaboration
@app.route("/projects/<project_id>/collaborators", methods=["GET"])
@login_required
def get_collaborators(project_id):
    username = session["user"]
    
    project = projects_collection.find_one({
        "id": project_id,
        "$or": [
            {"user": username},
            {"collaborators": username}
        ]
    })
    
    if not project:
        return jsonify({"error": "Project not found or access denied"}), 404
    
    collaborators = list(collaborators_collection.find({"project_id": project_id}))
    
    for collab in collaborators:
        collab["_id"] = str(collab["_id"])
    
    return jsonify({"collaborators": collaborators})

@app.route("/projects/<project_id>/collaborators", methods=["POST"])
@login_required
def add_collaborator(project_id):
    username = session["user"]
    data = request.json
    email = data.get("email")
    
    if not email:
        return jsonify({"error": "Email is required"}), 400
    
    project = projects_collection.find_one({
        "id": project_id,
        "user": username
    })
    
    if not project:
        return jsonify({"error": "Project not found or you don't have permission"}), 404
    
    # Check if user exists
    collaborator_user = users_collection.find_one({"email": email})
    if not collaborator_user:
        return jsonify({"error": "User with this email not found"}), 404
    
    collaborator_username = collaborator_user["username"]
    
    # Check if already a collaborator
    existing_collab = collaborators_collection.find_one({
        "project_id": project_id,
        "email": email
    })
    
    if existing_collab:
        return jsonify({"error": "User is already a collaborator"}), 400
    
    # Add to collaborators collection
    collaborator = {
        "project_id": project_id,
        "email": email,
        "username": collaborator_username,
        "added_by": username,
        "added_at": datetime.now(timezone.utc).isoformat()
    }
    
    collaborators_collection.insert_one(collaborator)
    
    # Update project collaborators list
    projects_collection.update_one(
        {"id": project_id},
        {"$addToSet": {"collaborators": collaborator_username}}
    )
    
    return jsonify({"message": "Collaborator added successfully"})

@app.route("/collaborators/<collaborator_id>", methods=["DELETE"])
@login_required
def remove_collaborator(collaborator_id):
    username = session["user"]
    
    try:
        object_id = ObjectId(collaborator_id)
    except:
        return jsonify({"error": "Invalid collaborator ID"}), 400
    
    collaborator = collaborators_collection.find_one({"_id": object_id})
    if not collaborator:
        return jsonify({"error": "Collaborator not found"}), 404
    
    project = projects_collection.find_one({
        "id": collaborator["project_id"],
        "user": username
    })
    
    if not project:
        return jsonify({"error": "Permission denied"}), 403
    
    # Remove from collaborators collection
    collaborators_collection.delete_one({"_id": object_id})
    
    # Remove from project collaborators list
    projects_collection.update_one(
        {"id": collaborator["project_id"]},
        {"$pull": {"collaborators": collaborator["username"]}}
    )
    
    return jsonify({"message": "Collaborator removed successfully"})

# File export endpoints
@app.route("/download_requirements/<format>", methods=["POST"])
@login_required
def download_requirements(format):
    """Generate a file with the requirements in the specified format (PDF or DOCX)"""
    data = request.json
    requirements = data.get("requirements", [])
    project_name = data.get("project_name", "Projet")
    format_date = datetime.now().strftime("%Y-%m-%d")
    file_name = f"exigences_{project_name.replace(' ', '_')}_{format_date}"
    
    if not requirements:
        return jsonify({"error": "No requirements provided"}), 400
    
    username = session["user"]
    
    if format == "pdf":
        # Generate PDF using ReportLab
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=42,
            leftMargin=42,
            topMargin=42,
            bottomMargin=42
        )
        
        # Define styles
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=18,
            spaceAfter=30,
            alignment=1  # Center alignment
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            spaceAfter=12,
            textColor=colors.darkblue
        )
        
        normal_style = styles['Normal']
        
        elements = []
        
        # Add title
        title = Paragraph(f"Exigences: {project_name}", title_style)
        elements.append(title)
        
        # Add date
        date_para = Paragraph(f"Date: {format_date}", normal_style)
        elements.append(date_para)
        elements.append(Spacer(1, 0.3*inch))
        
        for req in requirements:
            # Requirement title
            req_title = Paragraph(req.get("title", ""), heading_style)
            elements.append(req_title)
            
            # Requirement details in table format
            req_details = [
                f"Catégorie: {get_category_label(req.get('category', ''))}",
                f"Priorité: {get_priority_label(req.get('priority', ''))}",
                f"Statut: {get_status_label(req.get('status', ''))}"
            ]
            
            for detail in req_details:
                detail_para = Paragraph(detail, normal_style)
                elements.append(detail_para)
            
            # Description
            desc_title = Paragraph("Description:", normal_style)
            elements.append(desc_title)
            desc_para = Paragraph(req.get("description", ""), normal_style)
            elements.append(desc_para)
            
            elements.append(Spacer(1, 0.2*inch))
        
        # Build and save the PDF
        doc.build(elements)
        
        buffer.seek(0)
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"{file_name}.pdf",
            mimetype="application/pdf"
        )
    
    elif format == "docx":
        # Generate DOCX using python-docx
        doc = Document()
        
        # Add document title
        title = doc.add_heading(f"Exigences: {project_name}", level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        # Add date
        date_paragraph = doc.add_paragraph(f"Date: {format_date}")
        date_paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        
        doc.add_paragraph()  # Add spacing
        
        for req in requirements:
            # Requirement title
            doc.add_heading(req.get("title", ""), level=1)
            
            table = doc.add_table(rows=1, cols=2)
            table.style = 'Table Grid'
            table.autofit = True
            
            header_cells = table.rows[0].cells
            header_cells[0].text = "Métadonnée"
            header_cells[1].text = "Valeur"
            
            row_cells = table.add_row().cells
            row_cells[0].text = "Catégorie"
            row_cells[1].text = get_category_label(req.get("category", ""))
            
            row_cells = table.add_row().cells
            row_cells[0].text = "Priorité"
            row_cells[1].text = get_priority_label(req.get("priority", ""))
            
            row_cells = table.add_row().cells
            row_cells[0].text = "Statut"
            row_cells[1].text = get_status_label(req.get("status", ""))
            
            doc.add_paragraph().add_run("Description:").bold = True
            doc.add_paragraph(req.get("description", ""))
            
            doc.add_paragraph()
        
        buffer = BytesIO()
        doc.save(buffer)
        buffer.seek(0)
        
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"{file_name}.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    
    else:
        return jsonify({"error": f"Unsupported format: {format}"}), 400

@app.route("/export_test_cases", methods=["POST"])
@login_required
def export_test_cases():
    """Generate a file with test cases in the specified format (PDF or DOCX)"""
    data = request.json
    test_cases = data.get("test_cases", "")
    format_type = data.get("format", "pdf")  # 'pdf' or 'docx'
    project_id = data.get("project_id", "")
    requirement_id = data.get("requirement_id", "")
    requirement_title = data.get("requirement_title", "Test Cases")
    
    if not test_cases:
        return jsonify({"error": "No test cases provided"}), 400
    
    username = session["user"]
    format_date = datetime.now().strftime("%Y-%m-%d")
    
    # Sanitize filename
    safe_title = requirement_title.replace(" ", "_").replace("/", "_").replace("\\", "_")
    file_name = f"test_cases_{safe_title}_{format_date}"
    
    # Verify project access if project_id is provided
    if project_id:
        project = projects_collection.find_one({
            "id": project_id,
            "$or": [
                {"user": username},
                {"collaborators": username}
            ]
        })
        
        if not project:
            return jsonify({"error": "Project not found or access denied"}), 403
    
    # Process based on format type
    if format_type == "pdf":
        try:
            # Generate PDF using ReportLab
            buffer = BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=A4,
                rightMargin=42,
                leftMargin=42,
                topMargin=42,
                bottomMargin=42
            )
            
            # Define styles
            styles = getSampleStyleSheet()
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=18,
                spaceAfter=30,
                alignment=1
            )
            
            normal_style = styles['Normal']
            
            elements = []
            
            # Add title
            title = Paragraph(f"Test Cases: {requirement_title}", title_style)
            elements.append(title)
            
            # Add date
            date_para = Paragraph(f"Date: {format_date}", normal_style)
            elements.append(date_para)
            elements.append(Spacer(1, 0.3*inch))
            
            # Add test cases with monospaced font to preserve formatting
            test_case_style = ParagraphStyle(
                'TestCaseStyle',
                parent=normal_style,
                fontName='Courier',
                fontSize=9,
                leftIndent=0,
                rightIndent=0
            )
            
            # Split test cases into lines and add each as a paragraph to preserve formatting
            for line in test_cases.split('\n'):
                if line.strip():
                    para = Paragraph(line, test_case_style)
                    elements.append(para)
                else:
                    elements.append(Spacer(1, 0.1*inch))
            
            # Build and save the PDF
            doc.build(elements)
            
            buffer.seek(0)
            return send_file(
                buffer,
                as_attachment=True,
                download_name=f"{file_name}.pdf",
                mimetype="application/pdf"
            )
            
        except Exception as e:
            print(f"Error generating PDF: {str(e)}")
            return jsonify({"error": f"Failed to generate PDF: {str(e)}"}), 500
            
    elif format_type == "docx":
        try:
            # Generate DOCX using python-docx
            doc = Document()
            
            # Add document title
            title = doc.add_heading(f"Test Cases: {requirement_title}", level=1)
            title.alignment = WD_ALIGN_PARAGRAPH.CENTER
            
            # Add date
            date_paragraph = doc.add_paragraph(f"Date: {format_date}")
            date_paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            
            doc.add_paragraph()  # Add spacing
            
            # Add test cases with monospaced font to preserve formatting
            test_case_para = doc.add_paragraph()
            test_case_run = test_case_para.add_run(test_cases)
            test_case_run.font.name = 'Courier New'
            test_case_run.font.size = Pt(9)
            
            # Save to buffer
            buffer = BytesIO()
            doc.save(buffer)
            buffer.seek(0)
            
            return send_file(
                buffer,
                as_attachment=True,
                download_name=f"{file_name}.docx",
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            
        except Exception as e:
            print(f"Error generating DOCX: {str(e)}")
            return jsonify({"error": f"Failed to generate DOCX: {str(e)}"}), 500
    
    else:
        return jsonify({"error": f"Unsupported format: {format_type}"}), 400
def get_user_settings(username, project_id=None):
    """Get user settings for LLM service preferences"""
    try:
        # Try to get project-specific settings first
        if project_id:
            project_settings = user_settings_collection.find_one({
                "user": username,
                "project_id": project_id
            })
            if project_settings:
                return project_settings
        
        # Fallback to global user settings
        global_settings = user_settings_collection.find_one({
            "user": username,
            "project_id": {"$exists": False}
        })
        
        if global_settings:
            return global_settings
        
        # Return default settings if none exist
        return {
        "user": username,
        "llm_service": "auto",  # auto, claude, local_rag
        "claude_api_key": None,  # No default API key
        "use_project_key": False,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
        }
    except Exception as e:
        print(f"Error getting user settings: {e}")
        return None

def update_user_settings(username, settings_data, project_id=None):
    """Update user settings for LLM service preferences"""
    try:
        # Prepare the filter
        filter_query = {"user": username}
        if project_id:
            filter_query["project_id"] = project_id
        else:
            filter_query["project_id"] = {"$exists": False}
        
        # Prepare update data
        update_data = {
            "user": username,
            "llm_service": settings_data.get("llm_service", "auto"),
            "use_project_key": settings_data.get("use_project_key", False),
            "updated_at": datetime.now(timezone.utc)
        }
        
        # Add project_id if provided
        if project_id:
            update_data["project_id"] = project_id
        
        # Encrypt and store API key if provided
        if settings_data.get("claude_api_key"):
            encrypted_key = encrypt_api_key(settings_data["claude_api_key"])
            if encrypted_key:
                update_data["claude_api_key"] = encrypted_key
        elif "claude_api_key" in settings_data and not settings_data["claude_api_key"]:
            # Remove API key if explicitly set to empty
            update_data["claude_api_key"] = None
        
        # Check if settings already exist
        existing_settings = user_settings_collection.find_one(filter_query)
        
        if existing_settings:
            # Update existing settings
            update_data["created_at"] = existing_settings.get("created_at", datetime.now(timezone.utc))
            user_settings_collection.update_one(
                filter_query,
                {"$set": update_data}
            )
        else:
            # Create new settings
            update_data["created_at"] = datetime.now(timezone.utc)
            user_settings_collection.insert_one(update_data)
        
        return True
        
    except Exception as e:
        print(f"Error updating user settings: {e}")
        return False

def get_effective_llm_service(username, project_id=None):
    """Determine which LLM service to use based on user settings and availability"""
    try:
        # Get user settings
        settings = get_user_settings(username, project_id)
        if not settings:
            return "local_rag" if check_rag_availability() else None
        
        llm_service = settings.get("llm_service", "auto")
        
        if llm_service == "claude":
            # User explicitly wants Claude
            api_key = get_user_api_key_from_settings(username, project_id, settings)
            return "claude" if api_key else None
            
        elif llm_service == "local_rag":
            # User explicitly wants Local RAG
            return "local_rag" if check_rag_availability() else None
            
        else:  # llm_service == "auto"
            # Auto mode: try Claude first, fallback to Local RAG
            api_key = get_user_api_key_from_settings(username, project_id, settings)
            if api_key:
                return "claude"
            elif check_rag_availability():
                return "local_rag"
            else:
                return None
                
    except Exception as e:
        print(f"Error determining effective LLM service: {e}")
        return None

def get_user_api_key_from_settings(username, project_id=None, settings=None):
    """Get API key considering user settings preferences"""
    try:
        if not settings:
            settings = get_user_settings(username, project_id)
        
        # If user has API key in settings and wants to use it
        if settings and settings.get("claude_api_key"):
            return settings["claude_api_key"]  # CHANGED: No decryption
        
        # Check if user wants to use project-specific key from old system
        if settings and settings.get("use_project_key", False):
            old_api_key = get_user_api_key(username, project_id)
            if old_api_key:
                return old_api_key
        
        # No fallback to environment variable anymore
        return None
        
    except Exception as e:
        print(f"Error getting API key from settings: {e}")
        return None
@app.route("/user_settings", methods=["GET"])
@login_required
def get_user_settings_endpoint():
    """Get user settings for LLM service preferences"""
    username = session["user"]
    project_id = request.args.get("project_id")
    
    try:
        settings = get_user_settings(username, project_id)
        if not settings:
            return jsonify({"error": "Settings not found"}), 404
        
        # Remove sensitive data and prepare response
        response_settings = {
            "user": settings.get("user"),
            "llm_service": settings.get("llm_service", "auto"),
            "has_claude_api_key": bool(settings.get("claude_api_key")),
            "use_project_key": settings.get("use_project_key", False),
            "project_id": settings.get("project_id"),
            "created_at": settings.get("created_at"),
            "updated_at": settings.get("updated_at")
        }
        
        # Add available services info
        services_info = {
            "claude_available": bool(get_user_api_key_from_settings(username, project_id, settings)),
            "local_rag_available": check_rag_availability(),
            "effective_service": get_effective_llm_service(username, project_id)
        }
        
        return jsonify({
            "settings": response_settings,
            "services": services_info
        })
        
    except Exception as e:
        print(f"Error getting user settings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/user_settings", methods=["POST", "PUT"])
@login_required
def update_user_settings(username, settings_data, project_id=None):
    """Update user settings for LLM service preferences"""
    try:
        # Prepare the filter
        filter_query = {"user": username}
        if project_id:
            filter_query["project_id"] = project_id
        else:
            filter_query["project_id"] = {"$exists": False}
        
        # Prepare update data
        update_data = {
            "user": username,
            "llm_service": settings_data.get("llm_service", "auto"),
            "use_project_key": settings_data.get("use_project_key", False),
            "updated_at": datetime.now(timezone.utc)
        }
        
        # Add project_id if provided
        if project_id:
            update_data["project_id"] = project_id
        
        # Store API key directly if provided (no encryption)
        if settings_data.get("claude_api_key"):
            update_data["claude_api_key"] = settings_data["claude_api_key"]  # CHANGED: No encryption
        elif "claude_api_key" in settings_data and not settings_data["claude_api_key"]:
            # Remove API key if explicitly set to empty
            update_data["claude_api_key"] = None
        
        # Check if settings already exist
        existing_settings = user_settings_collection.find_one(filter_query)
        
        if existing_settings:
            # Update existing settings
            update_data["created_at"] = existing_settings.get("created_at", datetime.now(timezone.utc))
            user_settings_collection.update_one(
                filter_query,
                {"$set": update_data}
            )
        else:
            # Create new settings
            update_data["created_at"] = datetime.now(timezone.utc)
            user_settings_collection.insert_one(update_data)
        
        return True
        
    except Exception as e:
        print(f"Error updating user settings: {e}")
        return False

@app.route("/user_settings", methods=["DELETE"])
@login_required
def delete_user_settings_endpoint():
    """Delete user settings (reset to defaults)"""
    username = session["user"]
    project_id = request.args.get("project_id")
    
    try:
        # Prepare the filter
        filter_query = {"user": username}
        if project_id:
            filter_query["project_id"] = project_id
        else:
            filter_query["project_id"] = {"$exists": False}
        
        # Delete settings
        result = user_settings_collection.delete_one(filter_query)
        
        if result.deleted_count == 0:
            return jsonify({"error": "Settings not found"}), 404
        
        return jsonify({"message": "Settings deleted successfully"})
        
    except Exception as e:
        print(f"Error deleting user settings: {e}")
        return jsonify({"error": str(e)}), 500
    

#app.add_url_rule("/generate_test_cases", "generate_test_cases_enhanced", generate_test_cases_endpoint_enhanced, methods=["POST", "OPTIONS"])

# ==================================================
# USER SETTINGS MIGRATION HELPER
# ==================================================

@app.route("/migrate_user_settings", methods=["POST"])
@login_required
def migrate_user_settings():
    """Helper endpoint to migrate existing API keys to new settings system"""
    username = session["user"]
    
    try:
        # Check if user already has settings
        existing_settings = user_settings_collection.find_one({
            "user": username,
            "project_id": {"$exists": False}
        })
        
        if existing_settings:
            return jsonify({"message": "Settings already migrated"})
        
        # Look for existing API keys
        user_api_keys = list(api_keys_collection.find({"user": username}))
        
        migrated_count = 0
        
        for api_key_doc in user_api_keys:
            project_id = api_key_doc.get("project_id")
            
            # Create settings entry
            settings_data = {
                "llm_service": "claude",  # Assume Claude since they had API keys
                "claude_api_key": api_key_doc.get("api_key"),  # Already encrypted
                "use_project_key": False
            }
            
            success = update_user_settings(username, settings_data, project_id)
            if success:
                migrated_count += 1
        
        # Create default global settings if no API keys existed
        if not user_api_keys:
            default_settings = {
                "llm_service": "auto",
                "claude_api_key": None,
                "use_project_key": False
            }
            update_user_settings(username, default_settings)
            migrated_count = 1
        
        return jsonify({
            "message": f"Migration completed. {migrated_count} settings entries created.",
            "migrated_count": migrated_count
        })
        
    except Exception as e:
        print(f"Error migrating user settings: {e}")
        return jsonify({"error": str(e)}), 500
    
def validate_claude_api_key(api_key):
    """Validate Claude API key by making a test request"""
    if not api_key or not api_key.strip():
        return False, "API key is required"
    
    try:
        # Create client with the provided key
        client = anthropic.Anthropic(api_key=api_key.strip())
        
        # Make a minimal test request
        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}]
        )
        
        return True, "API key is valid"
    
    except anthropic.AuthenticationError:
        return False, "Invalid API key"
    except anthropic.PermissionDeniedError:
        return False, "API key doesn't have required permissions"
    except anthropic.RateLimitError:
        return False, "Rate limit exceeded, but API key appears valid"
    except Exception as e:
        return False, f"API key validation failed: {str(e)}"

def save_user_llm_settings(username, llm_choice, api_key=None, project_id=None):
    """Save user's LLM preference and API key for a specific project or globally"""
    try:
        # Prepare the filter for finding existing settings
        filter_query = {"user": username}
        if project_id:
            filter_query["project_id"] = project_id
        else:
            filter_query["project_id"] = {"$exists": False}
        
        # Prepare the data to save
        settings_data = {
            "user": username,
            "llm_service": llm_choice,  # "local" or "claude"
            "updated_at": datetime.now(timezone.utc)
        }
        
        # Add project_id if provided
        if project_id:
            settings_data["project_id"] = project_id
        
        # Handle API key
        if llm_choice == "claude":
            if not api_key:
                return False, "API key is required for Claude"
            
            # Validate API key
            is_valid, message = validate_claude_api_key(api_key)
            if not is_valid:
                return False, f"API key validation failed: {message}"
            
            # Store API key directly (no encryption)
            settings_data["claude_api_key"] = api_key  # CHANGED: No encryption
            settings_data["api_key_validated"] = True
            settings_data["api_key_validated_at"] = datetime.now(timezone.utc)
            
        elif llm_choice == "local":
            # Remove API key if switching to local
            settings_data["claude_api_key"] = None
            settings_data["api_key_validated"] = False
        
        # Check if settings already exist
        existing_settings = user_settings_collection.find_one(filter_query)
        
        if existing_settings:
            # Update existing settings
            settings_data["created_at"] = existing_settings.get("created_at", datetime.now(timezone.utc))
            user_settings_collection.update_one(
                filter_query,
                {"$set": settings_data}
            )
        else:
            # Create new settings
            settings_data["created_at"] = datetime.now(timezone.utc)
            user_settings_collection.insert_one(settings_data)
        
        return True, "Settings saved successfully"
        
    except Exception as e:
        print(f"Error saving user LLM settings: {e}")
        return False, f"Error saving settings: {str(e)}"

def get_user_llm_settings(username, project_id=None):
    """Get user's LLM settings for a specific project or global"""
    try:
        # Try to get project-specific settings first if project_id is provided
        if project_id:
            project_settings = user_settings_collection.find_one({
                "user": username,
                "project_id": project_id
            })
            if project_settings:
                return project_settings
        
        # Fallback to global settings
        global_settings = user_settings_collection.find_one({
            "user": username,
            "project_id": {"$exists": False}
        })
        
        return global_settings
        
    except Exception as e:
        print(f"Error getting user LLM settings: {e}")
        return None

def get_effective_llm_for_user(username, project_id=None):
    """Determine which LLM service the user should use"""
    try:
        settings = get_user_llm_settings(username, project_id)
        
        if not settings:
            # No settings found, default to local if available
            return "local" if check_rag_availability() else None
        
        llm_service = settings.get("llm_service", "local")
        
        if llm_service == "claude":
            # Check if API key is available and validated
            if settings.get("claude_api_key") and settings.get("api_key_validated"):
                return "claude"
            else:
                # API key not available or not validated, fallback to local
                return "local" if check_rag_availability() else None
        else:
            # User chose local
            return "local" if check_rag_availability() else None
            
    except Exception as e:
        print(f"Error determining effective LLM service: {e}")
        return "local" if check_rag_availability() else None
@app.route("/user_llm_settings", methods=["GET"])
@login_required
def get_user_llm_settings_endpoint():
    """Get user's LLM settings"""
    username = session["user"]
    project_id = request.args.get("project_id")
    
    try:
        settings = get_user_llm_settings(username, project_id)
        
        # Prepare response (don't expose encrypted API key)
        response_data = {
            "llm_service": settings.get("llm_service", "local") if settings else "local",
            "has_claude_key": bool(settings and settings.get("claude_api_key")),
            "api_key_validated": settings.get("api_key_validated", False) if settings else False,
            "project_id": project_id,
            "local_available": check_rag_availability(),
            "effective_service": get_effective_llm_for_user(username, project_id)
        }
        
        if settings:
            response_data.update({
                "created_at": settings.get("created_at"),
                "updated_at": settings.get("updated_at"),
                "api_key_validated_at": settings.get("api_key_validated_at")
            })
        
        return jsonify(response_data)
        
    except Exception as e:
        print(f"Error getting user LLM settings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/user_llm_settings", methods=["POST"])
@login_required
def save_user_llm_settings_endpoint():
    """Save user's LLM preference and API key"""
    username = session["user"]
    data = request.json
    
    llm_choice = data.get("llm_service")
    api_key = data.get("claude_api_key", "").strip()
    project_id = data.get("project_id")
    
    # Validate input
    if llm_choice not in ["local", "claude"]:
        return jsonify({"error": "llm_service must be 'local' or 'claude'"}), 400
    
    try:
        success, message = save_user_llm_settings(username, llm_choice, api_key, project_id)
        
        if success:
            # Get updated settings to return
            updated_settings = get_user_llm_settings(username, project_id)
            
            response_data = {
                "message": message,
                "settings": {
                    "llm_service": updated_settings.get("llm_service"),
                    "has_claude_key": bool(updated_settings.get("claude_api_key")),
                    "api_key_validated": updated_settings.get("api_key_validated", False),
                    "project_id": project_id,
                    "effective_service": get_effective_llm_for_user(username, project_id)
                }
            }
            
            return jsonify(response_data)
        else:
            return jsonify({"error": message}), 400
            
    except Exception as e:
        print(f"Error saving user LLM settings: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/validate_claude_key", methods=["POST"])
@login_required
def validate_claude_key_endpoint():
    """Validate Claude API key without saving"""
    data = request.json
    api_key = data.get("api_key", "").strip()
    
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    
    try:
        is_valid, message = validate_claude_api_key(api_key)
        return jsonify({
            "valid": is_valid,
            "message": message
        })
    except Exception as e:
        return jsonify({
            "valid": False, 
            "message": f"Validation error: {str(e)}"
        }), 500

@app.route("/user_llm_settings", methods=["DELETE"])
@login_required 
def delete_user_llm_settings_endpoint():
    """Reset user's LLM settings to default"""
    username = session["user"]
    project_id = request.args.get("project_id")
    
    try:
        filter_query = {"user": username}
        if project_id:
            filter_query["project_id"] = project_id
        else:
            filter_query["project_id"] = {"$exists": False}
        
        result = user_settings_collection.delete_one(filter_query)
        
        if result.deleted_count == 0:
            return jsonify({"message": "No settings found to delete"})
        
        return jsonify({"message": "Settings reset to default successfully"})
        
    except Exception as e:
        print(f"Error deleting user LLM settings: {e}")
        return jsonify({"error": str(e)}), 500


@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

if __name__ == "__main__":
    app.run(debug=True, port=5000, host='0.0.0.0')
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
import re
import io

import admin

load_dotenv()

# Custom JSON encoder to handle MongoDB ObjectId
class MongoJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ObjectId):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

app = Flask(__name__)
app.register_blueprint(admin_bp)

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

admin.users_collection = users_collection
admin.projects_collection = projects_collection
admin.collaborators_collection = collaborators_collection
admin.api_keys_collection = api_keys_collection
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

def get_encryption_key():
    """Get or generate an encryption key for API keys."""
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        # Generate a key if one doesn't exist
        key = Fernet.generate_key().decode()
        # In a production environment, you would save this key securely
        print(f"Generated new encryption key. Add this to your .env file: ENCRYPTION_KEY={key}")
    else:
        # Ensure the key is properly formatted
        try:
            key = key.encode() if isinstance(key, str) else key
            Fernet(key)
        except Exception as e:
            print(f"Invalid encryption key: {e}")
            # Generate a new key as fallback
            key = Fernet.generate_key().decode()
            print(f"Generated new encryption key. Add this to your .env file: ENCRYPTION_KEY={key}")
    
    return key.encode() if isinstance(key, str) else key

def encrypt_api_key(api_key):
    """Encrypt an API key."""
    if not api_key:
        return None
    
    try:
        f = Fernet(get_encryption_key())
        return f.encrypt(api_key.encode()).decode()
    except Exception as e:
        print(f"Error encrypting API key: {e}")
        return None

def decrypt_api_key(encrypted_key):
    """Decrypt an API key."""
    if not encrypted_key:
        return None
    
    try:
        f = Fernet(get_encryption_key())
        return f.decrypt(encrypted_key.encode()).decode()
    except Exception as e:
        print(f"Error decrypting API key: {e}")
        return None
    
def get_user_api_key(username, project_id=None):
    """Get a user's API key, with optional project-specific override."""
    # First try to get a project-specific key if project_id is provided
    if project_id:
        project_key = api_keys_collection.find_one({
            "user": username,
            "project_id": project_id
        })
        if project_key and project_key.get("api_key"):
            decrypted_key = decrypt_api_key(project_key["api_key"])
            if decrypted_key:
                return decrypted_key
    
    # If no project key, try to get the user's default key
    user_key = api_keys_collection.find_one({
        "user": username,
        "project_id": {"$exists": False}
    })
    if user_key and user_key.get("api_key"):
        decrypted_key = decrypt_api_key(user_key["api_key"])
        if decrypted_key:
            return decrypted_key
    
    # If no user keys, use the default from .env
    return os.getenv("CLAUDE_API_KEY")

def get_anthropic_client(username, project_id=None):
    """Get an Anthropic client using the appropriate API key."""
    api_key = get_user_api_key(username, project_id)
    if not api_key:
        raise ValueError("No API key available")
    return anthropic.Anthropic(api_key=api_key)

def extract_text_from_pdf(pdf_file):
    """Extract text from a PDF file object without saving to disk"""
    text = ""
    try:
        # PyPDF2 can read from a file-like object directly
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        for page in pdf_reader.pages:   
            text += page.extract_text() + "\n"
    except Exception as e:
        return f"Error extracting text from PDF: {str(e)}"
    return text.strip()

def extract_text_from_docx(docx_file):
    """Extract text from a DOCX file object without saving to disk"""
    text = ""
    try:
        file_content = docx_file.read()
        docx_io = io.BytesIO(file_content)
        doc = docx.Document(docx_io)
        for para in doc.paragraphs:
            text += para.text + "\n"
    except Exception as e:
        return f"Error extracting text from DOCX: {str(e)}"
    return text.strip()


def detect_priority(text):
    """
    Analyze text to automatically detect the priority level.
    Returns 'high', 'medium', or 'low'.
    """
    text_lower = text.lower()
    
    high_priority_keywords = [
        'critique', 'crucial', 'urgent', 'obligatoire', 'immédiat', 'vital',
        'impératif', 'essentiel', 'doit', 'prioritaire', 'sécurité', 'fatal',
        'risque', 'danger', 'critical', 'must', 'required', 'mandatory',
        'immediately', 'security', 'safety', 'urgent', 'high priority'
    ]
    
    low_priority_keywords = [
        'optionnel', 'facultatif', 'souhaitable', 'suggéré', 'bonus', 
        'accessoire', 'mineur', 'pourrait', 'éventuel', 'agréable', 'simple',
        'optional', 'nice to have', 'could', 'minor', 'suggested', 'eventually',
        'future', 'low priority', 'when possible', 'later'
    ]
    
    high_count = sum(1 for word in high_priority_keywords if word in text_lower)
    low_count = sum(1 for word in low_priority_keywords if word in text_lower)
    
    if high_count > low_count:
        return 'high'
    elif low_count > high_count:
        return 'low'
    else:
        return 'medium'
    
    
def generate_test_case_prompt(requirements, format_type, context="", example_case=""):
    """Utility function to generate the prompt for test case generation"""
    try:
        input_text = context + " " + requirements
        detected_lang = langdetect.detect(input_text)
        lang = "en" if detected_lang == "en" else "fr"
    except:
        lang = "fr"

    if lang == "fr":
        example_format_default = """
**Cas fonctionnels**
Scenario (1) : Connexion OK avec des identifiants valides.
Précondition : L'utilisateur est inscrit avec un e-Mail valide et un MP.
Etapes :
    1. Accéder à la page de connexion.
    2. Saisir l'e-Mail et le MP valides.
    3. Cliquer sur "Se connecter".
Résultat attendu : L'utilisateur est redirigé vers la page d'accueil.

Scenario (2) : Erreur de connexion avec des identifiants invalides.
Précondition : L'utilisateur a un e-Mail valide mais un mot de passe invalide.
Etapes :
    1. Accéder à la page de connexion.
    2. Saisir un e-Mail valide et un MP invalide.
    3. Cliquer sur "Se connecter".
Résultat attendu : Un message d'erreur est affiché, l'utilisateur reste sur la page de connexion.
"""
    else:
        example_format_default = """
**Functional Test Cases**
Scenario (1): Successful login with valid credentials.
Precondition: User is registered with a valid email and password.
Steps:
    1. Access the login page.
    2. Enter valid email and password.
    3. Click on "Login".
Expected Result: User is redirected to the home page.

Scenario (2): Failed login with invalid credentials.
Precondition: User has a valid email but an incorrect password.
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

# Auth Endpoints
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
            "is_admin": user.get("role") == "admin"
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
                "is_admin": user.get("role") == "admin"
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
            api_key = os.getenv("CLAUDE_API_KEY", "")
        
        if not api_key:
            return jsonify({"error": "No API key available"}), 500
            
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
    
    # Encrypt the API key
    encrypted_key = encrypt_api_key(api_key)
    if not encrypted_key:
        return jsonify({"error": "Failed to encrypt API key"}), 500
    
    query = {"user": username}
    if project_id:
        query["project_id"] = project_id
    else:
        query["project_id"] = {"$exists": False}
    
    existing_key = api_keys_collection.find_one(query)
    
    if existing_key:
        api_keys_collection.update_one(
            {"_id": existing_key["_id"]},
            {"$set": {"api_key": encrypted_key}}
        )
    else:
        key_data = {
            "user": username,
            "api_key": encrypted_key,
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
    collaborator_email = data.get("username")  # The field is called username but contains email
    
    print(f"Adding collaborator: {collaborator_email} to project {project_id}")
    
    if not collaborator_email:
        return jsonify({"error": "Email is required"}), 400
    
    # Verify user owns this project
    project = projects_collection.find_one({
        "id": project_id,
        "user": username
    })
    
    if not project:
        return jsonify({"error": "Project not found or you don't have permission"}), 404
    
    # Check if collaborator exists by email
    collaborator = users_collection.find_one({"username": collaborator_email})
    
    if not collaborator:
        return jsonify({"error": f"User with email '{collaborator_email}' not found"}), 404
    
    # In this system, username is the email
    collaborator_username = collaborator["username"]
    
    # Check if already a collaborator
    if "collaborators" in project and collaborator_username in project.get("collaborators", []):
        return jsonify({"error": "User is already a collaborator"}), 400
    
    # Initialize collaborators array if it doesn't exist
    if "collaborators" not in project:
        projects_collection.update_one(
            {"id": project_id},
            {"$set": {"collaborators": []}}
        )
    
    # Add to project collaborators
    projects_collection.update_one(
        {"id": project_id},
        {"$addToSet": {"collaborators": collaborator_username}}
    )
    
    # Check if the collaborator is already in the collaborators collection
    existing_collab = collaborators_collection.find_one({
        "project_id": project_id,
        "username": collaborator_username
    })
    
    if not existing_collab:
        # Add to collaborators collection
        collaborators_collection.insert_one({
            "project_id": project_id,
            "username": collaborator_username,
            "email": collaborator_username,  # Email and username are the same
            "added_by": username,
            "added_at": datetime.now(timezone.utc)
        })
    
    return jsonify({
        "message": "Collaborator added successfully",
        "collaborator": {
            "username": collaborator_username,
            "email": collaborator_username
        }
    })

@app.route("/projects/<project_id>/collaborators/<collaborator_username>", methods=["DELETE"])
@login_required
def remove_collaborator(project_id, collaborator_username):
    username = session["user"]
    
    # Verify user owns this project
    project = projects_collection.find_one({
        "id": project_id,
        "user": username
    })
    
    if not project:
        return jsonify({"error": "Project not found or you don't have permission"}), 404
    
    # Remove from project collaborators
    projects_collection.update_one(
        {"id": project_id},
        {"$pull": {"collaborators": collaborator_username}}
    )
    
    # Remove from collaborators collection
    collaborators_collection.delete_one({
        "project_id": project_id,
        "username": collaborator_username
    })
    
    return jsonify({"message": "Collaborator removed successfully"})

# Project Management
@app.route("/projects", methods=["GET"])
@login_required
def get_projects():
    username = session["user"]
    
    projects = list(projects_collection.find({
        "$or": [
            {"user": username},
            {"collaborators": username}
        ]
    }))
    
    for project in projects:
        project["_id"] = str(project["_id"])
        project["is_owner"] = project["user"] == username
    
    return jsonify({"projects": projects})

@app.route("/projects", methods=["POST"])
@login_required
def create_project():
    data = request.json
    username = session["user"]
    
    project = {
        "id": str(uuid.uuid4()),
        "user": username,
        "name": data.get("name"),
        "context": data.get("context", ""),
        "collaborators": [],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    # Insert the project and get the _id
    result = projects_collection.insert_one(project)
    
    # Create a copy of the project to return
    response_project = project.copy()
    response_project["_id"] = str(result.inserted_id)  # Convert ObjectId to string
    
    return jsonify({"message": "Project created", "project": response_project})

@app.route("/projects/<project_id>", methods=["GET"])
@login_required
def get_project(project_id):
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
    
    project["_id"] = str(project["_id"])
    project["is_owner"] = project["user"] == username
    
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
    
    project = projects_collection.find_one({
        "id": project_id,
        "$or": [
            {"user": username},
            {"collaborators": username}
        ]
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
        "priority_auto_generated": priority == auto_priority  # Flag to indicate auto-generation
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
        # Re-analyze priority if description changes
        auto_priority = detect_priority(data["description"])
        
        # If priority was previously auto-generated or priority field is not provided
        # in the request, update with the new auto-generated priority
        if requirement.get("priority_auto_generated", False) or "priority" not in data:
            update_data["priority"] = auto_priority
            update_data["priority_auto_generated"] = True
    
    # If priority is explicitly provided, use it and mark as not auto-generated
    if "priority" in data:
        update_data["priority"] = data["priority"]
        update_data["priority_auto_generated"] = False
        
    if "category" in data:
        update_data["category"] = data["category"]
    
    if "status" in data:
        update_data["status"] = data["status"]
    
    if update_data:
        update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        requirements_collection.update_one(
            {"id": requirement_id},
            {"$set": update_data}
        )
    
    # Get the updated requirement
    updated_requirement = requirements_collection.find_one({"id": requirement_id})
    if updated_requirement:
        updated_requirement["_id"] = str(updated_requirement["_id"])
    
    return jsonify({
        "message": "Requirement updated successfully",
        "requirement": updated_requirement
    })

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
        "update_type": "manual_edit"  # Add a field to track update type
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

@app.route("/test", methods=["GET"])
def test_endpoint():
    return jsonify({"message": "API is working!"})

@app.route("/generate_test_cases", methods=["POST", "OPTIONS"])
def generate_test_cases_endpoint():
    if request.method == "OPTIONS":
        return "", 200
    
    # Check login
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    requirements = data.get("requirements", "")
    format_type = data.get("format_type", "default")
    context = data.get("context", "")
    example_case = data.get("example_case", "")
    project_id = data.get("project_id", "")
    requirement_id = data.get("requirement_id", "")
    requirement_title = data.get("requirement_title", "")
    
    if not requirements:
        return jsonify({"error": "No requirements provided"}), 400
    
    username = session["user"]
    
    try:
        # Get the appropriate API key and create the client
        anthropic_client = get_anthropic_client(username, project_id)
        
        test_case_instruction = generate_test_case_prompt(requirements, format_type, context, example_case)
        
        response = anthropic_client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=4000,
            messages=[{"role": "user", "content": test_case_instruction}]
        )
        
        full_response = response.content[0].text
        
        # Save to history
        history_data = {
            "user": username,
            "test_cases": full_response,
            "timestamp": datetime.now(timezone.utc),
            "requirements": requirements,
            "context": context,
            "project_id": project_id
        }
        
        if requirement_id:
            history_data["requirement_id"] = requirement_id
        if requirement_title:
            history_data["requirement_title"] = requirement_title
            
        history_collection.insert_one(history_data)
        
        return jsonify({
            "test_cases": full_response,
            "message": "Test cases generated successfully"
        })
    
    except Exception as e:
        print(f"Error generating test cases: {e}")
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
                    elif event.type == "message_stop":
                        history_collection.insert_one({
                            "user": username,
                            "test_cases": full_response,
                            "timestamp": datetime.now(timezone.utc),
                            "requirements": requirements,
                            "context": context,
                            "project_id": project_id
                        })
                        yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), content_type="text/event-stream")

@app.route("/generate_test_cases_for_requirement", methods=["POST"])
@login_required
@limiter.limit("5 per minute")
def generate_test_cases_for_requirement():
    data = request.json
    requirement_id = data.get("requirement_id")
    format_type = data.get("format_type", "default")
    example_case = data.get("example_case", "")
    
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
    
    test_case_instruction = generate_test_case_prompt(
        requirement["description"], 
        format_type, 
        requirement["title"], 
        example_case
    )
    
    def generate():
        try:
            full_response = ""
            anthropic_client = get_anthropic_client(username, requirement["project_id"])
            
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
                    elif event.type == "message_stop":
                        history_collection.insert_one({
                            "user": username,
                            "test_cases": full_response,
                            "timestamp": datetime.now(timezone.utc),
                            "requirement_id": requirement_id,
                            "requirement_title": requirement["title"],
                            "project_id": requirement["project_id"]
                        })
                        yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), content_type="text/event-stream")

@app.route("/chat_with_assistant", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
def chat_with_assistant():
    data = request.json
    user_message = data.get("message", "")
    test_cases = data.get("test_cases", "")
    project_id = data.get("project_id", "")
    requirement_id = data.get("requirement_id", "")
    requirement_title = data.get("requirement_title", "")
    requirements = data.get("requirements", "")
    chat_history = data.get("chat_history", [])
    direct_mode = data.get("direct_mode", False)  # Flag to indicate direct modification mode
    active_history_id = data.get("active_history_id")  # Get active history ID if provided
    
    username = session["user"]
    
    print(f"Chat request received. Direct mode: {direct_mode}, Active history ID: {active_history_id}")
    
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
            full_response = ""
            anthropic_client = get_anthropic_client(username, project_id)
            
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
            
            # Extract test cases from response if present
            updated_test_cases = None
            code_block_match = re.search(r'```(?:.*?)\n([\s\S]*?)```', full_response)
            if code_block_match:
                updated_test_cases = code_block_match.group(1).strip()
                
                # If we found updated test cases and they're different from the original
                if updated_test_cases and updated_test_cases != test_cases:
                    print("Found updated test cases in AI response")
                    
                    # Prepare common update data
                    update_data = {
                        "test_cases": updated_test_cases,
                        "timestamp": datetime.now(timezone.utc),
                        "update_type": "ai_assistant",
                        "source_message": user_message
                    }
                    
                    # If active_history_id is provided, try to update that entry
                    if active_history_id:
                        try:
                            # Update existing history entry
                            history_collection.update_one(
                                {"_id": ObjectId(active_history_id)},
                                {"$set": update_data}
                            )
                            print(f"Updated existing history item: {active_history_id}")
                        except Exception as e:
                            print(f"Update failed, creating new entry instead: {str(e)}")
                            # Fallback to creating a new entry
                            update_data.update({
                                "user": username,
                                "requirements": requirements,
                                "context": "",
                                "project_id": project_id,
                                "requirement_id": requirement_id,
                                "requirement_title": requirement_title
                            })
                            history_collection.insert_one(update_data)
                    else:
                        # Create new history entry if no active_history_id
                        update_data.update({
                            "user": username,
                            "requirements": requirements,
                            "context": "",
                            "project_id": project_id,
                            "requirement_id": requirement_id,
                            "requirement_title": requirement_title
                        })
                        history_collection.insert_one(update_data)
                    
                    # Send updated test cases and confirmation to the client
                    yield f"data: {json.dumps({
                        'updated_test_cases': updated_test_cases,
                        'confirmation': 'Modifications appliquées.'
                    })}\n\n"
                    
                    print("Saved updated test cases to history")
            
            # Save the chat interaction to history
            history_collection.insert_one({
                "user": username,
                "type": "ai_chat",
                "message": user_message,
                "response": full_response,
                "timestamp": datetime.now(timezone.utc),
                "project_id": project_id,
                "requirement_id": requirement_id
            })
            
            yield "data: [DONE]\n\n"
            
        except Exception as e:
            print(f"Error in chat assistant: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"
    
    return Response(generate(), content_type="text/event-stream", headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive'
    })
@app.route("/history", methods=["GET"])
@login_required
@limiter.exempt
def get_history():
    try:
        username = session["user"]
        limit = int(request.args.get("limit", 20))
        skip = int(request.args.get("skip", 0))
        project_id = request.args.get("project_id")
        requirement_id = request.args.get("requirement_id")
        
        # Print debugging information
        print(f"Fetching history for user: {username}")
        print(f"Project ID: {project_id}")
        print(f"Requirement ID: {requirement_id}")
        
        # Build query for filtering history
        query = {"user": username}
        
        if project_id:
            query["project_id"] = project_id
        
        if requirement_id:
            query["requirement_id"] = requirement_id
        
        # Only fetch test case records, not chat records
        query["test_cases"] = {"$exists": True}
        
        print(f"Query: {query}")
        
        # Get history records
        history = list(history_collection.find(query)
            .sort("timestamp", -1)
            .skip(skip)
            .limit(limit))
        
        print(f"Found {len(history)} history records")
        
        for item in history:
            item["_id"] = str(item["_id"])
            # If the timestamp is a datetime object, convert it to ISO string
            if isinstance(item.get("timestamp"), datetime):
                item["timestamp"] = item["timestamp"].isoformat()
            
            # Add update type if available
            if "update_type" in item:
                item["update_source"] = (
                    "AI Assistant" if item["update_type"] == "ai_assistant" else
                    "Manual Edit" if item["update_type"] == "manual_edit" else
                    "Generated" 
                )
        
        return jsonify({"history": history})
    except Exception as e:
        print(f"Error in get_history: {e}")
        # Return empty history on error, don't fail
        return jsonify({"history": [], "error": str(e)})
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

# Add this new endpoint to app.py

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
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

if __name__ == "__main__":
    app.run(debug=True, port=5000, host='0.0.0.0')
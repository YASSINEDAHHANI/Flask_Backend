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

load_dotenv()

app = Flask(__name__)
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

# Rate limiter configuration
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["30 per minute"]
)

# File upload configuration
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

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

def get_user_api_key(username, project_id=None):
    if project_id:
        project_key = api_keys_collection.find_one({
            "user": username,
            "project_id": project_id
        })
        if project_key:
            return project_key["api_key"]
    
    user_key = api_keys_collection.find_one({
        "user": username,
        "project_id": {"$exists": False}
    })
    if user_key:
        return user_key["api_key"]
    
    return os.getenv("API_KEY")

def get_anthropic_client(username, project_id=None):
    api_key = get_user_api_key(username, project_id)
    if not api_key:
        raise ValueError("No API key available")
    return anthropic.Anthropic(api_key=api_key)

def extract_text_from_pdf(pdf_file):
    text = ""
    try:
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"
    except Exception as e:
        return str(e)
    return text.strip()

def extract_text_from_docx(docx_file):
    text = ""
    try:
        doc = docx.Document(docx_file)
        for para in doc.paragraphs:
            text += para.text + "\n"
    except Exception as e:
        return str(e)
    return text.strip()

def generate_test_cases(requirements, format_type, context="", example_case=""):
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
            "email": user.get("email")
        })
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/register", methods=["POST"])
def register():
    data = request.json
    username = data.get("username")
    password = data.get("password")
    email = data.get("email")

    if not all([username, password, email]):
        return jsonify({"error": "Missing required fields"}), 400

    if users_collection.find_one({"username": username}):
        return jsonify({"error": "Username already exists"}), 400

    if users_collection.find_one({"email": email}):
        return jsonify({"error": "Email already registered"}), 400

    user = {
        "username": username,
        "password": password,
        "email": email,
        "created_at": datetime.now(timezone.utc)
    }

    users_collection.insert_one(user)
    return jsonify({"message": "Registration successful"})

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
                "email": user.get("email")
            })
    return jsonify({"logged_in": False}), 200

# API Key Management
@app.route("/api_keys", methods=["GET"])
@login_required
def get_api_keys():
    username = session["user"]
    keys = list(api_keys_collection.find({"user": username}))
    
    for key in keys:
        key["_id"] = str(key["_id"])
        key["api_key"] = "*****" + key["api_key"][-4:] if key.get("api_key") else ""
    
    return jsonify({"api_keys": keys})

@app.route("/api_keys", methods=["POST"])
@login_required
def create_api_key():
    username = session["user"]
    data = request.json
    api_key = data.get("api_key")
    project_id = data.get("project_id")
    
    if not api_key:
        return jsonify({"error": "API key is required"}), 400
    
    query = {"user": username}
    if project_id:
        query["project_id"] = project_id
    else:
        query["project_id"] = {"$exists": False}
    
    existing_key = api_keys_collection.find_one(query)
    
    if existing_key:
        api_keys_collection.update_one(
            {"_id": existing_key["_id"]},
            {"$set": {"api_key": api_key}}
        )
    else:
        key_data = {
            "user": username,
            "api_key": api_key,
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
    collaborator_username = data.get("username")
    
    if not collaborator_username:
        return jsonify({"error": "Username is required"}), 400
    
    # Verify user owns this project
    project = projects_collection.find_one({
        "id": project_id,
        "user": username
    })
    
    if not project:
        return jsonify({"error": "Project not found or you don't have permission"}), 404
    
    # Check if collaborator exists
    collaborator = users_collection.find_one({"username": collaborator_username})
    if not collaborator:
        return jsonify({"error": "User not found"}), 404
    
    # Check if already a collaborator
    if collaborator_username in project.get("collaborators", []):
        return jsonify({"error": "User is already a collaborator"}), 400
    
    # Add to project collaborators
    projects_collection.update_one(
        {"id": project_id},
        {"$addToSet": {"collaborators": collaborator_username}}
    )
    
    # Add to collaborators collection
    collaborators_collection.insert_one({
        "project_id": project_id,
        "username": collaborator_username,
        "email": collaborator.get("email"),
        "added_by": username,
        "added_at": datetime.now(timezone.utc)
    })
    
    return jsonify({
        "message": "Collaborator added successfully",
        "collaborator": {
            "username": collaborator_username,
            "email": collaborator.get("email")
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
    
    projects_collection.insert_one(project)
    
    return jsonify({"message": "Project created", "project": project})

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
    
    requirement = {
        "id": str(uuid.uuid4()),
        "user": username,
        "project_id": project_id,
        "title": data.get("title"),
        "description": data.get("description", ""),
        "category": data.get("category", "functional"),
        "priority": data.get("priority", "medium"),
        "status": data.get("status", "draft"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    
    result = requirements_collection.insert_one(requirement)
    requirement["_id"] = str(result.inserted_id)
    
    return jsonify({"message": "Requirement created", "requirement": requirement})

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

# Test Case Generation
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
    
    test_case_instruction = generate_test_cases(requirements, format_type, context, example_case)
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
    
    test_case_instruction = generate_test_cases(
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
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), content_type="text/event-stream")

# Chat with Assistant
@app.route("/chat_with_assistant", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
def chat_with_assistant():
    data = request.json
    user_message = data.get("message", "")
    test_cases = data.get("test_cases", "")
    project_id = data.get("project_id", "")
    requirement_id = data.get("requirement_id", "")
    chat_history = data.get("chat_history", [])
    
    username = session["user"]
    
    context_parts = ["You are a test case assistant helping to improve test cases."]
    
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
            project = projects_collection.find_one({
                "id": requirement["project_id"],
                "$or": [
                    {"user": username},
                    {"collaborators": username}
                ]
            })
            
            if project:
                context_parts.append(f"Requirement: {requirement.get('title', '')}\n{requirement.get('description', '')}")
    
    context_parts.append(f"Current test cases:\n{test_cases}")
    context_parts.append(f"User message: {user_message}")
    
    if chat_history:
        history_context = "\n".join([f"{msg['role']}: {msg['content']}" for msg in chat_history])
        context_parts.append(f"Conversation history:\n{history_context}")
    
    context = "\n\n".join(context_parts)
    
    def generate():
        try:
            full_response = ""
            anthropic_client = get_anthropic_client(username, project_id)
            
            messages = [{"role": "user", "content": context}]
            
            with anthropic_client.messages.stream(
                model="claude-3-haiku-20240307",
                max_tokens=2000,
                messages=messages
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.text:
                            full_response += event.delta.text
                            yield f"data: {json.dumps({'chunk': event.delta.text})}\n\n"
            
            history_collection.insert_one({
                "user": username,
                "type": "ai_chat",
                "message": user_message,
                "response": full_response,
                "timestamp": datetime.now(timezone.utc),
                "project_id": project_id,
                "requirement_id": requirement_id,
                "test_cases": test_cases
            })
            
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"
    
    return Response(generate(), content_type="text/event-stream")

# History Management
@app.route("/history", methods=["GET"])
@login_required
def get_history():
    username = session["user"]
    limit = int(request.args.get("limit", 10))
    skip = int(request.args.get("skip", 0))
    
    history = list(history_collection.find({"user": username})
        .sort("timestamp", -1)
        .skip(skip)
        .limit(limit))
    
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

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

if __name__ == "__main__":
    app.run(debug=True, port=5000, host='0.0.0.0')
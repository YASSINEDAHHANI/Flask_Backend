import os
from flask import Flask, request, jsonify, session, send_file, Response
from flask_cors import CORS
from werkzeug.utils import secure_filename
import PyPDF2
import docx
import anthropic
from dotenv import load_dotenv
from pymongo import MongoClient
from datetime import datetime, timezone
import langdetect
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
import json
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecret")

# Configure CORS with credentials support
CORS(app, 
     supports_credentials=True,
     resources={
         r"/*": {
             "origins": ["http://localhost:3000"],
             "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
             "allow_headers": ["Content-Type", "Authorization"]
         }
     })

# Rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["5 per minute"]
)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# API Key for Claude AI
API_KEY = os.getenv("API_KEY")
if not API_KEY:
    raise ValueError("❌ API Key for Anthropic (Claude AI) is missing!")
client = anthropic.Anthropic(api_key=API_KEY)

# MongoDB Configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["chat_app"]
history_collection = db["chat_history"]
users_collection = db["users"]

# File Processing Functions
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
    if not API_KEY:
        return "Error: API Key not found in environment variables"
    
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

# Authentication Routes
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
            "username": username
        })
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return jsonify({"message": "Logged out successfully"})

@app.route("/check_session", methods=["GET"])
def check_session():
    if "user" in session:
        return jsonify({"logged_in": True, "username": session["user"]})
    return jsonify({"logged_in": False}), 401

# API Routes
@app.route("/")
def home():
    return jsonify({"message": "Flask Backend is Running!"})

@app.route("/upload", methods=["POST"])
def upload_file():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
        
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400
    
    filename = secure_filename(file.filename)
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(file_path)

    extracted_text = ""
    if filename.endswith(".pdf"):
        extracted_text = extract_text_from_pdf(file_path)
    elif filename.endswith(".docx"):
        extracted_text = extract_text_from_docx(file_path)
    else:
        return jsonify({"error": "Unsupported file format"}), 400

    return jsonify({
        "extracted_text": extracted_text, 
        "filename": filename
    })

@app.route("/generate_test_cases_stream", methods=["POST"])
@limiter.limit("5 per minute")
def generate_test_cases_stream():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    requirements = data.get("requirements", "")
    format_type = data.get("format_type", "default")
    context = data.get("context", "")
    example_case = data.get("example_case", "")
    
    if not requirements:
        return jsonify({"error": "No requirements provided"}), 400
    
    test_case_instruction = generate_test_cases(requirements, format_type, context, example_case)
    username = session["user"]

    def generate():
        try:
            full_response = ""
            with client.messages.stream(
                model="claude-3-5-haiku-20241022",
                max_tokens=8000,
                messages=[{"role": "user", "content": test_case_instruction}]
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.text:
                            full_response += event.delta.text
                            yield f"data: {json.dumps({'chunk': event.delta.text})}\n\n"
                    elif event.type == "message_stop":
                        history_entry = {
                            "user": username,  
                            "test_cases": full_response,
                            "timestamp": datetime.now(timezone.utc),
                            "requirements": requirements,
                            "context": context
                        }
                        history_collection.insert_one(history_entry)
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), mimetype="text/event-stream")

@app.route('/download_pdf', methods=['POST'])
def download_pdf():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json
    test_cases = data.get("test_cases", "")
    
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    
    # PDF Styling
    p.setFont("Helvetica-Bold", 16)
    p.drawString(100, 780, "Test Cases Report")
    p.setFont("Helvetica", 10)
    
    y_position = 730
    for line in test_cases.split('\n'):
        if line.startswith('**') and line.endswith('**'):
            p.setFont("Helvetica-Bold", 12)
            p.drawString(100, y_position, line.replace('**', ''))
            p.setFont("Helvetica", 10)
        elif line.strip().startswith('Scenario'):
            p.setFont("Helvetica-Bold", 11)
            p.drawString(100, y_position, line)
            p.setFont("Helvetica", 10)
        else:
            p.drawString(100, y_position, line)
        
        y_position -= 15
        if y_position < 50:
            p.showPage()
            y_position = 750
    
    p.save()
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="test_cases.pdf",
        mimetype="application/pdf"
    )

@app.route('/download_docx', methods=['POST'])
def download_docx():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json
    test_cases = data.get("test_cases", "")
    
    doc = docx.Document()
    doc.add_heading('Test Cases Report', 0)
    
    for line in test_cases.split('\n'):
        if line.startswith('**') and line.endswith('**'):
            doc.add_heading(line.replace('**', ''), level=2)
        elif line.strip().startswith('Scenario'):
            doc.add_heading(line, level=3)
        else:
            doc.add_paragraph(line)
    
    buffer = BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    
    return send_file(
        buffer,
        as_attachment=True,
        download_name="test_cases.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

# After request handler for CORS
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', 'http://localhost:3000')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

if __name__ == "__main__":
    app.run(debug=True, port=5000)
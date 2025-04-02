import os
from flask import Flask, request, jsonify, session
from flask_cors import CORS
from werkzeug.utils import secure_filename
import PyPDF2
import docx
import anthropic
from dotenv import load_dotenv
from pymongo import MongoClient
from datetime import datetime, timezone
import langdetect

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv("SECRET_KEY", "supersecret")

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# API Key for Claude AI
API_KEY = os.getenv("API_KEY")

if not API_KEY:
    raise ValueError("❌ API Key for Anthropic (Claude AI) is missing!")
client = anthropic.Anthropic(api_key=API_KEY)

# MongoDB Configuration (Abstracted for Future Flexibility)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["chat_app"]
history_collection = db["chat_history"]
users_collection = db["users"]

# ========== FILE PROCESSING FUNCTIONS ==========

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

def generate_response_with_claude(user_input):
    try:
        response = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=8000,
            messages=[{"role": "user", "content": user_input}]
        )
        return response.content[0].text
    except Exception as e:
        return f"Error: {str(e)}"

# ========== AUTHENTICATION ==========

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username")
    password = data.get("password")

    user = users_collection.find_one({"username": username, "password": password})
    if user:
        session["user"] = username
        return jsonify({"message": "Login successful", "username": username})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return jsonify({"message": "Logged out successfully"})

# ========== API ROUTES ==========

@app.route("/")
def home():
    return jsonify({"message": "Flask Backend is Running!"})

@app.route("/upload", methods=["POST"])
def upload_file():
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

    return jsonify({"extracted_text": extracted_text, "filename": filename})

# Function to generate test cases based on requirements
def generate_test_cases(requirements, format_type, context="", example_case=""):
    if not API_KEY:
        return "Erreur: Clé API non trouvée dans les variables d'environnement. Vérifiez votre fichier .env"
    
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

@app.route("/generate_test_cases", methods=["POST"])
def generate_test_cases_endpoint():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    requirements = data.get("requirements", "")
    format_type = data.get("format_type", "default")
    context = data.get("context", "")
    example_case = data.get("example_case", "")
    
    if not requirements:
        return jsonify({"error": "No requirements provided"}), 400
    
    # Generate the test case instructions
    test_case_instruction = generate_test_cases(requirements, format_type, context, example_case)
    
    # Use Claude AI to generate the response
    ai_output = generate_response_with_claude(test_case_instruction)
    
    # Save generated test cases to the database for the logged-in user
    history_entry = {
        "user": session["user"],
        "test_cases": ai_output,
        "timestamp": datetime.now(timezone.utc)
    }
    history_collection.insert_one(history_entry)
    
    return jsonify({"message": "Test cases generated", "test_cases": ai_output})

@app.route("/history", methods=["GET"])
def get_history():
    if "user" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    history = list(history_collection.find({"user": session["user"]}, {"_id": 0}))
    return jsonify({"history": history})

if __name__ == "__main__":
    app.run(debug=True, port=5000)

import os
from flask import Blueprint, jsonify, request, session
from functools import wraps
from datetime import datetime, timezone
import uuid
from bson import ObjectId
from minio import Minio
from minio.error import S3Error
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# These will be initialized when the blueprint is registered
users_collection = None
projects_collection = None
collaborators_collection = None
api_keys_collection = None
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "rag-documents")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"
minio_client = None
def init_admin_collections(app_users, app_projects, app_collaborators, app_api_keys=None):
    """Initialize collections for admin blueprint"""
    global users_collection, projects_collection, collaborators_collection, api_keys_collection, minio_client
    users_collection = app_users
    projects_collection = app_projects
    collaborators_collection = app_collaborators
    api_keys_collection = app_api_keys
    
    # Initialize MinIO client
    try:
        minio_client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE
        )
        
        # Ensure bucket exists
        if not minio_client.bucket_exists(MINIO_BUCKET):
            minio_client.make_bucket(MINIO_BUCKET)
            print(f"✅ Created MinIO bucket: {MINIO_BUCKET}")
        else:
            print(f"✅ MinIO bucket {MINIO_BUCKET} already exists")
            
    except Exception as e:
        print(f"⚠️ Failed to initialize MinIO client: {e}")
        minio_client = None

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Unauthorized"}), 401
            
        user = users_collection.find_one({"username": session["user"]})
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
            
        return f(*args, **kwargs)
    return decorated_function

def manager_or_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Unauthorized"}), 401
            
        user = users_collection.find_one({"username": session["user"]})
        if not user or user.get("role") not in ["manager", "admin"]:
            return jsonify({"error": "Manager or admin access required"}), 403
            
        return f(*args, **kwargs)
    return decorated_function
# ========== NEW MINIO DOCUMENT MANAGEMENT ENDPOINTS ==========

@admin_bp.route("/upload_document", methods=["POST"])
@admin_required
def admin_upload_document():
    """Admin endpoint to upload documents to MinIO for RAG processing"""
    username = session["user"]
    
    if not minio_client:
        return jsonify({"error": "MinIO not available"}), 503
    
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    # Check file type
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"error": "Only PDF files are supported"}), 400

    try:
        # Read file data
        file_data = file.read()
        original_filename = file.filename
        
        # Generate unique filename to avoid conflicts
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_filename = f"{timestamp}_{original_filename}"
        
        # Upload to MinIO
        from io import BytesIO
        file_stream = BytesIO(file_data)
        minio_client.put_object(
            MINIO_BUCKET, 
            unique_filename, 
            file_stream, 
            length=len(file_data),
            content_type="application/pdf"
        )
        
        print(f"📁 Document uploaded to MinIO: {unique_filename} by {username}")
        
        # Try to process with RAG if available
        processing_result = None
        try:
            # Import here to avoid circular import
            from app import minio_rag_system
            if minio_rag_system:
                processing_result = minio_rag_system.process_single_document(file_data, unique_filename)
        except Exception as e:
            print(f"⚠️ RAG processing failed: {e}")
            processing_result = False
        
        return jsonify({
            "message": f"Document '{original_filename}' uploaded successfully to MinIO",
            "filename": unique_filename,
            "original_filename": original_filename,
            "uploaded_by": username,
            "upload_timestamp": datetime.now().isoformat(),
            "rag_processed": processing_result,
            "size_bytes": len(file_data)
        })
        
    except S3Error as e:
        print(f"❌ MinIO upload error: {e}")
        return jsonify({"error": f"Failed to upload to MinIO: {str(e)}"}), 500
    except Exception as e:
        print(f"❌ Upload error: {e}")
        return jsonify({"error": f"Failed to upload document: {str(e)}"}), 500

@admin_bp.route("/list_documents", methods=["GET"])
@admin_required
def admin_list_documents():
    """Admin endpoint to list all documents in MinIO"""
    if not minio_client:
        return jsonify({"error": "MinIO not available"}), 503
    
    try:
        # Get list of files from MinIO
        objects = minio_client.list_objects(MINIO_BUCKET)
        files = []
        total_size = 0
        
        for obj in objects:
            file_info = {
                "filename": obj.object_name,
                "size": obj.size,
                "last_modified": obj.last_modified.isoformat() if obj.last_modified else None,
                "etag": obj.etag
            }
            files.append(file_info)
            total_size += obj.size
        
        # Get RAG stats if available
        rag_stats = None
        try:
            from app import minio_rag_system
            if minio_rag_system:
                rag_stats = minio_rag_system.get_database_stats()
        except Exception as e:
            print(f"⚠️ Failed to get RAG stats: {e}")
        
        return jsonify({
            "files": files,
            "total_files": len(files),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "bucket": MINIO_BUCKET,
            "rag_stats": rag_stats
        })
        
    except S3Error as e:
        print(f"❌ MinIO list error: {e}")
        return jsonify({"error": f"Failed to list documents: {str(e)}"}), 500
    except Exception as e:
        print(f"❌ List error: {e}")
        return jsonify({"error": f"Failed to list documents: {str(e)}"}), 500

@admin_bp.route("/delete_document", methods=["DELETE"])
@admin_required
def admin_delete_document():
    """Admin endpoint to delete a document from MinIO"""
    username = session["user"]
    
    if not minio_client:
        return jsonify({"error": "MinIO not available"}), 503
    
    data = request.json
    filename = data.get("filename")
    
    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    
    try:
        # Delete file from MinIO
        minio_client.remove_object(MINIO_BUCKET, filename)
        
        print(f"🗑️ Document deleted from MinIO: {filename} by {username}")
        
        return jsonify({
            "message": f"Document '{filename}' deleted successfully",
            "filename": filename,
            "deleted_by": username,
            "deletion_timestamp": datetime.now().isoformat()
        })
        
    except S3Error as e:
        print(f"❌ MinIO delete error: {e}")
        return jsonify({"error": f"Failed to delete from MinIO: {str(e)}"}), 500
    except Exception as e:
        print(f"❌ Delete error: {e}")
        return jsonify({"error": f"Failed to delete document: {str(e)}"}), 500

@admin_bp.route("/rebuild_rag_index", methods=["POST"])
@admin_required
def admin_rebuild_rag_index():
    """Admin endpoint to rebuild the RAG index from all documents in MinIO"""
    username = session["user"]
    
    try:
        # Import here to avoid circular import
        from app import minio_rag_system
        if not minio_rag_system:
            return jsonify({"error": "MinIO RAG system not available"}), 503
        
        # Clear existing vector store
        import shutil
        persist_dir = minio_rag_system.persist_directory
        if os.path.exists(persist_dir):
            shutil.rmtree(persist_dir)
            print(f"🔄 Cleared existing vector store: {persist_dir}")
        
        # Reset vector store
        minio_rag_system.vector_store = None
        minio_rag_system._setup_vector_store()
        
        # Reprocess all documents
        success = minio_rag_system.process_documents_from_minio()
        
        if not success:
            return jsonify({"error": "Failed to rebuild RAG index"}), 500
        
        # Get updated stats
        stats = minio_rag_system.get_database_stats()
        
        print(f"✅ RAG index rebuilt by {username}")
        
        return jsonify({
            "message": "RAG index rebuilt successfully",
            "rebuilt_by": username,
            "rebuild_timestamp": datetime.now().isoformat(),
            "stats": stats
        })
        
    except Exception as e:
        print(f"❌ RAG rebuild error: {e}")
        return jsonify({"error": f"Failed to rebuild RAG index: {str(e)}"}), 500

@admin_bp.route("/rag_stats", methods=["GET"])
@admin_required
def admin_rag_stats():
    """Admin endpoint to get RAG system statistics"""
    try:
        # Get MinIO file list
        files = []
        total_size = 0
        
        if minio_client:
            try:
                objects = minio_client.list_objects(MINIO_BUCKET)
                for obj in objects:
                    files.append(obj.object_name)
                    total_size += obj.size
            except Exception as e:
                print(f"⚠️ Failed to list MinIO files: {e}")
        
        # Get RAG stats
        rag_stats = None
        try:
            from app import minio_rag_system
            if minio_rag_system:
                rag_stats = minio_rag_system.get_database_stats()
        except Exception as e:
            print(f"⚠️ Failed to get RAG stats: {e}")
        
        return jsonify({
            "minio_info": {
                "endpoint": MINIO_ENDPOINT,
                "bucket": MINIO_BUCKET,
                "total_files": len(files),
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "files": files
            },
            "rag_stats": rag_stats,
            "system_status": {
                "minio_available": minio_client is not None,
                "rag_available": rag_stats is not None
            }
        })
        
    except Exception as e:
        print(f"❌ Stats error: {e}")
        return jsonify({"error": f"Failed to get stats: {str(e)}"}), 500

@admin_bp.route("/download_document/<filename>", methods=["GET"])
@admin_required
def admin_download_document(filename):
    """Admin endpoint to download a document from MinIO"""
    if not minio_client:
        return jsonify({"error": "MinIO not available"}), 503
    
    try:
        # Get object from MinIO
        response = minio_client.get_object(MINIO_BUCKET, filename)
        data = response.read()
        response.close()
        response.release_conn()
        
        # Return file as download
        from flask import send_file
        from io import BytesIO
        
        return send_file(
            BytesIO(data),
            as_attachment=True,
            download_name=filename,
            mimetype="application/pdf"
        )
        
    except S3Error as e:
        print(f"❌ MinIO download error: {e}")
        return jsonify({"error": f"Failed to download from MinIO: {str(e)}"}), 500
    except Exception as e:
        print(f"❌ Download error: {e}")
        return jsonify({"error": f"Failed to download document: {str(e)}"}), 500
# User management endpoints
@admin_bp.route("/users", methods=["GET"])
@admin_required
def get_all_users():
    """Get all users"""
    users = list(users_collection.find({}))
    
    # Convert ObjectId to string
    for user in users:
        user["_id"] = str(user["_id"])
        # Remove password for security
        if "password" in user:
            user["password"] = "********"
    
    return jsonify({"users": users})

@admin_bp.route("/users/<user_id>", methods=["GET"])
@admin_required
def get_user(user_id):
    """Get a specific user by ID"""
    try:
        if ObjectId.is_valid(user_id):
            user = users_collection.find_one({"_id": ObjectId(user_id)})
        else:
            user = users_collection.find_one({"username": user_id})
            
        if not user:
            return jsonify({"error": "User not found"}), 404
            
        user["_id"] = str(user["_id"])
        # Remove password for security
        if "password" in user:
            user["password"] = "********"
            
        return jsonify({"user": user})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/users", methods=["POST"])
@admin_required
def create_user():
    """Create a new user"""
    data = request.json
    
    # Validate required fields
    if not data.get("username") or not data.get("password"):
        return jsonify({"error": "Username and password are required"}), 400
    
    # Validate role
    valid_roles = ["user", "manager", "admin"]
    role = data.get("role", "user")
    if role not in valid_roles:
        return jsonify({"error": f"Invalid role. Must be one of: {valid_roles}"}), 400
    
    # Check if username already exists
    existing_user = users_collection.find_one({"username": data["username"]})
    if existing_user:
        return jsonify({"error": "Username already exists"}), 400
    
    # Create new user
    new_user = {
        "username": data["username"],
        "password": data["password"],
        "email": data.get("email", data["username"]),
        "role": role,
        "created_at": datetime.now(timezone.utc),
        "created_by": session["user"]
    }
    
    result = users_collection.insert_one(new_user)
    new_user["_id"] = str(result.inserted_id)
    
    # Remove password from response
    new_user["password"] = "********"
    
    return jsonify({"message": "User created successfully", "user": new_user}), 201

@admin_bp.route("/users/<user_id>", methods=["PUT"])
@admin_required
def update_user(user_id):
    """Update a user"""
    data = request.json
    
    try:
        # Find the user
        if ObjectId.is_valid(user_id):
            user = users_collection.find_one({"_id": ObjectId(user_id)})
            if not user:
                return jsonify({"error": "User not found"}), 404
            user_filter = {"_id": ObjectId(user_id)}
        else:
            user = users_collection.find_one({"username": user_id})
            if not user:
                return jsonify({"error": "User not found"}), 404
            user_filter = {"username": user_id}
        
        # Prepare update data
        update_data = {}
        
        # Allow updating certain fields
        if "email" in data:
            update_data["email"] = data["email"]
        if "role" in data:
            valid_roles = ["user", "manager", "admin"]
            if data["role"] in valid_roles:
                update_data["role"] = data["role"]
            else:
                return jsonify({"error": f"Invalid role. Must be one of: {valid_roles}"}), 400
        if "password" in data:
            update_data["password"] = data["password"]
        
        # Add updated_at timestamp
        update_data["updated_at"] = datetime.now(timezone.utc)
        update_data["updated_by"] = session["user"]
        
        # Update the user
        if update_data:
            users_collection.update_one(user_filter, {"$set": update_data})
            
        # Get updated user
        updated_user = users_collection.find_one(user_filter)
        updated_user["_id"] = str(updated_user["_id"])
        
        # Remove password from response
        if "password" in updated_user:
            updated_user["password"] = "********"
            
        return jsonify({"message": "User updated successfully", "user": updated_user})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/users/<user_id>", methods=["DELETE"])
@admin_required
def delete_user(user_id):
    """Delete a user"""
    try:
        # Find the user
        if ObjectId.is_valid(user_id):
            user = users_collection.find_one({"_id": ObjectId(user_id)})
            if not user:
                return jsonify({"error": "User not found"}), 404
            user_filter = {"_id": ObjectId(user_id)}
        else:
            user = users_collection.find_one({"username": user_id})
            if not user:
                return jsonify({"error": "User not found"}), 404
            user_filter = {"username": user_id}
        
        # Don't allow deleting yourself
        if user["username"] == session["user"]:
            return jsonify({"error": "Cannot delete your own account"}), 400
            
        # Delete the user
        users_collection.delete_one(user_filter)
        
        return jsonify({"message": "User deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Project management endpoints
@admin_bp.route("/projects", methods=["GET"])
@admin_required
def get_all_projects():
    """Get all projects for admin panel"""
    try:
        # Check if collections are initialized
        if projects_collection is None or collaborators_collection is None:
            return jsonify({"error": "Database collections not properly initialized"}), 500
        
        # Get all projects
        projects = list(projects_collection.find({}))
        
        # Convert ObjectId to string and add additional info
        for project in projects:
            project["_id"] = str(project["_id"])
            
            # Get collaborator count safely
            try:
                collab_count = collaborators_collection.count_documents({"project_id": project["id"]})
                project["collaborator_count"] = collab_count
            except Exception:
                project["collaborator_count"] = 0
            
            # Get requirements count safely
            try:
                # Import requirements collection from main app
                import sys
                if 'app' in sys.modules:
                    app_module = sys.modules['app']
                    if hasattr(app_module, 'requirements_collection'):
                        requirements_collection = app_module.requirements_collection
                        req_count = requirements_collection.count_documents({"project_id": project["id"]})
                        project["requirements_count"] = req_count
                    else:
                        project["requirements_count"] = 0
                else:
                    project["requirements_count"] = 0
            except Exception:
                project["requirements_count"] = 0
            
            # Get collaborator details safely
            try:
                collaborators = list(collaborators_collection.find({"project_id": project["id"]}))
                for collab in collaborators:
                    collab["_id"] = str(collab["_id"])
                project["collaborator_details"] = collaborators
            except Exception:
                project["collaborator_details"] = []
        
        return jsonify({"projects": projects})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/projects/<project_id>", methods=["GET"])
@admin_required
def get_project(project_id):
    """Get a specific project with detailed information including requirements"""
    try:
        # Check if collections are initialized
        if projects_collection is None:
            return jsonify({"error": "Projects collection not properly initialized"}), 500
            
        # Find the project
        project = projects_collection.find_one({"id": project_id})
        if not project:
            return jsonify({"error": "Project not found"}), 404
            
        project["_id"] = str(project["_id"])
        
        # Get project collaborators with detailed info
        try:
            collaborators = list(collaborators_collection.find({"project_id": project_id}))
            for collab in collaborators:
                collab["_id"] = str(collab["_id"])
            project["collaborator_details"] = collaborators
        except Exception:
            project["collaborator_details"] = []
        
        # Get project requirements safely
        try:
            import sys
            if 'app' in sys.modules:
                app_module = sys.modules['app']
                if hasattr(app_module, 'requirements_collection'):
                    requirements_collection = app_module.requirements_collection
                    requirements = list(requirements_collection.find({"project_id": project_id}))
                    for req in requirements:
                        req["_id"] = str(req["_id"])
                    project["requirements"] = requirements
                else:
                    project["requirements"] = []
            else:
                project["requirements"] = []
        except Exception:
            project["requirements"] = []
        
        # Get test cases count safely
        try:
            import sys
            if 'app' in sys.modules:
                app_module = sys.modules['app']
                if hasattr(app_module, 'history_collection'):
                    history_collection = app_module.history_collection
                    test_cases_count = history_collection.count_documents({
                        "project_id": project_id,
                        "test_cases": {"$exists": True, "$ne": ""}
                    })
                    project["test_cases_count"] = test_cases_count
                else:
                    project["test_cases_count"] = 0
            else:
                project["test_cases_count"] = 0
        except Exception:
            project["test_cases_count"] = 0
            
        return jsonify({"project": project})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/projects/<project_id>", methods=["PUT"])
@admin_required
def update_project(project_id):
    """Update a project"""
    data = request.json
    
    try:
        # Check if collections are initialized
        if projects_collection is None:
            return jsonify({"error": "Projects collection not properly initialized"}), 500
            
        # Find the project
        project = projects_collection.find_one({"id": project_id})
        if not project:
            return jsonify({"error": "Project not found"}), 404
        
        # Prepare update data
        update_data = {}
        
        # Allow updating certain fields
        if "name" in data:
            update_data["name"] = data["name"]
        if "context" in data:
            update_data["context"] = data["context"]
        if "language" in data:
            update_data["language"] = data["language"]
        
        # Add updated_at timestamp
        update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        update_data["updated_by"] = session["user"]
        
        # Update the project
        if update_data:
            result = projects_collection.update_one({"id": project_id}, {"$set": update_data})
            if result.matched_count == 0:
                return jsonify({"error": "Project not found during update"}), 404
            
        # Get updated project
        updated_project = projects_collection.find_one({"id": project_id})
        if updated_project:
            updated_project["_id"] = str(updated_project["_id"])
        else:
            return jsonify({"error": "Project not found after update"}), 404
            
        return jsonify({"message": "Project updated successfully", "project": updated_project})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/projects/<project_id>", methods=["DELETE"])
@admin_required
def delete_project(project_id):
    """Delete a project and its related data"""
    try:
        # Check if collections are initialized
        if projects_collection is None:
            return jsonify({"error": "Projects collection not properly initialized"}), 500
            
        # Find the project first
        project = projects_collection.find_one({"id": project_id})
        if not project:
            return jsonify({"error": "Project not found"}), 404
            
        # Delete the project
        result = projects_collection.delete_one({"id": project_id})
        if result.deleted_count == 0:
            return jsonify({"error": "Failed to delete project"}), 500
        
        # Delete project collaborators
        try:
            if collaborators_collection is not None:
                collaborators_collection.delete_many({"project_id": project_id})
        except Exception:
            pass  # Continue even if this fails
        
        # Delete project requirements
        try:
            import sys
            if 'app' in sys.modules:
                app_module = sys.modules['app']
                if hasattr(app_module, 'requirements_collection'):
                    requirements_collection = app_module.requirements_collection
                    requirements_collection.delete_many({"project_id": project_id})
        except Exception:
            pass  # Continue even if this fails
        
        # Delete project history
        try:
            import sys
            if 'app' in sys.modules:
                app_module = sys.modules['app']
                if hasattr(app_module, 'history_collection'):
                    history_collection = app_module.history_collection
                    history_collection.delete_many({"project_id": project_id})
        except Exception:
            pass  # Continue even if this fails
        
        return jsonify({"message": "Project and its related data deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Manager-specific endpoints
@admin_bp.route("/managed-projects", methods=["GET"])
@manager_or_admin_required
def get_managed_projects():
    """Get projects managed by the current user"""
    try:
        # Check if collections are initialized
        if projects_collection is None or collaborators_collection is None:
            return jsonify({"error": "Database collections not properly initialized"}), 500
            
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        
        if user.get("role") == "admin":
            # Admins can see all projects
            projects = list(projects_collection.find({}))
        else:
            # Managers can see only projects they created
            projects = list(projects_collection.find({"user": current_user}))
        
        # Convert ObjectId to string and add collaborator details
        for project in projects:
            project["_id"] = str(project["_id"])
            
            # Get collaborator details
            collaborators = list(collaborators_collection.find({"project_id": project["id"]}))
            for collab in collaborators:
                collab["_id"] = str(collab["_id"])
            project["collaborator_details"] = collaborators
        
        return jsonify({"projects": projects})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/assignable-users", methods=["GET"])
@manager_or_admin_required
def get_assignable_users():
    """Get users that can be assigned to projects (regular users only)"""
    try:
        # Check if collections are initialized
        if users_collection is None:
            return jsonify({"error": "Users collection not properly initialized"}), 500
            
        users = list(users_collection.find(
            {"role": {"$nin": ["manager", "admin"]}},
            {"username": 1, "email": 1, "created_at": 1, "_id": 1}
        ))
        
        # Convert ObjectId to string
        for user in users:
            user["_id"] = str(user["_id"])
        
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Replace your current get_dashboard_data function with this enhanced version:

@admin_bp.route("/dashboard", methods=["GET"])
@admin_required
def get_dashboard_data():
    """Get enhanced statistics for the admin dashboard including recent data"""
    try:
        # Check if collections are initialized
        if users_collection is None or projects_collection is None:
            return jsonify({"error": "Database collections not properly initialized"}), 500
            
        # Count users by role including managers
        users_by_role = {}
        for role in ["admin", "manager", "user"]:
            count = users_collection.count_documents({"role": role})
            users_by_role[role] = count
        
        # Also count users with no role (treat as 'user')
        no_role_count = users_collection.count_documents({"role": {"$exists": False}})
        users_by_role["user"] += no_role_count
        
        # Total users
        total_users = users_collection.count_documents({})
        
        # Total projects
        total_projects = projects_collection.count_documents({})
        
        # Count projects by creator (for contributors table)
        project_creators = list(projects_collection.aggregate([
            {"$group": {"_id": "$user", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10}
        ]))
        
        # Get recent users (last 5 created)
        recent_users = list(users_collection.find({}).sort("created_at", -1).limit(5))
        for user in recent_users:
            user["_id"] = str(user["_id"])
            if "password" in user:
                user["password"] = "********"  # Hide password for security
        
        # Get recent projects (last 5 created)
        recent_projects = list(projects_collection.find({}).sort("created_at", -1).limit(5))
        for project in recent_projects:
            project["_id"] = str(project["_id"])
        
        # Get MinIO document stats
        document_stats = {"total_documents": 0, "total_size_mb": 0, "minio_available": False}
        
        if minio_client:
            try:
                objects = minio_client.list_objects(MINIO_BUCKET)
                total_size = 0
                doc_count = 0
                
                for obj in objects:
                    doc_count += 1
                    total_size += obj.size
                
                document_stats = {
                    "total_documents": doc_count,
                    "total_size_mb": round(total_size / (1024 * 1024), 2),
                    "minio_available": True,
                    "bucket": MINIO_BUCKET
                }
            except Exception as e:
                print(f"⚠️ Error getting MinIO stats: {e}")
                document_stats["minio_available"] = False
        
        # Get RAG system stats
        rag_stats = {"rag_available": False}
        try:
            from app import minio_rag_system
            if minio_rag_system:
                rag_data = minio_rag_system.get_database_stats()
                rag_stats = {
                    "rag_available": True,
                    "total_chunks": rag_data.get("total_chunks", 0),
                    "processed_files": rag_data.get("unique_files", 0)
                }
        except Exception as e:
            print(f"⚠️ Error getting RAG stats: {e}")
        
        return jsonify({
            "users_stats": {
                "total": total_users,
                "by_role": users_by_role
            },
            "projects_stats": {
                "total": total_projects,
                "by_user": project_creators  # Add this for the contributors table
            },
            "document_stats": document_stats,
            "rag_stats": rag_stats,
            # ✅ ADD THESE NEW FIELDS:
            "recent_users": recent_users,
            "recent_projects": recent_projects
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
# Manager dashboard data
@admin_bp.route("/manager-dashboard", methods=["GET"])
@manager_or_admin_required
def get_manager_dashboard_data():
    """Get dashboard data for managers"""
    try:
        # Check if collections are initialized
        if users_collection is None or projects_collection is None or collaborators_collection is None:
            return jsonify({"error": "Database collections not properly initialized"}), 500
            
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        
        if user.get("role") == "admin":
            # Admins see all data
            managed_projects = list(projects_collection.find({}))
            total_assigned_users = users_collection.count_documents({"role": "user"})
        else:
            # Managers see only their data
            managed_projects = list(projects_collection.find({"user": current_user}))
            
            # Count unique users assigned to this manager's projects
            project_ids = [p["id"] for p in managed_projects]
            assigned_users = collaborators_collection.distinct("username", {"project_id": {"$in": project_ids}})
            total_assigned_users = len(assigned_users)
        
        # Convert ObjectId to string
        for project in managed_projects:
            project["_id"] = str(project["_id"])
        
        # Get recent assigned users
        recent_collaborators = list(collaborators_collection.aggregate([
            {"$match": {"project_id": {"$in": [p["id"] for p in managed_projects]}}},
            {"$sort": {"added_at": -1}},
            {"$limit": 5}
        ]))
        
        for collab in recent_collaborators:
            collab["_id"] = str(collab["_id"])
        
        return jsonify({
            "managed_projects": {
                "total": len(managed_projects),
                "projects": managed_projects[:5]  # Recent 5 projects
            },
            "assigned_users": {
                "total": total_assigned_users,
                "recent": recent_collaborators
            },
            "recent_activity": {
                "projects_created": len([p for p in managed_projects if p.get("created_at")]),
                "users_assigned": len(recent_collaborators)
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Endpoint to get users that managers have assigned to projects
@admin_bp.route("/manager-users", methods=["GET"])
@manager_or_admin_required
def get_manager_users():
    """Get users assigned to projects by the current manager"""
    try:
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        
        if user.get("role") == "admin":
            # Admins can see all user assignments
            collaborations = list(collaborators_collection.find({}))
        else:
            # Managers can see only users they assigned
            managed_projects = list(projects_collection.find({"user": current_user}))
            project_ids = [p["id"] for p in managed_projects]
            collaborations = list(collaborators_collection.find({"project_id": {"$in": project_ids}}))
        
        # Get unique usernames and their details
        usernames = list(set([collab["username"] for collab in collaborations]))
        users = list(users_collection.find(
            {"username": {"$in": usernames}},
            {"username": 1, "email": 1, "created_at": 1, "_id": 1}
        ))
        
        # Convert ObjectId to string and add assignment info
        for user in users:
            user["_id"] = str(user["_id"])
            user_collabs = [c for c in collaborations if c["username"] == user["username"]]
            user["projects_assigned"] = len(user_collabs)
            user["last_assigned"] = max([c.get("added_at", datetime.min.replace(tzinfo=timezone.utc)) for c in user_collabs])
        
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
from flask import Blueprint, jsonify, request, session
from functools import wraps
from datetime import datetime, timezone
import uuid
from bson import ObjectId

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

users_collection = None
projects_collection = None
collaborators_collection = None
api_keys_collection = None

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
    
    # Check if username already exists
    existing_user = users_collection.find_one({"username": data["username"]})
    if existing_user:
        return jsonify({"error": "Username already exists"}), 400
    
    # Create new user
    new_user = {
        "username": data["username"],
        "password": data["password"],
        "role": data.get("role", "user"),
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
        if "username" in data:
            update_data["username"] = data["username"]
        if "role" in data:
            update_data["role"] = data["role"]
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
    """Get all projects"""
    projects = list(projects_collection.find({}))
    
    # Convert ObjectId to string
    for project in projects:
        project["_id"] = str(project["_id"])
    
    return jsonify({"projects": projects})

@admin_bp.route("/projects/<project_id>", methods=["GET"])
@admin_required
def get_project(project_id):
    """Get a specific project and its collaborators"""
    try:
        # Find the project
        project = projects_collection.find_one({"id": project_id})
        if not project:
            return jsonify({"error": "Project not found"}), 404
            
        project["_id"] = str(project["_id"])
        
        # Get project collaborators
        collaborators = list(collaborators_collection.find({"project_id": project_id}))
        for collab in collaborators:
            collab["_id"] = str(collab["_id"])
            
        # Add collaborators to project
        project["collaborator_details"] = collaborators
            
        return jsonify({"project": project})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/projects/<project_id>", methods=["PUT"])
@admin_required
def update_project(project_id):
    """Update a project"""
    data = request.json
    
    try:
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
        
        # Add updated_at timestamp
        update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        update_data["updated_by"] = session["user"]
        
        # Update the project
        if update_data:
            projects_collection.update_one({"id": project_id}, {"$set": update_data})
            
        # Get updated project
        updated_project = projects_collection.find_one({"id": project_id})
        updated_project["_id"] = str(updated_project["_id"])
            
        return jsonify({"message": "Project updated successfully", "project": updated_project})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/projects/<project_id>", methods=["DELETE"])
@admin_required
def delete_project(project_id):
    """Delete a project and its collaborators"""
    try:
        # Find the project
        project = projects_collection.find_one({"id": project_id})
        if not project:
            return jsonify({"error": "Project not found"}), 404
            
        # Delete the project
        projects_collection.delete_one({"id": project_id})
        
        # Delete project collaborators
        collaborators_collection.delete_many({"project_id": project_id})
        
        return jsonify({"message": "Project and its collaborators deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Statistics and dashboard data
@admin_bp.route("/dashboard", methods=["GET"])
@admin_required
def get_dashboard_data():
    """Get statistics for the admin dashboard"""
    try:
        # Count users by role
        users_by_role = {}
        for role in ["admin", "user"]:
            count = users_collection.count_documents({"role": role})
            users_by_role[role] = count
        
        # Total users
        total_users = users_collection.count_documents({})
        
        # Total projects
        total_projects = projects_collection.count_documents({})
        
        # Count projects by user
        user_projects = list(projects_collection.aggregate([
            {"$group": {"_id": "$user", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10}
        ]))
        
        # Get recent users
        recent_users = list(users_collection.find({}).sort("created_at", -1).limit(5))
        for user in recent_users:
            user["_id"] = str(user["_id"])
            if "password" in user:
                user["password"] = "********"
        
        # Get recent projects
        recent_projects = list(projects_collection.find({}).sort("created_at", -1).limit(5))
        for project in recent_projects:
            project["_id"] = str(project["_id"])
        
        return jsonify({
            "users_stats": {
                "total": total_users,
                "by_role": users_by_role
            },
            "projects_stats": {
                "total": total_projects,
                "by_user": user_projects
            },
            "recent_users": recent_users,
            "recent_projects": recent_projects
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
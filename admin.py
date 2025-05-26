from flask import Blueprint, jsonify, request, session
from functools import wraps
from datetime import datetime, timezone
import uuid
from bson import ObjectId

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# These will be initialized when the blueprint is registered
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
    """Get all projects with enhanced details"""
    try:
        projects = list(projects_collection.find({}))
        
        # Convert ObjectId to string and add additional details
        for project in projects:
            project["_id"] = str(project["_id"])
            
            # Add collaborator count
            collab_count = collaborators_collection.count_documents({"project_id": project["id"]})
            project["collaborator_count"] = collab_count
            
            # Get requirements count
            try:
                from app import requirements_collection
                req_count = requirements_collection.count_documents({"project_id": project["id"]})
                project["requirements_count"] = req_count
            except (ImportError, NameError):
                project["requirements_count"] = 0
        
        return jsonify({"projects": projects})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/projects/<project_id>", methods=["GET"])
@admin_required
def get_project(project_id):
    """Get a specific project with detailed information including requirements"""
    try:
        # Find the project
        project = projects_collection.find_one({"id": project_id})
        if not project:
            return jsonify({"error": "Project not found"}), 404
            
        project["_id"] = str(project["_id"])
        
        # Get project collaborators with detailed info
        collaborators = list(collaborators_collection.find({"project_id": project_id}))
        for collab in collaborators:
            collab["_id"] = str(collab["_id"])
            
        # Add collaborators to project
        project["collaborator_details"] = collaborators
        
        # Get project requirements (if requirements_collection exists)
        try:
            # Import the requirements collection from the main app
            from app import requirements_collection
            requirements = list(requirements_collection.find({"project_id": project_id}))
            for req in requirements:
                req["_id"] = str(req["_id"])
            project["requirements"] = requirements
        except (ImportError, NameError):
            # If requirements_collection is not available, set empty list
            project["requirements"] = []
        
        # Get test cases count (if history_collection exists)
        try:
            from app import history_collection
            test_cases_count = history_collection.count_documents({
                "project_id": project_id,
                "test_cases": {"$exists": True, "$ne": ""}
            })
            project["test_cases_count"] = test_cases_count
        except (ImportError, NameError):
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

# Manager-specific endpoints
@admin_bp.route("/managed-projects", methods=["GET"])
@manager_or_admin_required
def get_managed_projects():
    """Get projects managed by the current user"""
    try:
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

# Statistics and dashboard data
@admin_bp.route("/dashboard", methods=["GET"])
@admin_required
def get_dashboard_data():
    """Get enhanced statistics for the admin dashboard"""
    try:
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
        
        # Count projects by creator role
        project_creators = list(projects_collection.aggregate([
            {"$group": {"_id": "$user", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 10}
        ]))
        
        # Enhanced project statistics by creator role
        projects_by_creator_role = {"admin": 0, "manager": 0, "user": 0}
        
        # Get all projects with their creators
        all_projects = list(projects_collection.find({}, {"user": 1, "id": 1}))
        for project in all_projects:
            creator = users_collection.find_one({"username": project["user"]})
            creator_role = creator.get("role", "user") if creator else "user"
            projects_by_creator_role[creator_role] += 1
        
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
        
        # Manager-specific statistics
        manager_stats = {
            "total_managers": users_by_role.get("manager", 0),
            "projects_by_managers": projects_by_creator_role["manager"],
            "average_projects_per_manager": 0
        }
        
        if manager_stats["total_managers"] > 0:
            manager_stats["average_projects_per_manager"] = round(
                manager_stats["projects_by_managers"] / manager_stats["total_managers"], 1
            )
        
        # Get top manager contributors
        manager_contributors = []
        managers = list(users_collection.find({"role": "manager"}, {"username": 1}))
        for manager in managers:
            project_count = projects_collection.count_documents({"user": manager["username"]})
            if project_count > 0:
                manager_contributors.append({
                    "_id": manager["username"],
                    "count": project_count,
                    "role": "manager"
                })
        
        manager_contributors.sort(key=lambda x: x["count"], reverse=True)
        
        return jsonify({
            "users_stats": {
                "total": total_users,
                "by_role": users_by_role
            },
            "projects_stats": {
                "total": total_projects,
                "by_user": project_creators,
                "by_creator_role": projects_by_creator_role
            },
            "manager_stats": manager_stats,
            "manager_contributors": manager_contributors[:5],  # Top 5 managers
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
from flask import Blueprint, jsonify, request, session
from functools import wraps
from datetime import datetime, timezone
import uuid
from bson import ObjectId

manager_bp = Blueprint('manager', __name__, url_prefix='/manager')

# These will be initialized when the blueprint is registered
users_collection = None
projects_collection = None
collaborators_collection = None
requirements_collection = None
api_keys_collection = None

def manager_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Unauthorized"}), 401
            
        user = users_collection.find_one({"username": session["user"]})
        if not user or user.get("role") not in ["manager", "admin"]:
            return jsonify({"error": "Manager or admin access required"}), 403
            
        return f(*args, **kwargs)
    return decorated_function

# Manager Dashboard
@manager_bp.route("/dashboard", methods=["GET"])
@manager_required
def get_manager_dashboard():
    """Get dashboard data for managers"""
    try:
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        user_role = user.get("role", "user")
        
        if user_role == "admin":
            # Admins see everything in manager panel too
            managed_projects = list(projects_collection.find({}))
            total_users = users_collection.count_documents({"role": {"$nin": ["manager", "admin"]}})
            all_collaborators = list(collaborators_collection.find({}))
        else:
            # Managers see only their data
            managed_projects = list(projects_collection.find({"user": current_user}))
            
            # Count unique users assigned to this manager's projects
            project_ids = [p["id"] for p in managed_projects]
            all_collaborators = list(collaborators_collection.find({"project_id": {"$in": project_ids}}))
            assigned_users = collaborators_collection.distinct("username", {"project_id": {"$in": project_ids}})
            total_users = len(assigned_users)
        
        # Convert ObjectId to string for projects
        for project in managed_projects:
            project["_id"] = str(project["_id"])
        
        # Get recent assigned users (last 5)
        recent_collaborators = list(collaborators_collection.aggregate([
            {"$match": {"project_id": {"$in": [p["id"] for p in managed_projects]}}},
            {"$sort": {"added_at": -1}},
            {"$limit": 5}
        ]))
        
        for collab in recent_collaborators:
            collab["_id"] = str(collab["_id"])
        
        # Calculate statistics
        total_requirements = 0
        if requirements_collection is not None:
            try:
                project_ids = [p["id"] for p in managed_projects]
                total_requirements = requirements_collection.count_documents({"project_id": {"$in": project_ids}})
            except:
                total_requirements = 0
        
        # Get projects by creation date (recent activity)
        recent_projects = sorted(managed_projects, key=lambda x: x.get("created_at", ""), reverse=True)[:5]
        
        # Count projects by status/activity
        active_projects = len([p for p in managed_projects if p.get("collaborators", [])])
        
        return jsonify({
            "user_info": {
                "username": current_user,
                "role": user_role,
                "is_admin": user_role == "admin"
            },
            "stats": {
                "total_projects": len(managed_projects),
                "active_projects": active_projects,
                "total_assigned_users": total_users,
                "total_requirements": total_requirements,
                "total_collaborations": len(all_collaborators)
            },
            "managed_projects": {
                "total": len(managed_projects),
                "recent": recent_projects
            },
            "assigned_users": {
                "total": total_users,
                "recent": recent_collaborators
            },
            "recent_activity": {
                "projects_created": len([p for p in managed_projects if p.get("created_at")]),
                "users_assigned": len(recent_collaborators)
            }
        })
    except Exception as e:
        print(f"Error in manager dashboard: {e}")
        return jsonify({"error": str(e)}), 500

# User Management for Managers
@manager_bp.route("/users", methods=["GET"])
@manager_required
def get_assignable_users():
    """Get users that can be assigned to projects (regular users only)"""
    try:
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        user_role = user.get("role", "user")
        
        if user_role == "admin":
            # Admins can see all regular users
            users = list(users_collection.find(
                {"role": {"$nin": ["manager", "admin"]}},
                {"username": 1, "email": 1, "created_at": 1, "created_by": 1, "_id": 1, "role": 1}
            ))
        else:
            # CHANGED: Managers can now see ALL regular users (not just the ones they created)
            users = list(users_collection.find(
                {"role": {"$nin": ["manager", "admin"]}},
                {"username": 1, "email": 1, "created_at": 1, "created_by": 1, "_id": 1, "role": 1}
            ))
        
        # Convert ObjectId to string and add assignment info
        for user in users:
            user["_id"] = str(user["_id"])
            
            # Add info about project assignments for this manager
            if user_role != "admin":
                user_projects = collaborators_collection.count_documents({
                    "username": user["username"],
                    "project_id": {"$in": [p["id"] for p in projects_collection.find({"user": current_user})]}
                })
                user["projects_assigned"] = user_projects
            else:
                # For admins, show all project assignments
                user["projects_assigned"] = collaborators_collection.count_documents({"username": user["username"]})
            
            # Check if user was created by current manager
            user["created_by_me"] = user.get("created_by") == current_user
        
        return jsonify({"users": users})
    except Exception as e:
        print(f"Error fetching users: {e}")
        return jsonify({"error": str(e)}), 500

@manager_bp.route("/users", methods=["POST"])
@manager_required
def create_user():
    """Create a new regular user (managers can only create regular users)"""
    data = request.json
    current_user = session["user"]
    
    # Validate required fields
    if not data.get("username") or not data.get("password"):
        return jsonify({"error": "Username and password are required"}), 400
    
    # Managers can only create regular users
    role = "user"  # Force role to be user for manager-created accounts
    
    # Check if username already exists
    existing_user = users_collection.find_one({"username": data["username"]})
    if existing_user:
        return jsonify({"error": "Username already exists"}), 400
    
    try:
        # Create new user
        new_user = {
            "username": data["username"],
            "password": data["password"],
            "email": data.get("email", data["username"]),
            "role": role,
            "created_at": datetime.now(timezone.utc),
            "created_by": current_user,
            "created_by_role": "manager"
        }
        
        result = users_collection.insert_one(new_user)
        new_user["_id"] = str(result.inserted_id)
        
        # Remove password from response
        new_user["password"] = "********"
        
        return jsonify({
            "message": "User created successfully", 
            "user": new_user
        }), 201
        
    except Exception as e:
        print(f"Error creating user: {e}")
        return jsonify({"error": str(e)}), 500

@manager_bp.route("/users/<user_id>", methods=["PUT"])
@manager_required
def update_user(user_id):
    """Update a user (managers can only update users they created or manage)"""
    data = request.json
    current_user = session["user"]
    
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
        
        # Check if manager has permission to update this user
        current_manager = users_collection.find_one({"username": current_user})
        if current_manager.get("role") != "admin":
            # Managers can only update users they created
            if user.get("created_by") != current_user:
                return jsonify({"error": "You can only update users you created"}), 403
        
        # Prepare update data
        update_data = {}
        
        # Allow updating certain fields
        if "email" in data:
            update_data["email"] = data["email"]
        
        # Managers cannot change user roles (always remains 'user')
        if "password" in data:
            update_data["password"] = data["password"]
        
        # Add updated_at timestamp
        update_data["updated_at"] = datetime.now(timezone.utc)
        update_data["updated_by"] = current_user
        
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
        print(f"Error updating user: {e}")
        return jsonify({"error": str(e)}), 500

@manager_bp.route("/users/<user_id>", methods=["DELETE"])
@manager_required
def delete_user(user_id):
    """Delete a user (managers can only delete users they created)"""
    current_user = session["user"]
    
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
        
        # Check if manager has permission to delete this user
        current_manager = users_collection.find_one({"username": current_user})
        if current_manager.get("role") != "admin":
            # Managers can only delete users they created
            if user.get("created_by") != current_user:
                return jsonify({"error": "You can only delete users you created"}), 403
        
        # Don't allow deleting yourself
        if user["username"] == current_user:
            return jsonify({"error": "Cannot delete your own account"}), 400
        
        # Remove user from all collaborations first
        collaborators_collection.delete_many({"username": user["username"]})
        
        # Delete the user
        users_collection.delete_one(user_filter)
        
        return jsonify({"message": "User deleted successfully"})
    except Exception as e:
        print(f"Error deleting user: {e}")
        return jsonify({"error": str(e)}), 500

# Project Management for Managers
@manager_bp.route("/projects", methods=["GET"])
@manager_required
def get_managed_projects():
    """Get projects managed by the current user with enhanced details"""
    try:
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        user_role = user.get("role", "user")
        
        if user_role == "admin":
            # Admins can see all projects
            projects = list(projects_collection.find({}))
        else:
            # Managers can see only projects they created
            projects = list(projects_collection.find({"user": current_user}))
        
        # Convert ObjectId to string and add additional details
        for project in projects:
            project["_id"] = str(project["_id"])
            project["is_owner"] = project["user"] == current_user
            
            # Add collaborator count and details
            collaborators = list(collaborators_collection.find({"project_id": project["id"]}))
            project["collaborator_count"] = len(collaborators)
            project["collaborator_details"] = collaborators
            
            # Convert collaborator ObjectIds to strings
            for collab in project["collaborator_details"]:
                collab["_id"] = str(collab["_id"])
            
            # Get requirements count - FIXED
            try:
                if requirements_collection is not None:
                    req_count = requirements_collection.count_documents({"project_id": project["id"]})
                    project["requirements_count"] = req_count
                else:
                    project["requirements_count"] = 0
            except Exception:
                project["requirements_count"] = 0
        
        return jsonify({"projects": projects})
    except Exception as e:
        print(f"Error fetching projects: {e}")
        return jsonify({"error": str(e)}), 500

@manager_bp.route("/projects", methods=["POST"])
@manager_required
def create_project_with_users():
    """Create a new project with assigned users"""
    data = request.json
    current_user = session["user"]
    
    project_name = data.get("name")
    project_context = data.get("context", "")
    assigned_users = data.get("assigned_users", [])  # List of usernames to assign as collaborators
    
    if not project_name:
        return jsonify({"error": "Project name is required"}), 400
    
    # Validate that all assigned users exist
    for user_email in assigned_users:
        user = users_collection.find_one({"username": user_email})
        if not user:
            return jsonify({"error": f"User '{user_email}' not found"}), 400
        if user.get("role") in ["manager", "admin"]:
            return jsonify({"error": f"Cannot assign manager/admin '{user_email}' as collaborator"}), 400
    
    try:
        project = {
            "id": str(uuid.uuid4()),
            "user": current_user,
            "name": project_name,
            "context": project_context,
            "collaborators": assigned_users,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by_role": "manager"
        }
        
        # Insert the project
        result = projects_collection.insert_one(project)
        project_id = project["id"]
        
        # Add collaborator records for each assigned user
        for user_email in assigned_users:
            collaborators_collection.insert_one({
                "project_id": project_id,
                "username": user_email,
                "email": user_email,
                "added_by": current_user,
                "added_at": datetime.now(timezone.utc),
                "assigned_by_manager": True
            })
        
        # Create response project
        response_project = project.copy()
        response_project["_id"] = str(result.inserted_id)
        
        return jsonify({
            "message": f"Project created and assigned to {len(assigned_users)} users",
            "project": response_project
        })
    except Exception as e:
        print(f"Error creating project: {e}")
        return jsonify({"error": str(e)}), 500

@manager_bp.route("/projects/<project_id>", methods=["GET"])
@manager_required
def get_project_details(project_id):
    """Get detailed project information including requirements and collaborators"""
    try:
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        user_role = user.get("role", "user")
        
        # Find the project
        project = projects_collection.find_one({"id": project_id})
        if not project:
            return jsonify({"error": "Project not found"}), 404
        
        # Check access permissions
        if user_role != "admin" and project["user"] != current_user:
            return jsonify({"error": "Access denied"}), 403
            
        project["_id"] = str(project["_id"])
        project["is_owner"] = project["user"] == current_user
        
        # Get project collaborators with detailed info
        collaborators = list(collaborators_collection.find({"project_id": project_id}))
        for collab in collaborators:
            collab["_id"] = str(collab["_id"])
            
        # Add collaborators to project
        project["collaborator_details"] = collaborators
        
        # Get project requirements - FIXED
        try:
            if requirements_collection is not None:
                requirements = list(requirements_collection.find({"project_id": project_id}))
                for req in requirements:
                    req["_id"] = str(req["_id"])
                project["requirements"] = requirements
            else:
                project["requirements"] = []
        except Exception:
            project["requirements"] = []
        
        return jsonify({"project": project})
    except Exception as e:
        print(f"Error fetching project details: {e}")
        return jsonify({"error": str(e)}), 500

@manager_bp.route("/projects/<project_id>", methods=["DELETE"])
@manager_required
def delete_project(project_id):
    """Delete a project (managers can only delete their own projects)"""
    try:
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        user_role = user.get("role", "user")
        
        # Find the project
        project = projects_collection.find_one({"id": project_id})
        if not project:
            return jsonify({"error": "Project not found"}), 404
        
        # Check permissions
        if user_role != "admin" and project["user"] != current_user:
            return jsonify({"error": "You can only delete projects you own"}), 403
            
        # Delete the project
        projects_collection.delete_one({"id": project_id})
        
        # Delete project collaborators
        collaborators_collection.delete_many({"project_id": project_id})
        
        # Delete project requirements - FIXED
        try:
            if requirements_collection is not None:
                requirements_collection.delete_many({"project_id": project_id})
        except Exception:
            pass  # Continue even if requirements deletion fails
        
        return jsonify({"message": "Project and its associated data deleted successfully"})
    except Exception as e:
        print(f"Error deleting project: {e}")
        return jsonify({"error": str(e)}), 500

# Statistics for Manager
@manager_bp.route("/stats", methods=["GET"])
@manager_required
def get_manager_stats():
    """Get comprehensive statistics for the manager"""
    try:
        current_user = session["user"]
        user = users_collection.find_one({"username": current_user})
        user_role = user.get("role", "user")
        
        if user_role == "admin":
            # Admin stats - all data
            total_projects = projects_collection.count_documents({})
            total_users = users_collection.count_documents({"role": {"$nin": ["manager", "admin"]}})
            total_collaborations = collaborators_collection.count_documents({})
            
            # Recent activity
            recent_projects = list(projects_collection.find({}).sort("created_at", -1).limit(5))
            recent_users = list(users_collection.find({"role": {"$nin": ["manager", "admin"]}}).sort("created_at", -1).limit(5))
        else:
            # Manager stats - only their data
            total_projects = projects_collection.count_documents({"user": current_user})
            
            # Get projects managed by this manager
            managed_projects = list(projects_collection.find({"user": current_user}))
            project_ids = [p["id"] for p in managed_projects]
            
            # Count users assigned to manager's projects
            assigned_usernames = collaborators_collection.distinct("username", {"project_id": {"$in": project_ids}})
            total_users = len(assigned_usernames)
            
            # Count collaborations in manager's projects
            total_collaborations = collaborators_collection.count_documents({"project_id": {"$in": project_ids}})
            
            # Recent activity in manager's projects
            recent_projects = managed_projects[-5:] if len(managed_projects) >= 5 else managed_projects
            
            # Recent users created by or assigned to this manager
            recent_users = list(users_collection.find({
                "$or": [
                    {"created_by": current_user},
                    {"username": {"$in": assigned_usernames}}
                ]
            }).sort("created_at", -1).limit(5))
        
        # Process recent data
        for project in recent_projects:
            if "_id" in project:
                project["_id"] = str(project["_id"])
                
        for user in recent_users:
            if "_id" in user:
                user["_id"] = str(user["_id"])
            # Remove password from response
            if "password" in user:
                user["password"] = "********"
        
        return jsonify({
            "totals": {
                "projects": total_projects,
                "users": total_users,
                "collaborations": total_collaborations
            },
            "recent": {
                "projects": recent_projects,
                "users": recent_users
            },
            "user_info": {
                "username": current_user,
                "role": user_role,
                "is_admin": user_role == "admin"
            }
        })
    except Exception as e:
        print(f"Error getting manager stats: {e}")
        return jsonify({"error": str(e)}), 500
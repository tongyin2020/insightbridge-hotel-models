"""
RBAC (Role-Based Access Control) 装饰器

提供细粒度的角色和权限控制:
- 角色层级: super_admin > admin > hotel_operator > guest
- 权限控制: 读取、写入、删除、管理
- 租户隔离: 自动验证hotel_id归属

适用于: DirectorAI, MARE, InsightBridge
"""

import os
import logging
from enum import Enum
from functools import wraps
from typing import Optional, Set, Callable, Union, List

from fastapi import HTTPException, Request, Depends
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 角色与权限定义
# ---------------------------------------------------------------------------

class Role(str, Enum):
    """系统角色"""
    SUPER_ADMIN = "super_admin"    # 超级管理员 (可管理所有酒店)
    ADMIN = "admin"                 # 管理员 (可管理指定酒店)
    HOTEL_OPERATOR = "hotel_operator"  # 酒店操作员
    ANALYST = "analyst"             # 数据分析师 (只读)
    GUEST = "guest"                 # 访客 (极有限权限)


class Permission(str, Enum):
    """操作权限"""
    # 基础权限
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    
    # 管理权限
    MANAGE_USERS = "manage_users"
    MANAGE_HOTELS = "manage_hotels"
    MANAGE_PRICING = "manage_pricing"
    MANAGE_SETTINGS = "manage_settings"
    
    # 高级权限
    VIEW_ANALYTICS = "view_analytics"
    EXPORT_DATA = "export_data"
    AI_CHAT = "ai_chat"
    
    # 系统权限
    SYSTEM_CONFIG = "system_config"
    AUDIT_LOGS = "audit_logs"


# 角色 -> 权限映射
ROLE_PERMISSIONS: dict[Role, Set[Permission]] = {
    Role.SUPER_ADMIN: {
        Permission.READ,
        Permission.WRITE,
        Permission.DELETE,
        Permission.MANAGE_USERS,
        Permission.MANAGE_HOTELS,
        Permission.MANAGE_PRICING,
        Permission.MANAGE_SETTINGS,
        Permission.VIEW_ANALYTICS,
        Permission.EXPORT_DATA,
        Permission.AI_CHAT,
        Permission.SYSTEM_CONFIG,
        Permission.AUDIT_LOGS,
    },
    Role.ADMIN: {
        Permission.READ,
        Permission.WRITE,
        Permission.DELETE,
        Permission.MANAGE_USERS,
        Permission.MANAGE_PRICING,
        Permission.MANAGE_SETTINGS,
        Permission.VIEW_ANALYTICS,
        Permission.EXPORT_DATA,
        Permission.AI_CHAT,
    },
    Role.HOTEL_OPERATOR: {
        Permission.READ,
        Permission.WRITE,
        Permission.MANAGE_PRICING,
        Permission.VIEW_ANALYTICS,
        Permission.AI_CHAT,
    },
    Role.ANALYST: {
        Permission.READ,
        Permission.VIEW_ANALYTICS,
        Permission.EXPORT_DATA,
    },
    Role.GUEST: {
        Permission.READ,
    },
}

# 角色层级 (数值越高权限越大)
ROLE_HIERARCHY: dict[Role, int] = {
    Role.SUPER_ADMIN: 100,
    Role.ADMIN: 80,
    Role.HOTEL_OPERATOR: 50,
    Role.ANALYST: 30,
    Role.GUEST: 10,
}


# ---------------------------------------------------------------------------
# 权限检查函数
# ---------------------------------------------------------------------------

def has_permission(user_role: str, permission: Permission) -> bool:
    """检查角色是否拥有指定权限"""
    try:
        role = Role(user_role)
        permissions = ROLE_PERMISSIONS.get(role, set())
        return permission in permissions
    except ValueError:
        return False


def has_role_level(user_role: str, min_role: Role) -> bool:
    """检查角色是否达到最低等级要求"""
    try:
        role = Role(user_role)
        user_level = ROLE_HIERARCHY.get(role, 0)
        min_level = ROLE_HIERARCHY.get(min_role, 0)
        return user_level >= min_level
    except ValueError:
        return False


def can_access_hotel(user: dict, hotel_id: str) -> bool:
    """检查用户是否可以访问指定酒店"""
    user_role = user.get("role", "guest")
    
    # 超级管理员可访问所有酒店
    if user_role == Role.SUPER_ADMIN.value:
        return True
    
    # 其他角色只能访问自己的酒店
    user_hotel_id = user.get("hotel_id")
    return user_hotel_id == hotel_id


# ---------------------------------------------------------------------------
# FastAPI 依赖项
# ---------------------------------------------------------------------------

class RBACChecker:
    """RBAC检查器 - FastAPI依赖项"""
    
    def __init__(
        self,
        required_permissions: Optional[List[Permission]] = None,
        min_role: Optional[Role] = None,
        check_hotel_access: bool = False,
        hotel_id_param: str = "hotel_id",  # 从路径/查询/请求体中获取hotel_id的参数名
    ):
        """
        Args:
            required_permissions: 需要的权限列表 (满足任一即可)
            min_role: 最低角色要求
            check_hotel_access: 是否检查酒店访问权限
            hotel_id_param: hotel_id参数名
        """
        self.required_permissions = required_permissions or []
        self.min_role = min_role
        self.check_hotel_access = check_hotel_access
        self.hotel_id_param = hotel_id_param
    
    async def __call__(
        self,
        request: Request,
        current_user: dict = Depends(get_current_user),  # 假设已有的认证依赖
    ) -> dict:
        """验证权限并返回用户"""
        user_role = current_user.get("role", "guest")
        
        # 检查最低角色
        if self.min_role and not has_role_level(user_role, self.min_role):
            logger.warning(
                f"RBAC拒绝: 用户 {current_user.get('email')} 角色 {user_role} "
                f"低于要求 {self.min_role.value}"
            )
            raise HTTPException(
                status_code=403,
                detail=f"需要 {self.min_role.value} 或更高权限"
            )
        
        # 检查权限
        if self.required_permissions:
            has_any = any(
                has_permission(user_role, perm)
                for perm in self.required_permissions
            )
            if not has_any:
                logger.warning(
                    f"RBAC拒绝: 用户 {current_user.get('email')} 缺少权限 "
                    f"{[p.value for p in self.required_permissions]}"
                )
                raise HTTPException(
                    status_code=403,
                    detail="没有执行此操作的权限"
                )
        
        # 检查酒店访问权限
        if self.check_hotel_access:
            hotel_id = await self._extract_hotel_id(request)
            if hotel_id and not can_access_hotel(current_user, hotel_id):
                logger.warning(
                    f"RBAC拒绝: 用户 {current_user.get('email')} "
                    f"无权访问酒店 {hotel_id}"
                )
                raise HTTPException(
                    status_code=403,
                    detail="无权访问此酒店数据"
                )
        
        return current_user
    
    async def _extract_hotel_id(self, request: Request) -> Optional[str]:
        """从请求中提取hotel_id"""
        # 1. 路径参数
        if self.hotel_id_param in request.path_params:
            return request.path_params[self.hotel_id_param]
        
        # 2. 查询参数
        if self.hotel_id_param in request.query_params:
            return request.query_params[self.hotel_id_param]
        
        # 3. 请求体 (只对POST/PUT/PATCH)
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.json()
                if isinstance(body, dict) and self.hotel_id_param in body:
                    return body[self.hotel_id_param]
            except:
                pass
        
        return None


# ---------------------------------------------------------------------------
# 装饰器
# ---------------------------------------------------------------------------

def require_permission(*permissions: Permission):
    """
    权限检查装饰器
    
    使用示例:
        @require_permission(Permission.WRITE, Permission.MANAGE_PRICING)
        async def update_price(request: Request):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # 获取当前用户 (假设在request.state中)
            request = kwargs.get("request") or (args[0] if args else None)
            if not request or not hasattr(request, "state"):
                raise HTTPException(status_code=500, detail="无法获取请求上下文")
            
            current_user = getattr(request.state, "user", None)
            if not current_user:
                raise HTTPException(status_code=401, detail="未认证")
            
            user_role = current_user.get("role", "guest")
            has_any = any(has_permission(user_role, perm) for perm in permissions)
            
            if not has_any:
                raise HTTPException(
                    status_code=403,
                    detail=f"需要权限: {[p.value for p in permissions]}"
                )
            
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator


def require_role(min_role: Role):
    """
    角色等级检查装饰器
    
    使用示例:
        @require_role(Role.ADMIN)
        async def admin_dashboard(request: Request):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            request = kwargs.get("request") or (args[0] if args else None)
            if not request or not hasattr(request, "state"):
                raise HTTPException(status_code=500, detail="无法获取请求上下文")
            
            current_user = getattr(request.state, "user", None)
            if not current_user:
                raise HTTPException(status_code=401, detail="未认证")
            
            user_role = current_user.get("role", "guest")
            
            if not has_role_level(user_role, min_role):
                raise HTTPException(
                    status_code=403,
                    detail=f"需要 {min_role.value} 或更高权限"
                )
            
            return await func(*args, **kwargs)
        
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# FastAPI 依赖项快捷方式
# ---------------------------------------------------------------------------

# 预定义的常用权限检查
RequireRead = RBACChecker(required_permissions=[Permission.READ])
RequireWrite = RBACChecker(required_permissions=[Permission.WRITE])
RequireDelete = RBACChecker(required_permissions=[Permission.DELETE])
RequireAdmin = RBACChecker(min_role=Role.ADMIN)
RequireSuperAdmin = RBACChecker(min_role=Role.SUPER_ADMIN)

# 带酒店访问检查的依赖
RequireHotelAccess = RBACChecker(
    required_permissions=[Permission.READ],
    check_hotel_access=True
)
RequireHotelWrite = RBACChecker(
    required_permissions=[Permission.WRITE],
    check_hotel_access=True
)


# ---------------------------------------------------------------------------
# 路由使用示例
# ---------------------------------------------------------------------------

"""
from fastapi import APIRouter, Depends
from security_upgrade.common.rbac_decorator import (
    RBACChecker, Permission, Role,
    RequireAdmin, RequireHotelAccess, RequireHotelWrite
)

router = APIRouter()

# 示例1: 基础权限检查
@router.get("/analytics")
async def get_analytics(
    user: dict = Depends(RBACChecker(required_permissions=[Permission.VIEW_ANALYTICS]))
):
    return {"data": "analytics"}

# 示例2: 角色 + 酒店访问检查
@router.put("/hotels/{hotel_id}/settings")
async def update_settings(
    hotel_id: str,
    user: dict = Depends(RBACChecker(
        required_permissions=[Permission.MANAGE_SETTINGS],
        check_hotel_access=True
    ))
):
    return {"updated": True}

# 示例3: 使用预定义依赖
@router.get("/admin/users")
async def list_users(user: dict = Depends(RequireAdmin)):
    return {"users": []}

# 示例4: 多权限检查 (满足任一)
@router.delete("/records/{id}")
async def delete_record(
    user: dict = Depends(RBACChecker(
        required_permissions=[Permission.DELETE, Permission.MANAGE_PRICING]
    ))
):
    return {"deleted": True}
"""


# ---------------------------------------------------------------------------
# 占位符: 需要从实际项目导入
# ---------------------------------------------------------------------------

async def get_current_user(request: Request) -> dict:
    """
    占位符 - 实际使用时从项目的auth模块导入
    
    例如:
    from app.auth.utils import get_current_user
    """
    # 这里应该是实际的认证逻辑
    # 返回包含 id, email, role, hotel_id 的用户字典
    raise NotImplementedError("请导入实际的 get_current_user 函数")

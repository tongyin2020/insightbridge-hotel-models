"""
MFA/2FA 双因素认证模块

支持:
- TOTP (Google Authenticator / Microsoft Authenticator)
- 短信验证码 (阿里云SMS)
- 邮件验证码

适用于: DirectorAI, MARE, InsightBridge
"""

import os
import hmac
import hashlib
import struct
import time
import base64
import secrets
import logging
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TOTP (RFC 6238) 实现
# ---------------------------------------------------------------------------

class TOTPManager:
    """TOTP 时间同步一次性密码管理器"""
    
    def __init__(self, secret: Optional[str] = None, digits: int = 6, interval: int = 30):
        """
        Args:
            secret: Base32编码的密钥 (32字符)
            digits: 验证码位数 (默认6位)
            interval: 时间步长秒数 (默认30秒)
        """
        self.secret = secret or self.generate_secret()
        self.digits = digits
        self.interval = interval
    
    @staticmethod
    def generate_secret(length: int = 32) -> str:
        """生成随机Base32密钥"""
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
        return ''.join(secrets.choice(alphabet) for _ in range(length))
    
    def _get_counter(self, timestamp: Optional[float] = None) -> int:
        """获取当前时间计数器"""
        if timestamp is None:
            timestamp = time.time()
        return int(timestamp) // self.interval
    
    def _hotp(self, counter: int) -> str:
        """HOTP算法 (RFC 4226)"""
        # 解码Base32密钥
        key = base64.b32decode(self.secret.upper() + '=' * (8 - len(self.secret) % 8))
        
        # 计数器转8字节大端序
        counter_bytes = struct.pack('>Q', counter)
        
        # HMAC-SHA1
        hmac_hash = hmac.new(key, counter_bytes, hashlib.sha1).digest()
        
        # 动态截断
        offset = hmac_hash[-1] & 0x0F
        binary = struct.unpack('>I', hmac_hash[offset:offset + 4])[0] & 0x7FFFFFFF
        
        # 取模得到验证码
        otp = binary % (10 ** self.digits)
        return str(otp).zfill(self.digits)
    
    def generate(self, timestamp: Optional[float] = None) -> str:
        """生成当前TOTP验证码"""
        counter = self._get_counter(timestamp)
        return self._hotp(counter)
    
    def verify(self, code: str, tolerance: int = 1) -> bool:
        """
        验证TOTP码
        
        Args:
            code: 用户输入的验证码
            tolerance: 允许的时间偏移步数 (默认±1, 即前后30秒)
        
        Returns:
            bool: 验证是否通过
        """
        if not code or len(code) != self.digits:
            return False
        
        current_counter = self._get_counter()
        
        # 检查当前及前后tolerance个时间步
        for offset in range(-tolerance, tolerance + 1):
            expected = self._hotp(current_counter + offset)
            if hmac.compare_digest(code, expected):
                return True
        
        return False
    
    def get_provisioning_uri(self, account: str, issuer: str = "HotelAI") -> str:
        """
        生成TOTP配置URI (用于二维码)
        
        Google Authenticator扫描此二维码即可添加账户
        """
        from urllib.parse import quote
        return (
            f"otpauth://totp/{quote(issuer)}:{quote(account)}"
            f"?secret={self.secret}"
            f"&issuer={quote(issuer)}"
            f"&digits={self.digits}"
            f"&period={self.interval}"
        )


# ---------------------------------------------------------------------------
# 验证码存储与验证 (短信/邮件)
# ---------------------------------------------------------------------------

class VerificationCodeManager:
    """验证码管理器 (短信/邮件)"""
    
    def __init__(self, db_collection, code_length: int = 6, expire_minutes: int = 5):
        """
        Args:
            db_collection: MongoDB collection (或其他存储)
            code_length: 验证码长度
            expire_minutes: 过期时间(分钟)
        """
        self.db = db_collection
        self.code_length = code_length
        self.expire_minutes = expire_minutes
    
    def generate_code(self) -> str:
        """生成随机数字验证码"""
        return ''.join(secrets.choice('0123456789') for _ in range(self.code_length))
    
    async def create_and_store(
        self,
        user_id: str,
        purpose: str,  # 'login', 'password_reset', 'mfa_setup'
        delivery_method: str,  # 'sms', 'email'
    ) -> Tuple[str, datetime]:
        """
        创建并存储验证码
        
        Returns:
            (code, expires_at)
        """
        code = self.generate_code()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.expire_minutes)
        
        # 删除该用户该用途的旧验证码
        await self.db.delete_many({
            "user_id": user_id,
            "purpose": purpose,
        })
        
        # 存储新验证码
        await self.db.insert_one({
            "user_id": user_id,
            "purpose": purpose,
            "delivery_method": delivery_method,
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at.isoformat(),
            "attempts": 0,
            "max_attempts": 5,
            "verified": False,
        })
        
        return code, expires_at
    
    async def verify(self, user_id: str, purpose: str, code: str) -> Tuple[bool, str]:
        """
        验证验证码
        
        Returns:
            (success, message)
        """
        record = await self.db.find_one({
            "user_id": user_id,
            "purpose": purpose,
            "verified": False,
        })
        
        if not record:
            return False, "验证码不存在或已使用"
        
        # 检查过期
        expires_at = datetime.fromisoformat(record["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            return False, "验证码已过期"
        
        # 检查尝试次数
        if record["attempts"] >= record["max_attempts"]:
            return False, "验证码尝试次数过多，请重新获取"
        
        # 更新尝试次数
        await self.db.update_one(
            {"_id": record["_id"]},
            {"$inc": {"attempts": 1}}
        )
        
        # 验证码比对 (timing-safe)
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        if not hmac.compare_digest(code_hash, record["code_hash"]):
            return False, "验证码错误"
        
        # 标记为已使用
        await self.db.update_one(
            {"_id": record["_id"]},
            {"$set": {"verified": True, "verified_at": datetime.now(timezone.utc).isoformat()}}
        )
        
        return True, "验证成功"


# ---------------------------------------------------------------------------
# MFA 状态管理
# ---------------------------------------------------------------------------

class MFAManager:
    """MFA 多因素认证管理器"""
    
    def __init__(self, users_collection, mfa_codes_collection):
        self.users = users_collection
        self.codes = mfa_codes_collection
        self.totp_manager = TOTPManager()
        self.code_manager = VerificationCodeManager(mfa_codes_collection)
    
    async def enable_totp(self, user_id: str) -> dict:
        """
        为用户启用TOTP
        
        Returns:
            {
                "secret": "BASE32SECRET",
                "qr_uri": "otpauth://totp/...",
                "backup_codes": ["12345678", ...]
            }
        """
        # 生成新密钥
        secret = TOTPManager.generate_secret()
        
        # 生成备用恢复码 (10个8位随机码)
        backup_codes = [
            ''.join(secrets.choice('0123456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(8))
            for _ in range(10)
        ]
        backup_codes_hashed = [
            hashlib.sha256(code.encode()).hexdigest()
            for code in backup_codes
        ]
        
        # 获取用户邮箱用于生成URI
        user = await self.users.find_one({"id": user_id})
        email = user.get("email", user_id) if user else user_id
        
        # 暂存(未验证状态)
        await self.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "mfa_totp_secret_pending": secret,
                    "mfa_backup_codes_pending": backup_codes_hashed,
                }
            }
        )
        
        totp = TOTPManager(secret)
        
        return {
            "secret": secret,
            "qr_uri": totp.get_provisioning_uri(email, "HotelAI"),
            "backup_codes": backup_codes,
            "message": "请使用认证器App扫描二维码，然后输入验证码确认启用"
        }
    
    async def confirm_totp_enable(self, user_id: str, code: str) -> Tuple[bool, str]:
        """
        确认启用TOTP (用户输入第一个验证码)
        """
        user = await self.users.find_one({"id": user_id})
        if not user:
            return False, "用户不存在"
        
        pending_secret = user.get("mfa_totp_secret_pending")
        if not pending_secret:
            return False, "未找到待激活的MFA配置"
        
        # 验证码验证
        totp = TOTPManager(pending_secret)
        if not totp.verify(code):
            return False, "验证码错误，请重试"
        
        # 激活MFA
        await self.users.update_one(
            {"id": user_id},
            {
                "$set": {
                    "mfa_enabled": True,
                    "mfa_method": "totp",
                    "mfa_totp_secret": pending_secret,
                    "mfa_backup_codes": user.get("mfa_backup_codes_pending", []),
                    "mfa_enabled_at": datetime.now(timezone.utc).isoformat(),
                },
                "$unset": {
                    "mfa_totp_secret_pending": "",
                    "mfa_backup_codes_pending": "",
                }
            }
        )
        
        return True, "MFA已成功启用"
    
    async def verify_mfa(self, user_id: str, code: str) -> Tuple[bool, str]:
        """
        验证MFA码 (登录时调用)
        
        支持:
        - TOTP验证码
        - 备用恢复码
        """
        user = await self.users.find_one({"id": user_id})
        if not user:
            return False, "用户不存在"
        
        if not user.get("mfa_enabled"):
            return True, "MFA未启用，跳过验证"
        
        method = user.get("mfa_method", "totp")
        
        if method == "totp":
            secret = user.get("mfa_totp_secret")
            if not secret:
                return False, "MFA配置异常"
            
            totp = TOTPManager(secret)
            
            # 先尝试TOTP验证
            if totp.verify(code):
                return True, "MFA验证成功"
            
            # 再尝试备用码
            code_hash = hashlib.sha256(code.encode()).hexdigest()
            backup_codes = user.get("mfa_backup_codes", [])
            
            if code_hash in backup_codes:
                # 使用后删除该备用码
                backup_codes.remove(code_hash)
                await self.users.update_one(
                    {"id": user_id},
                    {"$set": {"mfa_backup_codes": backup_codes}}
                )
                remaining = len(backup_codes)
                return True, f"备用码验证成功 (剩余{remaining}个)"
            
            return False, "验证码错误"
        
        return False, "不支持的MFA方法"
    
    async def disable_mfa(self, user_id: str, code: str) -> Tuple[bool, str]:
        """禁用MFA (需要验证当前MFA码)"""
        success, msg = await self.verify_mfa(user_id, code)
        if not success:
            return False, f"MFA验证失败: {msg}"
        
        await self.users.update_one(
            {"id": user_id},
            {
                "$set": {"mfa_enabled": False},
                "$unset": {
                    "mfa_method": "",
                    "mfa_totp_secret": "",
                    "mfa_backup_codes": "",
                }
            }
        )
        
        return True, "MFA已禁用"
    
    async def regenerate_backup_codes(self, user_id: str, code: str) -> Tuple[bool, list]:
        """重新生成备用码"""
        success, msg = await self.verify_mfa(user_id, code)
        if not success:
            return False, []
        
        new_codes = [
            ''.join(secrets.choice('0123456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(8))
            for _ in range(10)
        ]
        new_codes_hashed = [
            hashlib.sha256(c.encode()).hexdigest()
            for c in new_codes
        ]
        
        await self.users.update_one(
            {"id": user_id},
            {"$set": {"mfa_backup_codes": new_codes_hashed}}
        )
        
        return True, new_codes


# ---------------------------------------------------------------------------
# FastAPI 路由示例
# ---------------------------------------------------------------------------

def create_mfa_routes(users_collection, mfa_codes_collection):
    """创建MFA相关的FastAPI路由"""
    from fastapi import APIRouter, HTTPException, Depends
    from pydantic import BaseModel
    
    router = APIRouter(prefix="/mfa", tags=["mfa"])
    mfa_manager = MFAManager(users_collection, mfa_codes_collection)
    
    class EnableTOTPResponse(BaseModel):
        secret: str
        qr_uri: str
        backup_codes: list
        message: str
    
    class VerifyRequest(BaseModel):
        code: str
    
    class MessageResponse(BaseModel):
        success: bool
        message: str
    
    @router.post("/totp/enable", response_model=EnableTOTPResponse)
    async def enable_totp(current_user: dict = Depends(get_current_user)):
        """启用TOTP双因素认证"""
        result = await mfa_manager.enable_totp(current_user["id"])
        return result
    
    @router.post("/totp/confirm", response_model=MessageResponse)
    async def confirm_totp(
        req: VerifyRequest,
        current_user: dict = Depends(get_current_user)
    ):
        """确认启用TOTP (输入验证码)"""
        success, message = await mfa_manager.confirm_totp_enable(
            current_user["id"], req.code
        )
        if not success:
            raise HTTPException(status_code=400, detail=message)
        return {"success": True, "message": message}
    
    @router.post("/verify", response_model=MessageResponse)
    async def verify_mfa(
        req: VerifyRequest,
        current_user: dict = Depends(get_current_user)
    ):
        """验证MFA码"""
        success, message = await mfa_manager.verify_mfa(
            current_user["id"], req.code
        )
        if not success:
            raise HTTPException(status_code=401, detail=message)
        return {"success": True, "message": message}
    
    @router.post("/disable", response_model=MessageResponse)
    async def disable_mfa(
        req: VerifyRequest,
        current_user: dict = Depends(get_current_user)
    ):
        """禁用MFA"""
        success, message = await mfa_manager.disable_mfa(
            current_user["id"], req.code
        )
        if not success:
            raise HTTPException(status_code=400, detail=message)
        return {"success": True, "message": message}
    
    return router


# ---------------------------------------------------------------------------
# 使用示例
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # TOTP测试
    totp = TOTPManager()
    print(f"Secret: {totp.secret}")
    print(f"Current code: {totp.generate()}")
    print(f"QR URI: {totp.get_provisioning_uri('test@hotel.com')}")
    
    # 验证测试
    code = totp.generate()
    print(f"Verify '{code}': {totp.verify(code)}")

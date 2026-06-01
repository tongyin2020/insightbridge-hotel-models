"""
阿里云 KMS 密钥管理客户端

功能:
- 从KMS获取加密密钥/敏感配置
- 自动密钥轮换
- 缓存管理

依赖: pip install alibabacloud-kms20160120
"""

import os
import json
import logging
import time
from typing import Optional, Dict, Any
from functools import lru_cache
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 阿里云KMS客户端
# ---------------------------------------------------------------------------

class AliyunKMSClient:
    """阿里云密钥管理服务客户端"""
    
    def __init__(
        self,
        access_key_id: Optional[str] = None,
        access_key_secret: Optional[str] = None,
        region_id: str = "cn-hangzhou",
    ):
        """
        初始化KMS客户端
        
        Args:
            access_key_id: 阿里云AccessKey ID (或从环境变量ALIBABA_CLOUD_ACCESS_KEY_ID获取)
            access_key_secret: 阿里云AccessKey Secret
            region_id: 地域ID
        """
        self.access_key_id = access_key_id or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
        self.access_key_secret = access_key_secret or os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        self.region_id = region_id
        
        self._client = None
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = 300  # 5分钟缓存
    
    def _get_client(self):
        """延迟初始化KMS客户端"""
        if self._client is None:
            try:
                from alibabacloud_kms20160120.client import Client
                from alibabacloud_tea_openapi import models as open_api_models
                
                config = open_api_models.Config(
                    access_key_id=self.access_key_id,
                    access_key_secret=self.access_key_secret,
                    region_id=self.region_id,
                )
                self._client = Client(config)
            except ImportError:
                logger.warning("阿里云KMS SDK未安装，将使用环境变量回退模式")
                self._client = "fallback"
            except Exception as e:
                logger.error(f"KMS客户端初始化失败: {e}")
                self._client = "fallback"
        
        return self._client
    
    def _get_from_cache(self, key: str) -> Optional[str]:
        """从缓存获取密钥值"""
        if key in self._cache:
            entry = self._cache[key]
            if time.time() - entry["timestamp"] < self._cache_ttl:
                return entry["value"]
            del self._cache[key]
        return None
    
    def _set_cache(self, key: str, value: str) -> None:
        """设置缓存"""
        self._cache[key] = {
            "value": value,
            "timestamp": time.time(),
        }
    
    def get_secret(self, secret_name: str, version_id: Optional[str] = None) -> Optional[str]:
        """
        获取密钥/敏感配置值
        
        Args:
            secret_name: 密钥名称 (如 "hotel-ai/jwt-secret")
            version_id: 版本ID (默认获取当前版本)
        
        Returns:
            密钥值字符串
        """
        # 检查缓存
        cache_key = f"{secret_name}:{version_id or 'current'}"
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached
        
        client = self._get_client()
        
        # 回退到环境变量
        if client == "fallback":
            env_key = secret_name.replace("/", "_").replace("-", "_").upper()
            value = os.getenv(env_key)
            if value:
                self._set_cache(cache_key, value)
            return value
        
        try:
            from alibabacloud_kms20160120 import models as kms_models
            
            request = kms_models.GetSecretValueRequest(
                secret_name=secret_name,
                version_id=version_id,
            )
            response = client.get_secret_value(request)
            value = response.body.secret_data
            
            self._set_cache(cache_key, value)
            logger.info(f"从KMS获取密钥成功: {secret_name}")
            return value
            
        except Exception as e:
            logger.error(f"从KMS获取密钥失败 ({secret_name}): {e}")
            # 回退到环境变量
            env_key = secret_name.replace("/", "_").replace("-", "_").upper()
            return os.getenv(env_key)
    
    def get_secret_json(self, secret_name: str) -> Optional[Dict]:
        """获取JSON格式的密钥配置"""
        value = self.get_secret(secret_name)
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                logger.error(f"密钥值不是有效JSON: {secret_name}")
        return None
    
    def create_secret(
        self,
        secret_name: str,
        secret_data: str,
        description: str = "",
        tags: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        创建新密钥
        
        Args:
            secret_name: 密钥名称
            secret_data: 密钥值
            description: 描述
            tags: 标签字典
        
        Returns:
            是否成功
        """
        client = self._get_client()
        if client == "fallback":
            logger.warning("KMS不可用，无法创建密钥")
            return False
        
        try:
            from alibabacloud_kms20160120 import models as kms_models
            
            request = kms_models.CreateSecretRequest(
                secret_name=secret_name,
                secret_data=secret_data,
                description=description,
            )
            if tags:
                request.tags = json.dumps(tags)
            
            client.create_secret(request)
            logger.info(f"创建KMS密钥成功: {secret_name}")
            return True
            
        except Exception as e:
            logger.error(f"创建KMS密钥失败 ({secret_name}): {e}")
            return False
    
    def rotate_secret(self, secret_name: str, new_secret_data: str) -> bool:
        """
        轮换密钥 (创建新版本)
        
        Args:
            secret_name: 密钥名称
            new_secret_data: 新密钥值
        
        Returns:
            是否成功
        """
        client = self._get_client()
        if client == "fallback":
            logger.warning("KMS不可用，无法轮换密钥")
            return False
        
        try:
            from alibabacloud_kms20160120 import models as kms_models
            
            # 添加新版本
            request = kms_models.PutSecretValueRequest(
                secret_name=secret_name,
                secret_data=new_secret_data,
                version_id=f"v{int(time.time())}",
            )
            client.put_secret_value(request)
            
            # 清除缓存
            cache_key = f"{secret_name}:current"
            if cache_key in self._cache:
                del self._cache[cache_key]
            
            logger.info(f"轮换KMS密钥成功: {secret_name}")
            return True
            
        except Exception as e:
            logger.error(f"轮换KMS密钥失败 ({secret_name}): {e}")
            return False
    
    def clear_cache(self) -> None:
        """清除所有缓存"""
        self._cache.clear()


# ---------------------------------------------------------------------------
# 配置加载器 (集成KMS)
# ---------------------------------------------------------------------------

class SecureConfigLoader:
    """安全配置加载器 - 优先从KMS加载，回退到环境变量"""
    
    # 密钥映射: 环境变量名 -> KMS密钥名
    SECRET_MAPPING = {
        "JWT_SECRET": "hotel-ai/jwt-secret",
        "MONGO_URL": "hotel-ai/mongo-url",
        "REDIS_URL": "hotel-ai/redis-url",
        "OPENAI_API_KEY": "hotel-ai/openai-api-key",
        "SMTP_PASSWORD": "hotel-ai/smtp-password",
        "TWILIO_AUTH_TOKEN": "hotel-ai/twilio-auth-token",
        "WECHAT_APP_SECRET": "hotel-ai/wechat-app-secret",
        "STRIPE_SECRET_KEY": "hotel-ai/stripe-secret-key",
    }
    
    def __init__(self, use_kms: bool = True):
        """
        Args:
            use_kms: 是否启用KMS (生产环境应为True)
        """
        self.use_kms = use_kms and os.getenv("ENVIRONMENT", "").lower() == "production"
        self.kms_client = AliyunKMSClient() if self.use_kms else None
        self._loaded_config: Dict[str, str] = {}
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        获取配置值
        
        优先级:
        1. 已加载的缓存
        2. KMS (如果启用)
        3. 环境变量
        4. 默认值
        """
        # 检查缓存
        if key in self._loaded_config:
            return self._loaded_config[key]
        
        value = None
        
        # 尝试从KMS获取
        if self.use_kms and key in self.SECRET_MAPPING:
            kms_key = self.SECRET_MAPPING[key]
            value = self.kms_client.get_secret(kms_key)
            if value:
                logger.debug(f"从KMS加载配置: {key}")
        
        # 回退到环境变量
        if value is None:
            value = os.getenv(key)
            if value:
                logger.debug(f"从环境变量加载配置: {key}")
        
        # 使用默认值
        if value is None:
            value = default
        
        # 缓存
        if value is not None:
            self._loaded_config[key] = value
        
        return value
    
    def get_int(self, key: str, default: int = 0) -> int:
        """获取整数配置"""
        value = self.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            return default
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        """获取布尔配置"""
        value = self.get(key)
        if value is None:
            return default
        return value.lower() in ("true", "1", "yes", "on")
    
    def get_list(self, key: str, separator: str = ",") -> list:
        """获取列表配置"""
        value = self.get(key)
        if not value:
            return []
        return [x.strip() for x in value.split(separator) if x.strip()]
    
    def require(self, key: str) -> str:
        """获取必需配置 (缺失时抛出异常)"""
        value = self.get(key)
        if value is None:
            raise ValueError(f"必需的配置项缺失: {key}")
        return value
    
    def load_all_secrets(self) -> Dict[str, str]:
        """预加载所有KMS密钥"""
        if not self.use_kms:
            return {}
        
        loaded = {}
        for env_key, kms_key in self.SECRET_MAPPING.items():
            value = self.kms_client.get_secret(kms_key)
            if value:
                loaded[env_key] = value
                self._loaded_config[env_key] = value
        
        logger.info(f"从KMS预加载了 {len(loaded)} 个密钥")
        return loaded


# ---------------------------------------------------------------------------
# 全局配置实例
# ---------------------------------------------------------------------------

# 单例配置加载器
_config_loader: Optional[SecureConfigLoader] = None


def get_config() -> SecureConfigLoader:
    """获取全局配置加载器"""
    global _config_loader
    if _config_loader is None:
        _config_loader = SecureConfigLoader()
    return _config_loader


def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    """快捷方法: 获取密钥/配置"""
    return get_config().get(key, default)


# ---------------------------------------------------------------------------
# 使用示例
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 测试配置加载
    config = SecureConfigLoader(use_kms=False)  # 本地测试不使用KMS
    
    print(f"JWT_SECRET: {config.get('JWT_SECRET', 'default-secret')[:20]}...")
    print(f"ENVIRONMENT: {config.get('ENVIRONMENT', 'development')}")

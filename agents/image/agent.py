"""
ImageAgent - 生图子Agent
专门负责：根据用户画像生成图片
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from hub import config, PosterPromptBuilder


@dataclass
class ImageGenResult:
    """图像生成结果"""
    success: bool
    url: Optional[str] = None
    local_path: Optional[str] = None
    revised_prompt: Optional[str] = None
    error: Optional[str] = None
    model: Optional[str] = None


class ImageGenerator:
    """图像生成器"""
    
    def __init__(self, provider: str = None, model_id: str = None, api_key: str = None):
        self.provider = provider or "openrouter"
        self.model_id = model_id or config.get("image_gen_model")
        self.api_key = api_key or self._load_api_key()
        self.timeout = 180
    
    def _load_api_key(self) -> str:
        """加载 API Key（统一走 api_layer 配置，避免重复逻辑）"""
        from services.api_layer import get_provider_config
        return get_provider_config().api_key
    
    async def generate(self, prompt: str, **kwargs) -> ImageGenResult:
        """生成图像"""
        size = kwargs.get("size", "1024x1024")
        
        if not self.api_key:
            return ImageGenResult(success=False, error="API Key 未配置")
        
        if self.provider == "openrouter":
            return await self._generate_openrouter(prompt, size)
        elif self.provider == "flux":
            return await self._generate_flux(prompt, size)
        elif self.provider == "dalle":
            return await self._generate_dalle(prompt, size)
        else:
            return ImageGenResult(success=False, error=f"Unknown provider: {self.provider}")
    
    async def _generate_openrouter(self, prompt: str, size: str) -> ImageGenResult:
        """OpenRouter 图像生成 API"""
        import httpx
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image"],
            "max_tokens": 2048
        }
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload
                )
                
                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices", [])
                    if choices:
                        message = choices[0].get("message", {})
                        content = message.get("content", [])

                        # content 可能是字符串（纯文本）或列表（多模态）
                        if isinstance(content, str):
                            content = []

                        # 在 content 列表中找 image_url 类型
                        url = ""
                        text_content = ""
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") == "image_url":
                                    img_url = part.get("image_url", {})
                                    url = img_url.get("url", "") if isinstance(img_url, dict) else str(img_url)
                                elif part.get("type") == "text":
                                    text_content = part.get("text", "")

                        if url:
                            return ImageGenResult(
                                success=True,
                                url=url,
                                revised_prompt=text_content or None,
                                model=self.model_id
                            )

                    # 打印完整响应体便于调试
                    print(f"   ⚠️ 生图响应解析失败，完整内容: {response.text[:500]}")
                    return ImageGenResult(success=False, error="No image_url in response")
                
                else:
                    return ImageGenResult(
                        success=False,
                        error=f"API error: {response.status_code} - {response.text[:200]}"
                    )
                    
        except Exception as e:
            return ImageGenResult(success=False, error=str(e))
    
    async def _generate_flux(self, prompt: str, size: str) -> ImageGenResult:
        """Black Forest Labs Flux API"""
        import httpx
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model_id,
            "prompt": prompt,
            "image_size": size,
            "num_images": 1,
            "prompt_enhancement": True
        }
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://api.blackforestlabs.ai/v1/generation",
                    headers=headers,
                    json=payload
                )
                
                if response.status_code == 200:
                    data = response.json()
                    images = data.get("images", [])
                    if images:
                        return ImageGenResult(
                            success=True,
                            url=images[0].get("url"),
                            revised_prompt=images[0].get("revised_prompt"),
                            model=self.model_id
                        )
                
                return ImageGenResult(success=False, error=f"Flux API error: {response.status_code}")
                
        except Exception as e:
            return ImageGenResult(success=False, error=str(e))
    
    async def _generate_dalle(self, prompt: str, size: str) -> ImageGenResult:
        """OpenAI DALL-E API"""
        import httpx
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": self.model_id or "dall-e-3",
            "prompt": prompt,
            "n": 1,
            "size": size
        }
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://api.openai.com/v1/images/generations",
                    headers=headers,
                    json=payload
                )
                
                if response.status_code == 200:
                    data = response.json()
                    images = data.get("data", [])
                    if images:
                        return ImageGenResult(
                            success=True,
                            url=images[0].get("url"),
                            revised_prompt=images[0].get("revised_prompt"),
                            model=self.model_id
                        )
                
                return ImageGenResult(success=False, error=f"DALL-E API error: {response.status_code}")
                
        except Exception as e:
            return ImageGenResult(success=False, error=str(e))
    
    async def save_image(self, image_data: str, filename: str) -> str:
        """保存图像到本地"""
        import base64
        import httpx
        from PIL import Image
        import io

        output_dir = Path("/Users/kk/.openclaw/media/home/hub/poster")
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / filename

        if image_data.startswith("data:"):
            header, data = image_data.split(",", 1)
            img_bytes = base64.b64decode(data)
            img = Image.open(io.BytesIO(img_bytes))
            img.save(output_path, "PNG")
        elif image_data.startswith("http"):
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(image_data)
                response.raise_for_status()
                output_path.write_bytes(response.content)
        else:
            img_bytes = base64.b64decode(image_data)
            img = Image.open(io.BytesIO(img_bytes))
            img.save(output_path, "PNG")

        return str(output_path)
    


class ImageAgent:
    """
    生图子Agent
    
    专门负责：根据用户画像生成图片
    供主Agent调用
    """
    
    def __init__(self, workspace_path: str = None):
        self.prompt_builder = PosterPromptBuilder()
        self.generator = None  # 懒加载
    
    def _get_generator(self) -> ImageGenerator:
        if self.generator is None:
            self.generator = ImageGenerator()
        return self.generator
    
    async def generate(
        self,
        theme: str,
        context: Dict = None
    ) -> ImageGenResult:
        """
        生图主方法。

        当 context 包含 articles/columns（来自 PosterHandler 文案结果）时，
        自动使用 build_with_context() 生成更贴合手抄报内容的 prompt。
        否则使用 build() 按用户画像生成通用 prompt。
        """
        context = context or {}

        profile = context.get("user_profile", {})
        print(f"   👤 ImageAgent: child_age={profile.get('child_age')}, "
              f"style={profile.get('style_preference')}")

        # 有文案结构时用富 prompt，否则用基础 prompt
        if context.get("articles") or context.get("columns"):
            prompt = await self.prompt_builder.build_with_context(theme, context, profile)
        else:
            prompt = await self.prompt_builder.build(theme, context, profile=profile)
        print(f"   🎨 ImageAgent Prompt: {prompt[:80]}...")

        generator = self._get_generator()
        result = await generator.generate(prompt)

        if result.success and result.url:
            from datetime import datetime
            import uuid
            filename = f"{theme}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
            result.local_path = await generator.save_image(result.url, filename)
            print(f"   ✅ ImageAgent: 图片已保存 {result.local_path}")

        return result

"""
DocSys API Layer - 外部服务调用层
支持多种 Provider: OpenRouter / Kimi / Ollama

Token 追踪集成:
- 所有 LLM 和 Embedding 调用自动记录到 TokenTracker
"""

import os
import base64
import json
import time
import httpx
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

# ============== Token Tracker 集成 ==============

try:
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from utils.token_test import TokenTracker
    _token_tracker: Optional[TokenTracker] = None
    
    def get_tracker() -> TokenTracker:
        global _token_tracker
        if _token_tracker is None:
            _token_tracker = TokenTracker()
        return _token_tracker
except ImportError:
    # 如果 token_test.py 不存在，提供一个 no-op 实现
    class NoOpTracker:
        def record(self, *args, **kwargs): pass
        def enable(self): pass
        def disable(self): pass
    get_tracker = lambda: NoOpTracker()


# ============== Provider 配置 ==============

@dataclass
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str
    embedding_model: str
    multimodal_model: str = ""  # 支持 vision 的模型

def _get_openrouter_key() -> str:
    """获取 OpenRouter API Key"""
    # 1. 环境变量
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    
    # 2. DocSys 配置文件
    config_path = Path.home() / ".openclaw" / "config" / "image_gen.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            key = config.get("api_key") or config.get("apiKey") or ""
            if key:
                return key
        except:
            pass
    
    # 3. OpenRouter 标准配置
    config_path = Path.home() / ".openrouter" / "config"
    if config_path.exists():
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read(str(config_path))
            key = config.get("default", "api_key", fallback="")
            if key:
                return key
        except:
            pass
    
    # 4. OPENAI_API_KEY (有些平台用这个)
    key = os.environ.get("OPENAI_API_KEY", "")
    return key

def get_provider_config() -> ProviderConfig:
    """自动检测可用的 Provider"""
    
    # 优先使用 OpenRouter (LLM + Embedding)
    api_key = _get_openrouter_key()
    if api_key:
        return ProviderConfig(
            name="openrouter",
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            model="minimax/minimax-m2.7",
            embedding_model="qwen/qwen3-embedding-8b",
            multimodal_model="google/gemini-2.0-flash-001"
        )
    
    # 使用 Kimi (Moonshot)
    api_key = os.environ.get("KIMI_API_KEY", "")
    if api_key:
        return ProviderConfig(
            name="kimi",
            api_key=api_key,
            base_url="https://api.moonshot.cn/v1",
            model="moonshot-v1-8k",
            embedding_model="text-embedding-v1"
        )
    
    # 使用 Ollama (本地)
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return ProviderConfig(
        name="ollama",
        api_key="",
        base_url=f"{ollama_host}/v1",
        model=os.environ.get("OLLAMA_MODEL", "qwen3.5-9b"),
        embedding_model=""
    )


# ============== OCR 服务 ==============

class OCRService:
    """PaddleOCR 服务（本地，不消耗 API token）"""
    
    def __init__(self):
        from paddleocr import PaddleOCR
        self.ocr = PaddleOCR(
            use_textline_orientation=True,
            lang='ch'
        )
    
    async def extract_text(self, image_path: str) -> str:
        """从图片提取文字"""
        result = self.ocr.ocr(image_path)
        
        if not result or not result[0]:
            return ""
        
        lines = []
        for line in result[0]:
            if line and len(line) >= 2:
                text = line[1][0]
                lines.append(text)
        
        return "\n".join(lines)
    
    async def extract_structured(self, image_path: str) -> Dict[str, Any]:
        """提取结构化信息"""
        text = await self.extract_text(image_path)
        return {
            "text": text,
            "fields": {}
        }


# ============== LLM 服务 ==============

class LLMService:
    """LLM 服务（带 Token 追踪）"""
    
    def __init__(self, config: Optional[ProviderConfig] = None):
        self.config = config or get_provider_config()
        self._tracker = get_tracker()
    
    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        track: bool = True,
        component: str = "LLM.generate"
    ) -> str:
        """生成文本"""
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json"
        }
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        start_time = time.time()
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.config.base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            
            if response.status_code != 200:
                raise Exception(f"LLM API error: {response.status_code} - {response.text[:500]}")
            
            result = response.json()
            
            # 提取 usage
            usage = result.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            duration_ms = (time.time() - start_time) * 1000
            
            # 记录 token
            if track:
                self._tracker.record(
                    component=component,
                    model=self.config.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms
                )
            
            return result["choices"][0]["message"]["content"]
    
    async def generate_multimodal(
        self,
        prompt: str,
        image_path: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        track: bool = True,
        component: str = "LLM.generate_multimodal"
    ) -> str:
        """多模态生成 - 直接理解图片，无需OCR"""
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json"
        }
        
        # 读取图片并转为 base64
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()
        
        # 推断 MIME 类型
        suffix = Path(image_path).suffix.lower()
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp"
        }
        mime_type = mime_types.get(suffix, "image/jpeg")
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        
        # 图片消息
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{image_data}"
                    }
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        })
        
        payload = {
            "model": self.config.multimodal_model or self.config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        start_time = time.time()
        
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{self.config.base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            
            if response.status_code != 200:
                raise Exception(f"Multimodal API error: {response.status_code} - {response.text[:500]}")
            
            result = response.json()
            
            # 提取 usage
            usage = result.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            duration_ms = (time.time() - start_time) * 1000
            
            # 记录 token
            if track:
                self._tracker.record(
                    component=component,
                    model=self.config.multimodal_model or self.config.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms
                )
            
            return result["choices"][0]["message"]["content"]
    
    async def extract_metadata(self, text: str, space: str) -> Dict[str, Any]:
        """从文本提取元数据（仅文本）"""
        if space == "family":
            system = """你是一个家庭学习资料管理助手。从用户上传的文档内容中提取元数据。
输出格式（JSON）：
{
    "member": "家庭成员（孩子/爸爸/妈妈/老人）",
    "doc_type": "文档类型（错题/试卷/笔记/作业）",
    "category": "学科（数学/语文/英语/物理/化学）",
    "tags": ["知识点1", "知识点2"]
}
只输出JSON，不要其他内容。"""
        else:
            system = """你是一个办公文档管理助手。从用户上传的文档内容中提取元数据。
输出格式（JSON）：
{
    "project": "项目名称",
    "doc_type": "文档类型（发票/合同/报告/清单）",
    "business_category": "业务分类（差旅/采购/招待/办公）",
    "tags": ["标签1", "标签2"]
}
只输出JSON，不要其他内容。"""
        
        prompt = f"文档内容：\n{text[:2000]}\n\n请提取元数据："
        
        result = await self.generate(
            prompt, 
            system=system, 
            component="LLM.extract_metadata"
        )
        
        try:
            json_str = self._extract_json(result)
            return json.loads(json_str.strip())
        except:
            return {}
    
    async def extract_metadata_multimodal(self, image_path: str, space: str) -> Dict[str, Any]:
        """多模态提取元数据 - 直接从图片理解，无需OCR"""
        if space == "family":
            system = """你是一个家庭学习资料管理助手。你会看到用户上传的图片，请直接分析图片内容并提取元数据。

分析要点：
1. 图片中有什么？（错题/试卷/作业/笔记/通知/其他）
2. 是什么学科？（数学/语文/英语/物理/化学/其他）
3. 涉及哪些知识点？
4. 图片中是否有家庭成员信息？（如家长签字、孩子姓名等）
5. 整体难度如何？（基础/中等/困难）
6. 图片方向是横版还是竖版？

输出格式（JSON）：
{
    "member": "家庭成员（孩子/爸爸/妈妈/老人），根据图片判断",
    "doc_type": "文档类型（错题/试卷/笔记/作业/通知/其他）",
    "category": "学科（数学/语文/英语/物理/化学/其他）",
    "tags": ["知识点1", "知识点2"],
    "difficulty": "难度（基础/中等/困难）",
    "orientation": "图片方向（横版/竖版）",
    "has_signature": false,
    "extracted_text": "从图片中识别到的文字内容（如果有）"
}
只输出JSON，不要其他内容。"""
        else:
            system = """你是一个办公文档管理助手。你会看到用户上传的图片，请直接分析图片内容并提取元数据。

分析要点：
1. 图片中有什么？（发票/合同/报告/清单/通知/其他）
2. 是什么业务类型？（差旅/采购/招待/办公/其他）
3. 涉及哪些关键词？
4. 图片方向是横版还是竖版？
5. 提取可见的文字内容

输出格式（JSON）：
{
    "project": "项目名称（根据内容推断）",
    "doc_type": "文档类型（发票/合同/报告/清单/通知/其他）",
    "business_category": "业务分类（差旅/采购/招待/办公/其他）",
    "tags": ["关键词1", "关键词2"],
    "orientation": "图片方向（横版/竖版）",
    "extracted_text": "从图片中识别到的文字内容（如果有）"
}
只输出JSON，不要其他内容。"""
        
        prompt = "请分析这张图片并提取元数据。"
        
        result = await self.generate_multimodal(
            prompt, 
            image_path, 
            system=system, 
            max_tokens=2048,
            component="LLM.extract_metadata_multimodal"
        )
        
        try:
            json_str = self._extract_json(result)
            return json.loads(json_str.strip())
        except Exception as e:
            print(f"   ⚠️ 解析元数据失败: {e}, 原始结果: {result[:200]}")
            return {}
    
    async def generate_summary(
        self,
        text: str,
        metadata: Dict[str, Any],
        space: str
    ) -> Tuple[str, List[str]]:
        """生成语义摘要和同义词（仅文本）"""
        if space == "family":
            system = """你是一个学习资料助手。为用户提供语义摘要，包含同义词扩展。
输出格式（JSON）：
{
    "summary": "用一段话描述这个错题/试卷的核心内容",
    "synonyms": ["同义词1", "同义词2"]
}
只输出JSON。"""
        else:
            system = """你是一个办公文档助手。为用户提供语义摘要，包含同义词扩展。
输出格式（JSON）：
{
    "summary": "用一段话描述这个文档的核心内容",
    "synonyms": ["相关术语1", "相关术语2"]
}
只输出JSON。"""
        
        prompt = f"文档内容：\n{text[:2000]}\n\n元数据：{json.dumps(metadata, ensure_ascii=False)}\n\n生成摘要："
        
        result = await self.generate(
            prompt, 
            system=system,
            component="LLM.generate_summary"
        )
        
        try:
            json_str = self._extract_json(result)
            data = json.loads(json_str.strip())
            return data.get("summary", ""), data.get("synonyms", [])
        except:
            return result, []
    
    async def generate_summary_multimodal(
        self,
        image_path: str,
        metadata: Dict[str, Any],
        space: str
    ) -> Tuple[str, List[str]]:
        """多模态生成语义摘要 - 直接理解图片"""
        if space == "family":
            system = """你是一个学习资料助手。你会看到用户上传的图片，请直接分析图片内容，生成语义摘要。

输出格式（JSON）：
{
    "summary": "用一段话描述这个错题/试卷/作业的核心内容，包含学科、知识点、错误原因等关键信息",
    "synonyms": ["相关知识点同义词1", "相关知识点同义词2", "题型关键词"]
}
只输出JSON。"""
        else:
            system = """你是一个办公文档助手。你会看到用户上传的图片，请直接分析图片内容，生成语义摘要。

输出格式（JSON）：
{
    "summary": "用一段话描述这个文档的核心内容，包含业务类型、金额、对方单位等关键信息",
    "synonyms": ["相关术语1", "相关术语2", "业务关键词"]
}
只输出JSON。"""
        
        prompt = f"元数据：{json.dumps(metadata, ensure_ascii=False)}\n\n请分析图片生成摘要："
        
        result = await self.generate_multimodal(
            prompt, 
            image_path, 
            system=system, 
            max_tokens=2048,
            component="LLM.generate_summary_multimodal"
        )
        
        try:
            json_str = self._extract_json(result)
            data = json.loads(json_str.strip())
            return data.get("summary", ""), data.get("synonyms", [])
        except Exception as e:
            print(f"   ⚠️ 解析摘要失败: {e}, 原始结果: {result[:200]}")
            return result, []
    
    def _extract_json(self, text: str) -> str:
        """提取 JSON 部分"""
        if "```json" in text:
            return text.split("```json")[1].split("```")[0]
        elif "```" in text:
            return text.split("```")[1].split("```")[0]
        elif "{" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            return text[start:end]
        return text
    
    async def translate(
        self,
        text: str,
        source_lang: str = "auto",
        target_lang: str = "zh"
    ) -> str:
        """翻译文本"""
        lang_map = {
            "en": "English",
            "zh": "Chinese",
            "ja": "Japanese",
            "ko": "Korean",
            "fr": "French",
            "de": "German",
            "es": "Spanish"
        }
        
        source_name = lang_map.get(source_lang, source_lang)
        target_name = lang_map.get(target_lang, target_lang)
        
        system = f"""你是一个专业的翻译助手。请将以下{source_name}文本翻译成{target_name}。

翻译要求：
1. 保持原文风格（正式/口语/学术等）
2. 专业术语保持准确
3. 如果是代码或技术内容，保持代码原样
4. 只输出翻译结果，不要解释

请直接输出翻译结果："""
        
        return await self.generate(
            text, 
            system=system, 
            temperature=0.3, 
            max_tokens=8192,
            component="LLM.translate"
        )
    
    async def extract_text_multimodal(self, image_path: str) -> str:
        """多模态提取图片中的文字"""
        system = """你是一个OCR助手。请仔细识别图片中所有的文字内容，包括：
1. 所有可见的文字（无论语言）
2. 标点符号
3. 数字
4. 保持原有换行和分段

请只输出识别到的文字，不要其他解释。如果图片中没有文字，请输出"[无文字内容]"。"""
        
        prompt = "请识别这张图片中的所有文字。"
        
        return await self.generate_multimodal(
            prompt, 
            image_path, 
            system=system, 
            max_tokens=4096,
            component="LLM.extract_text_multimodal"
        )


# ============== Embedding 服务 ==============

class EmbeddingService:
    """Embedding 服务（带 Token 追踪）"""
    
    def __init__(self, config: Optional[ProviderConfig] = None):
        self.config = config or get_provider_config()
        self._tracker = get_tracker()
        self._cache: Dict[str, List[float]] = {}
    
    async def embed(
        self, 
        text: str,
        track: bool = True,
        component: str = "Embedding.embed"
    ) -> List[float]:
        """生成文本向量"""
        cache_key = text[:100]
        if cache_key in self._cache:
            return self._cache[cache_key]
        
        # 如果是 Ollama 或无 API key 或 API 失败，使用随机向量（POC）
        if self.config.name == "ollama" or not self.config.api_key:
            import random
            vector = [random.random() for _ in range(1024)]
            self._cache[cache_key] = vector
            return vector
        
        try:
            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": self.config.embedding_model,
                "input": text[:8000]
            }
            
            start_time = time.time()
            
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self.config.base_url}/embeddings",
                    headers=headers,
                    json=payload
                )
                
                if response.status_code != 200:
                    raise Exception(f"API error: {response.status_code}")
                
                result = response.json()
                vector = result["data"][0]["embedding"]
                
                # 估算 token（Embedding 通常按字符计费，这里用输入文本长度估算）
                # OpenRouter embedding 通常 1 token ≈ 4 字符
                input_tokens = len(text) // 4
                duration_ms = (time.time() - start_time) * 1000
                
                if track:
                    self._tracker.record(
                        component=component,
                        model=self.config.embedding_model,
                        input_tokens=input_tokens,
                        output_tokens=0,  # Embedding 输出是向量，不计 output tokens
                        duration_ms=duration_ms
                    )
                
                self._cache[cache_key] = vector
                return vector
                
        except Exception as e:
            print(f"   ⚠️ Embedding API 失败，降级到随机向量: {e}")
            import random
            vector = [random.random() for _ in range(1024)]
            self._cache[cache_key] = vector
            return vector
    
    async def embed_batch(
        self, 
        texts: List[str],
        component: str = "Embedding.embed_batch"
    ) -> List[List[float]]:
        """批量生成向量"""
        return [await self.embed(t, component=component) for t in texts]


# ============== 服务工厂 ==============

class ServiceFactory:
    """服务工厂"""
    
    _config: Optional[ProviderConfig] = None
    _ocr: Optional[OCRService] = None
    _llm: Optional[LLMService] = None
    _embedding: Optional[EmbeddingService] = None
    
    @classmethod
    def get_config(cls) -> ProviderConfig:
        if cls._config is None:
            cls._config = get_provider_config()
        return cls._config
    
    @classmethod
    def get_ocr(cls) -> OCRService:
        if cls._ocr is None:
            cls._ocr = OCRService()
        return cls._ocr
    
    @classmethod
    def get_llm(cls) -> LLMService:
        if cls._llm is None:
            cls._llm = LLMService(cls.get_config())
        return cls._llm
    
    @classmethod
    def get_embedding(cls) -> EmbeddingService:
        if cls._embedding is None:
            cls._embedding = EmbeddingService(cls.get_config())
        return cls._embedding


# ============== 测试 ==============

async def test_services():
    """测试服务"""
    config = get_provider_config()
    print(f"=== 使用 Provider: {config.name} ===\n")
    
    # 测试 LLM
    print("1. 测试 LLM...")
    try:
        llm = LLMService(config)
        result = await llm.generate("你好，请用3个词介绍自己")
        print(f"   ✅ LLM: {result}")
    except Exception as e:
        print(f"   ❌ LLM: {e}")
    
    # 测试 Embedding
    print("\n2. 测试 Embedding...")
    try:
        emb = EmbeddingService(config)
        vector = await emb.embed("这是一个测试")
        print(f"   ✅ Embedding: 维度={len(vector)}")
    except Exception as e:
        print(f"   ❌ Embedding: {e}")
    
    # 测试 OCR
    print("\n3. 测试 OCR...")
    try:
        ocr = OCRService()
        print("   ✅ OCR: 已初始化")
    except Exception as e:
        print(f"   ❌ OCR: {e}")
    
    # 打印 token 统计
    tracker = get_tracker()
    tracker.print_report("服务测试 Token 统计")


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_services())

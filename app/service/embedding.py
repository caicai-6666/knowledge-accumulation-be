"""使用 LlamaIndex 异步调用 vLLM embedding，供索引与检索复用。"""

import math

import httpx
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from pydantic import Field, PrivateAttr

from app.config.embedding import EmbeddingSettings
from app.model.question_memory import normalize_embedding
from app.infrastructure.model_concurrency import get_model_request_limiter


def validate_vector(vector: list[float], dimensions: int) -> None:
    vector = normalize_embedding(vector)
    if len(vector) != dimensions:
        raise ValueError("向量维度不符或含非有限数值")
    if not any(vector):
        raise ValueError("余弦匹配不接受零向量")


class VLLMEmbedding(OpenAILikeEmbedding):
    expected_dimensions: int = Field(ge=1, le=4096)
    normalize: bool = True
    _request_maximum: int = PrivateAttr()

    def __init__(self, *, max_concurrent_requests: int, **kwargs):
        super().__init__(**kwargs)
        # 所有单条、批量与查询请求共用一个限制，不依赖批量调度器的并发行为。
        self._request_maximum = max_concurrent_requests

    def _prepare_vector(self, vector: list[float]) -> list[float]:
        validate_vector(vector, self.expected_dimensions)
        if self.normalize:
            norm = math.hypot(*vector)
            return [value / norm for value in vector]
        return vector

    async def _aget_text_embedding(self, text: str) -> list[float]:
        async with get_model_request_limiter('embedding', self._request_maximum):
            vectors = await self._request_embeddings([text], self._text_engine)
        return self._prepare_vector(vectors[0])

    async def _aget_query_embedding(self, query: str) -> list[float]:
        async with get_model_request_limiter('embedding', self._request_maximum):
            vectors = await self._request_embeddings([query], self._query_engine)
        return self._prepare_vector(vectors[0])

    async def _aget_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        async with get_model_request_limiter('embedding', self._request_maximum):
            vectors = await self._request_embeddings(texts, self._text_engine)
        if len(vectors) != len(texts):
            raise ValueError("批量 embedding 返回数量与输入不一致")
        return [self._prepare_vector(vector) for vector in vectors]

    async def _request_embeddings(self, texts, engine):
        # OpenAIEmbedding 默认把换行替换为空格，会破坏 vLLM 文本指令边界。
        # 保留 LlamaIndex 客户端、缓存、重试与限流，只替换其正文预处理步骤。
        client = self._get_aclient()
        @self._create_retry_decorator()
        async def request():
            return await client.embeddings.create(input=texts, model=engine, **self.additional_kwargs)
        response = await request()
        rows = sorted(response.data, key=lambda row: row.index)
        if [row.index for row in rows] != list(range(len(texts))):
            raise ValueError('embedding 响应数量或序号不合法')
        return [row.embedding for row in rows]

    def _get_text_embedding(self, text):
        raise NotImplementedError("vLLM 服务请使用异步 embedding 方法")

    def _get_query_embedding(self, query):
        raise NotImplementedError("vLLM 服务请使用异步 embedding 方法")

    def _get_text_embeddings(self, texts):
        raise NotImplementedError("vLLM 服务请使用异步 embedding 方法")


class EmbeddingService:
    def __init__(self, settings: EmbeddingSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._http = httpx.AsyncClient(timeout=settings.timeout_seconds, trust_env=False, follow_redirects=False, transport=transport)
        base = str(settings.base_url).rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        # 任意 vLLM 模型名使用 OpenAILikeEmbedding，不套用 OpenAI 模型名枚举。
        self.model = VLLMEmbedding(
            model_name=settings.model, api_base=base,
            api_key=settings.api_key.get_secret_value() or "unused",
            timeout=settings.timeout_seconds, max_retries=2,
            async_http_client=self._http,
            additional_kwargs={"encoding_format": "float"},
            embed_batch_size=settings.batch_size,
            num_workers=settings.max_concurrent_requests,
            max_concurrent_requests=settings.max_concurrent_requests,
            expected_dimensions=settings.dimensions, normalize=settings.normalize,
        )

    async def embed_text(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("待生成向量的文本不能为空")
        vector = await self.model.aget_text_embedding(text)
        validate_vector(vector, self.settings.dimensions)
        return vector

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if any(not text.strip() for text in texts):
            raise ValueError("待生成向量的文本不能为空")
        return await self.model.aget_text_embedding_batch(texts)

    async def close(self) -> None:
        await self._http.aclose()

"""底层算力接入 —— 模型中立 LLM 客户端（架构文档：执行层/接口可替换）。

- provider 可配置（anthropic | openai），API key 走环境变量（P0：不烘焙；严格模式缺 key 拒用）
- 基于 httpx，无重型 SDK 依赖；`http_post` 可注入 → 测试用 mock，不烧 token
- 统一 `complete()`（文本）与 `parse_json()`（结构化输出），记录用量供审计/成本
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable


class LLMConfigError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    provider: str = ""          # anthropic | openai | openai_compatible | ""（未配置）
    api_key: str = ""           # 从环境变量读，不烘焙
    model: str = ""             # 未设时按 provider 默认
    base_url: str = ""          # openai_compatible 用（DeepSeek/本地 vLLM/Groq 等任意兼容端点）
    max_tokens: int = 2000
    timeout_s: float = 60.0
    max_retries: int = 3
    strict: bool = False        # NG_ENV=production 时缺配置 → 拒绝调用


DEFAULT_MODELS = {"anthropic": "claude-haiku-4-5-20251001",
                  "openai": "gpt-4o-mini",
                  "openai_compatible": ""}


def _secret_file(name: str) -> str:
    """Docker secrets 挂载路径（/run/secrets/<NAME>）——存在则读内容。

    安全（2026-08-31）：LLM key 走 compose secrets 挂载为文件，不落 rendered config。
    """
    try:
        p = Path(f"/run/secrets/{name}")
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def _key_from(env: dict, name: str) -> str:
    """env 优先，缺则读 Docker secret 文件。"""
    val = (env.get(name) or "").strip()
    return val or _secret_file(name)


def load_config_from_env(env: dict | None = None) -> LLMConfig:
    """只要用户有 API 就接得进：Anthropic / OpenAI / 任意 OpenAI 兼容端点。

    key 来源优先级：环境变量 → Docker secret 文件（/run/secrets/<NAME>）→ .env。
    """
    if env is None:
        from dotenv import load_dotenv
        load_dotenv()   # 读 gitignore 的 .env（不覆盖已存在的环境变量）
        env = dict(os.environ)
    provider = (env.get("LLM_PROVIDER") or "").strip().lower()
    anthropic_key = _key_from(env, "ANTHROPIC_API_KEY")
    openai_key = _key_from(env, "OPENAI_API_KEY")
    generic_key = _key_from(env, "LLM_API_KEY")
    api_key = anthropic_key or openai_key or generic_key
    base_url = (env.get("LLM_BASE_URL") or env.get("OPENAI_BASE_URL") or "").strip()
    if provider in ("", "auto"):
        if anthropic_key:
            provider = "anthropic"
        elif base_url:
            provider = "openai_compatible"
        elif openai_key or generic_key:
            provider = "openai"
        else:
            provider = "anthropic"
    strict = str(env.get("NG_ENV", "")).lower() == "production" \
        or str(env.get("NG_STRICT_TOKENS", "")).lower() in {"1", "true", "yes"}
    return LLMConfig(provider=provider, api_key=api_key,
                     model=(env.get("LLM_MODEL") or "").strip(),
                     base_url=base_url, strict=strict)


class LLMClient:
    """跨 provider 的 LLM 调用。`http_post(url, headers, json, timeout) -> Response-like`。"""

    def __init__(self, config: LLMConfig | None = None, *, http_post: Callable | None = None):
        self.cfg = config or load_config_from_env()
        self._http_post = http_post or self._default_post
        self._usage: list[dict] = []   # 用量/成本记录

    # ---- 公开接口 ----
    def complete(self, system: str, user: str, *,
                 json_mode: bool = False, temperature: float = 0.2) -> str:
        self._require_ready()
        if json_mode:
            system = system + "\n只输出合法的 JSON 对象，不要其他任何文字。"
        payload = self._build_payload(system, user, temperature)
        last_err: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                data, status = self._call(payload)
                u = data.get("usage") or {}
                # openai 用 prompt/completion_tokens，anthropic 用 input/output_tokens
                self._usage.append({"provider": self.cfg.provider,
                                    "model": data.get("model") or self.cfg.model,
                                    "ts": time.time(), "status": status,
                                    "input_tokens": u.get("input_tokens") or u.get("prompt_tokens") or 0,
                                    "output_tokens": u.get("output_tokens") or u.get("completion_tokens") or 0})
                return self._extract_text(data)
            except Exception as e:      # noqa: BLE001 —— 逐次重试
                last_err = e
                if attempt < self.cfg.max_retries:
                    time.sleep(1.0 * attempt)
        raise RuntimeError(f"LLM 调用失败({self.cfg.provider}): {last_err}")

    def parse_json(self, system: str, user: str, *, temperature: float = 0.1) -> dict:
        """结构化输出：模型返回 JSON 文本 → dict。"""
        raw = self.complete(system, user, json_mode=True, temperature=temperature)
        return json.loads(self._extract_json(raw))

    def usage(self) -> list[dict]:
        return list(self._usage)

    # ---- 内部 ----
    def _require_ready(self):
        if not self.cfg.provider or not self.cfg.api_key:
            raise LLMConfigError(
                "算力未配置：设 LLM_PROVIDER + ANTHROPIC_API_KEY/OPENAI_API_KEY"
                + ("（NG_ENV=production 强制要求）" if self.cfg.strict else ""))

    def _build_payload(self, system: str, user: str, temperature: float) -> dict:
        if self.cfg.provider == "anthropic":
            return {"model": self.cfg.model or DEFAULT_MODELS["anthropic"],
                    "max_tokens": self.cfg.max_tokens, "system": system,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": user}]}
        # openai | openai_compatible（同一 wire 格式）
        return {"model": self.cfg.model or DEFAULT_MODELS.get("openai", "gpt-4o-mini"),
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": temperature, "max_tokens": self.cfg.max_tokens}

    def _call(self, payload: dict) -> tuple[dict, int]:
        if self.cfg.provider == "anthropic":
            resp = self._http_post(
                "https://api.anthropic.com/v1/messages",
                {"x-api-key": self.cfg.api_key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
                payload, self.cfg.timeout_s)
        else:
            base = self.cfg.base_url.rstrip("/")
            url = f"{base}/chat/completions" if base else "https://api.openai.com/v1/chat/completions"
            resp = self._http_post(
                url, {"Authorization": f"Bearer {self.cfg.api_key}",
                      "Content-Type": "application/json"},
                payload, self.cfg.timeout_s)
        resp.raise_for_status()
        return resp.json(), resp.status_code

    @staticmethod
    def _default_post(url: str, headers: dict, json: dict, timeout: float) -> Any:
        import httpx
        with httpx.Client(timeout=timeout) as c:
            return c.post(url, headers=headers, json=json)

    @staticmethod
    def _extract_text(data: dict) -> str:
        if "content" in data and isinstance(data["content"], list):
            return "".join(b.get("text", "") for b in data["content"]
                           if b.get("type") == "text")          # anthropic
        if "choices" in data:
            return data["choices"][0]["message"]["content"] or ""   # openai
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _extract_json(raw: str) -> str:
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:]
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            return s[a:b + 1]
        raise ValueError(f"模型输出非 JSON: {raw[:200]}")

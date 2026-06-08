"""LLM clients — provider-agnostic.

Use `make_llm(provider=...)` everywhere; the concrete classes are an implementation
detail. Supported providers: "anthropic" (default), "openai", "mock" (offline).
"""
from __future__ import annotations

import os
from typing import Optional

from .config import DEFAULT_PROVIDER, PROVIDER_DEFAULT_MODEL

_DEFAULT_SYSTEM = "You are Caliper, a careful scientific analysis planner."


class BaseLLM:
    """Interface: turn a prompt (+ optional system) into a text completion."""
    name = "base"

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        raise NotImplementedError


class AnthropicLLM(BaseLLM):
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-4-8",
                 api_key: Optional[str] = None, max_tokens: int = 4000,
                 timeout: float = 120.0):
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    def _ensure(self):
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("`pip install anthropic` to use the anthropic provider") from e
            self._client = Anthropic(api_key=self._api_key, timeout=self.timeout)

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        self._ensure()
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            system=system or _DEFAULT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(getattr(b, "text", "") for b in msg.content
                       if getattr(b, "type", None) == "text")


class OpenAILLM(BaseLLM):
    name = "openai"

    def __init__(self, model: str = "gpt-5",
                 api_key: Optional[str] = None, max_tokens: int = 16000,  # headroom for reasoning tokens
                 timeout: float = 180.0):  # reasoning models are slow
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = None

    def _ensure(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("`pip install openai` to use the openai provider") from e
            self._client = OpenAI(api_key=self._api_key, timeout=self.timeout)

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        self._ensure()
        messages = [{"role": "system", "content": system or _DEFAULT_SYSTEM},
                    {"role": "user", "content": prompt}]
        try:
            resp = self._client.chat.completions.create(
                model=self.model, max_tokens=self.max_tokens, messages=messages)
        except Exception as e:  # GPT-5+ require max_completion_tokens (TypeError or 400)
            if "max_completion_tokens" in str(e) or "max_tokens" in str(e):
                resp = self._client.chat.completions.create(
                    model=self.model, max_completion_tokens=self.max_tokens, messages=messages)
            else:
                raise
        return resp.choices[0].message.content or ""


# --- Offline deterministic stand-in -------------------------------------------------

import json as _json

_MOCK_CODE = '''import os, json, math
try:
    inp = json.loads(os.environ.get("CALIPER_INPUTS", "[]"))
    path = inp[0]["path"] if inp else None
except Exception:
    path = None
genes = []
if path:
    import csv
    rows = list(csv.reader(open(path)))
    header = rows[0][1:]; half = len(header) // 2
    for r in rows[1:]:
        name = r[0]; vals = [float(x) for x in r[1:]]
        a, b = vals[:half], vals[half:]
        ma = sum(a)/len(a) + 1.0; mb = sum(b)/len(b) + 1.0
        genes.append((name, math.log2(mb/ma)))
else:
    genes = [("MYC",2.6),("EGFR",2.0),("KRAS",1.8),("CDKN2A",2.1),("VEGFA",1.9),
             ("TP53",-1.3),("PTEN",-1.4),("GAPDH",0.0),("ACTB",0.02),("BCL2",0.1)]
de = sorted([g for g in genes if abs(g[1]) > 1.0], key=lambda x: -abs(x[1]))
fig = None
try:
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    top = de[:10][::-1]
    plt.figure(figsize=(5.4, 3.4))
    plt.barh([g for g, _ in top], [v for _, v in top],
             color=["#0e8f57" if v > 0 else "#c0735a" for _, v in top])
    plt.axvline(0, color="#999", lw=.6); plt.xlabel("log2 fold-change")
    plt.title("Top differentially-expressed genes"); plt.tight_layout()
    fig = os.path.abspath("de_genes.png"); plt.savefig(fig, dpi=140); plt.close()
except Exception:
    pass
res = {"n_genes": len(genes), "n_de": len(de),
       "top": [{"gene": g, "log2fc": round(l, 3)} for g, l in de[:10]]}
if fig:
    res["figures"] = [fig]
print("CALIPER_RESULT:" + json.dumps(res))
'''

_MOCK_PLAN = "```json\n" + _json.dumps({
    "summary": "Differential expression — top up/down genes, with a figure.",
    "steps": [{"tool": "deseq2",
               "rationale": "Two-condition count matrix -> up/down-regulated genes + plot.",
               "code": _MOCK_CODE}],
}, indent=2) + "\n```"

_MOCK_TRUST = '{"trust": 0.78, "rationale": "Clean two-group design, adequate replicates; '\
              'standard DE call. Mild uncertainty: no multiple-testing correction applied."}'


class MockLLM(BaseLLM):
    """Returns canned, deterministic responses keyed off prompt sentinels."""
    name = "mock"

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        if "RESPOND_WITH: plan_json" in prompt:
            return _MOCK_PLAN
        if "RESPOND_WITH: trust_json" in prompt:
            return _MOCK_TRUST
        return "{}"


def make_llm(provider: Optional[str] = None, model: Optional[str] = None, **kw) -> BaseLLM:
    """Factory. provider defaults to env CALIPER_PROVIDER or config.DEFAULT_PROVIDER."""
    provider = (provider or os.environ.get("CALIPER_PROVIDER") or DEFAULT_PROVIDER).lower()
    model = model or PROVIDER_DEFAULT_MODEL.get(provider)
    if provider == "anthropic":
        return AnthropicLLM(model=model, **kw)
    if provider == "openai":
        return OpenAILLM(model=model, **kw)
    if provider == "mock":
        return MockLLM()
    raise ValueError(f"Unknown provider {provider!r}; use anthropic | openai | mock")

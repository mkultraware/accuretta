# SPDX-License-Identifier: AGPL-3.0-only
"""How a model is named in the UI: family, size and quant from the file name and GGUF
metadata.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.runtime.gguf import read_gguf_metadata
from accuretta.runtime.logos import _match_logo


# llama.cpp's general.file_type values (enum llama_ftype) for shipped quants.
_GGUF_FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M",
    16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S",
    22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M",
    28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 36: "TQ1_0",
    37: "TQ2_0", 38: "MXFP4_MOE",
}


_NAME_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])(I?Q\d(?:_[A-Z0-9]{1,4})*|BF16|FP16|F16|F32|MXFP4(?:_MOE)?|TQ\d_\d)(?![A-Za-z0-9])",
    re.IGNORECASE)


# Name segments that describe the file, not the model: imatrix and uploader
# build tags, quantization-aware training, multi-token-prediction heads and
# the container format.
_NAME_FILE_TAGS = {"ud", "ad", "i1", "imat", "imatrix", "qat", "mtp", "gguf", "ggml"}


# Lowercase words that read as acronyms ("gpt-oss" -> "GPT OSS").
_NAME_ACRONYMS = {"gpt": "GPT", "oss": "OSS", "it": "IT", "vl": "VL", "moe": "MoE", "glm": "GLM", "lfm": "LFM"}


_NAME_SIZE_RE = re.compile(r"^(?:\d+x)?\d+(?:\.\d+)?[bm]$|^a\d+(?:\.\d+)?b$", re.IGNORECASE)


# family key -> (label, logo). Logos are the plain lobe-icons glyphs vendored
# in assets/vendor/lobe-icons; a family without one (its logo is a wordmark
# or too detailed to read at 18px) or an unknown family gets a letter.
MODEL_FAMILIES = {
    "qwen": ("Qwen", "qwen"), "llama": ("Llama", "meta"), "gemma": ("Gemma", "gemma"),
    "mistral": ("Mistral", "mistral"), "deepseek": ("DeepSeek", "deepseek"), "phi": ("Phi", "microsoft"),
    "glm": ("GLM", "chatglm"), "kimi": ("Kimi", "kimi"), "granite": ("Granite", "ibm"),
    "nemotron": ("Nemotron", "nvidia"), "command": ("Command", "cohere"), "gpt-oss": ("GPT-OSS", "openai"),
    "hunyuan": ("Hunyuan", "hunyuan"), "ernie": ("ERNIE", "wenxin"), "minimax": ("MiniMax", "minimax"),
    "olmo": ("OLMo", "ai2"), "exaone": ("EXAONE", "lg"), "internlm": ("InternLM", "internlm"),
    "falcon": ("Falcon", "tii"), "smollm": ("SmolLM", "huggingface"), "seed": ("Seed", "bytedance"),
    "step": ("Step", "stepfun"), "yi": ("Yi", "yi"), "baichuan": ("Baichuan", "baichuan"),
    "grok": ("Grok", "grok"), "jamba": ("Jamba", "ai21"), "arcee": ("Arcee", "arcee"),
    "solar": ("Solar", "upstage"), "reka": ("Reka", "reka"), "skywork": ("Skywork", "skywork"),
    "mimo": ("MiMo", ""), "lfm": ("LFM", "liquid"), "arctic": ("Arctic", "snowflake"),
    "dbrx": ("DBRX", "dbrx"), "stablelm": ("StableLM", "stability"), "openelm": ("OpenELM", "apple"),
    "rwkv": ("RWKV", "rwkv"), "jina": ("Jina", "jina"), "bge": ("BGE", "baai"),
    "cogito": ("Cogito", "deepcogito"), "hermes": ("Hermes", "nousresearch"), "dolphin": ("Dolphin", ""),
    "perplexity": ("Perplexity", "perplexity"), "sensenova": ("SenseNova", "sensenova"),
    "spark": ("Spark", "spark"), "codegeex": ("CodeGeeX", "codegeex"), "llava": ("LLaVA", "llava"),
    "longcat": ("LongCat", "longcat"), "kat": ("KAT", "kwaipilot"), "ling": ("Ling", "antgroup"),
    "essential": ("Essential", "essentialai"), "openchat": ("OpenChat", "openchat"),
    "sakana": ("Sakana", "sakana"), "pharia": ("Pharia", "alephalpha"), "yandexgpt": ("YandexGPT", "yandex"),
    "jan": ("Jan", "menlo"), "pangu": ("Pangu", "huawei"), "cerebras": ("Cerebras", "cerebras"),
    "phind": ("Phind", "phind"), "inkling": ("Inkling", "thinkingmachines"),
}


# The base model's maker decides first, so a lab's model built on another
# lab's architecture (Kimi on DeepSeek's) keeps its own family, and a
# finetune keeps its base model's family.
_FAMILY_BY_ORG = {
    "qwen": "qwen", "alibaba": "qwen", "alibabacloud": "qwen",
    "meta": "llama", "metallama": "llama", "facebook": "llama",
    "google": "gemma", "mistral": "mistral", "mistralai": "mistral",
    "deepseek": "deepseek", "deepseekai": "deepseek", "microsoft": "phi",
    "zai": "glm", "zaiorg": "glm", "thudm": "glm", "zhipuai": "glm",
    "moonshot": "kimi", "moonshotai": "kimi", "ibm": "granite", "ibmgranite": "granite",
    "nvidia": "nemotron", "cohere": "command", "cohereforai": "command", "coherelabs": "command",
    "openai": "gpt-oss", "tencent": "hunyuan", "baidu": "ernie",
    "minimax": "minimax", "minimaxai": "minimax", "allenai": "olmo",
    "lgai": "exaone", "lgaiexaone": "exaone", "internlm": "internlm",
    "tiiuae": "falcon", "huggingfacetb": "smollm",
    "bytedance": "seed", "bytedanceseed": "seed", "stepfun": "step", "stepfunai": "step",
    "01ai": "yi", "baichuan": "baichuan", "baichuaninc": "baichuan", "xai": "grok", "xaiorg": "grok",
    "ai21": "jamba", "ai21labs": "jamba", "arcee": "arcee", "arceeai": "arcee", "upstage": "solar",
    "reka": "reka", "rekaai": "reka", "skywork": "skywork", "skyworkai": "skywork",
    "xiaomi": "mimo", "xiaomimimo": "mimo", "liquid": "lfm", "liquidai": "lfm",
    "snowflake": "arctic", "databricks": "dbrx", "stability": "stablelm", "stabilityai": "stablelm",
    "apple": "openelm", "rwkv": "rwkv", "blinkdl": "rwkv", "jinaai": "jina", "baai": "bge",
    "deepcogito": "cogito", "nousresearch": "hermes", "cognitivecomputations": "dolphin", "dphn": "dolphin",
    "perplexity": "perplexity", "perplexityai": "perplexity", "sensetime": "sensenova", "iflytek": "spark",
    "llavahf": "llava", "liuhaotian": "llava", "meituan": "longcat", "meituanlongcat": "longcat",
    "kwaipilot": "kat", "inclusionai": "ling", "antgroup": "ling", "essentialai": "essential",
    "openchat": "openchat", "sakanaai": "sakana", "alephalpha": "pharia", "yandex": "yandexgpt",
    "janhq": "jan", "menloresearch": "jan", "huawei": "pangu", "cerebras": "cerebras", "phind": "phind",
    "thinkingmachines": "inkling", "thinkingmachineslab": "inkling",
}


# Then the architecture. "llama" is left out: many labs ship on it, so a bare
# llama architecture says nothing about who made the model.
_FAMILY_BY_ARCH = (
    ("qwen", "qwen"), ("gemma", "gemma"), ("mistral", "mistral"), ("deepseek", "deepseek"),
    ("phi", "phi"), ("chatglm", "glm"), ("glm", "glm"), ("granite", "granite"),
    ("nemotron", "nemotron"), ("command-r", "command"), ("cohere", "command"),
    ("gpt-oss", "gpt-oss"), ("hunyuan", "hunyuan"), ("ernie", "ernie"), ("minimax", "minimax"),
    ("olmo", "olmo"), ("exaone", "exaone"), ("internlm", "internlm"), ("falcon", "falcon"),
    ("smollm", "smollm"), ("seed", "seed"), ("step", "step"), ("llama4", "llama"),
    ("baichuan", "baichuan"), ("grok", "grok"), ("jamba", "jamba"), ("arcee", "arcee"),
    ("lfm", "lfm"), ("arctic", "arctic"), ("dbrx", "dbrx"), ("stablelm", "stablelm"),
    ("openelm", "openelm"), ("rwkv", "rwkv"), ("jina", "jina"), ("bailing", "ling"),
    ("mimo", "mimo"), ("pangu", "pangu"), ("longcat", "longcat"), ("inkling", "inkling"),
)


# Last, words in the file name: base families first, "llama" after them (so
# "DeepSeek-R1-Distill-Llama" stays DeepSeek), then finetune brands.
_FAMILY_BY_NAME = tuple((re.compile(pattern), family) for pattern, family in (
    (r"qwen|qwq", "qwen"), (r"gemma", "gemma"),
    (r"mistral|mixtral|ministral|devstral|magistral|codestral|pixtral|voxtral", "mistral"),
    (r"deepseek", "deepseek"), (r"\bphi-?\d", "phi"), (r"\bglm|chatglm", "glm"), (r"kimi", "kimi"),
    (r"granite", "granite"), (r"nemotron", "nemotron"), (r"command-?r|\baya-", "command"),
    (r"gpt-oss", "gpt-oss"), (r"hunyuan", "hunyuan"), (r"ernie", "ernie"), (r"minimax", "minimax"),
    (r"olmo", "olmo"), (r"exaone", "exaone"), (r"internlm|intern-?s1", "internlm"), (r"falcon", "falcon"),
    (r"smollm", "smollm"), (r"seed-?oss", "seed"), (r"\bstep-?\d", "step"), (r"\byi-", "yi"),
    (r"baichuan", "baichuan"), (r"grok", "grok"), (r"jamba", "jamba"), (r"arcee|\bafm-", "arcee"),
    (r"\bsolar", "solar"), (r"\breka-", "reka"), (r"skywork", "skywork"), (r"\bmimo", "mimo"),
    (r"\blfm", "lfm"), (r"snowflake-arctic|\barctic-", "arctic"), (r"dbrx", "dbrx"),
    (r"stablelm|stable-code", "stablelm"), (r"openelm", "openelm"), (r"rwkv", "rwkv"), (r"jina", "jina"),
    (r"\bbge-|aquila", "bge"), (r"sensenova", "sensenova"), (r"iflytek", "spark"),
    (r"codegeex", "codegeex"), (r"llava", "llava"), (r"longcat", "longcat"), (r"\bkat-", "kat"),
    (r"\bling-|\bring-", "ling"), (r"\brnj-", "essential"), (r"pharia", "pharia"),
    (r"yandexgpt", "yandexgpt"), (r"pangu", "pangu"), (r"cerebras", "cerebras"), (r"inkling", "inkling"),
    (r"llama", "llama"),
    (r"hermes", "hermes"), (r"dolphin", "dolphin"), (r"cogito", "cogito"), (r"openchat", "openchat"),
    (r"r1-1776", "perplexity"), (r"phind", "phind"), (r"\bjan-", "jan"),
))


def _model_family(name: str, meta: dict) -> tuple[str, str, str]:
    keys = meta.get("keys") or {}
    org_raw = str(keys.get("general.base_model.0.organization") or "").strip()
    org = re.sub(r"[^a-z0-9]", "", org_raw.lower())
    family = _FAMILY_BY_ORG.get(org, "")
    if not family:
        arch = str(meta.get("architecture") or keys.get("general.architecture") or "").lower()
        family = next((fam for prefix, fam in _FAMILY_BY_ARCH if arch.startswith(prefix)), "")
    if not family:
        low = name.lower()
        family = next((fam for pattern, fam in _FAMILY_BY_NAME if pattern.search(low)), "")
    if family:
        return (family, *MODEL_FAMILIES[family])
    # Unknown: name it after the base model's maker, else the file's first word.
    label = org_raw or (re.match(r"[A-Za-z]+", name) or [""])[0]
    key = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "other"
    return key, (label or "Other"), _match_logo(label)


def _quant_bits(quant: str) -> int:
    q = (quant or "").upper()
    if q in ("BF16", "FP16", "F16"):
        return 16
    if q == "F32":
        return 32
    if q.startswith("MXFP4"):
        return 4
    if q.startswith("TQ"):
        return 2
    m = re.match(r"I?Q(\d)", q)
    return int(m.group(1)) if m else 0


def _display_word(seg: str) -> str:
    if seg.lower() in _NAME_ACRONYMS:
        return _NAME_ACRONYMS[seg.lower()]
    if _NAME_SIZE_RE.match(seg):
        word = seg[:-1] + seg[-1].upper()
        return "A" + word[1:] if word[:1] in ("a", "A") else word
    return seg[:1].upper() + seg[1:] if seg.islower() else seg


def model_display_info(name: str, meta: dict | None = None) -> dict:
    """What the model menu shows for one GGUF file: display_name ("Qwen3.8
    27B"), group (files of one model share it), quant ("Q4_K_XL"), quant_tags
    (["UD"]), bits (4) and the family key, label and logo. The quant comes from the
    file name, else from general.file_type. Never raises."""
    meta = meta or {}
    keys = meta.get("keys") or {}
    stem = re.sub(r"\.gguf$", "", str(name or ""), flags=re.IGNORECASE)
    stem = re.sub(r"[-_.]\d{5}-of-\d{5}$", "", stem, flags=re.IGNORECASE)
    quant = ""
    found = list(_NAME_QUANT_RE.finditer(stem))
    if found:
        hit = found[-1]
        quant = hit.group(1).upper()
        stem = f"{stem[:hit.start()]} {stem[hit.end():]}".strip(" -_.")
    if not quant:
        try:
            quant = _GGUF_FILE_TYPES.get(int(keys.get("general.file_type")), "")
        except (TypeError, ValueError):
            quant = ""
    tags, words = [], []
    for seg in re.split(r"[-_\s]+|(?<=[A-Za-z])\.(?=[A-Za-z])", stem):
        if not seg:
            continue
        if seg.lower() in _NAME_FILE_TAGS:
            if seg.lower() not in ("gguf", "ggml"):
                tags.append(seg)
            continue
        words.append(_display_word(seg))
    display = " ".join(words) or stem or str(name or "")
    family, family_label, family_logo = _model_family(str(name or ""), meta)
    arch = keys.get("general.architecture")
    return {
        "display_name": display,
        "architecture": arch.strip() if isinstance(arch, str) else "",
        "group": display.lower(),
        "quant": quant,
        "quant_tags": tags,
        "bits": _quant_bits(quant),
        "family": family,
        "family_label": family_label,
        "family_logo": family_logo,
    }


_MODEL_INFO_CACHE: dict[tuple, dict] = {}


def model_display_info_for(entry: dict) -> dict:
    """model_display_info for a scan_gguf_dir entry, cached per file version.
    Only the first megabyte is read: the name, architecture and base-model
    keys sit at the start of the header. A name without a quant needs
    general.file_type, which often sits past the tokenizer, so those files
    get the full header read."""
    key = (entry.get("path"), entry.get("size"), entry.get("modified_at"))
    info = _MODEL_INFO_CACHE.get(key)
    if info is None:
        path, name = entry.get("path") or "", entry.get("name") or ""
        meta = read_gguf_metadata(path, max_bytes=1024 * 1024, retry_bytes=0)
        if not _NAME_QUANT_RE.search(name) and "general.file_type" not in (meta.get("keys") or {}):
            meta = read_gguf_metadata(path)
        info = model_display_info(name, meta)
        _MODEL_INFO_CACHE[key] = info
    return info

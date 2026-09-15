"""PH0-06 基金文件核验：定位最新招募说明书 → 下载到仓库外 → 提取估值汇率等条款候选句。

- PDF 原件为第三方文件，只保存在 <data_root>/evidence/ph0-06/<code>/，仓库内仅保留来源、哈希、页码与短摘录。
- 提取只做候选句定位与分类；“条款 → 规则”的解读由人工写入 data/fund_rules/<code>.toml，并注明依据。
依赖 pypdf（uv 依赖组 research）。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx

from qdii.core.types import RawMessage
from qdii.io.clock import SystemClock
from qdii.io.http import EndpointRequest, Fetcher

UA = "qdii-premium-research/0.1 (personal research)"
FUNDS = ("513100", "159696", "159501", "159660", "513390")
MAX_EXCERPT = 220

# 句子分类：只有 VALUATION_FX 用于判定净值估值汇率规则
CATEGORIES = (
    ("PCF_CASH_SUBSTITUTE", ("替代",)),
    ("IOPV", ("参考净值",)),
    ("BENCHMARK", ("业绩比较基准",)),
    ("VALUATION_FX", ("估值", "汇率")),
    ("FX_OTHER", ("汇率",)),
)
DAY_DEFINITION = re.compile(r"(估值日|T\+1日|工作日)[^。；]{0,10}(指|是指|为)")


@dataclass(frozen=True)
class Document:
    code: str
    announcement_id: str
    title: str
    publish_date: str
    url: str
    sha256: str
    bytes: int
    pages: int
    received_utc: str


@dataclass(frozen=True)
class Excerpt:
    page: int
    category: str
    text: str


async def _get(fetcher: Fetcher, endpoint_id: str, url: str, headers: dict | None = None) -> RawMessage:
    req = EndpointRequest(endpoint_id, url.split("/")[2], "GET", url,
                          tuple(sorted({"User-Agent": UA, **(headers or {})}.items())))
    msg = await fetcher.fetch(req, 0)
    await asyncio.sleep(1.2)
    return msg


async def find_prospectus(fetcher: Fetcher, code: str, max_pages: int = 5) -> dict | None:
    for page in range(1, max_pages + 1):
        url = "https://api.fund.eastmoney.com/f10/JJGG?" + urlencode(
            {"fundcode": code, "pageIndex": page, "pageSize": 20, "type": 1})
        msg = await _get(fetcher, f"research.eastmoney.jjgg.{code}", url, {"Referer": "https://fundf10.eastmoney.com/"})
        rows = (json.loads(msg.body).get("Data") or []) if msg.status == 200 else []
        for row in rows:  # 接口按发布日期倒序
            title = row.get("TITLE", "")
            if "招募说明书" in title and not any(x in title for x in ("摘要", "提示", "更正")):
                return row
        if len(rows) < 20:
            break
    return None


def classify(sentence: str) -> str | None:
    for name, words in CATEGORIES:
        if all(w in sentence for w in words):
            return name
    return None


def keep_in_repo(e: Excerpt) -> bool:
    if e.category in ("VALUATION_FX", "IOPV"):
        return True
    return e.category == "DAY_DEFINITION" and "估值日" in e.text


def extract(pdf_bytes: bytes) -> tuple[int, list[Excerpt]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    seen: set[str] = set()
    out: list[Excerpt] = []
    for i, page in enumerate(reader.pages, start=1):
        text = re.sub(r"\s+", "", page.extract_text() or "")
        for sentence in re.split(r"(?<=[。；])", text):
            cat = classify(sentence)
            if cat is None and DAY_DEFINITION.search(sentence):
                cat = "DAY_DEFINITION"
            if cat is None:
                continue
            key = sentence[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(Excerpt(i, cat, sentence[:MAX_EXCERPT] + ("…" if len(sentence) > MAX_EXCERPT else "")))
    return len(reader.pages), out


async def run(data_root: Path, out_dir: Path, funds: tuple[str, ...] = FUNDS) -> Path:
    evidence = data_root / "evidence" / "ph0-06"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    summary = {"run": f"RESEARCH-RULES-{stamp}", "funds": {}}
    async with httpx.AsyncClient(timeout=90, follow_redirects=True, trust_env=False) as client:
        fetcher = Fetcher(client, SystemClock(), summary["run"])
        for code in funds:
            row = await find_prospectus(fetcher, code)
            if row is None:
                summary["funds"][code] = {"error": "未找到招募说明书"}
                continue
            url = f"https://pdf.dfcfw.com/pdf/H2_{row['ID']}_1.pdf"
            msg = await _get(fetcher, f"research.eastmoney.pdf.{code}", url)
            if msg.status != 200 or not msg.body.startswith(b"%PDF"):
                summary["funds"][code] = {"error": f"下载失败 HTTP {msg.status}", "url": url}
                continue
            target = evidence / code / f"{row['ID']}.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(msg.body)
            pages, excerpts = extract(msg.body)
            doc = Document(code, row["ID"], row["TITLE"], row.get("PUBLISHDATEDesc", ""), url,
                           hashlib.sha256(msg.body).hexdigest(), len(msg.body), pages,
                           datetime.fromtimestamp(msg.received_utc_ns / 1e9, tz=UTC).isoformat(timespec="seconds"))
            full = {"document": asdict(doc), "excerpts": [asdict(e) for e in excerpts]}
            (target.parent / f"{row['ID']}.extract.json").write_text(json.dumps(full, ensure_ascii=False, indent=1),
                                                                     encoding="utf-8")
            # 仓库内（公开）只保留判定所需的短摘录：估值汇率、估值日定义、IOPV 计算方式
            kept = [asdict(e) for e in excerpts if keep_in_repo(e)]
            (out_dir / f"extract_{code}.json").write_text(
                json.dumps({"document": asdict(doc), "excerpts": kept,
                            "note": "完整提取保存在仓库外 evidence/ph0-06/<code>/<ID>.extract.json"},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            counts: dict[str, int] = {}
            for e in excerpts:
                counts[e.category] = counts.get(e.category, 0) + 1
            summary["funds"][code] = {"document": asdict(doc), "excerpt_counts": counts}
    path = out_dir / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return path

"""期货基差研究工具（I/O 层）：离线重算、原始响应选择与哈希、输出冲突、网络模式先落盘、旧版对比。

原始日志在 tmp_path 中由录制夹具重新封装（body 取夹具原值；父响应 hash 见夹具文件），不访问网络。
"""

from __future__ import annotations

import asyncio
import gzip
import importlib.util
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from qdii.io import rawlog
from tests.helpers import make_msg

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "recorded" / "yahoo_basis_endpoints_20260917.json"
LEGACY = REPO / "reports" / "mvp" / "evidence" / "futures-basis-2026-09-18.json"
RUN = "RESEARCH-20260918T062540Z"
EP_FUT, EP_IDX = "research.yahoo.chart.NQZ26", "research.yahoo.chart.NDX"


def load_tool():
    spec = importlib.util.spec_from_file_location("futures_basis_intraday", REPO / "tools" / "futures_basis_intraday.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tool(monkeypatch):
    mod = load_tool()

    def no_network(*a, **k):
        raise AssertionError("offline mode must not touch the network")

    monkeypatch.setattr(mod.httpx, "AsyncClient", no_network)
    monkeypatch.setattr(mod, "fetch", no_network)
    return mod


def series(symbol: str, days: tuple[str, ...] | None = None) -> dict:
    s = json.loads(FIXTURE.read_text(encoding="utf-8"))["series"][symbol]
    if days is None:
        return s
    from datetime import datetime
    from zoneinfo import ZoneInfo

    keep = [k for k, t in enumerate(s["timestamp"])
            if datetime.fromtimestamp(t, ZoneInfo("America/New_York")).date().isoformat() in days]
    return {**s, "timestamp": [s["timestamp"][k] for k in keep], "close": [s["close"][k] for k in keep]}


def body(s: dict) -> bytes:
    return json.dumps({"chart": {"result": [{"meta": s["meta"], "timestamp": s["timestamp"],
                                             "indicators": {"quote": [{"close": s["close"]}]}}], "error": None}}).encode()


def msg(endpoint_id: str, payload: bytes, *, seq: int, received: int, status: int = 200, run_id: str = RUN):
    return make_msg(payload, status=status, source_id="yahoo", endpoint_id=endpoint_id, received_utc_ns=received,
                    seq=seq, run_id=run_id)


def raw_file(tmp_path: Path, msgs, name: str = "06.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(rawlog.encode(m) + "\n" for m in msgs), encoding="utf-8")
    return path


def default_msgs(days: tuple[str, ...] | None = None):
    f, i = series("NQZ26.CME", days), series("^NDX", days)
    return [msg(EP_FUT, body(f), seq=0, received=f["received_utc_ns"]),
            msg(EP_IDX, body(i), seq=1, received=i["received_utc_ns"])]


def cli(tool, raw: Path, out: Path, *extra: str) -> int:
    return tool.run(["NQZ26", "--raw-file", str(raw), "--run-id", RUN, "--output", str(out), *extra])


def test_offline_recompute_uses_only_the_raw_file(tool, tmp_path):
    out = tmp_path / "v2.json"
    assert cli(tool, raw_file(tmp_path, default_msgs()), out) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["meta"]["mode"] == "OFFLINE_RAW_FILE"
    assert [r["endpoint_id"] for r in doc["inputs"]["responses"]] == [EP_FUT, EP_IDX]
    assert all(r["body_sha256_verified"] for r in doc["inputs"]["responses"])
    a = doc["analysis"]
    assert a["cross_session"]["n"] == 1 and a["cross_session"]["status"] == "OK"
    assert (a["asia_decision_error_status"], a["asia_decision_error_bound_bp"], a["decision_grade"]) == (
        "UNIDENTIFIED", None, False)
    assert doc["inputs"]["calendar"]["provider_version"].startswith("exchange_calendars==")


def test_same_input_gives_identical_analysis_and_metadata_is_separate(tool, tmp_path):
    raw = raw_file(tmp_path, default_msgs())
    assert cli(tool, raw, tmp_path / "a.json") == 0 and cli(tool, raw, tmp_path / "b.json") == 0
    a, b = (json.loads((tmp_path / n).read_text(encoding="utf-8")) for n in ("a.json", "b.json"))
    assert a["analysis"] == b["analysis"] and a["inputs"] == b["inputs"]
    assert set(a) == {"schema_version", "meta", "inputs", "analysis", "legacy_comparison"}


def test_gzip_segment_is_readable(tool, tmp_path):
    plain = raw_file(tmp_path, default_msgs())
    gz = tmp_path / "06.jsonl.gz"
    gz.write_bytes(gzip.compress(plain.read_bytes()))
    assert cli(tool, gz, tmp_path / "v2.json") == 0


def test_hash_mismatch_is_rejected_and_nothing_is_written(tool, tmp_path, capsys):
    msgs = default_msgs()
    msgs[1] = replace(msgs[1], body_sha256="0" * 64)
    out = tmp_path / "v2.json"
    assert cli(tool, raw_file(tmp_path, msgs), out) == 1
    assert not out.exists() and "HASH_MISMATCH" in capsys.readouterr().err


def test_missing_response_is_rejected(tool, tmp_path, capsys):
    out = tmp_path / "v2.json"
    assert cli(tool, raw_file(tmp_path, default_msgs()[:1]), out) == 1
    assert not out.exists() and "MISSING_RESPONSE" in capsys.readouterr().err


def test_duplicate_response_is_not_resolved_by_taking_the_last(tool, tmp_path, capsys):
    msgs = default_msgs()
    msgs.append(replace(msgs[1], msg_id="yahoo:dup:2"))
    assert cli(tool, raw_file(tmp_path, msgs), tmp_path / "v2.json") == 1
    assert "DUPLICATE_RESPONSE" in capsys.readouterr().err


def test_other_run_ids_in_the_same_file_are_ignored(tool, tmp_path):
    msgs = default_msgs() + [replace(m, run_id="RESEARCH-OTHER", msg_id=m.msg_id + "x") for m in default_msgs()]
    assert cli(tool, raw_file(tmp_path, msgs), tmp_path / "v2.json") == 0


@pytest.mark.parametrize(("change", "code"), [
    ({"status": 500}, "HTTP_ERROR"),
    ({"body": b"<html>"}, "JSON_INVALID"),
])
def test_http_and_json_errors_are_explicit(tool, tmp_path, capsys, change, code):
    msgs = default_msgs()
    m = msgs[0]
    if "body" in change:
        m = msg(EP_FUT, change["body"], seq=0, received=m.received_utc_ns)
    else:
        m = replace(m, **change)
    msgs[0] = m
    assert cli(tool, raw_file(tmp_path, msgs), tmp_path / "v2.json") == 1
    assert code in capsys.readouterr().err


def test_empty_series_is_no_data_not_a_crash(tool, tmp_path):
    empty = [msg(ep, body({**series(sym), "timestamp": [], "close": []}), seq=k, received=series(sym)["received_utc_ns"])
             for k, (ep, sym) in enumerate(((EP_FUT, "NQZ26.CME"), (EP_IDX, "^NDX")))]
    out = tmp_path / "v2.json"
    assert cli(tool, raw_file(tmp_path, empty), out) == 3
    assert json.loads(out.read_text(encoding="utf-8"))["analysis"]["cross_session"]["status"] == "NO_DATA"


def test_single_session_is_no_data(tool, tmp_path):
    out = tmp_path / "v2.json"
    assert cli(tool, raw_file(tmp_path, default_msgs(("2026-09-17",))), out) == 3
    a = json.loads(out.read_text(encoding="utf-8"))["analysis"]
    assert a["cross_session"]["n"] == 0 and a["asia_decision_error_status"] == "UNIDENTIFIED"


def test_existing_output_is_never_overwritten(tool, tmp_path, capsys):
    out = tmp_path / "v2.json"
    out.write_text("historical", encoding="utf-8")
    assert cli(tool, raw_file(tmp_path, default_msgs()), out) == 2
    assert out.read_text(encoding="utf-8") == "historical" and "拒绝覆盖" in capsys.readouterr().err


def test_official_closes_are_read_offline_and_verify_the_close_record(tool, tmp_path):
    root = tmp_path / "data"
    nasdaq = {"data": {"symbol": "NDX", "tradesTable": {"rows": [{"date": "09/17/2026", "close": "29,446.98"},
                                                                 {"date": "09/16/2026", "close": "28,945.06"}]}}}
    seg = root / "raw" / "nasdaq" / "2026-09-18"
    seg.mkdir(parents=True)
    (seg / "05.jsonl").write_text(rawlog.encode(make_msg(json.dumps(nasdaq).encode(), source_id="nasdaq",
                                                         endpoint_id="nasdaq.ndx_history", seq=7)) + "\n",
                                  encoding="utf-8")
    official, conflicts = tool.load_official_closes(root, date(2026, 9, 16), date(2026, 9, 18))
    assert official[date(2026, 9, 17)].value == "29446.98" and conflicts == []
    out = tmp_path / "v2.json"
    assert cli(tool, raw_file(tmp_path, default_msgs()), out, "--official-close-root", str(root)) == 0
    a = json.loads(out.read_text(encoding="utf-8"))["analysis"]
    assert [v["status"] for v in a["official_close_diagnostic"]["verification"]] == ["VERIFIED"]
    last = next(s for s in a["sessions"]["session_close_basis"] if s["date"] == "2026-09-17")
    assert last["official_close_index"] == 29446.98046875 and last["bar_close_index"] == 29442.1171875
    assert a["official_close_diagnostic"]["status"] == "NO_DATA"  # 09-16 没有收盘时刻记录，不足以成对


def test_conflicting_official_closes_are_excluded(tool, tmp_path):
    root = tmp_path / "data"
    seg = root / "raw" / "nasdaq" / "2026-09-18"
    seg.mkdir(parents=True)
    lines = []
    for k, close in enumerate(("29,446.98", "29,446.99")):
        doc = {"data": {"symbol": "NDX", "tradesTable": {"rows": [{"date": "09/17/2026", "close": close}]}}}
        lines.append(rawlog.encode(make_msg(json.dumps(doc).encode(), source_id="nasdaq",
                                            endpoint_id="nasdaq.ndx_history", seq=k)))
    (seg / "05.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    official, conflicts = tool.load_official_closes(root, date(2026, 9, 16), date(2026, 9, 18))
    assert date(2026, 9, 17) not in official and conflicts[0]["date"] == "2026-09-17"


def test_legacy_comparison_attributes_each_difference(tool, tmp_path):
    out = tmp_path / "v2.json"
    assert cli(tool, raw_file(tmp_path, default_msgs()), out, "--legacy", str(LEGACY)) == 0
    cmp = json.loads(out.read_text(encoding="utf-8"))["legacy_comparison"]
    pair = next(p for p in cmp["pairs"] if (p["from"], p["to"]) == ("2026-09-16", "2026-09-17"))
    assert pair["legacy_reproduced"] is True and pair["same_records"] is True and pair["change"] == "ROUNDING_ONLY"
    assert pair["legacy_close_label_ny"] == "15:55" and pair["new_close_interval_ny"] == "15:55—16:00"
    last = cmp["last_close_basis"]
    assert last["legacy_bp"] == 98.6 and last["legacy_reproduced"] is True and last["change"] == "ENDPOINT_CHANGED"
    assert last["legacy_records"]["futures_interval_ny"] == "16:00—16:05"
    assert last["legacy_records"]["index_kind"] == "SESSION_CLOSE_RECORD"
    assert last["new_bar_close_bp"] == pytest.approx((29744.50 / 29442.1171875 - 1) * 1e4)
    assert cmp["intraday"]["change"] == "METHOD_CHANGED_NOT_COMPARABLE"
    assert sum(1 for p in cmp["pairs"] if p["change"] == "ONLY_IN_LEGACY") == 20  # 夹具只含 09-16、09-17


def test_network_mode_writes_the_raw_log_first_and_reparses_it(tmp_path, monkeypatch):
    mod = load_tool()  # 网络模式：用假抓取器替换 HTTP，但走真实的写日志与重解析
    canned = {EP_FUT: default_msgs()[0], EP_IDX: default_msgs()[1]}

    class FakeFetcher:
        def __init__(self, client, clock, run_id):
            self.run_id = run_id

        async def fetch(self, req, seq):
            return replace(canned[req.endpoint_id], run_id=self.run_id)

    monkeypatch.setattr(mod, "Fetcher", FakeFetcher)
    monkeypatch.setattr(mod, "PAUSE_S", 0.0)
    rc = mod.run(["NQZ26", "--research-root", str(tmp_path / "research"), "--output-dir", str(tmp_path)])
    assert rc == 0
    outs = list(tmp_path.glob("futures-basis-RESEARCH-*.json"))
    assert len(outs) == 1
    doc = json.loads(outs[0].read_text(encoding="utf-8"))
    assert doc["meta"]["mode"] == "NETWORK" and doc["inputs"]["run_id"] in outs[0].name
    assert Path(doc["inputs"]["raw_files"][0]).is_file()  # 重解析的来源是刚写下的原始日志


def test_network_mode_closes_the_writer_when_a_fetch_fails(tmp_path, monkeypatch):
    mod = load_tool()
    closed = []

    class Recording(rawlog.RawLogWriter):
        def close(self):
            closed.append(True)
            super().close()

    class Failing:
        def __init__(self, *a):
            pass

        async def fetch(self, req, seq):
            raise RuntimeError("boom")

    monkeypatch.setattr(mod.rawlog, "RawLogWriter", Recording)
    monkeypatch.setattr(mod, "Fetcher", Failing)
    monkeypatch.setattr(mod, "PAUSE_S", 0.0)
    with pytest.raises(RuntimeError):
        asyncio.run(mod.fetch("NQZ26", tmp_path))
    assert closed == [True]

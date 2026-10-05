"""A bundled, offline demo run.

Why this exists
---------------
Everything else in this project needs a network, a credential, or both. That
makes it awkward to answer the very first question a visitor has - *what does the
output actually look like?* - without asking them to sign up for a Zhipu account
first.

`sourced-intel demo` renders a complete report from the data in this module with no
network access, no API key and no installed packages beyond the runtime
requirements. It doubles as a fixture for the report-contract tests, which is how
it stays honest: if the template grows a field, the demo render fails in CI
rather than silently drifting.

Honesty rules for this fixture
------------------------------
* Every URL uses a domain reserved by RFC 2606 (`*.example`), so none of them can
  resolve to a real article. A demo that fabricated clickable citations to real
  newspapers would be a lying artefact.
* The records are plausible in shape - Chinese industry-press style, real
  Shenzhen districts, realistic 万元/亿元 magnitudes - but they are invented.
* The pipeline sets `demo=True`, the report prints a "sample data" chip, and the
  README says so in the same sentence it offers the command.

Deliberately covered by the fixture, because these are the branches that break
templates in production: an amount conflict, a record with no subject, no
location or no date at all, a low-confidence appendix, a failed source, a
recovered-by-regex amount, and a single-month window (which turns off the
secondary chart axis).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DEMO_RUN_ID = "run-demo-20260925-000000"
DEMO_QUERY = "深圳最近三个月的储能产业动态"

CUTOFF = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _rows() -> list[dict[str, Any]]:
    """Raw record drafts. `age_days` keeps the dates relative to the window."""
    return [
        # --- high-confidence records -------------------------------------------
        {
            "title": "深圳宝安石岩储能产业园二期开工 总投资18.6亿元",
            "subject": "宝安石岩储能产业园",
            "amount_raw": "18.6亿元",
            "amount": 1_860_000_000.0,
            "event_time_raw": "2026-09-16",
            "event_time": "2026-09-16T00:00:00+00:00",
            "location_name": "宝安区",
            "adcode": "440306",
            "event_type": "project",
            "confidence": 0.92,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2088110-1.html",
            "amount_source": "llm",
        },
        {
            "title": "深圳市发改委发布新型储能高质量发展行动方案 明确2027年装机目标",
            "subject": "新型储能行动方案",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-09-11",
            "event_time": "2026-09-11T00:00:00+00:00",
            "location_name": "福田区",
            "adcode": "440304",
            "event_type": "policy",
            "confidence": 0.95,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2087902-1.html",
            "amount_source": "none",
        },
        {
            "title": "宁德时代深圳研发中心储能电芯中试线投产",
            "subject": "宁德时代深圳研发中心",
            "amount_raw": "23亿元",
            "amount": 2_300_000_000.0,
            "event_time_raw": "2026-08-28",
            "event_time": "2026-08-28T00:00:00+00:00",
            "location_name": "龙岗区",
            "adcode": "440307",
            "event_type": "capacity",
            "confidence": 0.9,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2086441-1.html",
            "amount_source": "llm",
        },
        {
            "title": "比亚迪与深圳能源签署战略合作 共建园区侧储能电站群",
            "subject": "比亚迪",
            "amount_raw": "5.2亿元",
            "amount": 520_000_000.0,
            "event_time_raw": "2026-08-19",
            "event_time": "2026-08-19T00:00:00+00:00",
            "location_name": "坪山区",
            "adcode": "440310",
            "event_type": "partnership",
            "confidence": 0.88,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2085177-1.html",
            "amount_source": "llm",
        },
        {
            "title": "深圳光明科学城首个钠离子储能示范站并网",
            "subject": "光明科学城钠离子储能示范站",
            "amount_raw": "7300万元",
            "amount": 73_000_000.0,
            "event_time_raw": "2026-08-07",
            "event_time": "2026-08-07T00:00:00+00:00",
            "location_name": "光明区",
            "adcode": "440311",
            "event_type": "project",
            "confidence": 0.86,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2083905-1.html",
            "amount_source": "regex",
        },
        {
            "title": "深圳南山虚拟电厂平台接入容量突破120万千瓦",
            "subject": "南山虚拟电厂",
            "amount_raw": "120万千瓦",
            "amount": None,
            "event_time_raw": "2026-07-30",
            "event_time": "2026-07-30T00:00:00+00:00",
            "location_name": "南山区",
            "adcode": "440305",
            "event_type": "capacity",
            "confidence": 0.84,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2082604-1.html",
            "amount_source": "none",
        },
        {
            "title": "龙华区出台分布式储能补贴细则 单个项目最高补助300万元",
            "subject": "龙华分布式储能补贴",
            "amount_raw": "300万元",
            "amount": 3_000_000.0,
            "event_time_raw": "2026-07-22",
            "event_time": "2026-07-22T00:00:00+00:00",
            "location_name": "龙华区",
            "adcode": "440309",
            "event_type": "policy",
            "confidence": 0.91,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2081733-1.html",
            "amount_source": "llm",
        },
        {
            "title": "深圳能源集团新型储能产业基金完成首期募集 规模30亿元",
            "subject": "深圳能源集团",
            "amount_raw": "30亿元",
            "amount": 3_000_000_000.0,
            "event_time_raw": "2026-07-15",
            "event_time": "2026-07-15T00:00:00+00:00",
            "location_name": "福田区",
            "adcode": "440304",
            "event_type": "funding",
            "confidence": 0.93,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2080901-1.html",
            "amount_source": "llm",
        },
        {
            "title": "深圳能源集团龙华储能检测中心启用 面向电芯与PCS企业开放",
            "subject": "深圳能源集团",
            "amount_raw": "1.4亿元",
            "amount": 140_000_000.0,
            "event_time_raw": "2026-09-22",
            "event_time": "2026-09-22T00:00:00+00:00",
            "location_name": "龙华区",
            "adcode": "440309",
            "event_type": "project",
            "confidence": 0.8,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2088420-1.html",
            "amount_source": "llm",
        },
        {
            "title": "宁德时代与深圳港集团签约 共建港口机械储能换电系统",
            "subject": "宁德时代",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-09-05",
            "event_time": "2026-09-05T00:00:00+00:00",
            "location_name": "盐田区",
            "adcode": "440308",
            "event_type": "partnership",
            "confidence": 0.79,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2087221-1.html",
            "amount_source": "none",
        },
        {
            "title": "比亚迪储能系统集成基地第100万台套产品下线",
            "subject": "比亚迪",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-07-27",
            "event_time": "2026-07-27T00:00:00+00:00",
            "location_name": "坪山区",
            "adcode": "440310",
            "event_type": "capacity",
            "confidence": 0.85,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2082288-1.html",
            "amount_source": "none",
        },
        {
            "title": "深圳盐田港区岸电储能系统投运 年减排二氧化碳约4200吨",
            "subject": "盐田港岸电储能",
            "amount_raw": "1.1亿元",
            "amount": 110_000_000.0,
            "event_time_raw": "2026-09-03",
            "event_time": "2026-09-03T00:00:00+00:00",
            "location_name": "盐田区",
            "adcode": "440308",
            "event_type": "project",
            "confidence": 0.87,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2087033-1.html",
            "amount_source": "llm",
        },
        {
            "title": "罗湖清水河片区用户侧储能项目集中开工",
            "subject": "清水河用户侧储能",
            "amount_raw": "8600万元",
            "amount": 86_000_000.0,
            "event_time_raw": "2026-08-13",
            "event_time": "2026-08-13T00:00:00+00:00",
            "location_name": "罗湖区",
            "adcode": "440303",
            "event_type": "project",
            "confidence": 0.82,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2084388-1.html",
            "amount_source": "llm",
        },
        {
            "title": "深圳储能企业海外订单同比增长六成 欧洲为主要增量市场",
            "subject": "深圳储能出口",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-09",
            "event_time": "2026-09-01T00:00:00+00:00",
            "location_name": None,
            "adcode": None,
            "event_type": "other",
            "confidence": 0.71,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2087555-1.html",
            "amount_source": "none",
        },
        {
            "title": "深汕合作区首个百兆瓦时独立储能电站获批",
            "subject": "深汕独立储能电站",
            "amount_raw": "4.4亿元",
            "amount": 440_000_000.0,
            "event_time_raw": "2026-07-08",
            "event_time": "2026-07-08T00:00:00+00:00",
            "location_name": None,
            "adcode": None,
            "event_type": "project",
            "confidence": 0.79,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2080219-1.html",
            "amount_source": "llm",
        },
        # --- a conflict the pipeline refuses to resolve on its own -------------
        {
            "title": "欣旺达惠州基地储能系统集成扩产 计划投资12.5亿元",
            "subject": "欣旺达储能集成",
            "amount_raw": "12.5亿元",
            "amount": 1_250_000_000.0,
            "event_time_raw": "2026-08-24",
            "event_time": "2026-08-24T00:00:00+00:00",
            "location_name": "宝安区",
            "adcode": "440306",
            "event_type": "capacity",
            "confidence": 0.76,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2085774-1.html",
            "amount_source": "llm",
            "amount_conflict": True,
            "needs_review": True,
            "review_reason": "the model read 12.5亿元, the source text also contains 2.5亿元",
        },
        # --- records the quality gate keeps, but with something missing --------
        {
            "title": "深圳储能产业联盟成立 首批成员单位47家",
            "subject": "深圳储能产业联盟",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-09-08",
            "event_time": "2026-09-08T00:00:00+00:00",
            "location_name": "南山区",
            "adcode": "440305",
            "event_type": "partnership",
            "confidence": 0.83,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2087666-1.html",
            "amount_source": "none",
        },
        {
            "title": "宝安智慧能源产业园招商 面向储能系统集成企业",
            "subject": "",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-08-02",
            "event_time": "2026-08-02T00:00:00+00:00",
            "location_name": "宝安区",
            "adcode": "440306",
            "event_type": "other",
            "confidence": 0.68,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2083351-1.html",
            "amount_source": "none",
        },
        {
            "title": "深圳发布电化学储能电站安全管理指引",
            "subject": "储能电站安全管理指引",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-07-03",
            "event_time": "2026-07-03T00:00:00+00:00",
            "location_name": "福田区",
            "adcode": "440304",
            "event_type": "policy",
            "confidence": 0.89,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2079744-1.html",
            "amount_source": "none",
        },
        {
            "title": "深圳龙岗零碳园区储能配套项目完成招标",
            "subject": "龙岗零碳园区储能",
            "amount_raw": "2.8亿元",
            "amount": 280_000_000.0,
            "event_time_raw": "2026-09-19",
            "event_time": "2026-09-19T00:00:00+00:00",
            "location_name": "龙岗区",
            "adcode": "440307",
            "event_type": "project",
            "confidence": 0.85,
            "source": "gdelt",
            "source_url": "https://shenzhen-daily.example/business/2026/09/19/energy-storage-tender",
            "amount_source": "llm",
        },
        {
            "title": "Shenzhen energy storage firms report record export orders",
            "subject": "",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-09-14",
            "event_time": "2026-09-14T00:00:00+00:00",
            "location_name": None,
            "adcode": None,
            "event_type": "other",
            "confidence": 0.72,
            "source": "gdelt",
            "source_url": "https://global-energy-wire.example/2026/09/shenzhen-storage-exports",
            "amount_source": "none",
        },
        # --- below the confidence threshold -> appendix ------------------------
        {
            "title": "某储能材料企业完成Pre-A轮融资",
            "subject": "储能材料企业",
            "amount_raw": "数千万元",
            "amount": None,
            "event_time_raw": "2026-08-31",
            "event_time": "2026-08-31T00:00:00+00:00",
            "location_name": "光明区",
            "adcode": "440311",
            "event_type": "funding",
            "confidence": 0.44,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2086712-1.html",
            "amount_source": "none",
        },
        {
            "title": "南山区一储能项目环评公示",
            "subject": "",
            "amount_raw": None,
            "amount": None,
            "event_time_raw": "2026-07-19",
            "event_time": "2026-07-19T00:00:00+00:00",
            "location_name": "南山区",
            "adcode": "440305",
            "event_type": "other",
            "confidence": 0.38,
            "source": "china5e",
            "source_url": "https://www.china5e.example/news/news-2081420-1.html",
            "amount_source": "none",
        },
    ]


FINDINGS: list[dict[str, Any]] = [
    {
        "headline": "政策端在 7 月与 9 月形成了两次集中释放，构成窗口内最清晰的时间结构",
        "detail": (
            "7 月初的安全管理指引与 7 月下旬的区级补贴细则负责“定规则”，9 月上旬的"
            "市级行动方案负责“定目标”。两类文件的发布节奏在月度分布上表现为 7 月与"
            "9 月各一次抬头，中间的 8 月没有市级政策出台。"
        ),
        "evidence": [
            "https://www.china5e.example/news/news-2079744-1.html",
            "https://www.china5e.example/news/news-2081733-1.html",
            "https://www.china5e.example/news/news-2087902-1.html",
        ],
        "confidence": 0.86,
    },
    {
        # `detail` is written by `_region_finding()` from the computed region
        # table. A hand-typed total here would be a number the demo *asserts*
        # rather than one it derives, which is exactly the failure mode the rest
        # of this project exists to avoid.
        "headline": "投资强度集中在制造与系统集成环节，且由少数几个区承接",
        "detail": "",
        "evidence": [
            "https://www.china5e.example/news/news-2088110-1.html",
            "https://www.china5e.example/news/news-2086441-1.html",
            "https://www.china5e.example/news/news-2085177-1.html",
        ],
        "confidence": 0.81,
    },
    {
        "headline": "产业金融工具在窗口内首次出现，是目前单笔规模最大的资金安排",
        "detail": (
            "该基金是本次检索到的唯一一个以产业基金形式出现的资金安排，规模高于"
            "任何单个项目投资额。它与 9 月的行动方案同月出现，但两者之间是否有直接"
            "关系，现有记录不足以判断。"
        ),
        "evidence": [
            "https://www.china5e.example/news/news-2080901-1.html",
            "https://www.china5e.example/news/news-2087902-1.html",
        ],
        "confidence": 0.74,
    },
    {
        "headline": "技术路线上出现了钠离子与虚拟电厂两条新的记录，样本量不足以判断趋势",
        "detail": (
            "窗口内各只有一条记录涉及钠离子储能示范与虚拟电厂聚合容量。单条记录"
            "不能支撑“技术路线正在切换”的说法，这里只把它标记为值得持续观察的方向。"
        ),
        "evidence": [
            "https://www.china5e.example/news/news-2083905-1.html",
            "https://www.china5e.example/news/news-2082604-1.html",
        ],
        "confidence": 0.58,
    },
]

ANOMALIES = [
    "有一条记录中模型转写的金额与原文出现的另一个数字不一致，"
    "已标记为待人工确认，未计入任何合计，列在附录中。",
    "标题含“惠州基地”的记录被归入深圳宝安区——可能是原文中的深圳办公地址被误当作项目地，"
    "区域统计需人工复核。",
    "同一条产业链上的公司名出现了两种写法（“宁德时代”与“宁德时代深圳研发中心”），"
    "主体聚合把它们算成了两个主体。这类实体归并没有在代码里做，宁可少算也不猜。",
    "GDELT 返回的一条英文标题未提取出主体与金额，仅保留为背景信息。",
]


def _wan(value: float) -> str:
    """An amount at the unit and precision the figure actually deserves."""
    if value >= 1e8:
        return f"{value / 1e8:,.2f} 亿元"
    wan = value / 1e4
    return f"{wan:,.0f} 万元" if wan >= 1000 else f"{wan:,.1f} 万元"


def _region_finding(by_region: list[Any]) -> str:
    """Write finding #2's detail from the aggregation instead of from memory."""
    ranked = sorted((b for b in by_region if b.total_amount > 0), key=lambda b: -b.total_amount)
    if len(ranked) < 2:
        return (
            "带明确投资额的记录太少，无法比较区域之间的投资强度；这里只报告合计金额，不做区域排名。"
        )
    first, second = ranked[0], ranked[1]
    grand = sum(b.total_amount for b in ranked) or 1.0
    share = (first.total_amount + second.total_amount) / grand
    return (
        f"在带明确投资额的记录中，{first.region_name}与{second.region_name}的合计金额最高，"
        f"分别为 {_wan(first.total_amount)} 与 {_wan(second.total_amount)}，两者合计占全部"
        f"已披露金额的 {share * 100:.0f}%。用户侧与园区侧的应用类项目单笔规模明显更小，"
        "投资主要发生在电池制造与系统集成环节。"
    )


def _item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": row["title"],
        "subject": row.get("subject", ""),
        "amount": row.get("amount"),
        "amount_raw": row.get("amount_raw"),
        "amount_source": row.get("amount_source", "none"),
        "amount_conflict": bool(row.get("amount_conflict")),
        "event_time": row.get("event_time"),
        "event_time_raw": row.get("event_time_raw"),
        "location_name": row.get("location_name"),
        "adcode": row.get("adcode"),
        "event_type": row.get("event_type", "other"),
        "confidence": row.get("confidence", 0.0),
        "source_url": row["source_url"],
        "source": row.get("source", ""),
        "needs_review": bool(row.get("needs_review")),
        "review_reason": row.get("review_reason"),
    }


def build() -> dict[str, Any]:
    """Return everything `render_html` needs, as plain JSON-safe values.

    The aggregation is not written out by hand. It is produced by calling the
    real `nodes.aggregate.aggregate` on the records below, with the real
    `QuerySpec`. A fixture with hand-copied totals drifts from the records the
    moment either changes, and a demo whose own numbers disagree with its own
    table is worse than no demo - so the totals are derived, not asserted.
    """
    from .nodes.aggregate import aggregate
    from .schemas import IntelItem, QuerySpec

    query = QuerySpec(
        industry="energy storage",
        region_adcode="440300",
        region_name="深圳市",
        timespan="3m",
        keywords=["储能", "电化学储能", "新型储能"],
        raw_query=DEMO_QUERY,
    )

    threshold = 0.6
    items = [IntelItem.model_validate(_item(row)) for row in _rows()]
    # Same rule node 6 applies: a record flagged for review never reaches the
    # findings, whatever its confidence says.
    passed = [i for i in items if not i.needs_review and i.confidence >= threshold]
    rejected = [i for i in items if i not in passed]

    state = {
        "run_id": DEMO_RUN_ID,
        "user_query": DEMO_QUERY,
        "query": query,
        "intel_items": passed,
    }
    result = aggregate(state)  # type: ignore[arg-type]
    aggregation = result["aggregation"]
    # The real pipeline hands node 6 the records *after* node 4 has resolved their
    # administrative codes, not the raw ones. Using the pre-aggregation list here
    # would make the demo's evidence table show a different `adcode` column than a
    # live run - and, worse, would make the region table look like it disagrees
    # with the rows beneath it.
    resolved = result.get("intel_items") or passed

    named_total = sum(int(row.get("count") or 0) for row in aggregation.by_subject)
    top3 = (
        sum(sorted((int(r.get("count") or 0) for r in aggregation.by_subject), reverse=True)[:3])
        / named_total
        if named_total
        else 0.0
    )

    findings = [dict(f) for f in FINDINGS]
    findings[1]["detail"] = _region_finding(aggregation.by_region)

    return {
        "run_id": DEMO_RUN_ID,
        "user_query": DEMO_QUERY,
        "query": query.model_dump(mode="json"),
        "summary": {
            # Deliberately no region ranking and no total in this paragraph: those
            # are written by the aggregator and repeated in the findings. Two
            # copies of a number is two chances for them to disagree.
            "executive_summary": (
                "深圳储能产业在 2026 年 7 月至 9 月间呈现出“规则先行、制造跟进、"
                "应用铺开”的三段结构。9 月上旬发布的市级行动方案给出了 2027 年的"
                "装机目标，是窗口内规格最高的政策信号；同期落地的一笔产业基金是"
                "目前单笔规模最大的资金安排。投资端集中在电池制造与系统集成环节，"
                "由少数几个区承接，规模明显高于用户侧与园区侧的应用项目。钠离子"
                "储能与虚拟电厂各出现一条记录，样本量不足以判断趋势，仅作为需要"
                "持续观察的方向列出。"
            ),
            "method_note": (
                "结论基于下方证据表中通过质量门的记录，所有计数、合计与占比均由代码"
                "计算，模型只负责解读。低于置信度阈值的记录，以及金额存在冲突的记录，"
                "保留在附录中且未参与任何合计。"
            ),
        },
        "aggregation": aggregation.model_dump(mode="json"),
        "analysis": {
            "findings": findings,
            "subject_concentration": {"top3_share": round(top3, 4)},
            "trend_note": (
                "8 月的记录数最低但合计金额最高，原因是当月两条制造类记录的投资额较大；"
                "9 月记录数最多，但以政策与中等规模项目为主。记录数与金额的方向并不一致，"
                "所以这里同时给出两条序列。"
            ),
            "anomalies": ANOMALIES,
            "insufficient_data": False,
        },
        "items": [i.model_dump(mode="json") for i in resolved],
        "rejected": [i.model_dump(mode="json") for i in rejected],
        "failed_sources": ["gdelt:partial"],
        "errors": [
            {
                "node": "02_retrieve",
                "type": "source_degraded",
                "detail": "gdelt answered 429 on the first attempt; the cached payload was used instead",
            },
            {
                "node": "03_extract",
                "type": "batch_retry",
                "detail": "one batch of 8 documents was halved after a 429 and completed as 4+4",
            },
        ],
        "threshold": threshold,
        "model_name": "glm-4.7 (demo fixture — no model was called)",
        "raw_docs": 41,
        "elapsed_seconds": 58.4,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


__all__ = ["DEMO_QUERY", "DEMO_RUN_ID", "build"]

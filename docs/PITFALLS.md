# Pitfalls Log · PITFALLS

> Part of the English deliverable set — this is the file the README points at as
> "verified pitfalls, each with its reproduction command".
> Each entry is marked **[Measured]** or **[Inferred]**, and comes with reproducible verification commands.
> Measurements were taken on 2026-09-18 and 2026-09-25, on a MacBook Air (Apple Silicon),
> with a direct connection (no proxy enabled) unless an entry says otherwise.
> Entries 31–35 were added on 2026-09-25, when a full end-to-end run was re-measured.
> A few numbers quoted in earlier entries were later re-measured over a larger sample; where
> that happened the entry says so rather than the correction living only in the log.

---

## Pitfall 1 ⭐ GDELT's `timespan=3months` silently returns an empty object

**Severity: critical.** This pit does not raise an error; it only lets the entire pipeline "legally" obtain 0 records.

**[Measured] Symptom**

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?query=energy&mode=artlist&format=json&maxrecords=5&timespan=3months"
# HTTP 200, body is {}  —— an EMPTY OBJECT: not an empty array, and not an error
```

At the same moment, dropping `timespan`:

```bash
curl -s "https://api.gdeltproject.org/api/v2/doc/doc?query=energy&mode=artlist&format=json&maxrecords=5"
# HTTP 200, {"articles": [ ... 5 items ... ]}
```

**Root cause**: GDELT's DOC API does not accept the `3months` form for `timespan` (it only understands `\d+(min|h|d|w|m)`). When the parameter is invalid it **does not return 400; it returns an empty result set**.

**Fix**: This project's `GdeltAdapter.fetch()` **never uses the `timespan` parameter**; instead it fetches and then filters locally by `seendate` (`parsing.cutoff_from_timespan()`). This also eliminates both parameter-semantics guessing and one rate-limit risk.

**Code guard**: `GdeltAdapter.fetch()` treats "HTTP 200 but `articles` is empty" as a **failure**, falling back to the on-disk cache rather than treating it as "there really is no news".

**Regression test**: `tests/test_parsing.py::TestTimespan::test_invalid_word_form_is_rejected` asserts `timespan_to_days("3months") is None`.

---

## Pitfall 2 ⭐⭐ GDELT rate limiting is far stricter than the docs claim: measured 20–40 s before a single success

**Severity: high.** Directly threatens the acceptance criterion "end-to-end report in 60 s".

**[Measured] Data**

| Request interval | Result |
|---|---|
| 6 s (the "once every 5 s" the official docs claim) | continuous 429 |
| 18 s | continuous 429 |
| 22 s | continuous 429 |
| 25 s × 8 attempts | 5th attempt succeeded (cumulative ≈ **2 min 9 s**) |
| retry after 30 s | 2nd attempt succeeded (cumulative ≈ 55 s) |

The 429 response body is fixed:
```
Please limit requests to one every 5 seconds or contact kalev.leetaru5@gmail.com for larger queries.
```

**[Inferred]** This egress IP sits in some shared rate-limit pool (office / ISP shared egress), so the limiting is random, not a deterministic function of time — the same interval sometimes succeeds and sometimes fails.

**Fix (four layers)**
1. `tools/http.py::HOST_MIN_INTERVAL` sets a dedicated 20 s minimum interval for `api.gdeltproject.org` (0 s for other sources).
2. The GDELT adapter sends **only 1 request per run** (`retries=0`) — never retries until dawn.
3. **On-disk cache with 30-minute TTL** (`data/cache/gdelt/<sha1>.json`): when recording the demo video, the second query hits the cache directly, 0 wait.
4. **Degrade to an optional source**: when GDELT yields no data, the report is still generated from china5e's data, and the fact is written in the report's "Coverage & failures" section.

**Design implication (the point to make in the interview)**: source priority must be ranked by **measured availability**, not by "looks easy to use in the docs". So china5e is the primary source and GDELT is the breadth-supplement source.

---

## Pitfall 3 ⭐ GDELT `artlist` mode returns no body and no summary

**[Measured] Exactly 8 fields returned**

```json
{
  "url": "https://baijiahao.baidu.com/s?id=1874084929546832257",
  "url_mobile": "",
  "title": "2026年APEC能源智库论坛在大连举行",
  "seendate": "20260820T233000Z",
  "socialimage": "",
  "domain": "baijiahao.baidu.com",
  "language": "Chinese",
  "sourcecountry": "China"
}
```

No `body`, no `summary`, no `content`.

**Impact**: amount, subject, and location can only be extracted from the **headline**. Chinese headlines have decent information density (`总投资3.3亿元!墨玉储能项目即将投入商业运营` — "RMB 330M total investment: the Moyu energy-storage project is about to enter commercial operation"), but English headlines often yield no amount.

**Fix**: The extraction prompt (`03_extract.md`) explicitly tells the model `body: (headline only - no body available)`, and requires that when the headline lacks enough information, `confidence` be discounted or the record **simply not produced** (better fewer than fake).

---

## Pitfall 4 ⭐ `www.ccgp.gov.cn` is DNS-hijacked, not anti-scraping

**[Measured]**

```bash
nslookup www.ccgp.gov.cn
# www.ccgp.gov.cn  canonical name = www.ccgp.gov.cn.wscvip.cn
# Address: 118.252.10.37        ← NOT the government-procurement site's IP
```

The request returns HTTP 200, but the page title is `频繁访问!中国政府采购网` (a 2.9 KB interception page).

**Misdiagnosis risk**: it's easy to assume "anti-scraping" and then spend an hour forging Cookie/Headers — **the direction is completely wrong**; the problem is in DNS.

**Fix**: Drop this source outright. In the spec, the "bidding/tendering" path weighted under S3 is switched wholesale to other statically reachable industry sites.

**[Inferred]** The `.wscvip.cn` suffix is a hallmark of ISP / security-appliance domain-correction hijacking.

---

## Pitfall 5 ⭐ Two source domains no longer exist

**[Measured]**

| Domain | Result |
|---|---|
| `deal.ggzy.gov.cn` (National Public Resource Trading Platform) | **NXDOMAIN**, domain decommissioned |
| `zfcg.szggzy.com` (Shenzhen procurement subdomain) | connection failed |

**Lesson**: The source list written in the spec was **written from someone's memory**; building an adapter against it without real probing wastes a lot of time. This is exactly why section 13, item 2 of the spec says "do not assume any public interface from memory".

---

## Pitfall 6 ⭐⭐ The SPA trap: HTTP 200 but the page is an empty shell (easiest to misread as "scrapable")

**[Measured]**

| URL | HTTP | Bytes | Truth |
|---|---|---|---|
| `https://www.sz.gov.cn/cn/xxgk/zfxxgj/zcfg/index.html` | 200 | 55 892 | list rendered by **JS dynamically**; the `<a>` tags in the static HTML are all JS template strings (`' + result[i].url + '`) |
| `https://gdgpo.czt.gd.gov.cn/` (Guangdong provincial procurement) | 200 | **2 104** | SPA empty shell |
| `https://news.cnstock.com/news/sns_yw/index.html` | 200 | **1 292** | SPA empty shell |
| `https://news.bjx.com.cn/` (BJX) | 200 | 15 999 | has content but the `<a>` regex matched **0** usable headlines |

**Quick heuristics (recommend writing into any crawler project)**:
1. **Bytes < 2048 → immediately judge it an SPA empty shell**; don't waste effort writing a parser.
2. Large byte count still doesn't mean scrapable: if the captured anchors contain JS-template fragments like `' + `, `result[i]`, `${`, the list is front-end assembled.
3. The only reliable criterion is: **can you extract ≥5 "headline + link" pairs of meaningful text**.

**Fix**: `scripts/check_sources.py` implements all three heuristics — judges a source alive/dead within 15 s.

---

## Pitfall 7 ⭐ Baidu family (Baijiahao) body fetching is blocked by a security verification

**[Measured]**

```bash
curl -o /tmp/a.html "https://baijiahao.baidu.com/s?id=1874084929546832257"
# HTTP 200, 1488 bytes; the body is the Baidu bot-check page: "百度安全验证 网络不给力，请稍后重试"
```

**Impact**: Among the Chinese articles returned by GDELT, **Baijiahao makes up a high proportion**. This means the completion path "use GDELT to get a URL → then fetch the body" is essentially dead for Chinese sources.

**Fix**: Body fetching is always made **best-effort** (`RawDoc.has_body` check); on failure it falls back to headline extraction and never lets a fetch failure interrupt the pipeline.

---

## Pitfall 8 ⭐ List pages must anchor the `li` structure, not just match a generic `<a>`

**Why**: China5e's homepage, using a generic `<a>` regex, pulls a large number of navigation links ("About us", "Submit", etc.) — extremely low signal-to-noise; whereas the column pages' list items have a fixed structure.

**[Measured] Real column-page structure**

```html
<li class="singleline"><span>2026-09-18</span><a href="https://www.china5e.com/news/news-1209445-1.html" title="储能项目数量首超光伏 中国绿色能源出海迈入“系统输出”新阶段" target="_blank">储能项目数量首超光伏…</a></li>
```

**Fix**: Anchor on `li.singleline` plus the sibling `<span>` date:

```python
LIST_ITEM_RE = re.compile(
    r'<li class="singleline">\s*<span>([\d\-/]+)</span>\s*'
    r'<a\s+href="([^"]+)"\s+title="([^"]*)"', re.S)
```

**Extra finding**: The column page has pagination `/new-energy/stored-energy/index_2.html`; measured **427 pages** total, 40 items per page — plenty of data.

---

## Pitfall 9 ⭐⭐ The page hides the word "来源" (source) inside an HTML comment (double pit)

**[Measured] Original HTML**

```html
<div class="showtitinfo">2026-08-25 16:07:37
  <!--来源-->
              远光软件
  <!-- &#12288; 浏览次数：<span id="hits"></span>--></div>
```

**Pit 9a**: The regex `来源[：:]\s*(\S+)` fails to match — because "来源" is **in a comment**, and the actual byline `远光软件` has **no tag in front of it**.

**Pit 9b (more subtle)**: The comment body contains a `<span>`. The tag-stripping `<[^>]+>` stops early at the `>` of `<span id="hits">`, so `-->` is left dangling in the extracted result:
```
date_text: '2026-08-25 16:07:37\n\n远光软件\n\n-->'   ← the trailing -->
```

**Fix (two parts, both required)**
1. **Strip comments before stripping tags** (order cannot be reversed):
   ```python
   COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
   text = SCRIPT_STYLE_RE.sub(" ", html)
   text = COMMENT_RE.sub(" ", text)      # ← must run BEFORE TAG_RE
   text = TAG_RE.sub("", text)
   ```
2. Switch the source field to a **fallback strategy**: first try `来源[：:]`; if no match, **drop the timestamp and take the first non-empty line of text**, stripping decorative characters like `·|—-`.

**Regression test**: Besides `tests/test_pipipeline_smoke.py`, `tests/test_parsing.py::TestChina5eArticleFixture::test_publish_time_and_source` specifically guards this pit — **this test was originally red**, and only turned green after the fix.

---

## Pitfall 10 A proxy is not required (almost made a wrong assumption)

The user's machine has Clash Verge resident (port 7897), making it easy to assume up front that "a proxy is mandatory".

**[Measured]**

```bash
curl -x http://127.0.0.1:7897 ...   # curl: (7) Failed to connect to 127.0.0.1 port 7897
curl    https://api.gdeltproject.org/...   # reachable (returns 429 — app-level throttling ≠ network down)
curl    https://geo.datav.aliyun.com/...   # 200，69 077 bytes
```

**Conclusion**: Clash was not running at the time, and both GDELT and DataV were **directly reachable**. This project **does not need a proxy**.

**[Inferred]** If we copied the "must enable a proxy" assumption, the Docker setup would carry an extra set of proxy env vars, which instead introduces 502 / connection-refused failures (this pattern has already been hit once in the user's existing Docker config).

---

## Pitfall 11 ⭐ LangGraph state is "overwrite", not "append" — you must declare a reducer explicitly

**Symptom**: When a node returns `{"errors": [...]}`, the errors accumulated by previous nodes are all lost.

**Root cause**: In a `TypedDict`, the update semantics of a normal field are **overwrite**.

**Fix**:

```python
class GraphState(TypedDict, total=False):
    failed_sources: Annotated[list[str], operator.add]
    errors:         Annotated[list[dict[str, Any]], operator.add]
    trace:          Annotated[list[dict[str, Any]], operator.add]
    node_meta:      Annotated[dict[str, dict], _merge_node_meta]   # custom merge function
```

`node_meta` cannot use `operator.add` (dict + dict raises TypeError), so a custom merge function is required.

**Knock-on effect**: When writing the snapshot you can no longer simply do `{**state, **update}` — otherwise the snapshot would not show the accumulated results. So `graph.py::_apply_update()` manually simulates the reducer semantics, guaranteeing **the on-disk snapshot stays consistent with the runtime state**.

---

## Pitfall 12 ⭐ A LangGraph node cannot return an undefined key

**Symptom**: Initially wanted to use `"_trace_extra": usage` to pass token usage from a node to the orchestration layer; LangGraph throws directly (Invalid update: key not found).

**Fix**: Formally define the `node_meta` field in `GraphState` with a reducer; the node returns `{"node_meta": {"03_extract": {...}}}`.

**Side effect (good)**: Token usage thereby **enters the state snapshot**, and you can audit directly in `snapshots/*.json` how many tokens each node burned — exactly the effect the "tracing" requirement wanted.

---

## Pitfall 13 ⭐ A LangGraph node cannot have both a normal edge and a conditional edge

**Symptom**: `parse_intent` simultaneously does `add_edge("parse_intent", "retrieve")` and `add_conditional_edges("parse_intent", ...)`, failing at compile time.

**Fix**: Keep only the conditional edge; connect the remaining nodes in a straight line with normal edges:

```python
graph.add_conditional_edges("parse_intent", router, {"end": END, "continue": "retrieve"})
for i in range(1, len(NODES) - 1):
    graph.add_edge(NODES[i][0], NODES[i + 1][0])
```

---

## Pitfall 14 Amount parsing: `50兆瓦` is not 50 million

**Risk**: If the unit table treats a bare number as an amount, it would miscompute installed capacity as money.

**[Measured]** `parse_amount("装机50兆瓦")` must return `(None, None)`.

**Fix**: The regex forcibly requires that **a number be followed by a currency unit** (`亿元|亿|万元|万|million|billion|yuan`); bare numbers do not match. When multiple candidates exist, take the **largest numeric value** (usually the total investment rather than a phase-one investment).

**Regression test**: `TestParseAmount::test_bare_number_is_not_an_amount`.

---

## Pitfall 15 ⭐⭐⭐ Zhipu API key format trap: a bare 32-char hex is a "key id", not a key

**Symptom**: Every request returns

```json
{"error":{"code":"401","message":"令牌已过期或验证不正确"}}
```

This message steers you toward "the key expired, go renew it" — **completely wrong direction**. The real cause is usually an incomplete copy.

**[Measured 2026-09-18]** Using `0123456789abcdef0123456789abcdef` (32-char pure hex) against 4 combinations, all 401:

| Combination | Result |
|---|---|
| `Authorization: Bearer <key>` → open.bigmodel.cn | 401 令牌已过期或验证不正确 |
| `Authorization: <key>` (no Bearer) | 401 same as above |
| `x-api-key: <key>` | 401 same as above |
| `https://api.z.ai/api/paas/v4` (international site) | 401 token expired or incorrect |

**Key clue: there is no dot (`.`) in this string.**

**[Verified official format]** A Zhipu key is `[a-f0-9]{32}\.[A-Za-z0-9]{16}` — the first 32-char hex is the **key id**, the trailing 16 chars are the secret, with a single dot between. The credential we were handed consisted of **exactly those first 32 chars**, with the entire trailing segment missing — that is the cause of the 401.

**Fix**: Local validation only blocks **this one confirmed** shape (bare 32-char hex). **Deliberately does not block other shapes**: search results disagree on the key's shape (some say a `sk-` prefix), and writing an uncertain guess as a hard check would false-positive on legitimate credentials — which is worse than no check. All other shapes are passed through for the server to adjudicate.

**Fix**: `llm_keys.py` performs the shape check **before sending the request** (zero-dependency, usable by `preflight` too), with the error message pointing straight to the console page. 401/403 are classified as `LLMAuthError` and **never retried** — the original implementation retried 3 times on bad credentials, wasting 3 extra round-trips and hiding the real problem.

**Regression test**: `TestApiKeyShape` (4 cases).

---

## Pitfall 16 ⭐⭐⭐ Model name is an external contract: **guessed wrong twice in a row**, finally settled via the `/models` endpoint

**Symptom**: Calling a **non-existent model name**, Zhipu returns

```
HTTP 429  {"code": "1305", "message": "该模型当前访问量过大，请您稍后再试"}
```

**This error message is extremely misleading.** It reads like rate limiting, so you waste a lot of time on "add backoff, lower concurrency, swap the key, wait a while and retry" — when the real cause is that **the model name simply does not exist**.

**[Measured 2026-09-18 · real call to `GET /api/paas/v4/models`]** This key can access **only 11 models**:

```
glm-4.5  glm-4.5-air  glm-4.6  glm-4.7  glm-5  glm-5-turbo
glm-5.1  glm-5.2  glm-5.3  glm-5.3-flash  glm-5.3-flashx
```

**This project got it wrong twice here**, worth a separate note:

| Name used | Measured result | How it was decided at the time |
|---|---|---|
| `glm-4-flash` | 429 / 1305 | first version written **from memory** |
| `glm-4.7-flash` | 429 / 1305 | second version changed **from web search** — search results claimed emphatically it was "the current free mainstay", but **measured, it also does not exist** |
| `glm-4.7` | ✅ 200, 1.5s | **read out** from the `/models` endpoint |

**Lesson (the real value of this pit)**: Model name, availability, and billing tier are all **external contracts that change**. "I remember it's called…" and "the materials I found say…" **are not evidence** — both times we fell here. The only reliable source is the server itself — `/models` tells you the answer directly.

**Fix**:
1. Change the default model to the measured-confirmed `glm-4.7`;
2. `preflight.py` adds a **`GLM model catalogue`** check: pull `/models` once, compare `GLM_MODEL` against the real list **character by character**, and on mismatch immediately FAIL and list all available options;
3. `check_sources.py`'s GLM probe likewise prints the available model list first;
4. The **error text** for 429 explicitly notes "read the message first: 1305 is not rate limiting, it's a non-existent model name".

---

## Pitfall 17 ⭐⭐⭐ ollama's reasoning model returns an **empty string** (the most hidden one)

**Symptom**: The `qwen3.5:4b` call returns HTTP 200, but `message.content` is `""`; the upper layer sees "empty response".

**[Measured 2026-09-18]**

```
# think not set, num_predict=500
elapsed 19.7s   eval_count=500   content length = 0      ← all 500 tokens burned in the hidden reasoning channel
# with think=false
elapsed 1.8s    eval_count=57    content length = 180    ← correct JSON emitted
```

`qwen3.5:4b` is a hybrid reasoning model; **reasoning content goes through a separate `message.thinking` channel**; if you don't turn it off, the `num_predict` budget is exhausted in the reasoning channel and `content` is forever empty.

And **the same request, with thinking off, is 11× faster** (19.7s → 1.8s), cost 500 tokens → 57 tokens.

**Fix**: The ollama request body always carries `"think": false`. At the same time, `OllamaClient` explicitly checks for empty `content` and raises an error carrying `done_reason` / `eval_count` — so "the model said nothing" and "the connection dropped" can be distinguished.

---

## Pitfall 18 ⭐ Zhipu's free tier is throttled per ACCOUNT — and the first measurement of it was wrong twice over

The docs won't put this sentence in a prominent place, but exceeding it gives 429.

**Fix**: Add `GLM_MAX_CONCURRENCY` (default 1) and `GLM_MIN_INTERVAL_SECONDS` (default 1.0); `LLMRateLimit` and ordinary network errors get **separate backoff** (rate limit 4/8/16/30 s cap, ordinary errors 2/4/8 s), and `Retry-After` is read. **Do not use the same retry cadence for "rate limit" and "timeout"** — rate limits recover on a second scale, socket errors on a millisecond scale.

> **Correction (2026-09-25).** Two things were wrong with the paragraph above, and both were
> discovered only when a real run was actually measured:
>
> 1. **The settings did nothing.** `GLM_MAX_CONCURRENCY` and `GLM_MIN_INTERVAL_SECONDS` were
>    declared, documented and written into `.env.example` — and never read by any code. Node 3
>    ran four concurrent GLM requests with no pacing. See Pitfall 32.
> 2. **"1 concurrency" is not the actual limit, and the problem is not the spacing.** A ten-call
>    sweep on 2026-09-25 put the ceiling at **2 concurrent requests**: three in flight completed
>    only 4–5 of 10 calls at 0.0 s, 0.5 s and 1.0 s spacing alike, while two in flight completed
>    10/10 (and 12/12 with no spacing at all). The default is now 2. The 0.5 s interval is kept
>    as insurance, not as a knob. See Pitfall 32 for the full table.
>
> The lesson is not "the number was off by one". It is that **a configuration value nobody reads
> is not a mitigation, it is documentation of an intention** — and it will be reported to the
> reader as if the problem were already handled.

---

## Pitfall 19 ⭐ Floating point miscomputes amounts: `8.2 × 1e8 = 819999999.9999999`

```python
>>> 8.2 * 1e8
819999999.9999999      # NOT 820000000.0
```

Normally it doesn't matter, but this is **money**: it makes both "summing totals" and "equality checks on amounts" unstable in both directions, and the report might show `¥819,999,999`.

**Fix**: `parse_amount` returns `round(value, 2)`.

**Regression test**: `TestReconcileAmount::test_unparseable_model_amount_falls_through_to_regex`.

---

## Pitfall 20 ⭐ List pages mix in ads; small models turn ads into "intelligence"

**Symptom**: The 4th item in china5e's storage column is

```
全行业超细粉体加工｜气流粉碎机整线解决方案厂家推荐，从试样、定制到出口项目综合测评
```

**Measured**: 1 of 30 items per page is this kind of promotional line. It causes two losses — a wasted body fetch and a wasted LLM call, and the 4B model really will write it up as a record.

**Fix**: `parsing.is_noise_title()` drops it at the **list stage**:
- **Keyword rule** (validated against real data): `厂家推荐 / 厂家直销 / 诚招 / 招商加盟 / 专题研讨 / 培训班 / 招聘 / 征稿`
- **Structural rule** (conservative guard, not validated against real data, hence tightened): a title containing **more than 2** fullwidth/halfwidth vertical bars → judged a marketing list, not a news headline

**Note**: The structural rule deliberately only blocks "≥2 bars". Chinese news headlines also commonly use a single vertical bar as a separator; pushing down to 1 bar would false-kill real news. **The value of this rule is as a fallback, not as the main force.**

**Regression test**: `TestNoiseFilter` (5 cases).

---

## Pitfall 21 ⭐⭐⭐ **I misjudged the model**: I used a "correct answer" fabricated from memory to accuse the model of fabricating

This is the most worthwhile pit of all, because it's not a technical fault but a **validation-methodology fault**.

### The first round's misjudgment

Fed a real article to the model, which output `location_name = 山东省` (Shandong Province). I **assumed from impression** the correct location was elsewhere, and noted:

| Article | Location the model gave | The "correct" location I asserted |
|---|---|---|
| 全球首个熔盐耦合二氧化碳发电项目在华能开工 | `山东省` | `甘肃省酒泉市` (Jiuquan City, Gansu Province) ← **I fabricated this** |

**This wrong conclusion went into the docs, into the project memory, and was used as the argument that "we must use code to catch the model's hallucinations".**

### 2026-09-18 re-check: grab the original and verify character by character

```
正文：9月16日，全球首个熔盐耦合超临界二氧化碳循环发电商业示范项目"睿碳号"
      在华能山东八角电厂开工建设……该项目由华能山东分公司投资建设

「甘肃」出现 0 次      「酒泉」出现 0 次
「山东」出现 3 次      「华能」出现 8 次
```

**The model was right. I was wrong.** I mistook the fact "molten-salt heat storage is in Jiuquan, Gansu" — which is real but belongs to **a different report** — for the content of this article.

### Worse: this error also polluted the prompt

That fabricated "reference answer" was written by me into the **worked example** of `prompts/03_extract.md`:

```json
// the OLD example — every field was fabricated
{"doc_id":7, "title":"全球首个熔盐耦合二氧化碳发电项目在华能开工",
 "amount_raw":"12.5亿元", "event_time":"2026-08-18", "location_name":"甘肃省酒泉市",
 "subject":"中国华能集团"}
```

Verified character by character against the real body: location wrong (real is **山东省** / Shandong), date wrong (real **9月16日** / Sept 16), amount wrong (**the body has no amount at all**), subject also wrong (it is **华能山东分公司** / Huaneng Shandong Branch, not "中国华能集团" / China Huaneng Group).

Few-shot examples are the basis for the model's calibration behavior. **Using fabricated data as examples equals actively feeding the model wrong knowledge.**

### Fix

1. All three worked examples were **replaced with real documents + character-verified outputs**, and the source URL and verification date are noted at the top of each example;
2. Added `scripts/rehearse_glm.py --urls <URL...>`: **precisely replay** the documents in the examples.
   A column list page's ordering changes on every request; only a fixed URL can reproduce;
3. The examples cover three real cases: **has amount** (the Moyu project), **no amount** (the molten-salt project), **ad → 0 records** (the air-jet mill).

### Lesson

> **The "correct answer" must also come from reproducible evidence, not from memory.**

Judging the model with an unverified reference answer reverses the entire conclusion — and this error is especially hidden because it happens precisely when you "look like you're doing verification seriously".

**Ancillary observation (real, but distinguish the subject)**: `ollama qwen3.5:4b` on the same batch of tasks did show `subject` fields left largely blank, with noticeably lower field completeness than GLM — this is a real gap, and the basis for "primary use GLM, ollama only as fallback". But this is **two independent matters** from the location misjudgment above; do not conflate them.

**Anti-hallucination measures retained by the project** (these are unrelated to actual output; they are architectural):
1. Amount goes through `reconcile_amount()` code-level cross-validation (see Pitfall 19)
2. Geographic fields only do text normalization; if it can't map to `REGION_TABLE`, leave it empty — no administrative-level inference
3. `subject` empty → `needs_review=True` into a manual queue, never back-filled
4. Citation links are back-filled by code from its own document table; the model may only output `doc_id` — structurally eliminating hallucinated links

**Regression test**: `python3 scripts/rehearse_glm.py --urls <three real URLs>`, output should match the prompt examples character by character.

---

## Pitfall 22 ⭐⭐⭐ GLM's reasoning channel: empty `content` string + a **parameter-name trap**

### Symptom A: `content` is empty, but HTTP 200

```
glm-4.7  max_tokens=20  →  content=''   reasoning_tokens=29   ← all 20 tokens burned on thinking
glm-4.7  max_tokens=50  + thinking off →  content='收到'   total_tokens=10
```

GLM's reasoning model separates **reasoning content and the formal answer**:

| Field | Content |
|---|---|
| `choices[0].message.content` | the formal answer (what you actually want) |
| `choices[0].message.reasoning_content` | the reasoning process |
| `usage.completion_tokens_details.reasoning_tokens` | tokens burned on reasoning |

Give `max_tokens` too small and reasoning eats the budget, so `content` is an empty string. **The failure shape is identical to ollama in Pitfall 17.**

### Symptom B: The error text points you to the **wrong parameter name**

```
request thinking: {"type": "disabled"}
→ 400  code 1210  "该模型始终思考，不支持关闭思考；请使用 low、high 或 max。"

request thinking: {"type": "low"}          ← changed to follow that message
→ 400  code 1210  "该模型始终思考……"    ← the same error
```

The "low / high / max" in that message **does not refer to values of `thinking.type`**, but to **another parameter**, `reasoning_effort`:

| Request | Result |
|---|---|
| `thinking: {"type": "disabled"}` | ❌ 400 / 1210 |
| `thinking: {"type": "low"}` | ❌ 400 / 1210 |
| `reasoning_effort: "low"` | ✅ 200 |
| not sent | ✅ 200, but reasoning_tokens 97~135 |

### The single most critical measured conclusion

**`glm-4.7` and `glm-4.5-air` are the ones that can truly turn off reasoning** (the entire `glm-5.x` line cannot). After turning it off, a greeting's tokens go from **130 → 10**, a 13× difference. This pipeline does **transcription and arithmetic**, which needs no reasoning, so `GLM_THINKING=off` is the default.

**Fix**:
1. Add config item `GLM_THINKING`: `off` (default) / `low` / `auto`;
2. On first encountering 1210, `GLMClient` **auto-degrades** to `reasoning_effort: "low"`,
   and multiplies `max_tokens` ×3 to leave headroom for reasoning (then the whole round follows);
3. When `content` is empty and `reasoning_tokens > 0`, the error text **explicitly says "reasoning ate the budget"**,
   rather than letting the user think the network dropped.

**Verification**: `python3 scripts/preflight.py`'s `GLM live call` item prints
`model=glm-4.7  thinking=off  tokens=13`.

---

## Pitfall 23 ⭐⭐⭐ The model pads "this January" into "2026-01-01"

**Symptom**: The body only says "the plant was **connected to the grid for trial operation this January**", and the model outputs `event_time = "2026-01-01"`.

- "January" is **month-level** information
- "the 1st" is **fabricated by the model**
- and the same body actually also had a "September 14", which the model didn't use

This is the third form of the same class of error as Pitfall 21 (fabricated location) and Pitfall 19 (fabricated amount):

> **When the precision demanded by the format exceeds the precision the information actually has, the model fills the format with fabrication.**

**Fix (three layers)**:

1. **Prompt rule 3** changed to "copy at the original precision": given a day → `YYYY-MM-DD`; given only year-month → `YYYY-MM`;
   **explicitly forbid** padding a month into the 1st;
2. **`parse_datetime` supports `YYYY-MM`**, with the regex **anchored** `^...$`, so it won't swallow a full date.
   Also, relative expressions like "this January" **must return None** (don't guess when context is missing);
3. **`IntelItem` gains `event_time_raw`**: preserves the original precision; the report shows `2026-01`,
   rather than showing a `2026-01-01` that makes the reader think something happened on that day.

**Verification**: After fixing the prompt and rerunning the same document → `event_time = "2026-01"` ✅

**Residual bias (honest record)**: In the same document the model picked "this January" as the **background date**, rather than
"September 14" as the current-progress date. This shows that **few-shot examples can align format, but their binding power over subjective judgments like "which of several dates to pick" is limited**. The code-side countermeasure is to keep `event_time_raw` so the precision is visible,
rather than pretending this field is always right.

---

## Pitfall 24 ⭐⭐ Editing the same file in parallel within one message → the later write overwrites the earlier

This is an **operational-discipline pit**, not a code pit, but anyone using an agent to edit code will hit it.

**Symptom**: Sending two edits targeting `config.py` in the same message:

1. Edit A: add field `glm_thinking: str = "off"` in the dataclass
2. Edit B: add `glm_thinking=_env("GLM_THINKING", "off")` in `from_env()`

Both return "Successfully edited". But at runtime:

```
TypeError: Settings.__init__() got an unexpected keyword argument 'glm_thinking'
```

**A was overwritten by B** — both edits read from the same original content, and B's write wiped A's result.

**How it was found** (this diagnostic method is more worth copying than the conclusion):

```bash
python3 -c "
import sys; sys.path.insert(0,'src')
import dataclasses
from sourced_intel.config import Settings
print([f.name for f in dataclasses.fields(Settings)])"
```

The field is clearly in the file, yet runtime doesn't recognize it — meaning **you must look at runtime, not just at file contents**.

**Fix (three disciplines)**:
1. **Multiple edits to the same file must be serial**; one edit per change; only different files may be edited in parallel;
2. Verify with **runtime** immediately after editing (`dataclasses.fields`, a real `import`), don't just trust the edit tool's "success";
3. **Clear `__pycache__` before verifying**: this time an extra layer of `.pyc` cache interference made
   "file edited but import still old" look like black magic.

---

## Pitfall 25 ⭐⭐ The same logic written in two places = the same bug must be fixed twice

**Symptom**: Just fixed in `llm.py` "GLM reasoning model must turn off reasoning + enlarge `max_tokens`",
but **`preflight.py`'s GLM probe still used the old form** (`max_tokens: 8`, and sent no thinking at all),
so the health-check script reported:

```
[FAIL] GLM live call   unexpected status 0   evidence: TimeoutError: The read operation timed out
```

Yet **the pipeline itself was fine**. The situation "the health-check script is more fragile than the code under test" easily leads you to judge the direction wrong —
you go check the network, the key, the quota, when the problem is just that the probe was written wrong.

**Fix**: Extract the "reasoning-channel strategy" into a **single source of truth**:

- `Settings.glm_thinking` is the only config item;
- Both `GLMClient` and `preflight.py` read from it, neither hard-codes;
- Probe `max_tokens` raised from 8 to 200, timeout 40s → 90s.

**Lesson**: **Probe code is also code, and must be changed along with everything else.** Two hand-written HTTP calls will drift sooner or later.

---

## Pitfall 26 ⭐⭐⭐ `json_mode` only guarantees "valid JSON", not "matches your schema" — and the failure is **silent**

**Symptom**: End-to-end runs through, the report renders normally, status `completed`, no exception thrown. But in `trace.jsonl`
`05_analyze` has `"fallback": true` — the entire analysis was actually fallback text stitched by code; the model contributed not a word.

**Root cause (two stacked issues)**:

1. The model returned the findings list **under the `"answer"` key**, not the `"findings"` the schema requires;
2. Each finding's `confidence` is the string `"high"`, not the float `0.9`.

**Why it's completely invisible**: `Analysis.findings`'s default is `Field(default_factory=list)`.
Key-name mismatch → pydantic raises no error, just takes the default empty list → `if not candidate.findings:` judges
"no conclusion produced" → silently switches to fallback. **A misspelled key name and "the model truly had nothing to say" are completely isomorphic in the output.**

Worse, issue 2 is masked by issue 1: since findings is empty, pydantic never even gets a chance to validate the
`confidence` type inside. **Fix issue 1 and issue 2 blows up on the spot — they must be fixed together.**

```
Real evidence (obtained only after adding the diagnostic fields):
"payload_keys": ["answer"]
"payload_preview": "{\"answer\": [{\"headline\": \"多地储能项目密集开工并网\", ... \"confidence\": \"high\"}]}"
```

**Fix (two layers)**:

- **Add normalization at the code layer** (mandatory, can't rely on prompt alone): `_normalise_analysis_payload()` maps
  any list key among `answer`/`results`/`items`/`conclusions`/`analysis` to `findings`,
  and also accepts a bare array; `_coerce_confidence()` unifies `"high"`/`"90"`/`90` to `0.9`, and gives `0.5` (neutral) when unusable.
- **Tighten at the prompt layer** (auxiliary): explicitly "the top-level key can only be `findings`, don't wrap it in `answer`",
  and `confidence` must be a 0–1 number. Prompt bumped to **v1.2**.

**Effect**: Before fix `findings` = 0 + `fallback: true`; after fix **4 real conclusions**
(confidence 0.9/0.9/0.9/0.6, each with 1–2 real URLs), `errors` zeroed.

**Lessons**:
1. **`json_mode` / `response_format` only constrains syntax, not structure.** Any production system that "makes the model output JSON"
   must have a normalization layer; no matter how clearly the prompt is written, it's only probabilistic.
2. **Beware "silent defaults"**: For a schema used for LLM output, a list field's default value disguises "wrong structure" as
   "empty content". When diagnosing, print the raw payload first; don't guess.
3. **Bury diagnostic fields early**: In its first round this bug reported only one line,
   `all_findings_dropped: every finding lacked a verifiable source URL` — **and that line itself was wrong**
   (no finding was rejected at all; none were returned in the first place). After recording `payload_keys` / `payload_preview` into `errors`,
   the root cause was visible at a glance.

**Regression test**: `tests/test_analyze_payload.py` (19 cases), covering `answer` wrapping, bare arrays,
`"high"`/`"90"`/`None`/`True`/out-of-range values, and "the standard `findings` key must not be overridden by an alias".

---

## Pitfall 27 ⭐⭐ Place-name "longest-first" fails on **equal length**: district-level overridden by city-level

**Symptom**: `infer_adcode(标题="深圳南山储能项目开工")` returns Shenzhen city's `440300`, not Nanshan district's `440305`.

**Root cause**: The match loop is written `if key in text and len(key) > len(best_key)` — **strictly greater**.
`REGION_TO_ADCODE` registers both the long name and the short name (`"南山区"` → also registers `"南山"`),
and `"深圳"` and `"南山"` are **both length 2**, `2 > 2` is false → the first-traversed `"深圳"` wins.
**The dict's insertion order became an implicit priority, purely by accident.**

**Fix**: Extract into a single source of truth `config.match_region()`, changing the sort key to **`(administrative level, key length)`**:

```python
is_district = 0 if adcode.endswith("00") else 1   # 440300=city, 440305=district
score = (is_district, len(key))
```

The administrative level is derived directly **from the adcode itself** (last two digits `00` = city, non-`00` = district), needing no extra maintained column,
and won't drift from the data table. District-level always beats city-level; among the same level, compare length.

**A second bug fixed along the way**: In `REGION_TABLE`, the **display names of all 9 district-level rows were filled as `"深圳市"`**
(copy-paste residue), causing `resolve_region("深圳市南山区")` to get the right adcode (440305),
but the display name wrongly becomes `"深圳市"` — Nanshan/Futian/Luohu districts would all **collapse onto the same label** in the report header and map.

**Lessons**:
1. **Rules like "longest match" must specifically test the "equal length" case.** One character's difference between `>` and `>=` behaves like randomness
   — depending on the dict's insertion order.
2. **The same logic written in two files (`parse_intent.resolve_region` and `aggregate.infer_adcode`
   each copied one) means the bug is doubled.** This is a repeat of Pitfall 25; this time it was directly extracted into `match_region()`.
3. **The data table's third column can also lie**: a correct adcode doesn't mean a correct display name. When district-level and city-level share one display name,
   the information is gone.

---

## Pitfall 28 ⭐ `uvloop`'s macOS wheel is called `universal2`; grepping for `arm64` leads to the wrong conclusion

**Symptom**: Wanting to confirm whether macOS arm64 can install `uvicorn[standard]`, I checked the PyPI index for
`cp314-...-macosx...arm64.whl`, and `uvloop` came back as **0** — almost asserting from this that "macOS can't install it,
must switch Python version".

**Truth**: `uvloop`'s macOS wheel is named **`macosx_10_13_universal2.whl`**,
`universal2` itself already contains arm64, and **the string `arm64` simply does not appear in the filename**.
Measured: cp312 / cp313 / cp314 **all have** universal2 wheels.

Correct way to check:

```bash
curl -s https://pypi.tuna.tsinghua.edu.cn/simple/uvloop/ \
  | grep -oE 'uvloop-[0-9][0-9A-Za-z._+-]*\.whl' | sort -u | grep -i macos
```

(Note the character class must include `-`, otherwise a hyphenated filename like `uvloop-0.22.1-cp314-...` won't match,
and would again be misjudged as "none". **I hit this pit twice within an hour.**)

**Lesson**: **To judge "does a package have a wheel for a platform", look at the full wheel name, don't grep by platform keyword.**
`universal2`, `macosx_11_0_arm64`, `manylinux*`, `abi3` are all valid forms.

---

## Pitfall 29 ⭐ pytest's `tmp_path` reports an **ERROR** in a restricted environment — that's not a test failure

**Symptom**:

```
ERROR tests/test_pipeline_smoke.py::TestSnapshots::test_roundtrip_preserves_models
PermissionError: EEXIST: file already exists, mkdir
  '<system-tmpdir>/pytest-of-unknown'
```

Yet the same batch of tests ran all green the previous time.

**Distinguishing**: `ERROR` (fixture / collection phase blew up) ≠ `FAILED` (assertion didn't pass).
Here pytest was blocked by the environment's permission layer when creating the `tmp_path` base dir — **unrelated to the code under test**.

**Verification method**: Rerun with a writable base dir; all green confirms it's an environment issue:

```bash
pytest tests/ -m "not slow" -q --basetemp=/tmp/pt-intel
# → 106 passed
```

**Lesson**: When you see `ERROR`, first tell apart **whether the environment blew up or the code blew up**; don't go "fix" something in the tests that was correct to begin with.

---

## Pitfall 30 ⭐⭐⭐ I denied the existence of the REST API from **local observation** (same class as Pitfall 21, I did it again)

**Symptom**: Checking the `app/` directory, I only saw `streamlit_app.py`, so I judged "the REST API promised by the README
is fake, and the `api` service in `docker-compose.yml` will fail to start", and was already preparing to implement one.

**Truth**: `src/sourced_intel/api.py` **has always existed** (FastAPI, 5 endpoints),
and the `uvicorn sourced_intel.api:app --app-dir src` written in `docker-compose.yml` **lines up perfectly**.
"the API should live under the `app/` directory" was only an assumption in my head, not a fact.

Starting it once reveals the truth:

```bash
PYTHONPATH=src .venv/bin/python -m uvicorn sourced_intel.api:app --app-dir src --port 8598
curl -s localhost:8598/openapi.json   # → /health, /runs, /runs/{id}, /report, /trace
```

**Lessons**:
1. **"Not seen in the directory" ≠ "doesn't exist."** Before drawing a **negative conclusion** like "this feature isn't implemented", there must be one
   real run or a full-repo search as evidence. Negative conclusions need evidence just like positive ones.
2. This is the **same class of error** as Pitfall 21 (using a memory-fabricated "Gansu Jiuquan" to accuse the model): treating an assumption as fact.
   The same class of error has now been made twice — the rule is simple: **before saying "it doesn't exist", run it once or search once.**

---

## Pitfall 31 ⭐⭐⭐ `render_html` swallows every exception — so a template error silently downgrades the report to Markdown

**Severity: critical, because it is invisible.** `render_html` wraps the whole Jinja
render in `except Exception` and falls back to `render_markdown`. That is the right runtime
behaviour (a run must still produce *something*) and a terrible property to test against:
a broken template still "succeeds", the run still reports `status: completed`, and the only
difference is that the deliverable changed file type.

**[Measured] Symptom.** Rendering the real saved run reproduced the silent path:

```
ERROR sourced_intel.report.render:render.py:251
  HTML rendering failed ('dict object' has no attribute 'share'), falling back to Markdown
```

`Aggregation.by_subject` is typed `list[dict[str, Any]]` — free-form by design — and the
template did `s.share * 100` plus `"{:,.0f}".format(s.total_amount)`. One entry missing those
keys raised `UndefinedError` and the formatted report disappeared.

**Root cause.** The renderer trusted a free-form dictionary. In Jinja2 the default `Undefined`
prints as an empty string but **raises on any operation**, so `{{ s.subject }}` is safe while
`s.share * 100` is not — an inconsistency that is easy to miss when reading the template.

**Fixes**

1. `render._subject_rows()` normalises that free-form list into a guaranteed shape
   (`subject`, `count`, `share`, `total_amount`), recomputing `share` from counts when it is
   missing or given as a percentage. The template iterates the normalised rows.
2. `render_html`'s caller (`nodes/generate_report.py`) now records
   `{"type": "html_render_fallback"}` in the state's errors when the returned path is not
   `.html`, so a downgrade reaches the trace and the API instead of only the log.
3. `tests/test_report_contract.py` asserts the `.html` path was taken and that no `.md` sibling
   exists — an assertion that only has value because the fallback exists.

**Reproduce**

```bash
cd sourced-intel && .venv/bin/python -m pytest tests/test_report_contract.py -q
# The 24 tests include one that deliberately breaks the template and asserts the
# Markdown fallback still works, so the resilience stays intentional.
```

---

## Pitfall 32 ⭐⭐⭐ A configuration knob that nothing reads is not a mitigation — the GLM concurrency setting was dead

**Severity: critical (silent data loss).** Same class as the GDELT interval setting fixed
earlier, and it cost 40% of the extracted data on every single run.

**[Measured] Symptom.** Two consecutive real runs each logged two of these:

```
llm_error | glm failed after 2 attempts: GLM rate limited (429):
  {"error":{"code":"1302","message":"您的账户已达到速率限制，请您控制请求频率"}}
```

`03_extract` splits 36 documents into 5 batches and runs them through a
`ThreadPoolExecutor` sized by `EXTRACT_MAX_CONCURRENCY` (4). A 429 abandons the **whole batch**
— `_extract_batch` catches `LLMError` and `break`s — so 2 of 5 batches vanished. One run
produced 7 records from 36 documents, the next 15. The report looked normal.

**[Measured] Root cause.** `grep -rn "glm_max_concurrency" src/` returned only `config.py`:

```
src/sourced_intel/config.py:79:    glm_max_concurrency: int = 1
src/sourced_intel/config.py:116:   glm_max_concurrency=_env_int("GLM_MAX_CONCURRENCY", 1),
```

So `GLMClient` posted to GLM directly from four threads with no semaphore and no spacing,
while `config.py` said *"the pipeline serialises GLM calls by default"* — a comment describing
a behaviour that did not exist.

**Fixes**

1. `llm.ProviderPacer` — one semaphore plus a start-to-start minimum interval, held by
   `GLMClient` and passed only around the HTTP POST. It is deliberately released **before**
   the error handling, because the 1210 path recurses into `call()` and a single slot held
   across that recursion would deadlock (covered by a test with a 10 s join timeout).
2. The pacer reads both settings, and the defaults were re-measured rather than guessed.
   The first sweep only compared 4 concurrent against 2 concurrent; the follow-up swept the
   ceiling properly and the answer was sharper than expected:

   | configuration | calls completed |
   |---|---|
   | 4 concurrent, 0.5 s apart | 4 of 10 |
   | 3 concurrent, no spacing | 2 of 10 |
   | 3 concurrent, 0.5 s apart | 5 of 10 |
   | 3 concurrent, 1.0 s apart | 4 of 10 |
   | **2 concurrent, 0.5 s apart** | **10 of 10, twice** |
   | 2 concurrent, no spacing | 12 of 12 |
   | 2 concurrent, 0.25 s apart | 12 of 12 |

   The ceiling is **two concurrent requests**, and it is a hard ceiling: three fails whatever
   the spacing. Inside the ceiling the spacing is *not* load-bearing — which is why the 0.5 s
   default is kept as cheap insurance rather than as a tuning knob. Slowing it down further
   buys nothing; the wall clock is dominated by server-side queueing (a request for **eight
   tokens** showed a p50 latency of 15–31 s), not by our pacing.
3. `redacted()` reports both knobs, so the config view matches what the code actually uses.

**[Measured] Effect on a real run**

| | before (4 concurrent, unpaced) | after (2 concurrent, 0.5 s + batch splitting) |
|---|---|---|
| rate-limit errors in `03_extract` | 2 of 5 batches lost | **0** |
| records extracted | 7–15 | **18–29** |
| `03_extract` wall time | 13.7 s / 25.6 s (partial) | 31.1 s (all 5 batches) |

**Regression tests**: `tests/test_llm_pacing.py` (8 tests) — concurrency is actually capped,
the interval is actually enforced, the configured parallelism is not accidentally serialised,
the 1210 recursion does not deadlock, and both knobs are read from the environment.

---

## Pitfall 33 ⭐⭐ The "under 60 seconds" headline was partly bought with dropped data

**Severity: medium as a bug, high as a lesson.** Nothing in the system was broken; the
*claim* was.

The README promised a report in under 60 seconds, and a run measured 54.4 s. It was only when
a full run was re-measured after fixing Pitfall 32 that the two numbers were put side by side:

| run | elapsed | extract errors | records |
|---|---|---|---|
| before the fix, cold GDELT | 61.7 s | 2 batches lost (429) | 15 |
| before the fix, warm GDELT | 50.3 s | 2 batches lost (429) | 7 |
| after the fix | 93.3 s | 0 | 19 |

(An earlier un-remeasured reading of 54.4 s is quoted in older notes; the three runs above are
the ones with a saved trace and a final snapshot behind them.)

The fast number was fast **because a fifth of the retrieval was being thrown away**. Fixing the
rate limiting made the pipeline both more correct and ~50% slower, and the honest headline is
now a **distribution** rather than a number: across the ten recorded runs the total was
**41–110 s, median 61 s**, with the per-node spread published so a reader can see where it goes —
retrieval 0.9–42.5 s, `03_extract` 7.5–33.5 s (5 batches, 2 in flight), `05_analyze` 10.5–41.3 s
(a single call, provider latency is the variable), `06_generate_report` 3.5–10.2 s. An eleventh
run took 262 s and is reported separately, because it was deliberately configured with one
concurrent request to establish the ceiling; excluding it by name is a flag
(`scripts/run_stats.py --exclude 205122`), not a quietly chosen window.

The figures are a snapshot, and the script says so every time it runs: it prints the number of
runs it sampled, so a reader can tell whether a quoted range is still current. A freshly
unpacked copy of this project reports "no runs recorded yet" until it has been run once.

**Lesson.** A latency figure is only comparable at constant output. When a throughput fix and a
latency number are in the same change, report them together — otherwise the optimisation is
just measuring less work. Any future "we made it faster" claim in this project should be
accompanied by the record count from the same run, which is exactly what `scripts/run_stats.py`
prints: totals, per-node spread, document and record counts, and a `markdown fallbacks` line that
must read 0 (see Pitfall 31).

---

## Pitfall 34 ⭐⭐⭐ The reject path threw away the whole batch — and the fix for #32 was only a mitigation

**Severity: critical (silent data loss).** This is the reason Pitfall 32's fix was not enough.

**Where it came from.** Fixing the dead concurrency knob (Pitfall 32) made 429s stop happening
at the *default* settings. That felt like the bug. It was not: it was the *trigger* that had
stopped being pulled. The failure mode itself was still loaded, one thread away from firing —
because `_extract_batch` handled a rejected request like this:

```python
except LLMError as exc:
    errors.append({"node": NODE_NAME, "type": "llm_error", "detail": str(exc)})
    break        # <- the other 7 documents in this batch are gone
```

A 429 that survives the client's own backoff removed **every** document in the batch. With
`EXTRACT_BATCH_SIZE=8` that is 8 of 36 documents gone, `status: completed`, `report_format: html`,
and nothing in the status, the report, or the quality gate to say so. The only trace was one
`llm_error` line among 36 documents' worth of work.

**[Measured] The evidence that made it undeniable.** The same 36 documents, across recorded runs:

| run | records | `03_extract` errors |
|---|---|---|
| 2026-09-25 18:42 | 15 | 2 × 429 |
| 2026-09-25 18:43 | 7 | 2 × 429 |
| 2026-09-25 21:00 (pacer wired) | 19 | 0 |
| 2026-09-25 21:02 | 18 | 0 |
| 2026-09-25 21:35 | 18 | 0 |

7 vs 19 records from the same corpus, with `completed` both times. A reviewer reading only the
status would have called those runs equivalent.

**Fix: halve the batch instead of dropping it.** On a non-auth `LLMError`, `_extract_batch`
splits the batch in two and recurses (`_split_and_retry`), bounded by `MAX_SPLIT_DEPTH = 3`
(8 → 4 → 2 → 1). Each level is retried later, after the quota window has moved, and each half
is a smaller request that is easier to admit. The loss bound changes from *"8 documents"* to
*"1 document"*.

Three properties were deliberate:

* **Auth errors are exempt.** `LLMAuthError` is a subclass of `LLMError` and its contract is
  "never retried". Splitting on a bad credential would turn one doomed call into fifteen.
* **The cost stays attributable.** Token counters from both halves are summed back into the
  node's usage, and every split appends a `batch_split` error carrying the original batch size.
* **The degradation is countable.** `node_meta["03_extract"]["batches_split"]` reports how many
  splits a run needed, so "this run kept its data but at the cost of 3 retries" is visible
  without reading the error list.

**Regression tests**: `tests/test_extract_recovery.py` (10 tests) — a rejected batch returns all
its documents; halving stops at single documents (call count is exactly `2^(depth+1) - 1`);
nothing recovered means one error per document; an auth error produces exactly one call; usage is
summed across halves; a clean batch never splits.

**Lesson.** Fixing the trigger is not fixing the failure mode. `PITFALLS` #32 removed the reason
the 429 fired; this entry removed the reason it *hurt*. When a rejection handler can only
`break`, ask what it is throwing away — and then ask which of the two fixes a future change would
have to break to bring the bug back.

---

## Pitfall 35 ⭐⭐ The preflight writability probe raced with itself — a healthy directory reported as broken

**Severity: moderate (false alarm, non-deterministic).** Not data loss — a check that lies.

**Where it came from.** The preflight check for "data directory writable" wrote and unlinked a
**fixed filename**:

```python
probe = data_dir / ".preflight"     # one name, shared by every process
probe.write_text("ok", encoding="utf-8")
probe.unlink()                      # <- second process to get here raises
```

Two preflights running at once both write the same path; whichever unlinks second gets
`FileNotFoundError`, which lands in the `except` and is recorded as a **hard** failure:

```
data directory writable: .../data: [Errno 2] No such file or directory: '.../data/.preflight'
```

Exit code 1. A directory that was writable the whole time is reported as broken — and because the
trigger is timing, the same command passes on the next invocation. This is the worst shape a
check can take: the failure is real enough to block on and random enough to be dismissed.

**[Measured] The probe leaves files behind.** This is what the investigation actually turned up,
and it is the part the measurements support. `Glob` against the real filesystem — not `find`,
which the sandbox answers from its own view — found **eleven** `.preflight-*.tmp` files in the
development tree and **eleven** in the delivery copy. Eleven is exactly how many preflights
`tests/test_preflight_output.py` launches in one run: 2 in `setUpClass` + 6 concurrent + 3
sequential. The count matching the call sites, in both trees, is the evidence.

**A correction I have to make about my own evidence.** My first reading was "six concurrent runs,
five failed — that proves the fixed name races with itself." Here is the recorded detail of those
failures:

```
data directory writable: .../data: [safe-delete] .../.preflight
```

`[safe-delete]` is a delete interception installed by the **tooling that was running the command** —
not by the project, and not by anything a customer will have. Those processes were not losing a race
to each other; the tooling was refusing their `unlink`. The number I was about to write down as proof
did not prove what I thought it proved — which is the exact failure this file exists to catch,
committed while writing the file.

So the two claims are separated by what is behind each one:

| claim | status |
|---|---|
| Two processes writing then unlinking the **same** path race, and the loser raises `FileNotFoundError` | **[Inferred]** — POSIX semantics plus a read of the code. Not measured here: inside the sandbox, a caught `FileNotFoundError` and a refused unlink are indistinguishable. |
| A cleanup failure was being swallowed, leaving files on disk with no record of why | **[Measured]** — the files are there, and the WARN path was verified by injection (below). |

The fix is right either way — a unique name removes the race, and reporting the cleanup failure
makes the residue visible — but the entry does not get to claim a measurement it does not have.

**How it was found.** Not by reading the code: by running **two suites at the same time**, one in
the development tree and one in the delivery copy. The suite passed on its own and failed once when
the two overlapped, and that one test passed again when run by itself. That was enough to start
looking — and the trail from there led to the files on disk, not back to the race. Worth stating
plainly: a test suite is not evidence until it has been run under conditions that are not the ones
you chose.

**Fix, part one — a unique name.** `tempfile.mkstemp(dir=data_dir, prefix=".preflight-")` gives
each process its own path, so there is nothing left to collide over. The check now tests
**writability**, which is the question it was asked; it no longer accidentally tests
**exclusivity**, which nobody asked about.

**Fix, part two — the cleanup must not be silent.** The first version of this fix wrapped the
unlink in `except OSError: pass`. Eleven probe files were then found on disk with no diagnostic
anywhere saying so: a swallowed failure, the same shape as #31, written into the fix for a
different bug. They accumulated because that tooling intercepts deletes — and that is exactly why a
silent fallback is the wrong handling, because on the machine that matters the unlink succeeds and
the bug stays invisible in precisely the environment meant to prove the code works.
Removing the file is now recorded as a **WARN** carrying the path and the error, and deliberately
**not** as a hard failure — being able to write is what this check asks; failing to tidy up
afterwards is a lesser and different problem, but it belongs on the record either way.

**[Measured] The WARN path, with the failure injected rather than hoped for.** `Path.unlink`
patched to raise `PermissionError`:

```
  PASS  | data directory writable     | .../data
  WARN  | preflight probe cleaned up | could not remove .../.preflight-n01yf1dp.tmp (PermissionError: ...)
exit code 0, hard_failures 0
```

The directory really was writable, so it stays a pass; the file that could not be removed is
named. A check that keeps those two facts apart is a check you can act on.

**Regression tests**: `tests/test_preflight_output.py` — six preflights launched concurrently must
all exit 0, all emit pure JSON and all report `hard_failures: 0`; a sequential run must leave no
probe behind. Both leftover assertions are made on the **delta** against a baseline sampled in
`setUpClass`, never on the absolute contents of `data/`. That detail is the point: the first
version of the leftover test asserted the directory was empty, so it failed the moment any other
suite was running against the same checkout — a test reporting on its neighbours rather than on
the code it names.

**Lesson.** A health check that uses a fixed path in a shared directory is a check that will
report on whoever else is in that directory instead of on the system. When a check exists to
answer "can I write here", give it a name nobody else can be using — and when a failure resists
reproduction, find out what your measurement actually measured before writing it down as a cause.

There is a second lesson, cheaper to learn: the first fix for this introduced a silent
`except OSError: pass`. The project's own rule — never swallow a failure — applies to the code
written while fixing another violation of it. The leftover files were found by a test written in
the same sitting, not by re-reading the patch.

And a third, the one this entry cost the most to learn: when the recorded failure text contains a
token you did not predict — `[safe-delete]` here — explain **that** token before explaining your
hypothesis. The hypothesis came first and lived too long, because the number printed beside it
looked like a measurement.

---

## Pitfall 36 ⭐⭐⭐ `by_subject` is capped at 15 — summing it is not the denominator of the shares

**Severity: high (silent, and the wrong number is the *right* number printed beside the wrong label).**

**Where it came from.** The "subject activity" table shows a share column computed against "the
records that named a subject". `by_subject` is produced with `most_common(15)`, so when 18 records
named a subject, the list holds only 15 rows. The template then did `sum(row.count)` as the
denominator of every share. Result: the denominator printed **17** while every share was actually
divided by **18**.

**Why this is the worst shape of bug.** The shares themselves were correct (they used the real
total). The *label* beside them was wrong. So the table read "17 records named a subject, and these
shares sum to 94% of them" — internally consistent-looking, arithmetically false, and invisible to
any check that only looks at one column at a time. It was found by a test that asserted the sum of
the region counts equals the record count, which is the kind of cross-column invariant that a
one-column linter cannot express.

**Fix.** `Aggregation` gained a `with_subject` field — the true number of records that named a
subject, computed in `nodes/aggregate.py` before the truncation. The template and `_subject_rows`
both read it. The label is now the number the shares were actually divided by, so a correct share
can no longer look wrong.

**Lesson.** Any time a derived list is truncated for display (`most_common`, `head`, a page of
rows), the *count* that list truncates is a separate fact and must be stored separately. Summing
the truncated list and calling it the total is right until the list is ever longer than the cap —
and the bug is then in the one column that looks the most authoritative.

---

## Pitfall 37 ⭐⭐ The empty run was never exercised — it crashed the renderer and fabricated a share

**Severity: moderate (two independent defects, both in the "no data" path).** Not data loss: an
empty result is a *valid outcome*, and the renderer treated it as a template error.

**Where it came from.** Running the renderer against a genuinely empty run (no items, no regions,
no months, no subjects, no sources) exposed two failures that the normal demo path never touches:

1. **A crash that looked like success.** The findings hint referenced `nums.references`
   unconditionally, but `_sections` only emits that key when `has_sources` is true. An empty run
   raised, `render_html` caught it and silently wrote Markdown, and the caller saw "completed" —
   the exact failure shape of #31, reached through a different door.
2. **A fabricated figure.** "Top-3 concentration" printed `0.0%`. For an empty run there is no
   share at all — zero subjects were named — so `0.0%` *invents* a measurement where the report's
   one hard promise is to never invent one.

**Fix.** The hint is now conditional on `sources` existing. The concentration falls back to `None`
(rendered as `-` by the `pct` filter) whenever no subject was named. Both locked by
`TestEmptyRun` — two tests that render a truly empty run and assert (a) it produces HTML, not a
silent Markdown fallback, and (b) no `0.0%` appears.

**Lesson.** `0` and `None` are not the same fact, and in a report that promises every number is
computed, the difference is load-bearing: `0` means "the computation ran and the answer is zero",
`None` means "there was nothing to compute". Rendering the latter as the former is a fabrication,
no matter how defensible `0.0%` looks in a table of percentages. The fix is to route the absence
through the same empty-state every other missing field uses, instead of giving it a numeric
default of zero.

---

## Appendix: One-shot reproduction of all probe results

```bash
cd sourced-intel

# 0. Full-pipeline health check (dependency-free, installs nothing, takes seconds; fastest path)
python3 scripts/preflight.py

# 1. Full end-to-end run (dependency-free, real fetch + real GLM call) — the most convincing one
python3 scripts/rehearse_glm.py --docs 3

# 2. Reproduce the three prompt examples exactly (list-page order shifts; only fixed URLs replay)
python3 scripts/rehearse_glm.py --urls \
  https://www.china5e.com/news/news-1209422-1.html \
  https://www.china5e.com/news/news-1209430-1.html \
  https://www.china5e.com/news/news-1209444-1.html
# Expected: 墨玉 → 3.3亿元 / 安徽省 / 2026-01; 熔盐 → no amount / 山东省 / 2026-09-16; ad → 0 records

# 3. Source reachability (GDELT retries with backoff, up to ~3 min; everything else returns immediately)
python3 scripts/check_sources.py

# 4. Dependency-free unit tests (58 cases, 0.1 s)
python3 -m unittest discover -s tests -p "test_parsing.py" -v

# 5. Verify the DNS hijack in isolation (Pitfall 4)
nslookup www.ccgp.gov.cn

# 6. Verify the SPA shell in isolation (Pitfall 6)
curl -s -o /dev/null -w "%{size_download}\n" https://gdgpo.czt.gd.gov.cn/     # ≈2104

# 7. Verify key shape in isolation (Pitfall 15)
python3 -c "import sys;sys.path.insert(0,'src');from sourced_intel.llm_keys import key_problem;print(key_problem('0123456789abcdef0123456789abcdef'))"

# 8. Verify the GLM model catalogue in isolation (Pitfall 16) — only the server can say whether a model name really exists
curl -s https://open.bigmodel.cn/api/paas/v4/models \
  -H "Authorization: Bearer $GLM_API_KEY" | python3 -m json.tool | grep '"id"'

# 9. Verify ollama thinking mode in isolation (Pitfall 17) — compare output length with think on vs off
python3 -c "
import json,urllib.request
for think in (None, False):
    p={'model':'qwen3.5:4b','messages':[{'role':'user','content':'Return exactly: {\"ok\": true}'}],
       'stream':False,'format':'json','options':{'temperature':0.0,'num_predict':500}}
    if think is not None: p['think']=think
    r=json.loads(urllib.request.urlopen(urllib.request.Request(
        'http://127.0.0.1:11434/api/chat',data=json.dumps(p).encode(),
        headers={'Content-Type':'application/json'}),timeout=300).read())
    print('think=%-5s eval=%-4s content_len=%s' % (think, r.get('eval_count'), len(r['message']['content'])))
"
# Expected: think=None → content_len=0 (thinking burned all tokens); think=False → content_len>0

# 10. Verify district-level place-name matching (Pitfall 27) — district must beat city
python3 -c "
import sys;sys.path.insert(0,'src')
from sourced_intel.config import match_region, REGION_DISPLAY
for t in ['深圳市南山区','深圳南山储能项目开工','深圳','Shenzhen','火星']:
    a=match_region(t); print('%-26s -> %s %s' % (t, a, REGION_DISPLAY.get(a,'')))
"
# Expected: first two → 440305 南山区 (Nanshan District); '深圳' → 440300 深圳市 (Shenzhen City); last two → None

# 11. Verify LLM payload normalization (Pitfall 26) — swapping key names / confidence forms must yield identical results
python3 -c "
import sys;sys.path.insert(0,'src')
from sourced_intel.nodes.analyze import _normalise_analysis_payload as N
from sourced_intel.schemas import Analysis
f=lambda c:{'headline':'h','detail':'d','evidence':['https://x'],'confidence':c}
for tag,p in {'answer+high':{'answer':[f('high')]},'findings+0.8':{'findings':[f(0.8)]},'bare list':[f('medium')]}.items():
    a=Analysis.model_validate(N(p))
    print('%-14s findings=%d confidence=%s' % (tag, len(a.findings), [x.confidence for x in a.findings]))
"
# Expected: all three lines findings=1, confidence respectively [0.9] [0.8] [0.6]

# 12. REST API smoke test (Pitfall 30) — don't just check whether /docs opens
PYTHONPATH=src .venv/bin/python -m uvicorn sourced_intel.api:app --app-dir src --port 8598 &
sleep 8
curl -s localhost:8598/openapi.json | python3 -m json.tool | head -30
curl -s -X POST localhost:8598/runs -H 'Content-Type: application/json' \
  -d '{"query":"深圳储能项目情报，最近3个月"}' | python3 -m json.tool | head -20
# Expected: http 200, ~40s, documents≈39 records≈15 failed_sources=['gdelt']

# 13. Full test suite (19 analyze-payload regression cases + 4 preflight output-contract cases)
pytest tests/ -m "not slow" -q --basetemp=/tmp/pt-intel     # → 106 passed
pytest tests/ -m slow -q --basetemp=/tmp/pt-intel           # → 1 passed (real end-to-end)
```

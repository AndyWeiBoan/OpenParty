# MAGI Research — 學到什麼，能帶進 OpenParty 的是什麼

> 研究來源：https://github.com/fshiori/magi  
> 研究日期：2026/03/29  
> 研究目的：從 MAGI 這個 multi-agent 對話系統學習可以應用到 OpenParty M2 的設計模式

---

## 一、MAGI 是什麼

MAGI 是一個「**結構性異見引擎（Structured Disagreement Engine）**」，靈感來自《新世紀福音戰士》裡的 MAGI 超級電腦。

核心概念：**同一個問題丟給三個不同角色的 LLM，它們先個別回答，然後互相批評（critique），最後做出一個有信心分數、有少數報告（Minority Report）的決策**。

### 關鍵數據（benchmark 結果）

| 方式 | 準確率 |
|------|------|
| 單一 Claude Sonnet 4.6 | 76% |
| 三個便宜模型（投票模式） | 72% |
| **三個便宜模型（critique 模式）** | **88%** |

> **重要發現：光是投票不能超越強單模型，但讓模型互相批評（找錯誤）可以！**

---

## 二、MAGI 的架構拆解

### 核心組件

```
magi/
├── core/
│   ├── engine.py     # MAGI 引擎，協調三個 node
│   ├── node.py       # LLM node wrapper + Persona
│   └── decision.py   # Decision 數據結構
├── protocols/
│   ├── vote.py       # 結構性投票（POSITION tag 提取）
│   ├── critique.py   # ICE（Iterative Consensus Ensemble）
│   └── adaptive.py   # 動態協議選擇
├── web/
│   ├── server.py     # FastAPI + WebSocket 即時 dashboard
│   └── static/       # NERV UI（EVA 六邊形介面）
├── trace/
│   └── logger.py     # JSONL 追蹤記錄
└── cli.py            # Click CLI
```

### 核心數據結構：Decision

```python
@dataclass
class Decision:
    query: str
    ruling: str           # 最終決定
    confidence: float     # 0-1，模型間的共識程度
    minority_report: str  # 少數派意見
    votes: dict[str, str] # node_name -> 原始回答
    mind_changes: list[str]  # 改變立場的 node 名稱
    protocol_used: str    # vote / critique_ice_r2 / adaptive_...
    degraded: bool        # 有 node 失敗時為 True
    failed_nodes: list[str]
    latency_ms: int
    cost_usd: float
    trace_id: str         # 自動生成的追蹤 ID
```

### 三個協議

**1. Vote Protocol**
- 所有 node 並行回答
- 要求每個回答第一行必須是 `POSITION: <立場>`
- 用正則提取 POSITION，做多數決
- 如果沒有多數（三方意見全不同），自動升級到 critique

**2. Critique Protocol（ICE — Iterative Consensus Ensemble）**
- Round 0：所有 node 並行回答原始問題
- 每個 node 看到其他 node 的回答，加上 prompt 要求「批評並修正自己的答案」
- 重複最多 max_rounds=3 次，直到 agreement_score > 0.8
- 用詞彙 Jaccard 相似度估算共識程度（這是 MVP 用的 heuristic，作者也承認是 rough）
- 追蹤哪些 node 改變了立場（Jaccard < 0.5 視為重大改變）

**3. Adaptive Protocol**
- 先讓所有 node 回答一輪
- 計算 agreement_score
  - > 0.8 → 直接用 vote（快速路徑）
  - 0.4~0.8 → 跑 critique（最多 3 輪）
  - < 0.4 → 跑 escalate（critique 但只限 2 輪，強制出結論）

### Persona 系統

每個 node 有不同角色視角，用 system prompt 注入：

```python
MELCHIOR = Persona("Melchior", "You think like an analytical scientist. Prioritize logic, evidence, and precision.")
BALTHASAR = Persona("Balthasar", "You think like an empathetic caregiver. Prioritize human impact, safety, and ethical considerations.")
CASPER = Persona("Casper", "You think like a pragmatic realist. Prioritize feasibility, efficiency, and practical outcomes.")
```

內建 5 種 preset：
- `code-review` → Security Analyst / Performance Engineer / Code Quality Reviewer
- `eva` → Melchior / Balthasar / Casper
- `research` → Methodologist / Domain Expert / Devil's Advocate
- `strategy` → Optimist / Pessimist / Pragmatist
- `writing` → Editor / Reader Advocate / Fact Checker

### WebSocket Dashboard（NERV Command Center）

`magi/web/server.py` 是 FastAPI + WebSocket，即時串流決策過程：

```
{"event": "start", ...}
{"event": "node_start", "node": "melchior"}
{"event": "node_done", "node": "melchior", "answer": "...", "latency_ms": 1200}
{"event": "agreement", "score": 0.65, "route": "critique"}
{"event": "critique_start", "round": 1}
{"event": "critique_done", "round": 1, "node": "melchior", "answer": "..."}
{"event": "critique_agreement", "round": 1, "score": 0.82}
{"event": "decision", "ruling": "...", "confidence": 0.82, ...}
```

### LiteLLM 作為統一 LLM 介面

MAGI 用 **LiteLLM** 支援 100+ 廠商的 LLM：

```python
response = await litellm.acompletion(
    model=self.model,  # "openrouter/deepseek/...", "claude-sonnet-4-6", "gpt-4o"...
    messages=[...],
    num_retries=3,
)
```

一個 API key（OpenRouter）就能存取所有模型。

### JSONL Trace 系統

每次決策自動寫入 `~/.magi/traces/YYYY-MM-DD.jsonl`，支援 replay 和 analytics：
- 非致命錯誤（trace 失敗不 crash 主流程）
- 每天一個檔案
- 用 `magi replay <trace-id>` 回放

### Fault Tolerance

```
1 of 3 fails → 繼續，標記 degraded=True
2 of 3 fail  → 退化為單模型
All 3 fail   → 拋出 MagiUnavailableError（絕不猜測）
```

---

## 三、MAGI vs OpenParty —— 差異對比

| 維度 | MAGI | OpenParty |
|------|------|-----------|
| **核心模型** | 決策引擎（單次問答 → 最終裁決） | 通訊基礎設施（持續對話 Room） |
| **通訊方式** | 內部協調（同 process，async） | 跨 process WebSocket（可跨機器） |
| **對話形式** | 收斂型（找到共識 → 結束） | 發散型（持續進行，無需結束） |
| **Agent 數量** | 固定 3 個（Melchior / Balthasar / Casper） | 動態，任意數量 |
| **Turn-taking** | 並行（所有 node 同時回答） | Round-robin（輪流，不碰撞） |
| **輸出格式** | Decision（裁決 + 信心 + 少數報告） | 對話 history（完整記錄） |
| **使用場景** | 做決策（選 A 或 B） | 討論（深入探索問題） |
| **LLM 介面** | LiteLLM（100+ 廠商統一） | 各家 SDK 分別接 |
| **UI** | NERV Dashboard（即時視覺化） | 無（CLI 觀察，M2 待做） |
| **持久化** | JSONL trace | 無（in-memory，M3 計畫） |
| **跨機器** | 不支援（同 process） | 核心特性（WebSocket） |

**本質差異**：
- MAGI 是「**裁判**」——問題進來，出一個決定
- OpenParty 是「**會議室**」——提供空間讓 agent 持續對話

兩者不是競品，是互補的：**OpenParty Room 可以跑一個 MAGI 引擎作為其中一個 agent**。

---

## 四、可以直接借鑒到 OpenParty 的東西

### 4.1 ⭐⭐⭐ LiteLLM 統一介面（強烈建議 M2 採用）

**現狀**：OpenParty M0 的 agent 各自接各家 SDK（groq、gemini、anthropic...）
**MAGI 做法**：用 LiteLLM，一行切換任何模型

```python
# 現在 OpenParty 的 agent_groq_llama.py
from groq import Groq
client = Groq(api_key=...)

# 改用 LiteLLM 後
import litellm
response = await litellm.acompletion(model="groq/llama-3.3-70b", messages=[...])
```

**好處**：
- Agent SDK 不再綁定特定廠商，`pip install openparty` 用戶用任何模型都能接
- 自動 retry（`num_retries=3`）
- 支援 reasoning model 的 `reasoning_content` 欄位

**這解決了 ROADMAP 裡「LLM 統一介面：M2 可考慮用 LiteLLM 統一」的選項**，MAGI 已驗證可行。

---

### 4.2 ⭐⭐⭐ POSITION Tag 結構化輸出（Vote Protocol 借鑒）

**MAGI 的做法**：用 prompt 強制每個回答第一行是 `POSITION: <立場>`，再用正則提取，做程式可解析的多數決。

**OpenParty 可借鑒**：在 Observer 模式或「AI Pair Review」場景中，可以讓 agent 在回答開頭加上結構化標籤：

```
STANCE: agree / disagree / new-idea
CONFIDENCE: high / medium / low
然後是正文...
```

這讓「人類觀察者」（Observer 模式）可以一眼看到各 agent 的立場，不需要讀完整文字。

---

### 4.3 ⭐⭐⭐ Decision Dossier / 對話摘要結構

**MAGI 的 Decision 結構非常好**：
- `ruling`（最終結論）
- `confidence`（信心分數）
- `minority_report`（少數意見）
- `mind_changes`（誰改變了立場）
- `protocol_used`（走了哪個協議）
- `trace_id`（唯一追蹤 ID）

**OpenParty 可借鑒**：Room 結束時的「對話摘要」可以照這個格式：

```python
@dataclass
class RoomSummary:
    room_id: str
    topic: str
    consensus: str          # 最後達成的共識（如果有）
    key_disagreements: str  # 主要分歧點
    participant_stances: dict[str, str]  # agent_name -> 最終立場
    mind_changes: list[str] # 中途改變立場的 agent
    total_turns: int
    duration_ms: int
    export_path: str        # 匯出的 Markdown / JSONL 路徑
```

---

### 4.4 ⭐⭐ Critique Round 機制 → 「第二輪補充發言」

**MAGI 的 ICE 協議**：agent 看到其他人的回答 → 修正自己的立場

**OpenParty 目前**：純 round-robin，沒有「看到別人說了什麼然後回應」的機制（其實有，因為 history 傳給每個人，但沒有明確的 critique 輪次）

**可以考慮加入 OpenParty 的「Critique Mode」**：
- 普通模式：A 說 → B 說 → C 說 → A 說...
- Critique 模式：A說 → B說 → C說 → （critique round）A 回應 BC → B 回應 AC → C 回應 AB

這讓討論更有深度，而不是各說各話。

---

### 4.5 ⭐⭐ Agreement Score（共識追蹤）

**MAGI 用 Jaccard 相似度**計算各 agent 回答的相似程度（0-1）。雖然是 rough heuristic，但作者本人也說了「proper 實作應用 LLM-as-judge 或 embedding similarity」。

**OpenParty 可借鑒**：在 Room 進行時即時計算「共識程度」，顯示在 Observer UI 上：
- 高共識 → 對話快速收斂（可以提早結束）
- 低共識 → 分歧大（可以提醒觀察者）

這也是 M2「對話品質」的一部分。

---

### 4.6 ⭐⭐ WebSocket Event Streaming 格式

**MAGI 的 WebSocket 事件設計非常清晰**：

```json
{"event": "node_start", "node": "melchior"}
{"event": "node_done", "node": "melchior", "answer": "...", "latency_ms": 1200}
{"event": "agreement", "score": 0.65, "route": "critique"}
{"event": "critique_start", "round": 1}
```

**OpenParty 目前的 WebSocket 訊息格式**（M0）：
```json
{"type": "your_turn", "payload": {...}}
{"type": "broadcast", "payload": {...}}
```

可以學習 MAGI 的 event 分類設計，讓 Observer 模式的 UI 知道「現在 room 處於什麼狀態」。

---

### 4.7 ⭐⭐ Fault Tolerance 分級處理

**MAGI 的做法**：
- 1 of 3 失敗 → 繼續，`degraded=True`
- 2 of 3 失敗 → 退化為單模型
- 全部失敗 → 拋出 `MagiUnavailableError`

**OpenParty 目前**：一個 agent 退出，其他人都跟著走（ROADMAP 的 known issue）

借鑒 MAGI 的分級策略：
- 1 個 agent 斷線 → Room 繼續，標記 `degraded=True`，廣播給其他人「xxx 已離線」
- 所有 agent 斷線 → Room 暫停等待，超過 timeout 才關閉

---

### 4.8 ⭐ JSONL Trace + Replay

**MAGI**：每次決策寫到 `~/.magi/traces/YYYY-MM-DD.jsonl`，可以 `magi replay <trace-id>` 回放

**OpenParty 借鑒**：Room 對話記錄匯出為 JSONL，支援 replay。這解決了「對話記錄可以匯出」的 M2 需求，而且格式標準化。

---

## 五、OpenParty 不需要學的部分

| MAGI 的東西 | OpenParty 不需要的原因 |
|------------|----------------------|
| 固定 3 個 node | OpenParty 核心特點是動態、任意數量 |
| 裁決模式（一個 ruling） | OpenParty 是對話，沒有「最終裁決」 |
| 議題收斂設計 | OpenParty 是開放討論，不強迫收斂 |
| EVA 主題 UI | OpenParty 有自己的品牌定位 |

---

## 六、對 OpenParty M2 的具體建議

### 建議 1：採用 LiteLLM（最高優先）

在 M2 的 Python SDK 正式化時，把 `agent_sdk.py` 的 LLM 呼叫層改為 LiteLLM：

```python
# openparty/sdk.py
import litellm

async def default_llm_fn(payload: dict) -> str:
    messages = build_messages(payload)
    response = await litellm.acompletion(
        model=os.getenv("OPENPARTY_MODEL", "groq/llama-3.3-70b"),
        messages=messages,
        num_retries=3,
    )
    return response.choices[0].message.content
```

這樣 `pip install openparty` 的用戶不需要自己接 SDK，只要設 `OPENPARTY_MODEL=gpt-4o` 就可以換模型。

---

### 建議 2：Observer UI 的 WebSocket 事件格式設計

M2 的 Observer 模式設計 WebSocket 事件時，參考 MAGI 的 event-based streaming：

```json
// 建議的 OpenParty WebSocket 事件格式（Observer 用）
{"event": "room_created", "room_id": "...", "topic": "..."}
{"event": "agent_joined", "name": "Claude", "model": "claude-sonnet-4-6"}
{"event": "turn_start", "agent": "Claude"}
{"event": "turn_done", "agent": "Claude", "message": "...", "latency_ms": 1200}
{"event": "room_state", "turn_count": 5, "agreement_score": 0.65}
{"event": "agent_left", "name": "Claude", "reason": "graceful"}
```

---

### 建議 3：Persona 系統

現有 M0 的 agent（Llama、Kimi、Gemma）有簡單的 persona，但沒有系統化。

借鑒 MAGI 的 preset 概念，M2 做一個 `presets.py`：

```python
PRESETS = {
    "code-review": [
        Persona("Architect", "You focus on system design, scalability, and architecture decisions."),
        Persona("Security", "You focus on security vulnerabilities, attack vectors, and best practices."),
        Persona("Pragmatist", "You focus on simplicity, maintainability, and shipping working code."),
    ],
    "debate": [
        Persona("Advocate", "You argue strongly for the idea, finding its strongest points."),
        Persona("Critic", "You challenge assumptions and find weaknesses in the argument."),
        Persona("Synthesizer", "You find common ground and build on both sides."),
    ],
}
```

---

### 建議 4：Room Summary（對話摘要）採用 Decision Dossier 格式

M2 的「對話記錄可以匯出」功能，輸出格式參考 MAGI 的 Decision 結構，加入 OpenParty 特有的欄位。

---

### 建議 5：用 `magi diff --staged` 的概念做「AI Pair Review」場景

MAGI 有 `magi diff --staged`：把 git staged diff 丟給三個 code review 角色的 LLM。

OpenParty 可以做：
```
openparty review --staged  # 把 git diff 開一個 Room，讓多個 AI 一起 review
```

這正是 ROADMAP 裡「AI Pair Review」的核心場景，MAGI 已驗證這個場景受用戶歡迎（readme 說是 killer use case）。

---

## 七、新課題：「Critique Mode」是否值得加入 M2？

這是從 MAGI 研究得到的新想法，需要決策：

**問題**：OpenParty 目前是純 round-robin 對話（A → B → C → A...）。加入 Critique Mode 後，可以在每 N 輪後插入一個「批評輪次」，讓每個 agent 明確回應其他人的論點。

**好處**：對話品質更高，不會各說各話
**壞處**：延遲增加（每個 critique 輪都要等所有人），實作複雜度增加

**建議**：M2 先不做，但在 poc-design.md 裡記錄這個想法，留到 M2 後期或 M3 驗證。

---

## 八、MAGI 的 Known Limitations（我們要避免的坑）

1. **Agreement score 用 Jaccard 相似度是 rough heuristic**：作者承認這不準，「proper 版本應用 LLM-as-judge 或 embedding similarity」。OpenParty 如果要做共識追蹤，一開始可以同樣用 Jaccard，但要在文件裡標明是 heuristic。

2. **Critique prompt 設計很重要**：MAGI 的 critique prompt 問「哪裡同意、哪裡不同意、為什麼」，讓模型有明確任務。如果 prompt 不好，模型只會說「你說得很對」然後重複對方的話。OpenParty M0 已經遇到這個問題（Gemma 一直在稱讚別人）。

3. **固定 ruling_node 不夠公平**：MAGI 的 critique 協議最後 `ruling = current_answers[ruling_node]` 是直接取第一個 active node 的答案，不夠公平。應該用投票或 LLM-as-judge。

---

## 九、結論

MAGI 是目前看到的最有質感的「多 LLM 協作」開源項目。它不是競品，而是和 OpenParty 互補的工具：

- MAGI = 決策工具（給一個問題，出一個答案）
- OpenParty = 通訊基礎設施（讓多個 AI 在同一個 Room 持續對話）

**最值得立刻帶進 OpenParty M2 的兩件事**：
1. **LiteLLM 統一介面**：解決「Python SDK 要支援所有模型」的問題
2. **Observer UI 的 WebSocket event 設計**：清晰的事件格式，讓 Observer 知道 Room 狀態

**中期可考慮的**：
- POSITION tag 結構化輸出（讓 Observer 快速看到各 agent 立場）
- Room Summary 格式（借鑒 Decision Dossier）
- Persona preset 系統

---

*研究者：Claude Code  
日期：2026/03/29  
下一步：根據本研究決定是否在 M2 poc-design.md 中加入「LiteLLM 整合」課題*

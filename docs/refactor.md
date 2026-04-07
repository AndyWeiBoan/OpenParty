# Engine 抽象層重構方案

## 問題描述

目前 `engine` 的判斷邏輯散落在三個檔案中，每次新增一個 engine 就需要在三處同步修改：

| 檔案 | 引擎分支位置 | 說明 |
|------|-------------|------|
| `server.py` | `_spawn_agent_process()` | 組裝 bridge.py 的 CLI 參數時，opencode 和 claude 的參數格式不同 |
| `bridge.py` | `AgentBridge.__init__()` | opencode engine 在初始化時額外建立 `OpenCodeClient` |
| `bridge.py` | `AgentBridge.run()` | 呼叫 AI、重試邏輯各有一段 `if self.engine == "opencode" / else claude` |
| `openparty_tui.py` | `_fetch_models()` | 分別向 opencode HTTP API 和硬編碼 claude 列表取得可用模型 |

---

## 解法：Engine 策略介面（Strategy Pattern）

### 1. 新建 `engines/` 模組

```
OpenParty/
  engines/
    __init__.py        ← 匯出 EngineBackend, get_engine, list_engines
    base.py            ← 抽象類別 EngineBackend
    claude_engine.py   ← Claude Agent SDK 實作
    opencode_engine.py ← opencode HTTP API 實作
```

### 2. 抽象介面 `base.py`

```python
from abc import ABC, abstractmethod

class EngineBackend(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """engine 識別字串，如 'claude' 或 'opencode'"""

    @abstractmethod
    async def generate(
        self,
        prompt: "str | list[dict]",
        on_thinking: "Callable[[list[dict]], Awaitable[None]] | None" = None,
    ) -> tuple[str, str]:
        """呼叫 AI 並回傳 (reply_text, actual_model_id)。

        on_thinking: 生成過程中即時回調，傳入 agent_thinking blocks。
        """

    @abstractmethod
    async def list_models(self) -> list[dict]:
        """回傳可用模型列表，每個 dict 含 display / full_id / engine / base_name。"""

    @classmethod
    @abstractmethod
    def build_cli_args(cls, model_id: str, owner_name: str = "") -> list[str]:
        """回傳傳遞給 bridge.py 的額外 CLI 參數列表（server.py 呼叫）。"""

    async def startup(self) -> bool:
        """啟動前置作業（如確保 opencode serve 在執行）。預設無-op，回傳 True。"""
        return True
```

### 3. 各 engine 實作

**`claude_engine.py`**
- 把 `AgentBridge._call_claude()` 的邏輯移入 `generate()`
- `list_models()` 回傳硬編碼的 Opus/Sonnet/Haiku 列表
- `build_cli_args()` 只需 `["--model", model_id]`

**`opencode_engine.py`**
- 把 `OpenCodeClient` 和 `ensure_opencode_server()` 包進來
- `generate()` 呼叫 `_call_opencode_with_thinking()`
- `list_models()` 向 opencode `/provider` API 查詢
- `build_cli_args()` 回傳 `["--opencode-model", model_id, "--model", model_id]`
- `startup()` 呼叫 `ensure_opencode_server()`

### 4. `engines/__init__.py` — 工廠函式

```python
from .claude_engine import ClaudeEngine
from .opencode_engine import OpenCodeEngine

_REGISTRY: dict[str, type] = {
    "claude": ClaudeEngine,
    "opencode": OpenCodeEngine,
}

def get_engine(name: str, **kwargs) -> EngineBackend:
    if name not in _REGISTRY:
        raise ValueError(f"Unknown engine: {name}")
    return _REGISTRY[name](**kwargs)

def list_engine_names() -> list[str]:
    return list(_REGISTRY.keys())
```

---

## 各檔案修改量估算

### `bridge.py`

**重構前（現況）：**
```python
# __init__
if engine == "opencode":
    self._opencode = OpenCodeClient(...)

# run() — 呼叫 AI
if self.engine == "opencode":
    reply = await self._call_opencode_with_thinking(prompt_str)
else:
    reply, actual_model = await self._call_claude(prompt)

# run() — 重試
if self.engine == "opencode":
    reply = await self._call_opencode_with_thinking(prompt_str)
else:
    reply, _ = await self._call_claude(prompt)
```

**重構後：**
```python
# __init__
from engines import get_engine
self._engine = get_engine(engine, model=model, name=name, ...)
await self._engine.startup()

# run() — 呼叫 AI（統一介面）
reply, actual_model = await self._engine.generate(prompt, on_thinking=self._send_agent_thinking)

# run() — 重試（完全一致，無需特例）
reply, _ = await self._engine.generate(prompt, on_thinking=self._send_agent_thinking)
```

修改量：移除約 40 行分支程式碼，`AgentBridge` 本身縮減至不含任何 engine 判斷。

---

### `server.py`

**重構前：**
```python
if engine == "opencode":
    cmd += ["--opencode-model", model_id, "--model", model_id]
else:
    claude_model = model_id.split("/", 1)[-1] if "/" in model_id else model_id
    cmd += ["--model", claude_model]
```

**重構後：**
```python
from engines import get_engine
extra_args = get_engine(engine).build_cli_args(model_id)
cmd += extra_args
```

修改量：約 6 行換 2 行。

---

### `openparty_tui.py`

**重構前（`_fetch_models()`）：**
```python
if "opencode" in available_engines:
    # 50 行：HTTP 呼叫 + 解析 provider/model
if "claude" in available_engines:
    # 15 行：硬編碼三個模型
```

**重構後：**
```python
from engines import get_engine
result = []
for engine_name in available_engines:
    eng = get_engine(engine_name)
    result.extend(await eng.list_models())
return result
```

修改量：函式從約 70 行縮至約 10 行；新增 engine 時 TUI 完全不需要修改。

---

## 新增 Engine 的流程（重構後）

1. 建立 `engines/new_engine.py`，繼承 `EngineBackend`，實作四個抽象方法
2. 在 `engines/__init__.py` 的 `_REGISTRY` 加一行：`"new": NewEngine`
3. **不需要動** `server.py`、`bridge.py`、`openparty_tui.py`

---

## 遷移策略（分階段，不影響現有功能）

| 階段 | 工作內容 | 風險 |
|------|---------|------|
| Phase 1 | 建立 `engines/` 模組，移植現有兩個 engine 的邏輯 | 低（只是搬移，不改行為） |
| Phase 2 | 替換 `bridge.py` 的 engine 分支為統一介面 | 中（需測試 claude + opencode 各自的思考串流） |
| Phase 3 | 替換 `server.py` 的 CLI 參數組裝 | 低 |
| Phase 4 | 替換 `openparty_tui.py` 的 `_fetch_models()` | 低 |
| Phase 5 | 移除 `bridge.py` 中的 `OpenCodeClient`、`ensure_opencode_server()` | 清理死碼 |

每個階段可獨立 commit，出問題時可逐步回退。

---

## 補充：openparty_join.py

`openparty_join.py` 也有一處 `if engine == "opencode"` 分支（line 206），
遷移時一併納入 Phase 3 處理。

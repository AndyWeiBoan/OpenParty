# Textual 遷移計畫

## 背景

`observer_cli.py` 目前使用原生 `curses` 手刻整個 TUI。共 1,294 行，其中約 30–40% 是在補 curses 不足（resize 處理、scrollbar、color pair 管理、asyncio 橋接等），與業務邏輯無關。

遷移到 [Textual](https://github.com/Textualize/textual) 的主要動機：
- 消除 curses window 生命週期管理（resize 閃爍、游標跳位等已知 bug）
- 原生 asyncio 支援，簡化 WebSocket 整合
- Rich markup 取代手刻 inline renderer
- 自動 layout / scrollbar / SIGWINCH，可刪掉大量樣板代碼

---

## 現有架構摘要

### UI 區塊

```
┌─────────────────────────────────────┐
│           ChatLog (scroll)          │  h-2 rows，hand-rendered lines
├─────────────────────────────────────┤
│           SepBar (status)           │  1 row，owner hint / room info
├─────────────────────────────────────┤
│           InputBar (multi-line)     │  1–5 rows，Alt+Enter 換行
└─────────────────────────────────────┘
         ↑ floating popups
   CompletionPopup   ModelPickerModal
```

### 關鍵狀態

| 狀態 | 類型 | 說明 |
|------|------|------|
| `lines` | `list[tuple[str, int]]` | 所有顯示行 + color pair |
| `scroll_offset` | `int` | 0 = 最新，正值 = 往上捲 |
| `sel_start/sel_end` | `int \| None` | 滑鼠選取行範圍 |
| `completing` | `bool` | 是否顯示 completion popup |
| `picker_mode` | `str` | `""` / `"model"` / `"kick"` |
| `agents` | `list[dict]` | 目前房間的 agent 列表 |

### asyncio 整合方式（現在）

```
asyncio.gather(
    recv_loop(ws),      # 把 WS 訊息推進 asyncio.Queue
    ui_loop(stdscr, ws) # 每 50ms 輪詢 Queue + 讀鍵盤
)
```

問題：`ui_loop` 靠 `asyncio.sleep(0.05)` 輪詢，不是事件驅動。

---

## 遷移後架構設計

### App 結構

```python
class OpenPartyApp(App):
    CSS_PATH = "openparty.tcss"

    def compose(self):
        yield ChatLog(id="chat")
        yield SepBar(id="sep")
        yield MessageInput(id="input")

    async def on_mount(self):
        asyncio.create_task(self.recv_loop())
```

### Widget 對應表

| 現有 curses 元件 | Textual Widget | 備註 |
|-----------------|----------------|------|
| `chat_win` | `RichLog` 或自訂 `ChatLog(ScrollableContainer)` | 保留 `_parse_inline` 邏輯，輸出 Rich `Text` 物件 |
| `sep_win` | `Static` (id=`sep`) | 動態更新 `.update()` |
| `input_win` | `TextArea` 或自訂 `MessageInput` | 需支援 Alt+Enter 換行、cursor 位置 |
| `completion_win` | 自訂 `CompletionWidget` | 浮動於 InputBar 上方，`OptionList` 為基底 |
| `model_win` | `ModalScreen` + `Input` + `ListView` | /add-agent picker |
| kick picker | `ModalScreen` + `ListView` | /kick picker |

### 訊息流（遷移後）

```python
# recv_loop 直接 post Textual message，不再用 Queue
async def recv_loop(self):
    async for raw in self.ws:
        msg = json.loads(raw)
        self.post_message(ServerMessage(msg))

# Widget 用 on_server_message 處理
def on_server_message(self, event: ServerMessage):
    self.process_event(event.data)
```

---

## 難度分類

### 高難度（需要重寫）

**Mouse 文字選取 + Ctrl+C 複製**
- 現在：`BUTTON1_PRESSED/RELEASED` → `sel_start/sel_end` → `A_REVERSE` 高亮 → `pbcopy`
- Textual：無直接對應，需在 `ChatLog` 的 `on_mouse_down/up` 自己維護行索引選取
- 建議：先跳過，後續作為獨立 feature 補上

**Multi-line Input**
- 現在：`input_win` 動態高度（1–5 行），Alt+Enter 插入 `\n`
- Textual：`TextArea` 支援多行，但 Alt+Enter binding 和 submit on Enter 需自訂

### 中等難度（需要客製）

**Completion Popup（`#` / `@` / `/command`）**
- 現在：浮動 curses window，位置計算於 sep_win 上方
- Textual：自訂 `CompletionWidget`，用 `OptionList`，overlay 於 `InputBar` 上

**Inline Markup Renderer（`_parse_inline`）**
- 現在：回傳 `(text, curses_attr)` list，逐字元寫入
- Textual：改輸出 Rich `Text` 物件，append segments 即可
- `**bold**` → `Text.append(t, style="bold")`
- `#name` → `Text.append(t, style="cyan bold")`
- `@name` → `Text.append(t, style="yellow bold")`

**Agent 顏色輪轉**
- 現在：`agent_color_map: dict[str, int]` → `PAIR_AGENT_0..4`
- Textual：改為 `AGENT_STYLES = ["cyan", "yellow", "green", "magenta", "blue"]`

### 低難度（幾乎直接搬）

| 功能 | 說明 |
|------|------|
| WebSocket 連線 | 無需改動，async with websockets |
| 訊息結構解析（`handle_event`） | 邏輯不變，只改 render 呼叫 |
| `/command` 處理 | 邏輯不變，改用 `on_key` |
| Owner 顏色（`PAIR_OWNER`） | 改 `Style("black on white")` |
| SIGWINCH resize | Textual 自動處理，刪掉 |
| scrollbar | Textual 自動提供，刪掉手寫版 |
| word-wrap | Textual + Rich 內建，`unicode_wrap()` 可刪 |

---

## 可刪除的代碼（遷移後）

| 函數 / 區塊 | 行數 | 原因 |
|------------|------|------|
| `display_width()` | 10 | Rich 內建 CJK 寬度 |
| `truncate_to_display_width()` | 15 | 不再需要手動截斷 |
| `truncate_head()` | 15 | 同上 |
| `unicode_wrap()` | 55 | Rich / Textual 自動換行 |
| `_setup_colors()` | 20 | 改用 tcss / Rich Style |
| `_setup_windows()` | 35 | 改用 compose() |
| `resize()` | 25 | Textual 自動 |
| `_update_input_layout()` | 20 | Textual layout 自動 |
| `_redraw_sep()` | 10 | `Static.update()` |
| scrollbar render 邏輯 | 20 | Textual 自動 |
| SIGWINCH signal handler | 15 | Textual 自動 |
| `asyncio.sleep(0.05)` 輪詢 | 5 | 改事件驅動 |
| `message_queue` 全域 | 5 | 改 `post_message()` |
| **小計** | **~250 行** | |

---

## 遷移步驟

### Phase 1 — 骨架（預估 1 天）

- [ ] `pip install textual`
- [ ] 建立 `openparty_tui.py`（新檔，不動舊檔）
- [ ] 實作 `OpenPartyApp(App)`：`ChatLog` + `SepBar` + `MessageInput`
- [ ] 基本 CSS layout（`openparty.tcss`）
- [ ] WebSocket 連線 + `recv_loop` 用 `post_message`
- [ ] `handle_event` 邏輯搬入，最簡單的訊息顯示跑通

```tcss
/* openparty.tcss */
ChatLog {
    height: 1fr;
    border: none;
}
SepBar {
    height: 1;
    background: white;
    color: black;
}
MessageInput {
    height: auto;
    max-height: 5;
}
```

### Phase 2 — 訊息渲染（預估 1 天）

- [ ] 移植 `_parse_inline` → 輸出 Rich `Text`
- [ ] `_print_message` 改用 `RichLog.write(Text)`
- [ ] Agent 顏色輪轉（`AGENT_STYLES` list）
- [ ] Owner 訊息白底黑字
- [ ] 私訊 `【私訊 → name】` magenta 顯示
- [ ] 訊息 replay（history）

### Phase 3 — Completion + Picker（預估 1 天）

- [ ] `CompletionWidget`：`#` / `@` / `/command` 建議選單
  - 監聽 `MessageInput` 的 `on_key` / `on_change`
  - 顯示 / 隱藏浮動 widget
- [ ] `ModelPickerModal(ModalScreen)`：搜尋 + 過濾 + 選擇
- [ ] `KickPickerModal(ModalScreen)`：agent 列表
- [ ] 所有 `/command` 處理（`/leave`、`/add-agent`、`/kick`、`/kick-all`、`/broadcast`）

### Phase 4 — Edge Cases（預估 1–2 天）

- [ ] Alt+Enter 多行輸入（自訂 `MessageInput` binding）
- [ ] Mouse 選取（`on_mouse_down/up`，行索引 → highlight → pbcopy）
  - 可改用 `pyperclip` 取代 `subprocess pbcopy`，跨平台
- [ ] CJK 換行精度驗證（跑一批中文 / 日文訊息確認對齊）
- [ ] macOS mouse wheel（確認 Textual 在 macOS Terminal.app / iTerm2 下正常）
- [ ] 極小 terminal 尺寸容錯

### Phase 5 — 收尾（預估 0.5 天）

- [ ] 刪除舊 curses import 與相關代碼
- [ ] 整合測試：多 agent、私訊、broadcast、owner 踢人
- [ ] 確認 `observer_cli.py` 入口點切換（或保留兩版讓使用者選）

---

## 時程估計

| Phase | 工作 | 天數 |
|-------|------|------|
| 1 | 骨架 + 連線 + 基本顯示 | 1 |
| 2 | 完整訊息渲染 | 1 |
| 3 | Completion + Picker + Commands | 1 |
| 4 | Edge cases（mouse 選取最耗時） | 1–2 |
| 5 | 收尾 + 整合測試 | 0.5 |
| **合計** | | **4.5–5.5 天** |

---

## 風險與緩解

| 風險 | 機率 | 緩解方式 |
|------|------|----------|
| CJK 換行與現有行為不一致 | 中 | Phase 2 早期測試，必要時保留 `unicode_wrap()` |
| Mouse 選取難以完整復現 | 高 | 先上線無選取版，mouse 選取作為後續 issue |
| Textual 版本 API 變動 | 低 | 鎖定版本（`textual>=1.0`） |
| macOS Terminal mouse 相容性 | 中 | 測試 Terminal.app + iTerm2 + Ghostty |

---

## 依賴

```toml
# 新增到 pyproject.toml 或 requirements.txt
textual>=1.0.0
pyperclip>=1.9.0   # 取代 subprocess pbcopy，跨平台 clipboard
```

---

## 參考

- [Textual 官方文件](https://textual.textualize.io/)
- [RichLog widget](https://textual.textualize.io/widgets/rich_log/)
- [ModalScreen](https://textual.textualize.io/guide/screens/#modal-screens)
- [TextArea](https://textual.textualize.io/widgets/text_area/)

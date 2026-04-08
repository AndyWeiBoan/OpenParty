# 功能規劃：滑鼠拖曳選取文字自動複製並跳通知

## 決議摘要

在 `openparty_tui.py` 的 `ChatLog` widget（繼承自 Textual `RichLog`）內，**自行實作 mouse selection state**，使使用者能以左鍵拖曳選取聊天記錄中的任意文字，放開後自動複製到剪貼簿並顯示 toast 通知。

**不換框架、不動 server/bridge、不動 `observer_cli.py`。**

---

## 技術決策

| 項目 | 決定 | 理由 |
|------|------|------|
| 框架 | 保留 Textual | 換框架等同重寫 2314 行，代價過高 |
| Chat widget | 保留 `RichLog`（`ChatLog` 子類別） | `TextArea` 不支援 Rich markup / ANSI 顏色，遷移後樣式全失 |
| 文字選取 | 在 `ChatLog` 自行實作 | Textual `RichLog` 尚未支援 selection（issue #5334，無 ETA） |
| 剪貼簿 | `self.app.copy_to_clipboard(text)` | Textual 內建 OSC 52，跨平台（macOS/Linux/Windows/SSH）|
| macOS Terminal.app fallback | `pyperclip.copy(text)` | Terminal.app 不支援 OSC 52 |
| 通知 | `self.app.notify("✓ 已複製")` | Textual overlay toast，不觸發 ChatLog 重繪，零閃爍 |
| 實作參考 | `observer_cli.py` 的選取邏輯 | curses 版已驗證可行，演算法相同，座標系需調整 |
| server 重啟 | 不需要 | 純 TUI 前端修改，與 server.py / bridge.py 零關聯 |

---

## TODO 清單（含實作細節）

### Phase 1：建立 `_rendered_lines` 映射表

**目標**：讓 `ChatLog` 知道每個 visual row 對應哪條訊息的哪段原始文字。

- [ ] 在 `ChatLog.__init__` 新增實例變數：
  ```python
  self._rendered_lines: list[tuple[int, int, str]] = []
  # 每個元素：(message_index, char_offset_start, plain_text_of_this_visual_row)
  ```
- [ ] 在 `write_msg()` 寫入 `RichLog` 的同時，同步將訊息的 plain text（strip Rich markup）依 soft-wrap 寬度切成 visual row，append 到 `_rendered_lines`。
- [ ] **注意 word-wrap**：`RichLog` 預設啟用 soft-wrap，同一條訊息可能跨多個 visual row。須用 `self.size.width`（或 `self.content_size.width`）計算每行寬度，模擬 wrap 行為。
- [ ] **注意 scroll offset**：點擊/拖曳的 `event.y` 是相對於 widget 頂部的座標，對應到 `_rendered_lines` 的 index 為 `int(self.scroll_y) + event.y`。
- [ ] 實作 `on_resize(event)` handler：視窗大小改變時必須清空並重建 `_rendered_lines`（因為 soft-wrap 寬度改變），重建時若訊息數量龐大，應只重算可見區域 ± N 行的 buffer（lazy rebuild），避免全量重算造成 UI freeze。

---

### Phase 2：實作 mouse selection state

**參考**：`observer_cli.py` 的 `BUTTON1_PRESSED`/`BUTTON1_RELEASED` 處理邏輯（約第 964–1013 行）。

- [ ] 在 `ChatLog` 新增狀態變數：
  ```python
  self._sel_start: tuple[int, int] | None = None  # (visual_row, col)
  self._sel_end: tuple[int, int] | None = None
  self._selecting: bool = False
  ```
- [ ] 實作 `on_mouse_down(event: events.MouseDown)`：
  ```python
  if event.button == 1:
      row = int(self.scroll_y) + event.y
      self._sel_start = (row, event.x)
      self._sel_end = (row, event.x)
      self._selecting = True
      self.capture_mouse()  # 確保拖曳到 widget 外仍收到事件
  ```
- [ ] 實作 `on_mouse_move(event: events.MouseMove)`：
  ```python
  if self._selecting:
      row = int(self.scroll_y) + event.y
      self._sel_end = (row, event.x)
      self.refresh()  # 觸發重繪以顯示選取高亮
  ```
- [ ] 實作 `on_mouse_up(event: events.MouseUp)`：
  ```python
  if event.button == 1 and self._selecting:
      self._selecting = False
      self.release_mouse()
      selected = self._get_selected_text()
      if selected:
          self._copy_and_notify(selected)
      # 不立即清除 sel_start/sel_end，讓使用者看到選取範圍
  ```
- [ ] 實作 `on_blur()` safety release：若拖曳中途 widget 失去焦點（如 modal 彈出、WebSocket 斷線），`mouse_up` 可能永遠不觸發導致 TUI 卡死，需在 `on_blur` 裡強制釋放：
  ```python
  def on_blur(self) -> None:
      if self._selecting:
          self._selecting = False
          self.release_mouse()
  ```

---

### Phase 3：實作 `_get_selected_text()`

- [ ] 實作 `_cell_to_char_index(text: str, cell_col: int) -> int`：將終端機 visual column 座標轉換為 Python string index，**必須使用 `wcwidth` 處理 CJK 寬字元**（中文字佔 2 cell 但只佔 1 char index）：
  ```python
  def _cell_to_char_index(self, text: str, cell_col: int) -> int:
      from wcwidth import wcwidth
      col = 0
      for i, ch in enumerate(text):
          if col >= cell_col:
              return i
          col += max(wcwidth(ch), 1)
      return len(text)
  ```
  若未安裝 `wcwidth`，在 `requirements.txt` 補上 `wcwidth>=0.2`。

- [ ] 根據 `_sel_start` 和 `_sel_end`（已正規化為 min/max），從 `_rendered_lines` 取出對應的 plain text 片段（**column 必須先經 `_cell_to_char_index` 轉換**）：
  ```python
  def _get_selected_text(self) -> str:
      if self._sel_start is None or self._sel_end is None:
          return ""
      r0, c0 = min(self._sel_start, self._sel_end)
      r1, c1 = max(self._sel_start, self._sel_end)
      if r0 == r1:
          line_text = self._get_visual_row_text(r0)
          i0 = self._cell_to_char_index(line_text, c0)
          i1 = self._cell_to_char_index(line_text, c1)
          return line_text[i0:i1]
      else:
          parts = []
          row0_text = self._get_visual_row_text(r0)
          parts.append(row0_text[self._cell_to_char_index(row0_text, c0):])
          for r in range(r0 + 1, r1):
              parts.append(self._get_visual_row_text(r))
          row1_text = self._get_visual_row_text(r1)
          parts.append(row1_text[:self._cell_to_char_index(row1_text, c1)])
          return "\n".join(parts)
  ```
- [ ] `_get_visual_row_text(row: int) -> str`：安全查詢 `_rendered_lines`，超出範圍回傳空字串。

---

### Phase 4：實作 `_copy_and_notify()`

- [ ] 跨平台剪貼簿寫入：
  ```python
  def _copy_and_notify(self, text: str) -> None:
      # 優先嘗試 OSC 52（跨平台、SSH 友善），失敗時 fallback pyperclip
      try:
          self.app.copy_to_clipboard(text)  # 走 OSC 52
      except Exception:
          pass
      # pyperclip fallback：處理不支援 OSC 52 的終端（Apple Terminal、舊版 GNOME Terminal 等）
      try:
          import pyperclip
          pyperclip.copy(text)
      except ImportError:
          self.app.notify("⚠ 複製失敗：請安裝 pyperclip（pip install pyperclip）", severity="warning", timeout=4)
          return
      except Exception:
          self.app.notify("⚠ 複製失敗：剪貼簿寫入錯誤", severity="warning", timeout=4)
          return
      self.app.notify("✓ 已複製", timeout=2)
  ```
- [ ] 若要加 `pyperclip`，在 `requirements.txt` 補上 `pyperclip>=1.8`（optional dependency）。

---

### Phase 5：選取高亮渲染（可選，增強 UX）

- [ ] 覆寫 `ChatLog.render_line(y: int)` 或透過 `on_mouse_move` 的 `refresh()` 加上選取範圍的反白樣式。
- [ ] **閃爍風險警告**：`on_mouse_move` 高頻觸發時若每次都 `refresh()` 可能造成閃爍，建議用 `set_interval` 節流（throttle）或只在座標變化時才觸發重繪。
- [ ] 若閃爍問題嚴重，**可先省略高亮渲染**，只在 `mouse_up` 時複製 + 通知，UX 降級為「拖曳後複製但無視覺反饋」，之後再補高亮。

---

## 注意事項

1. **座標系差異（與 curses 版最大的不同）**：
   - curses 直接使用 terminal cell 座標，無需處理 scroll offset。
   - Textual `RichLog` 的 `event.y` 是相對於 widget 可見區域頂部，需加上 `int(self.scroll_y)` 才能對應到 `_rendered_lines` 的絕對 index。
   - `RichLog` 的 soft-wrap 會讓一條邏輯訊息被切成多個 visual row，`_rendered_lines` 必須按 visual row 建立，而非按訊息建立。

2. **`_rendered_lines` 同步問題**：
   - 聊天室在串流中會頻繁呼叫 `write_msg()`，`_rendered_lines` 必須在每次 `write_msg()` 後即時更新，且要考慮 terminal 視窗大小改變（`on_resize`）時需要重新計算 wrap。

3. **`capture_mouse()` / `release_mouse()`**：
   - 必須在 `mouse_down` 時呼叫 `capture_mouse()`，確保拖曳超出 widget 邊界時仍能收到 `mouse_move` 和 `mouse_up` 事件。
   - `release_mouse()` 必須在 `mouse_up` 呼叫，否則後續點擊其他 widget 會失效。

4. **macOS Terminal.app 的 OSC 52 限制**：
   - Apple 內建 Terminal.app 預設封鎖 OSC 52，必須 fallback 到 `pyperclip`。
   - iTerm2、kitty、Alacritty、WezTerm、Windows Terminal 均支援 OSC 52。

5. **不需要重開 server**：
   - 所有改動限於 `openparty_tui.py` 的 `ChatLog` class，為純前端 UI 邏輯。
   - 改完重啟 TUI 進程即可，`server.py`、`bridge.py`、所有 agent bridge 進程不受影響。

6. **CJK 寬字元（中文）的 column offset 問題**：
   - 終端機的 `event.x` 是 visual cell column，中文字佔 2 cell 但 Python string index 只算 1。
   - 若直接用 `line_text[c0:c1]` 做切片，所有含中文的選取都會偏移，必須使用 `wcwidth` 轉換。
   - 相關依賴：`wcwidth>=0.2`（加入 `requirements.txt`）。

7. **`capture_mouse()` 卡死風險**：
   - 若拖曳中途 modal 彈出或 widget 失去焦點，`mouse_up` 可能不觸發，TUI 會永久鎖定在 capture 狀態。
   - 解法：在 `on_blur()` 強制呼叫 `release_mouse()`（已列入 Phase 2 TODO）。

8. **工程量估計**：約 180–220 行（含 Phase 1–4 + wcwidth 轉換 + on_blur safety），Phase 5 高亮渲染視複雜度另計。

---

## 參考資料

- `observer_cli.py`：curses 版選取邏輯參考（`_get_selected_text()`、`copy_selection()`、`BUTTON1_PRESSED/RELEASED` 處理）
- Textual GitHub Issue #5334：RichLog selection feature request
- OpenCode `packages/opencode/src/cli/cmd/tui/util/selection.ts`：OpenTUI 的 `getSelection()` 實作參考
- Textual API：`App.copy_to_clipboard()`、`App.notify()`、`Widget.capture_mouse()`

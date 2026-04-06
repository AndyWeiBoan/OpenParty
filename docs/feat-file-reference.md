# 功能：檔案引用（`@` 觸發）

## 概述
在輸入框中實作 `@` 檔案引用功能——輸入 `@` 後開啟檔案選擇/自動完成清單，讓使用者在訊息中引用檔案。成員標記從 `@` 遷移至 `$`。

## 符號慣例變更
- `@` → **檔案引用**（國際慣例，如 opencode、Cursor、GitHub）
- `$` → **成員/代理標記**（原本使用 `@`）
- `#` → 維持不變（頻道/主題引用）

## 待辦清單

### 階段一：將成員標記從 `@` 遷移至 `$`
- [ ] 更新 `_update_completion()` 中的正則表達式（約第 923 行）：將 `([#@])` 改為 `([#$])`
- [ ] 更新 `_completion_enter()` 中的替換正則（約第 961 行）：將 `[#@]` 改為 `[#$]`
- [ ] 驗證 `$agent_name` 標記功能端對端正常運作
- [ ] 確認 `@` 不再觸發成員自動完成

### 階段二：檔案搜尋後端
- [ ] 新增檔案搜尋工具函式（例如 `_search_files(query: str) -> list[tuple[str, str]]`）
  - 使用 `pathlib.Path.glob()` 或 `os.scandir()` 進行目錄遍歷
  - 支援檔名模糊比對（考慮使用 `fnmatch` 或簡單子字串比對）
  - 回傳 `(顯示名稱, 完整路徑)` 元組清單
  - 遵守 `.gitignore` 規則（選配，有更好）
  - 限制結果數量（例如最多 20 筆）以確保效能
- [ ] 定義工作目錄上下文（專案根目錄 / cwd）

### 階段三：將 `@` 觸發接入自動完成管線
- [ ] **修正多行偵測邏輯**：將 `on_text_area_changed` 中取最後一行（`buf.split("\n")[-1]`）改為取**游標所在行、游標左側文字**：
  ```python
  row, col = inp.cursor_location
  current_line = inp.text.split("\n")[row][:col]  # 只看游標左側
  self._update_completion(current_line)
  ```
  這樣 `@` 在任意行都能正確觸發，且不會被游標右側的文字干擾
- [ ] 在 `_update_completion()` 中新增 `@` 檔案引用的正則分支：`r"@([^\s]*)$"`
- [ ] 偵測到 `@` 時，呼叫 `_search_files(partial)` 取得匹配的檔案
- [ ] 設定 `_completing_type = "file"` 以區分「mention」和「command」
- [ ] 將檔案結果傳入 `CompletionList.show_items()`

### 階段四：CompletionList 檔案顯示介面
- [ ] 更新 `CompletionList._refresh()` 以處理 `completing_type == "file"`
  - 顯示檔名 + 相對路徑（例如 `README.md  ./docs/README.md`）
  - 考慮用圖示/前綴區分檔案與目錄（例如 `📄` / `📁`）
- [ ] 確認上/下/Tab/Enter 導航對檔案項目正常運作

### 階段五：檔案選取與插入
- [ ] **修正多行替換邏輯**：`_completion_enter` 的 `re.sub` 改為只替換**游標所在行**，再拼回完整文字，避免誤替換其他行的 `@` 片段
- [ ] 選取檔案後（Enter/Tab），將輸入中的 `@partial` 替換為 `@完整/路徑`
- [ ] 將引用的檔案路徑儲存到訊息 metadata 中供下游處理
- [ ] 決定格式：`@相對路徑` vs `@絕對路徑`（建議使用相對路徑）

### 階段六：測試與打磨
- [ ] 測試深層巢狀目錄
- [ ] 測試含空格或特殊字元的檔名
- [ ] 測試大型 repo 的效能（1000+ 檔案）
- [ ] 測試與現有 `$` 成員標記的互動（確認無衝突）
- [ ] 測試多行輸入：`@` 引用在任意行都能運作，不僅限最後一行

## 參考：opencode 的做法
- 觸發：在游標位置用正則 `/@(\S*)$/` 偵測
- 搜尋：`files.searchFilesAndDirectories(query)` 模糊比對
- 顯示：彈出選單，結果分組（代理、最近檔案、搜尋結果）
- 插入：將 `@query` 替換為不可編輯的 pill 元素（DOM 限定，不適用於 TUI）

## 需要修改的關鍵檔案
- `/Users/andy/3rd-party/OpenParty/openparty_tui.py`
  - `_update_completion()`（約第 919 行）
  - `_completion_enter()`（約第 961 行）
  - `CompletionList` 類別（約第 266 行）
  - `on_text_area_changed()`（約第 898 行）

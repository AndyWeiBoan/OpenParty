# 功能：圖片貼上支援

## 概述

允許使用者在聊天輸入框中直接貼上圖片（截圖、剪貼簿圖片）。  
圖片落盤至 `/tmp/openparty/images/<session_id>/`，由 **server 端**統一讀取並 base64 編碼後，以 Anthropic image content block 形式傳給 agent。

## 架構流程

```
使用者 Ctrl+V（圖片）
  → TUI 偵測貼上事件 → 存為 /tmp/openparty/images/<session_id>/<uuid>.<ext>
  → payload["images"] = [{"path": "...", "mime": "image/png"}]
  → WebSocket → server.py
  → server 讀取檔案 → base64 encode → 組成 Anthropic image content block
  → agent 收到包含圖片的 messages，直接使用（無需額外 Read tool）
```

## 待辦清單

### 階段一：TUI 偵測與存檔

- [x] 在 `openparty_tui.py` 的輸入元件中覆寫 `on_paste` 事件（Textual 內建，跨平台自動處理 macOS `Cmd+V` / Linux `Ctrl+V`，無需個別攔截按鍵）
- [x] `on_paste` 觸發時，同步呼叫 OS 剪貼簿 API 檢查是否含圖片資料（**不依賴按鍵事件傳遞 binary data，終端機不會透過 keybinding 傳遞圖片 bytes**）：
  - macOS：`PIL.ImageGrab.grabclipboard()` 或 `osascript`
  - Linux X11：`xclip -selection clipboard -t image/png -o`
  - Linux Wayland：`wl-paste --type image/png`
- [x] 建立目錄 `/tmp/openparty/images/<session_id>/`（`os.makedirs(..., exist_ok=True)`）
- [x] 將圖片存為 `<uuid>.<ext>`（ext 由壓縮 pipeline 決定：無 alpha → `.jpg`，有 alpha → `.webp`）
- [ ] 在輸入區顯示圖片 chip，例如 `[🖼 image-1.png ✕]`（目前只在 chat log 顯示訊息）
- [ ] 支援傳送前刪除已附加圖片（點擊 ✕ 或 Backspace）
- [x] 支援單一訊息附加多張圖片

### 階段二：Payload 擴充

- [x] 在 `_handle_send()`（約第 1230 行）中，傳送前收集 pending image 清單
- [x] 擴充訊息 payload，新增 `images` 欄位：
  ```python
  payload = {
      "type": "message",
      "content": text,
      "images": [
          {"path": "/tmp/openparty/images/<session_id>/<uuid>.<ext>", "mime": "image/jpeg"}
          # ext is determined by _save_clipboard_image(): .jpg (no alpha) or .webp (alpha)
      ]
  }
  ```
- [x] 傳送成功後清空 pending image 清單

### 階段三：Server 處理並轉發圖片

- [x] 在 `server.py` 中，從 owner 訊息提取 `images` 欄位
- [x] 將 image metadata 存入 `room.history` entry（與 `files` 欄位並列）
- [x] 建構 agent turn 時，**由 server 統一**讀取圖片並 base64 編碼（`_build_image_blocks_from_history()`），結果以 `image_blocks` 欄位附於 `your_turn_payload`；bridge 的 `build_prompt()` 若偵測到 `image_blocks` 則回傳 content block list，由 `_call_claude()` 以 streaming AsyncIterable 格式傳給 SDK
- [x] 若圖片檔案不存在（已被清理），略過並記錄警告，不中斷傳送

### 階段四：邊緣情況與驗證

- [x] 圖片大小上限 5 MB（Anthropic API 限制），超過則拒絕存檔並顯示錯誤提示
- [x] 壓縮 pipeline（Pillow）在存檔前重新編碼，格式由程式碼決定，magic bytes 驗證在 Pillow 讀取時自動發生
- [ ] 支援貼上圖片 URL（純文字以 `http` 開頭且副檔名為圖片格式）——選配：自動抓取並嵌入

### 階段五：測試與清理

- [x] session 結束 / 房間關閉時，`on_unmount()` 刪除 `/tmp/openparty/images/<session_id>/` 目錄
- [x] 新增單元測試 `tests/test_image_paste.py`（16 tests，全 pass），覆蓋：payload 建構、base64 編碼、檔案不存在 fallback
- [x] 測試多張圖片同時傳送
- [x] 測試不同 MIME type（jpeg / webp）

## 圖片壓縮策略（multi-agent 討論共識）

### 為何需要 resize

- Claude SDK（`anthropic` Python library）直接把 base64 塞進 JSON request body，**不做任何自動 resize**，傳多大就送多大
- opencode 同樣是 in-memory base64 直送，也不做 resize
- Retina 截圖（2880×1800）等高解析度圖片動輒 3–8 MB，會：
  1. 輕易觸發 5 MB 上限
  2. 大幅增加 WebSocket 傳輸負擔
  3. 造成不必要的 token 費用（Anthropic 按像素計費，超過 1568px 長邊後視覺理解幾乎無收益）
- **不應依賴 API side-effect 行為**（如 API 端自動縮圖），應自行在存檔前控制

### 壓縮 Pipeline（實作於 `_save_clipboard_image()`）

函式接受目錄 + uuid，**自行決定副檔名**，回傳最終路徑與 MIME type，避免格式與副檔名不一致的 bug：

```python
import os
import uuid as uuid_mod
from PIL import Image

def _save_clipboard_image(img: Image.Image, save_dir: str, name: str) -> tuple[str, str]:
    """
    回傳 (final_path, mime_type)
    caller 不應預設副檔名，由本函式根據格式決定。
    """
    # 1. Resize：長邊等比縮放至 1568px
    img.thumbnail((1568, 1568), Image.LANCZOS)

    # 2. 判斷是否有透明通道
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)

    if has_alpha:
        # 保留透明度 → WebP
        final_path = os.path.join(save_dir, f"{name}.webp")
        img.save(final_path, "WEBP", quality=85)
        return final_path, "image/webp"
    else:
        # 無透明度 → JPEG，體積縮 5–10x
        if img.mode != "RGB":
            img = img.convert("RGB")
        final_path = os.path.join(save_dir, f"{name}.jpg")
        img.save(final_path, "JPEG", quality=85, optimize=True)
        return final_path, "image/jpeg"
```

**呼叫方式（在 `on_paste` 中）：**
```python
name = str(uuid_mod.uuid4())
path, mime = _save_clipboard_image(pil_img, save_dir, name)
pending_images.append({"path": path, "mime": mime})
```

### 壓縮效果預期

| 原始圖片 | resize 後 | 轉 JPEG 後 |
|----------|-----------|------------|
| Retina PNG 2880px，~4 MB | 1568px PNG，~1.5 MB | ~200–400 KB |
| 一般截圖 1920px，~2 MB | 1568px，~1.2 MB | ~150–300 KB |

95% 的截圖可壓到 500 KB 以下，5 MB 限制幾乎不會觸發。

## 參考：opencode 與 Claude Code 的做法

| 項目 | opencode | Claude Code | 我們的選擇 |
|------|----------|-------------|-----------|
| 儲存方式 | base64 in-memory | 落盤至 `/var/folders` | 落盤至 `/tmp/openparty/` |
| 傳輸格式 | JSON inline base64 data URL | 檔案路徑 | server 端讀檔 base64 encode |
| 清理機制 | session 結束自動 GC | 手動清理 | 重開機自然清除，session 結束主動刪除 |
| 前端顯示 | 圖片 thumbnail | `[Image #N]` chip | chip（TUI 限制，無法顯示縮圖） |

## 需要修改的關鍵檔案

- `/Users/andy/3rd-party/OpenParty/openparty_tui.py`
  - `_handle_send()`（約第 1230 行）：新增 image 收集與 payload 擴充
  - 輸入元件的 `on_paste` 或按鍵攔截（約第 890–930 行附近）
  - 新增 `_save_clipboard_image()` 工具函式
- `/Users/andy/3rd-party/OpenParty/server.py`
  - owner 訊息處理段落（約第 625–660 行）：提取 `images` 欄位
  - agent turn 建構段落：新增 image content block 組裝邏輯
- `/Users/andy/3rd-party/OpenParty/tests/test_image_paste.py`（新增）

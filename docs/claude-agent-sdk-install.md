# claude-agent-sdk 安裝切換指南

OpenParty 的 claude engine 依賴 `claude_agent_sdk` Python package，
其中 `_bundled/claude` 是實際執行的 CLI binary（約 190MB）。

---

## 切換到 editable install（開發用）

```bash
pip install -e ~/3rd-party/claude-agent-sdk-python
```

安裝後，`_bundled/` 目錄會是**空的**（source repo 不含 binary），
必須手動從舊的 pypi 安裝複製 binary：

```bash
cp "$(python -c 'import claude_agent_sdk; print(claude_agent_sdk.__file__)')" \
   # 不對——請改用下面的指令
```

正確做法：先確認 pypi 版的 binary 位置，再複製：

```bash
# 複製 binary 到 editable install 的 _bundled/
cp /Users/andy/.pyenv/versions/3.12.8/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude \
   ~/3rd-party/claude-agent-sdk-python/src/claude_agent_sdk/_bundled/claude
```

> 如果 pypi 版已經被覆蓋，可從其他有安裝 claude CLI 的環境取得 binary，
> 或重新安裝 pypi 版（見下方），複製完再切回 editable。

---

## 切回 pypi 版（穩定版）

```bash
pip install --force-reinstall claude-agent-sdk==0.1.56
```

或直接升到最新版：

```bash
pip install --force-reinstall claude-agent-sdk
```

---

## `_bundled/` 空的會怎樣

`server.py` 偵測 claude engine 的邏輯（`_check_claude_installed`）：

1. 從 `claude_agent_sdk.__file__` 找 `_bundled/claude`
2. 若不存在，fallback 到 `shutil.which("claude")`（找 PATH 裡的 claude binary）

如果 `_bundled/` 是空的，且 PATH 裡也找不到 `claude`，
則 `available_engines` 不含 `"claude"`，TUI 選單就不會顯示 claude engine。

---

## 確認目前用的是哪個版本

```bash
python -c "import claude_agent_sdk; print(claude_agent_sdk.__file__)"
```

- 輸出包含 `site-packages` → pypi 版
- 輸出包含 `3rd-party/claude-agent-sdk-python` → editable install

確認 binary 是否存在：

```bash
python -c "
import os, claude_agent_sdk
p = os.path.join(os.path.dirname(claude_agent_sdk.__file__), '_bundled', 'claude')
print('exists:', os.path.isfile(p))
print('path:', p)
"
```

# OpenParty Scene System — v2

> 讓 AI agent 不再是一群烏合之眾，而是有組織、有身份、有專業分工的虛擬團隊。

---

## 核心概念

```
Scene × Identity × Skill
```

| 概念 | 定義 | 作用 |
|------|------|------|
| **Scene** | 這個房間要做什麼、怎麼運作 | 設定協作規則、成員組成、允許的 Skill |
| **Identity** | 這個 Agent 在場景中是誰 | 決定角色定位、決策範圍、行為規範 |
| **Skill** | 這個 Agent 擅長什麼 | 定義專業能力，可跨場景共用 |

---

## 檔案系統

所有定義以 markdown 檔案儲存，路徑遵循 XDG Base Directory 規範：

```
~/.config/openparty/
  scenes/
    {scene-name}/
      SCENE.md              # 場景說明，送給所有 Agent
      IDENTITY-{role}.md    # 身份說明，只送給被指派的 Agent
  skills/
    {skill-name}/
      SKILL.md              # 技能說明，Agent 按需自行讀取
```

### 識別規則

- **Scene ID** = 資料夾名稱，如 `dev-team`、`army-platoon`
- **Identity 檔案** = `IDENTITY-{role}.md`，如 `IDENTITY-leader.md`、`IDENTITY-security.md`
- **Skill ID** = 資料夾名稱，如 `security-audit`、`python-expert`

---

## System Prompt 注入機制

Agent 加入 Room 時，bridge 從檔案系統讀取並組合 system prompt：

```
[SCENE.md 內容]
[IDENTITY-{role}.md 內容]
[Allowed Skills 清單]
```

這個組合作為 `ClaudeAgentOptions(system_prompt=...)` 注入——是真正的 **system prompt**，不是每輪的 user message。

> **已確認**：Claude Agent SDK 支援自訂 system_prompt（直接傳字串即可）。
> **已確認**：OpenCode engine 的 `POST /session/:id/message` 接受 `system` 參數，可以注入 system prompt。目前 `OpenCodeClient.call()` 尚未傳入此欄位，需要修改 bridge.py 加入支援。

### Scene 是 Room 層屬性

Scene 的設定屬於 Room，不屬於個別 Agent。流程如下：

1. **Owner 建立 Room** 後，在 Observer CLI 設定 Scene（如 `dev-team`）
2. **Scene 在第一個 Agent 發言後鎖定**，之前 Owner 可修改
3. **Agent 加入 Room** 時，server 根據 Scene 定義分配 Identity
4. **Server 讀取**對應的 `SCENE.md` + `IDENTITY-{role}.md`，在第一個 `your_turn` payload 裡附上 system prompt
5. **Bridge 用收到的 system prompt** 初始化 `ClaudeAgentOptions(system_prompt=...)`

這樣 Agent 不需要在啟動時知道自己的 Identity，由 server 統一管理。

---

## SCENE.md 格式

SCENE.md 是純 markdown，內容直接成為 system prompt 的一部分（所有 Agent 都看到）：

```markdown
# {場景名稱}

{場景描述——這個任務是什麼、目標是什麼}

## 協作模式

{說明成員如何互動、決策如何產生}

## 成員組成

- **{role-id}**：{職責摘要}
- **{role-id}**：{職責摘要}

## Allowed Skills

- {skill-id}
- {skill-id}
```

### 範例：`~/.config/openparty/scenes/dev-team/SCENE.md`

```markdown
# 研發團隊 (Dev Team)

你正在參與一個軟體研發團隊的多 AI 討論。目標是審查程式碼、做出技術決策。

## 協作模式

consensus-with-lead：Tech Lead 負責最終技術決策，但安全與品質專家在各自領域有否決權。

## 成員組成

- **tech-lead**：整體架構與技術方向
- **security**：安全審計，在安全議題上有否決權
- **senior-dev**：實作細節
- **qa**：品質標準，是上線的底線

## Allowed Skills

- code-review
- security-audit
- testing-strategy
```

---

## IDENTITY-{role}.md 格式

IDENTITY 檔案只送給被指派該身份的 Agent，緊接在 SCENE.md 之後注入：

```markdown
# 你的身份：{Identity 名稱}

{說明這個角色在場景中是誰、職責是什麼}

## 你的決策範圍

{說明這個角色能做什麼決定、在什麼議題上有權威}

## 你的行為規範

{說明這個角色怎麼說話、怎麼做決策、怎麼與其他成員互動}
```

### 範例：`~/.config/openparty/scenes/dev-team/IDENTITY-security.md`

```markdown
# 你的身份：Security Specialist

你是這個研發團隊的安全專家。

## 你的決策範圍

在安全相關議題上，你有否決權。如果一個方案有安全風險，你必須明確指出並阻止，不論是誰提出的方案。

## 你的行為規範

- 每次分析都要明確指出風險等級（Critical / High / Medium / Low）
- 對有安全疑慮的方案，直接說「我反對，原因是...」
- 在非安全議題上，服從 Tech Lead 的決策
- 你不需要每次發言都談安全，但凡涉及安全的議題，你是最終裁決者
```

---

## SKILL.md 格式

Skill 定義存在獨立的目錄下，跨場景共用。Agent 不會預先收到 Skill 的完整內容——而是收到可用的 Skill 清單，需要時自行用 Read tool 讀取。

```markdown
# {Skill 名稱}

{這個技能是什麼、能做什麼}

## 使用時機

{什麼情況下應該啟用這個技能}

## 執行方式

{如何運用這個技能分析問題或執行任務}
```

### 範例：`~/.config/openparty/skills/security-audit/SKILL.md`

```markdown
# Security Audit

對程式碼或設計方案進行安全審計。

## 使用時機

當被要求審查程式碼、API 設計、或任何涉及資料存取的方案時。

## 執行方式

1. 識別潛在漏洞（SQL injection、XSS、auth bypass、path traversal 等）
2. 評估攻擊面
3. 確認輸入驗證與 sanitization
4. 確認 secret/credentials 的處理方式
5. 標示每個問題的嚴重程度（Critical / High / Medium / Low）
```

### Skill 存取控制

- SCENE.md 的 `Allowed Skills` 清單是 whitelist
- Server 將 whitelist 附在 system prompt 末尾，告知 Agent「你可以用這些 Skill」
- Agent 需要某個 Skill 時，用 Read tool 自行讀取 `~/.config/openparty/skills/{skill-id}/SKILL.md`
- 不在 whitelist 裡的 Skill，Agent 不會知道它存在

### Skill 的兩種類型

| 類型 | 說明 | 實作方式 |
|------|------|----------|
| **知識/行為 Skill** | 專業知識、分析框架、行為規範 | SKILL.md 按需載入（Agent 用 Read tool 自行讀取，不預先注入） |
| **工具 Skill** | 實際執行能力（讀檔、執行程式碼等） | `allowed_tools` 參數控制，非文字可賦予 |

> System prompt 裡只附 Skill 清單（ID + 一行描述），不附完整內容——避免 context window 膨脹。

---

## Scene Leader（Phase 2）

某些場景需要一個協調者主動控制討論流程（不只是發言）。

### 權力層級

```
Owner（最高，永遠可覆蓋）
  ↓ 委託
Scene Leader（協調權，Owner 隨時可收回）
  ↓ 參與
其他 Agent（發言，無控制權）
```

Owner 的「不介入」是行為選擇（delegation mode），不是放棄控制權。

### 定義方式

在 IDENTITY-{role}.md 裡用 YAML frontmatter 標記，server 解析此欄位：

```markdown
---
is_leader: true
---

# 你的身份：Project Manager

你是這個任務的協調者，負責決定討論順序、追蹤進度、宣告任務完成。
```

Server 在讀取 IDENTITY 檔案時解析 frontmatter，若 `is_leader: true` 則授予 directive 執行權限。

### Leader 的能力

Leader 在回應中可附加結構化指令，server 解析後執行：

```json
{
  "content": "（正常發言內容）",
  "directive": {
    "next_speaker": "agent_id",
    "scene_complete": true
  }
}
```

Server 只在 `is_leader: true` 的 Agent 回應裡執行 directive，其他 Agent 的同樣欄位被忽略。Owner 指令永遠優先。

---

## 長時間執行場景

適合需要多輪反覆的場景（如 研究、修改→測試→驗證）：

**設計模式：明確 todo list + 終止條件 + Scene Leader 掌控**

SCENE.md 定義終止條件，Scene Leader 維護 todo 狀態，Owner 只看最終結果。

### 已知限制（Phase 1）

| 問題 | 現狀 | 解決方式（Phase 2） |
|------|------|---------------------|
| 狀態持久化 | 記憶體，中斷即消失 | 寫入磁碟 |
| session 恢復 | session_id 只在記憶體 | 持久化 session_id，重連後恢復 |
| context 爆滿 | FatalAgentError → 自動退場 | 持久化摘要，重建新 Agent 繼續 |

---

## 實作路徑

### Phase 1（現在可做）

- Observer CLI 新增「設定 Scene」指令（Room 建立後、第一次發言前可設定）
- Server 在 Room 物件加入 `scene_id` 屬性
- Agent 加入時，server 讀取 `~/.config/openparty/scenes/{scene}/SCENE.md` + 分配的 `IDENTITY-{role}.md`
- 將組合好的 system prompt 附在第一個 `your_turn` payload 的新欄位 `system_prompt` 裡
- Bridge 收到後用 `ClaudeAgentOptions(system_prompt=...)` 初始化 session
- Allowed Skills 清單附在 system prompt 末尾
- Server 記錄合規率 log（必選）

**合規率 log 格式：**

```json
{
  "session_id": "string",
  "scene": "string",
  "identity": "string",
  "turn": 1,
  "compliant": true,
  "violation_type": "out-of-domain | authority-exceeded | role-break | null",
  "timestamp": "ISO8601"
}
```

### Phase 2（依 Phase 1 數據決定）

| 合規率 | 決策 | 行動 |
|--------|------|------|
| **> 80%** | Prompt 治理有效 | 繼續，專注 UX 優化 |
| **60–80%** | 需補強 | 加入輕量 server 端驗證 + 重提示 |
| **< 60%** | Prompt 失效 | Server 端硬邏輯接管 |

Phase 2 同時處理：Scene Leader directive 機制、狀態持久化、session 恢復。

### Phase 3

- 使用者自定義場景目錄
- 場景分享與社群市集

---

*最後更新：2026/04/03*

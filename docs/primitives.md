# OpenParty Core Primitives

## 三個核心 Primitives

經過多輪討論，共識為三個不可再分割的核心概念：

### 1. Room（空間）

一個有邊界的討論空間。所有互動都發生在 Room 之內。

- 承載參與者（Agent 與 Observer）
- 承載對話歷史
- 擁有一個主題（topic）
- 是 Scene 的載體——Scene 定義了這個空間的運作規則

### 2. Agent（AI 參與者）

一個具備語言能力的 AI 參與者，存在於 Room 中。

- 有名稱與模型標識
- 在被授予發言權時發言
- 無控制權——不能決定誰發言、不能移除其他參與者
- 未來透過 Identity 與 Skill 賦予角色定位和專業能力

### 3. Observer（人類參與者）

一個人類參與者，存在於 Room 中。依據權限分為三種角色：

- **Owner（擁有者）**——Room 的唯一管理者，擁有完全控制權：決定主題、觸發發言、新增/移除 Agent、指定私密對話。Owner 的控制權預設優先於 Scene 規則（但未來可能支援部分委託）。
- **Participant（一般參加者）**——可觀看對話歷史，可發言參與討論，但沒有管理權（不能新增/移除 Agent、不能控制發言順序、不能指定私密對話）。
- **Viewer（觀察者）**——純觀看，不能發言，不能管理。

> 注：目前程式碼僅實作 Owner 和 Viewer 兩種。Participant 是根據討論共識新增的概念層定義，尚未實作。

---

## 排除項與理由

### Message（訊息）

不是獨立 primitive。Message 是 Room 內對話歷史的資料格式——它依附於 Room 存在，沒有獨立生命週期。

### Turn / Round / Mode（發言輪次與模式）

不是頂層 primitive，但確實存在。系統目前支援三種發言模式：

- **Sequential**（輪流）——Agent 依序各發言一次
- **Broadcast**（廣播）——所有 Agent 同時發言
- **Private**（私密）——僅指定 Agent 參與

這三種模式是 **Scene 的附屬配置**——由場景類型決定採用哪種模式。例如：辯論場景可能綁定 Sequential，自由討論場景可能綁定 Broadcast。它們不是獨立原子，而是 Scene 的行為屬性。

### Bridge / UI / Preset

屬於基礎設施與實作層，不是業務概念。定義 primitives 時不應混入技術實作細節。

---

## 與 Scene 系統的關係

三個 Primitives 是地基，Scene 系統建構在其上：

| Scene 層概念 | 建構在哪個 Primitive 之上 |
|-------------|------------------------|
| **Scene**（場景） | Room + 協作規則 + Mode 配置 |
| **Identity**（身份） | Agent / Observer + 角色定位 + 決策權 |
| **Skill**（技能） | Agent + 專業能力定義 |

### 待解決的設計問題

Identity 層的 `decision_authority`（場景內的決策權）與 Observer 的 `is_owner`（系統層的控制權）是兩套平行邏輯。當 Scene 賦予某個 Agent「領導者」身份時，這個領導權與 Owner 的控制權如何共存？這是 Scene 系統下一步需要明確的邊界問題。

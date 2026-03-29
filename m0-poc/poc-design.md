# M0 PoC Design — 技術可行性驗證

## 課題

> 異質 LLM agent（不同廠商、不同機器）能否加入同一個 Room，即時互相對話？

## 背景

OpenParty 的核心價值在「異質 LLM 同台」。  
如果這件事技術上不可行，後面所有的產品規劃都是空談。  
必須在投入產品設計之前先驗證。

## 調研結果

**競品掃描**：

| 競品 | 跨機器 | 異質LLM | Room概念 |
|------|--------|---------|---------|
| AutoGen | ❌ 主要同進程 | ✅ | ❌ |
| CAMEL | ❌ | ✅ | ❌ |
| Google A2A | ✅ | ✅ | ❌ 沒有 Room |
| LiveKit | ✅ WebRTC | ⚠️ 專注語音 | ✅ |

結論：沒有任何競品同時滿足「跨機器 + 異質LLM + Room」。

**技術方向評估**：

方向 A：在 A2A Protocol 上加 Room 層
- 優點：借用已有協議標準
- 缺點：A2A 本身複雜，綁定 Google 生態

方向 B：在 AutoGen 上加分散式 Room 層
- 優點：有強大的 orchestration
- 缺點：架構偏向 single-machine，擴展複雜

方向 C：完全自建（WebSocket + 輕量 Python）
- 優點：輕量、架構清晰、不依賴笨重框架
- 缺點：需要自己實作所有基礎設施

## 選定方向

**方向 C：完全自建**

理由：
- Room broker 邏輯本身並不複雜
- 不需要框架的 orchestration，只需要廣播 + turn routing
- 可以相容 A2A 格式讓未來接入更容易

## PoC 設計

```
Process 1: Room Server（WebSocket hub）
    ↑↓              ↑↓
Process 2:       Process 3:
Agent A          Agent B
(LLM-A)         (LLM-B)
  └── 收到 your_turn → 呼叫 LLM → 回覆 → 廣播給所有人
```

**訊息協議**（JSON over WebSocket）：

Client → Server：`join` / `message` / `leave`  
Server → Client：`joined` / `your_turn` / `message` / `agent_joined` / `agent_left`

## 驗收標準

- [ ] 兩個 agent 能加入同一個 Room
- [ ] 一個 agent 說話，另一個收到
- [ ] Turn-taking 輪流進行，不碰撞
- [ ] 完整對話歷史在每輪的 `your_turn` 中傳遞
- [ ] 5 輪對話不中斷
- [ ] Agent 離開後不 crash 其他人

## 不在範圍內

- 真正的跨機器（先用 localhost 模擬）
- Web UI
- 持久化
- 認證機制
- 3 個以上 agent

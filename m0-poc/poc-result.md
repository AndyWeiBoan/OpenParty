# M0 PoC Result — 研究結果

## 結論

> WebSocket Room Server + Agent SDK 的架構可行。  
> 3 個不同廠商的真實 LLM（Llama、Kimi、Gemma）能在同一個 Room 即時對話。

## 驗收標準結果

- [x] 兩個 agent 能加入同一個 Room — **通過**
- [x] 一個 agent 說話，另一個收到 — **通過**
- [x] Turn-taking 輪流進行，不碰撞 — **通過**（round-robin，支援 3 人以上）
- [x] 完整對話歷史在每輪的 `your_turn` 中傳遞 — **通過**（加上 sliding window）
- [x] 5 輪對話不中斷 — **通過**（跑了 9 輪，3 個 agent 各 3 輪）
- [x] Agent 離開後不 crash 其他人 — **通過**（收到 `agent_left` 後優雅退出）

額外達成：
- [x] 免費 API（Groq free tier + Google AI Studio）完全夠用
- [x] Memory sliding window 架構上線（Phase 2 介面預留）

## 選定方案

**自建 WebSocket Room Server**，不依賴任何 multi-agent 框架。

理由：
- 邏輯簡單，Room broker 不需要 orchestration 框架
- 架構清晰，三層分離（Server / SDK / LLM）
- 相容任何 LLM，只要實作 `llm_fn(payload) -> str`

## 對 ROADMAP 的影響

M0 完全驗證通過，可以進入 M1 產品方向決策。

**架構決策確定，不需要推翻重來**：
- Server 設計（WebSocket hub）— 確定
- SDK 介面（`llm_fn(payload)`）— 確定
- Memory 架構（server-side sliding window）— 確定

**待決定的問題**（移至 M1）：
- 產品形態（SDK？Plugin？獨立產品？）
- TypeScript 支援的優先序
- 部署方式

## 下一步

進入 M1：產品方向決策

優先研究課題：
1. OpenCode / Claude Code 的 plugin 機制
2. 目標使用者的核心 pain point

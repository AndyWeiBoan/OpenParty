1 [ X ][LOW] 滑鼠選取的時候始能選取一整行
    * expected: 要可以選取一段文字,滑鼠按下為起點,放開為終點
2 [✓][High] user 數入文字當下,畫面會閃爍,疑似光標在畫面與輸入匡來回移動導致的
    * expected: 光標如果在輸入狀態就到輸入匡,不應該在輸入停頓的時候又跑回聊天內容的區域
    * root cause: `ui_loop` 每 50ms 呼叫 `stdscr.get_wch()`，ncurses 的 `wgetch()` 內部會先執行
      `wrefresh(stdscr)`，將 stdscr 的游標位置（預設 (0,0)，即聊天區左上角）刷新到實體終端，
      導致每次 poll 都讓游標短暫閃現到聊天區。
      修正：改用 `ui.input_win.get_wch()` 取代 `stdscr.get_wch()`，並對 input_win 設定
      `nodelay(True)` 與 `keypad(True)`，讓隱式的 `wrefresh` 刷新到輸入框自身，游標不再跳動。
3 [ X ][High] log檔案應該要存放在獨立的資料夾
4 [✓][High] 多 agent 輪流發言時，排在後面的 agent 看不到同一輪其他 agent 的發言
    * 症狀：在有 3 個以上 agent 的房間裡，排在最後發言的 agent（例如 claude-opus）
      只能看到緊鄰它之前那一條訊息，無法看到同一輪更早的 owner 廣播或其他 agent 的提案。
    * root cause：`bridge.py` 的 `build_prompt()` 第 216 行：
        history_window = history[-8:] if session_id is None else history[-1:]
      當 agent 已建立 Claude session 後，只取 `history[-1:]`（最後 1 條），
      假設「其他訊息已在 session context 裡」。但此假設在多 agent 情境下錯誤——
      Claude session 只包含該 agent 自己的 prompt/response 歷史，
      其他 agent 在它等待期間的發言完全不在其 session 裡，因此被遺漏。
      排在輪次越後面的 agent 遺漏越多。
    * 解決方案：引入 round 機制
      - server.py 的 Room 加入 `current_round` 計數器
      - 每條 history entry 附上 `round` 欄位
      - `build_prompt()` 改為送「上一輪所有訊息 + 本輪到目前為止的訊息」給 agent
      - round 邊界定義：owner 發出新廣播，或所有 agent 在本輪都發言完畢，擇一觸發 round 遞增
      - 此方案不依賴 agent 數量啟發式截斷，在循序與廣播兩種模式下都正確

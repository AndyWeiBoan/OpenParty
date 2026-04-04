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

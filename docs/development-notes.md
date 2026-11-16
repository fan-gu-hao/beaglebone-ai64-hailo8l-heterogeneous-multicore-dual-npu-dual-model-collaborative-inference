# 開發紀錄 / Development notes

[首頁 / Home](../README.md)

| 遇到的問題 / Problem | 處理方式與意義 / Resolution and significance |
|---|---|
| 舊 I2C 列舉路徑不存在 / Old sysfs path absent | 從實際裝置樹找 `i2c@2020000`，再映射匯流排；避免寫死編號 / Resolve the controller through the live device tree. |
| GPIO 名稱與核心編號不固定 / Enumeration is unstable | 以控制器名稱、偏移與 pinmux 查核；Linux 保留 GPIO，R5F 控制時序 / Validate ownership and offsets rather than assuming enumeration order. |
| HX711 精密重量校正不穩 / Mass calibration drift | 專案目的為有無偵測，改用原始計數遲滯與去抖 / Use presence thresholds appropriate to the actual goal. |
| R5F 暖重啟後 RPMsg 消失或 -19 / Warm restart failures | 單次 sysfs 寫入、明確狀態處理；感測服務不盲目重試，必要時重啟板子 / Avoid restart loops; preserve failure visibility. |
| Hailo Python 模組未安裝 / Missing Python runtime binding | 使用既有 HailoRT C++ 標頭與程式庫，經管線與 Python 應用通訊 / Use a native worker with a narrow IPC contract. |
| 量化後杯子／手機分數降低 / Quantized score shifts | 分離原始／量化數值比對與模型準確度，分析門檻影響後保留限制 / Examine conversion effects without claiming ground-truth accuracy. |
| TI 浮點模型只有主機模擬可用 / FP32 target allocation warning | 後續走 INT8 目標編譯、檢查資料流與保留區，再上板驗證 / Host emulation success is not target deployment success. |
| C71 編譯找不到標準標頭 / Missing standard headers | 修正 C7000 工具鏈 include 路徑，先驗證介面再連結 / Separate compilation, linking and runtime milestones. |
| C71 向量表與程式區配置 / Vector placement | 獨立 16 KiB 向量槽，維持區域終點與對齊，逐段檢查 ELF / Validate segment ownership and alignment. |
| 模型需要額外 DDR／MSMC / Runtime memory demand | 裝置樹、連結器、快取及配置一致；重開機後驗證 Linux 已讓出記憶體 / Validate live reservations before loading firmware. |
| 固定測試圖不能證明動態傳圖 / Fixed-image-only test | 增加 V2 分塊輸入、CRC32、原圖／翻轉／恢復比較 / Validate externally supplied input and repeatability. |
| 骨架閃爍與換人 / Joint jitter and identity switching | 人物框追蹤、One Euro、門檻遲滯、短暫保留及重新取得 / Temporal stability with explicit freshness. |
| 物品先放上，影像類別稍晚才可靠 / Sensor/vision timing gap | 放置事件等待穩定類別；逾時保留未確認事件 / Preserve sensor truth while waiting briefly for naming. |
| 手腕保留點會形成假線索 / Held joints can mislead | 顯示保留與當次有效觀察分開，只有新鮮點進入時間窗 / Separate visual continuity from event evidence. |
| 手動開終端才能展示 / Interactive launch dependency | 版本化服務副本、裝置就緒、開機空秤歸零、紀錄輪替、重啟後驗證 / Convert the validated workflow into boot services. |

來源中的早期失敗紀錄與階段狀態保留，與後續通過的結果按時間解讀。編譯成功、硬體可執行、模型品質與整合展示是不同的驗收項目。

Earlier failure/phase records are retained. Successful compilation, target execution, model quality and integrated behavior are separate acceptance milestones.
